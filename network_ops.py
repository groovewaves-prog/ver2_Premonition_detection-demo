# -*- coding: utf-8 -*-
"""
AIOps Agent - Network Operations Module (v3 - 根本修正版)
=========================================================
★根本修正:
1. validate_response 完全削除（ストリーミングでは使用不可）
2. ストリーミングチャンク即時yield
3. 不要な待機処理の排除
"""

import re
import os
import time
import json
import hashlib
import logging
import concurrent.futures
from typing import Dict, List, Optional, Generator, Any
from enum import Enum

import google.generativeai as genai
from netmiko import ConnectHandler

from rate_limiter import GlobalRateLimiter, RateLimitConfig
from utils.sanitizer import sanitize_for_llm, sanitize_device_id

logger = logging.getLogger(__name__)

# =====================================================
# 定数
# =====================================================
MODEL_NAME = "gemma-3-4b-it"

SANDBOX_DEVICE = {
    'device_type': 'cisco_nxos',
    'host': 'sandbox-nxos-1.cisco.com',
    'username': 'admin',
    'password': 'Admin_1234!',
    'port': 22,
    'global_delay_factor': 2,
    'banner_timeout': 30,
    'conn_timeout': 30,
}


class RemediationEnvironment(Enum):
    DEMO = "demo"
    TEST = "test"
    PRODUCTION = "prod"


class RemediationResult:
    def __init__(self, step_name: str, status: str, data=None, error=None):
        self.step_name = step_name
        self.status = status
        self.data = data
        self.error = error
        self.timestamp = time.time()

    def __str__(self):
        if self.status == "success":
            return f"✅ {self.step_name}: {self.data}"
        elif self.status == "timeout":
            return f"⏱️ {self.step_name}: Timeout"
        return f"❌ {self.step_name}: {self.error}"

    def to_dict(self):
        return {
            "step": self.step_name,
            "status": self.status,
            "data": self.data,
            "error": self.error,
            "timestamp": self.timestamp
        }


# =====================================================
# グローバル状態
# =====================================================
_rate_limiter: Optional[GlobalRateLimiter] = None
_model: Optional[genai.GenerativeModel] = None
_api_configured = False


def _get_limiter() -> GlobalRateLimiter:
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = GlobalRateLimiter()
    return _rate_limiter


def _get_model(api_key: str) -> Optional[genai.GenerativeModel]:
    global _model, _api_configured
    if _api_configured and _model:
        return _model
    if not api_key:
        return None
    try:
        genai.configure(api_key=api_key)
        _model = genai.GenerativeModel(MODEL_NAME)
        _api_configured = True
        return _model
    except Exception as e:
        logger.error(f"API config error: {e}")
        return None


# =====================================================
# ユーティリティ
# =====================================================
def sanitize_output(text: str) -> str:
    """機密情報をマスク"""
    rules = [
        (r'(password|secret) \d+ \S+', r'\1 <HIDDEN>'),
        (r'(snmp-server community) \S+', r'\1 <HIDDEN>'),
    ]
    for pattern, replacement in rules:
        text = re.sub(pattern, replacement, text)
    return text


def compute_cache_hash(scenario: str, device_id: str, extra: str = "") -> str:
    """キャッシュキー生成"""
    return hashlib.md5(f"{scenario}|{device_id}|{extra}".encode()).hexdigest()


def _extract_text(chunk) -> str:
    """ストリーミングチャンクからテキスト抽出（安全版）"""
    # 方法1: 直接textプロパティ
    try:
        if hasattr(chunk, 'text') and chunk.text:
            return chunk.text
    except Exception:
        pass
    
    # 方法2: candidates経由
    try:
        if hasattr(chunk, 'candidates') and chunk.candidates:
            parts = chunk.candidates[0].content.parts
            return ''.join(p.text for p in parts if hasattr(p, 'text'))
    except Exception:
        pass
    
    return ''


def _is_retryable_error(e: Exception) -> bool:
    """リトライ可能なエラーか判定"""
    msg = str(e).lower()
    return any(x in msg for x in ['429', '503', 'overloaded', 'resource_exhausted'])


# =====================================================
# ★ストリーミングLLM呼び出し（遅延解消・検証削除）
# =====================================================
def _stream_generate(
    model: genai.GenerativeModel,
    prompt: str,
    max_retries: int = 2
) -> Generator[str, None, None]:
    """
    ストリーミング生成（根本修正版）
    
    ★改善ポイント:
    - validate_response 完全削除
    - レスポンス取得後は即座にイテレート開始
    - チャンクは即座にyield
    """
    limiter = _get_limiter()
    
    for attempt in range(max_retries + 1):
        try:
            # レート制限チェック（即時判定）
            if not limiter.wait_for_slot(timeout=30, model_id=MODEL_NAME):
                if attempt < max_retries:
                    yield "⏳ レート制限中...\n"
                    continue
                yield "❌ レート制限タイムアウト"
                return
            
            limiter.record_request(model_id=MODEL_NAME)
            
            # ★ストリーミング開始 - 即座にイテレート
            # temperature=0.1で出力のブレを抑制
            response = model.generate_content(
                prompt, 
                stream=True,
                generation_config={"temperature": 0.1}
            )
            
            # ★検証なし - 直接イテレート
            has_content = False
            for chunk in response:
                text = _extract_text(chunk)
                if text:
                    has_content = True
                    yield text
            
            if has_content:
                return
            
            # 空レスポンスの場合のみリトライ
            if attempt < max_retries:
                yield "\n⏳ 再試行中...\n"
                time.sleep(2)
                continue
            
            yield "❌ 応答が空でした"
            return
            
        except Exception as e:
            if _is_retryable_error(e) and attempt < max_retries:
                yield f"\n⏳ API混雑中...再試行します\n"
                time.sleep(3 * (attempt + 1))
                continue
            yield f"\n❌ エラー: {e}"
            return


# =====================================================
# ★ Ollama ストリーミング生成（ローカルLLM対応）
# =====================================================
def _stream_generate_ollama(
    ollama_url: str,
    ollama_model: str,
    prompt: str,
    max_retries: int = 2
) -> Generator[str, None, None]:
    """
    Ollama /api/chat エンドポイントをストリーミングで呼び出す。
    backend="ollama_only" 時にレポート/復旧プラン生成で使用。
    """
    try:
        import requests as _req
    except ImportError:
        yield "❌ requests パッケージが未インストールです"
        return

    _sys = (
        "あなたはネットワーク障害分析・復旧のエキスパートです。"
        "指示に従い日本語で回答してください。Markdown 形式で出力してください。"
    )

    for attempt in range(max_retries + 1):
        try:
            payload = {
                "model": ollama_model,
                "messages": [
                    {"role": "system", "content": _sys},
                    {"role": "user",   "content": prompt},
                ],
                "stream": True,
                "options": {"temperature": 0.1, "num_predict": 4096},
            }
            resp = _req.post(
                f"{ollama_url.rstrip('/')}/api/chat",
                json=payload, stream=True, timeout=180,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"Ollama HTTP {resp.status_code}: {resp.text[:200]}")

            has_content = False
            for line in resp.iter_lines():
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                text = data.get("message", {}).get("content", "")
                if text:
                    has_content = True
                    yield text
                if data.get("done"):
                    break

            if has_content:
                return
            if attempt < max_retries:
                time.sleep(2)
                continue
            yield "❌ Ollama: 応答が空でした"
            return

        except Exception as e:
            if attempt < max_retries:
                time.sleep(3 * (attempt + 1))
                continue
            yield f"❌ Ollama エラー: {e}"
            return


# =====================================================
# 障害ログ生成
# =====================================================
def generate_fake_log_by_ai(scenario_name: str, target_node, api_key: str) -> str:
    """シナリオに基づく障害ログ生成"""
    model = _get_model(api_key)
    if not model:
        return "Error: API not configured"

    limiter = _get_limiter()
    cache_key = compute_cache_hash(scenario_name, target_node.id, "log")
    
    cached = limiter.get_cache(cache_key)
    if cached:
        return cached

    vendor = target_node.metadata.get("vendor", "Generic")
    _safe_host = sanitize_device_id(target_node.id)
    _safe_vendor = sanitize_device_id(vendor)
    _safe_scenario = sanitize_for_llm(scenario_name, max_length=200)
    prompt = f"CLIログ生成。ホスト:{_safe_host} ベンダー:{_safe_vendor} シナリオ:{_safe_scenario}。コマンド2個と出力のみ。"

    try:
        if not limiter.wait_for_slot(timeout=30, model_id=MODEL_NAME):
            return "Error: Rate limit"
        limiter.record_request(model_id=MODEL_NAME)
        
        response = model.generate_content(prompt)
        result = response.text if response else "Error: No response"
        limiter.set_cache(cache_key, result)
        return result
    except Exception as e:
        return f"Error: {e}"


# =====================================================
# 初期症状予測
# =====================================================
def predict_initial_symptoms(scenario_name: str, api_key: str) -> Dict:
    """障害シナリオから初期症状を予測"""
    model = _get_model(api_key)
    if not model:
        return {}

    limiter = _get_limiter()
    cache_key = compute_cache_hash(scenario_name, "", "symptoms")
    
    cached = limiter.get_cache(cache_key)
    if cached:
        return cached

    _safe_scenario = sanitize_for_llm(scenario_name, max_length=200)
    prompt = f'シナリオ「{_safe_scenario}」の症状をJSON出力。キー:alarm,ping,log'

    try:
        if not limiter.wait_for_slot(timeout=30, model_id=MODEL_NAME):
            return {}
        limiter.record_request(model_id=MODEL_NAME)
        
        response = model.generate_content(prompt)
        text = response.text.replace("```json", "").replace("```", "").strip()
        result = json.loads(text)
        limiter.set_cache(cache_key, result)
        return result
    except Exception:
        return {}


# =====================================================
# 原因分析レポート（非ストリーミング）
# =====================================================
def generate_analyst_report(
    scenario: str,
    target_node,
    topology_context: str,
    target_conf: str,
    verification_context: str,
    api_key: str
) -> str:
    """原因分析レポート（非ストリーミング版）"""
    model = _get_model(api_key)
    if not model:
        return "Error: API not configured"

    limiter = _get_limiter()
    cache_key = compute_cache_hash(scenario, target_node.id if target_node else "", "report")
    
    cached = limiter.get_cache(cache_key)
    if cached:
        return cached

    device_id = sanitize_device_id(target_node.id) if target_node else "Unknown"
    vendor = sanitize_device_id(target_node.metadata.get("vendor", "Unknown")) if target_node else "Unknown"
    _safe_scenario = sanitize_for_llm(scenario, max_length=300)

    prompt = f"""あなたはネットワーク障害の分析エキスパートです。
以下の条件に従って、障害分析レポートを作成してください。

【指示（この部分は出力に含めないでください）】
- これは障害発生直後の分析です。復旧作業はまだ実施されていません
- 「復旧しました」「再確立しました」のような完了形は使用しないでください
- 文体は「です、ます」調で記載してください
- 下記のフォーマットに従って本文のみを出力してください

【対象情報】
シナリオ: {_safe_scenario}
デバイス: {device_id} ({vendor})

【出力フォーマット（この見出しは出力せず、以下の内容のみ出力）】
## 障害概要
## 発生原因（推定）
## 影響範囲
## 技術的根拠
"""

    try:
        if not limiter.wait_for_slot(timeout=30, model_id=MODEL_NAME):
            return "Error: Rate limit"
        limiter.record_request(model_id=MODEL_NAME)
        
        response = model.generate_content(
            prompt,
            generation_config={"temperature": 0.1}
        )
        result = response.text if response else "Error: No response"
        limiter.set_cache(cache_key, result)
        return result
    except Exception as e:
        return f"Error: {e}"


# =====================================================
# ★原因分析レポート（ストリーミング版・バックエンド自動選択）
# =====================================================
def generate_analyst_report_streaming(
    scenario: str,
    target_node,
    topology_context,
    target_conf: str,
    verification_context: str,
    api_key: str,
    max_retries: int = 2,
    backoff: float = 3.0,
    llm_config: dict = None,
    model_name: str = "gemma-3-4b-it",  # ★ 引数を追加
    is_prediction: bool = False,  # ★ 予兆確認手順モード
) -> Generator[str, None, None]:
    """
    原因分析レポート（ストリーミング版）
    
    ★ llm_config.backend に応じてバックエンドを自動選択:
      - "google"      → Google Gemma API
      - "ollama"      → Google Gemma API（ハイブリッド: 高品質タスクはクラウド）
      - "ollama_only" → Ollama ローカル
    """
    backend = (llm_config or {}).get("backend", "google")

    # Google API model（ollama_only 以外で必要）
    model = None
    if backend != "ollama_only":
        # _get_model(api_key) を直接使わず、ここで個別にモデルを生成する
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(model_name)  # ★ 引数で受け取ったモデル名を使用
        if not model:
            yield "Error: API not configured"
            return

    limiter = _get_limiter()
    
    # ★キャッシュチェック - ヒット時は即座に返却
    device_id = target_node.id if target_node else "Unknown"
    cache_key = compute_cache_hash(scenario, device_id, "report_stream")
    
    cached = limiter.get_cache(cache_key)
    if cached:
        yield cached
        return

    vendor = sanitize_device_id(target_node.metadata.get("vendor", "Unknown")) if target_node else "Unknown"
    device_id = sanitize_device_id(device_id)
    _safe_scenario = sanitize_for_llm(scenario, max_length=500)

    # ★ 予兆確認手順モードと通常モードでプロンプトを分岐
    if is_prediction:
        # ── 予兆モード: _build_prediction_report_scenario() が構築した
        #    診断ワークブックのプロンプトをメイン指示として使用する。
        #    汎用フォーマット（障害概要/発生原因…）で上書きしない。
        prompt = f"""あなたはネットワーク障害の予兆分析エキスパートです。
以下の【診断ワークブック指示】に厳密に従い、確認手順書を作成してください。

【共通指示（この部分は出力に含めないでください）】
- これは予兆検知に対する確認手順書です。まだ障害は発生していません
- 「復旧しました」「再確立しました」のような完了形は使用しないでください
- 文体は「です、ます」調で記載してください
- config系コマンド（設定変更）は絶対に含めないでください
- 対象は {vendor} のネットワーク専用機器です。Linux用コマンド（top, ps, grep等）は不要です

【対象デバイス】
デバイス: {device_id} ({vendor})

【診断ワークブック指示】
{_safe_scenario}

【★ 最重要ルール】
- 出力フォーマットは上記【診断ワークブック指示】内の番号付きセクション（1〜5）に従ってください
- 「## 障害概要」「## 発生原因」等の汎用フォーマットは使わないでください
- 「推奨アクション（ステップ①）」に表示済みのコマンドは絶対に再掲しないでください
- 「1. 深掘り診断コマンド」では、ステップ①の結果を踏まえた「次の一手」を提示してください
- 「2. 出力の読み方ガイド」では、ステップ①のコマンド出力と、1.の深掘りコマンド出力の両方について記載してください
- 「3. OK/NG判定基準表」では、ステップ①のコマンドの確認項目も含めてください
"""
    else:
        # ── 通常モード: 汎用の障害分析レポートフォーマット
        # ★ トポロジー情報を構造化テキストへ変換
        _topo_lines = []
        if topology_context:
            _tc_node = topology_context.get("node", {}) or {}
            _tc_meta = _tc_node.get("metadata", {}) or {}
            _tc_parent = topology_context.get("parent_id")
            _tc_children = topology_context.get("children_ids", [])
            if _tc_node.get("type"):
                _topo_lines.append(f"・機器タイプ: {_tc_node['type']}")
            if _tc_node.get("layer"):
                _topo_lines.append(f"・ネットワーク層: {_tc_node['layer']}")
            if _tc_meta.get("os"):
                _topo_lines.append(f"・OS: {_tc_meta['os']}")
            if _tc_meta.get("model"):
                _topo_lines.append(f"・機種: {_tc_meta['model']}")
            if _tc_parent:
                _topo_lines.append(f"・上位接続先: {_tc_parent}")
            if _tc_children:
                _topo_lines.append(f"・配下機器: {', '.join(str(c) for c in _tc_children[:10])}"
                                   + (f" 他{len(_tc_children)-10}台" if len(_tc_children) > 10 else ""))
        _topo_text = "\n".join(_topo_lines) if _topo_lines else "情報なし"

        # ★ デバイス設定情報をサニタイズ
        _safe_conf = sanitize_for_llm(str(target_conf), max_length=800) if target_conf else "なし"

        # ★ AI診断ログをサニタイズ
        _safe_verification = sanitize_for_llm(str(verification_context), max_length=800) if verification_context else "特になし"

        prompt = f"""あなたはネットワーク障害の分析エキスパートです。
以下の情報をすべて活用し、障害分析レポートを作成してください。
このレポートは運用チームの初動判断だけでなく、お客様への障害報告書の原案としても使用されます。

【指示（この部分は出力に含めないでください）】
- これは障害発生直後の分析です。復旧作業はまだ実施されていません
- 「復旧しました」「再確立しました」のような完了形は使用しないでください
- 文体は「です、ます」調の丁寧な報告文体で記載してください
- 各セクションは具体的なデバイス名、インターフェース名、プロトコル名を含めてください
- 推測には必ず「〜が疑われます」「〜の可能性があります」と明記してください
- 下記のフォーマットに厳密に従って本文のみを出力してください

【障害シナリオ】
{_safe_scenario}

【対象デバイス】
デバイス: {device_id} ({vendor})
{_topo_text}

【デバイス設定情報】
{_safe_conf}

【AI診断ログ・検証結果】
{_safe_verification}

【出力フォーマット（この見出しは出力せず、以下の内容のみ出力）】
## 障害概要
（何が発生し、どのデバイスに集中しているか。影響の第一印象を簡潔に記載）

## 発生原因（推定）
（デバイス設定情報・診断ログを根拠に、具体的な原因仮説を記載。物理層・データリンク層・ネットワーク層のどこに問題があるかを切り分けて記載）

## 影響範囲
（配下機器・上位接続先の情報を基に、影響を受けているサービス・拠点・ユーザーを具体的に記載。影響台数も含める）

## 技術的根拠
（診断ログの具体的な証拠を引用し、なぜその原因推定に至ったかを技術的に説明。該当するCLIコマンドの出力結果やログの所見を含める）

## 推奨対応
（優先順位を付けた番号付きリストで、具体的な調査・復旧手順を記載。各手順にはCLIコマンド名や確認ポイントを含める）
"""

    # ★ストリーミング生成（バックエンド自動選択）
    full_text = ""
    if backend == "ollama_only":
        _o_url = (llm_config or {}).get("ollama_url", "http://localhost:11434")
        _o_mdl = (llm_config or {}).get("ollama_model", "gemma3:12b")
        for chunk in _stream_generate_ollama(_o_url, _o_mdl, prompt, max_retries):
            full_text += chunk
            yield chunk
    else:
        for chunk in _stream_generate(model, prompt, max_retries):
            full_text += chunk
            yield chunk

    # 完了後にキャッシュ保存
    if full_text and not full_text.startswith("❌"):
        limiter.set_cache(cache_key, full_text)


# =====================================================
# 復旧コマンド生成（非ストリーミング）
# =====================================================
def generate_remediation_commands(
    scenario: str,
    analysis_result: str,
    target_node,
    api_key: str
) -> str:
    """復旧手順（非ストリーミング版）"""
    model = _get_model(api_key)
    if not model:
        return "Error: API not configured"

    limiter = _get_limiter()
    device_id = sanitize_device_id(target_node.id) if target_node else "Unknown"
    cache_key = compute_cache_hash(scenario, device_id, "remediation")

    cached = limiter.get_cache(cache_key)
    if cached:
        return cached

    vendor = sanitize_device_id(target_node.metadata.get("vendor", "Unknown")) if target_node else "Unknown"
    _safe_scenario = sanitize_for_llm(scenario, max_length=300)

    prompt = f"""あなたはネットワーク復旧のエキスパートです。
以下の条件に従って、復旧手順を作成してください。

【指示（この部分は出力に含めないでください）】
- 文体は「です、ます」調で記載してください
- 下記のフォーマットに従って本文のみを出力してください

【対象情報】
デバイス: {device_id} ({vendor})
シナリオ: {_safe_scenario}

【出力フォーマット（この見出しは出力せず、以下の内容のみ出力）】
## 前提作業
## 復旧コマンド
## 正常性確認
"""

    try:
        if not limiter.wait_for_slot(timeout=30, model_id=MODEL_NAME):
            return "Error: Rate limit"
        limiter.record_request(model_id=MODEL_NAME)
        
        response = model.generate_content(
            prompt,
            generation_config={"temperature": 0.1}
        )
        result = response.text if response else "Error: No response"
        limiter.set_cache(cache_key, result)
        return result
    except Exception as e:
        return f"Error: {e}"


# =====================================================
# ★復旧コマンド生成（ストリーミング版・遅延解消）
# =====================================================
def generate_remediation_commands_streaming(
    scenario: str,
    analysis_result: str,
    target_node,
    api_key: str,
    max_retries: int = 2,
    backoff: float = 3.0,
    llm_config: dict = None,
    model_name: str = "gemma-3-4b-it"  # ★ 引数を追加
) -> Generator[str, None, None]:
    """
    復旧手順（ストリーミング版）
    
    ★ llm_config.backend に応じてバックエンドを自動選択:
      - "google"/"ollama" → Google Gemma API
      - "ollama_only"     → Ollama ローカル
    """
    backend = (llm_config or {}).get("backend", "google")

    model = None
    if backend != "ollama_only":
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(model_name)  # ★ 引数で受け取ったモデル名を使用
        if not model:
            yield "Error: API not configured"
            return

    limiter = _get_limiter()
    
    # ★キャッシュチェック - ヒット時は即座に返却
    device_id = sanitize_device_id(target_node.id) if target_node else "Unknown"
    cache_key = compute_cache_hash(scenario, device_id, f"report_stream_{model_name}")

    cached = limiter.get_cache(cache_key)
    if cached:
        yield cached
        return

    vendor = sanitize_device_id(target_node.metadata.get("vendor", "Unknown")) if target_node else "Unknown"
    _safe_scenario = sanitize_for_llm(scenario, max_length=300)

    # ★ 分析レポート・トリアージ結果をサニタイズして注入
    _safe_analysis = sanitize_for_llm(str(analysis_result), max_length=1500) if analysis_result else "なし"

    prompt = f"""あなたはネットワーク復旧のエキスパートです。
以下の障害分析結果をすべて活用し、具体的な復旧手順を作成してください。
この復旧手順は運用チームが実機で実行する作業計画書として、またお客様への復旧報告書の原案としても使用されます。

【指示（この部分は出力に含めないでください）】
- 文体は「です、ます」調の丁寧な報告文体で記載してください
- 対象は {vendor} のネットワーク機器です。CLIコマンドは {vendor} の実機コマンドを使用してください
- 各セクションにデバイス名（{device_id}）を明記し、具体的なインターフェース名やプロトコル名を含めてください
- 下記のフォーマットに厳密に従って本文のみを出力してください

【障害シナリオ】
{_safe_scenario}

【対象デバイス】
デバイス: {device_id} ({vendor})

【障害分析レポート・トリアージ結果】
{_safe_analysis}

【出力フォーマット（この見出しは出力せず、以下の内容のみ出力）】
## 実施前提
（復旧作業に入る前に確認すべき事項を番号付きリストで記載。ネットワーク状況の把握、代替回線の有無、作業環境の準備、IOSバージョン確認、バックアップ確認、影響範囲の確認を含める）

## バックアップ手順
（復旧作業前のバックアップ手順を番号付きリストで記載。接続方法、enableモード、copy running-config、確認コマンド、保存場所の確認を含める）

## 復旧コマンド
（障害分析結果を踏まえた具体的な復旧手順を番号付きリストで記載。各手順にCLIコマンド名を含め、インターフェース確認→設定確認→ルーティング確認→回線事業者確認→設定保存の順で記載）

## ロールバック手順
（復旧作業後に問題が発生した場合の手順を番号付きリストで記載。バックアップからの復元手順、接続方法、copy startup-config running-config、設定保存を含める）

## 正常性確認
（復旧後に確認すべき項目を番号付きリストで記載。インターフェース接続、IPアドレス取得、Pingテスト、DNS解決、アプリケーション動作、ログ確認、監視システム確認を含める）
"""

    # ★ストリーミング生成（バックエンド自動選択）
    full_text = ""
    if backend == "ollama_only":
        _o_url = (llm_config or {}).get("ollama_url", "http://localhost:11434")
        _o_mdl = (llm_config or {}).get("ollama_model", "gemma3:12b")
        for chunk in _stream_generate_ollama(_o_url, _o_mdl, prompt, max_retries):
            full_text += chunk
            yield chunk
    else:
        for chunk in _stream_generate(model, prompt, max_retries):
            full_text += chunk
            yield chunk

    # 完了後にキャッシュ保存
    if full_text and not full_text.startswith("❌"):
        limiter.set_cache(cache_key, full_text)


# =====================================================
# 診断シミュレーション
# =====================================================
def run_diagnostic_simulation(
    scenario_type: str,
    target_node=None,
    api_key: str = None
) -> Dict:
    """診断シミュレーション実行"""
    time.sleep(1)

    if "正常" in scenario_type:
        return {"status": "SKIPPED", "sanitized_log": "No action required.", "error": None}

    if "[Live]" in scenario_type:
        commands = ["terminal length 0", "show version", "show interface brief"]
        try:
            with ConnectHandler(**SANDBOX_DEVICE) as ssh:
                if not ssh.check_enable_mode():
                    ssh.enable()
                raw_output = f"Connected to: {ssh.find_prompt()}\n"
                for cmd in commands:
                    raw_output += f"\n[{cmd}]\n{ssh.send_command(cmd)}\n"
        except Exception as e:
            return {"status": "ERROR", "sanitized_log": "", "error": str(e)}
        return {"status": "SUCCESS", "sanitized_log": sanitize_output(raw_output), "error": None}

    elif "全回線断" in scenario_type or "サイレント" in scenario_type or "両系" in scenario_type:
        return {"status": "ERROR", "sanitized_log": "", "error": "Connection timed out"}

    else:
        if api_key and target_node:
            raw_output = generate_fake_log_by_ai(scenario_type, target_node, api_key)
            return {"status": "SUCCESS", "sanitized_log": sanitize_output(raw_output), "error": None}
        return {"status": "ERROR", "sanitized_log": "", "error": "Missing API key or target"}


# =====================================================
# ダミーRunning Config生成（トラフィック分析用）
# =====================================================
def generate_fake_running_config(
    target_node,
    utilization_pct: float = 35.0,
    api_key: str = None,
) -> str:
    """トポロジのインターフェース情報からダミーRunning Config + show interfaces 出力を生成。

    AIを使わず、トポロジJSONのinterfaces定義から決定論的に生成する。
    utilization_pct で全体の利用率レベルを制御（劣化シナリオのmetric_valueに対応）。

    Returns:
        Running Config + show interfaces 形式のテキスト
    """
    import random as _rng

    device_id = target_node.id if hasattr(target_node, 'id') else str(target_node)
    vendor = "Cisco"
    if hasattr(target_node, 'metadata') and isinstance(target_node.metadata, dict):
        vendor = target_node.metadata.get('vendor', 'Cisco')
    elif hasattr(target_node, 'metadata'):
        vendor = getattr(target_node.metadata, 'vendor', 'Cisco')

    interfaces = []
    if hasattr(target_node, 'interfaces'):
        interfaces = target_node.interfaces or []
    elif isinstance(target_node, dict):
        interfaces = target_node.get('interfaces', [])

    if not interfaces:
        return f"! No interface data available for {device_id}\n"

    # シード固定で同一入力は同一出力
    _seed = hash(f"{device_id}_{utilization_pct:.1f}")
    rng = _rng.Random(_seed)

    lines = [
        f"! ============================================",
        f"! Device: {device_id} ({vendor})",
        f"! Generated Running Config + Interface Status",
        f"! ============================================",
        f"!",
        f"hostname {device_id}",
        f"!",
    ]

    for iface in interfaces:
        if not isinstance(iface, dict):
            continue
        name = iface.get('name', 'unknown')
        bw_mbps = iface.get('bandwidth_mbps', 100)
        connected_to = iface.get('connected_to', '')
        link_type = iface.get('link_type', 'copper')

        # 個別インターフェースの利用率に±15%のジッターを加える
        jitter = rng.uniform(-15.0, 15.0)
        intf_util = max(1.0, min(99.0, utilization_pct + jitter))
        rate_bps = int(bw_mbps * 1_000_000 * intf_util / 100)
        rate_mbps = rate_bps / 1_000_000

        # queue drops は利用率に応じて増加
        if intf_util < 60:
            output_drops = 0
            input_drops = 0
        elif intf_util < 80:
            output_drops = rng.randint(10, 200)
            input_drops = 0
        elif intf_util < 90:
            output_drops = rng.randint(200, 2000)
            input_drops = rng.randint(0, 50)
        else:
            output_drops = rng.randint(2000, 15000)
            input_drops = rng.randint(50, 500)

        lines.extend([
            f"!",
            f"interface {name}",
            f" description To_{connected_to}",
            f" bandwidth {bw_mbps * 1000}",  # kbps
            f" {'media-type sfp' if link_type == 'fiber' else '! media-type copper'}",
            f" duplex full",
            f" speed auto",
            f" service-policy output QOS-POLICY",
            f" load-interval 30",
            f" no shutdown",
            f"!",
            f"! --- show interfaces {name} ---",
            f"  {name} is up, line protocol is up",
            f"    Hardware is {'SFP-10G' if link_type == 'fiber' else 'GigabitEthernet'}",
            f"    Description: To_{connected_to}",
            f"    MTU 1500 bytes, BW {bw_mbps * 1000} Kbit/sec, DLY 10 usec",
            f"    reliability 255/255, txload {max(1, int(intf_util * 255 / 100))}/255, rxload {max(1, int(intf_util * 255 / 100 * rng.uniform(0.6, 1.0)))}/255",
            f"    5 minute input rate {int(rate_bps * rng.uniform(0.6, 1.0))} bits/sec",
            f"    5 minute output rate {rate_bps} bits/sec",
            f"      utilization: {intf_util:.1f}% ({rate_mbps:.1f} Mbps / {bw_mbps} Mbps)",
            f"    input queue drops {input_drops}, output queue drops {output_drops}",
            f"    {rng.randint(1000000, 99999999)} packets input, {rng.randint(100000000, 9999999999)} bytes",
            f"    {rng.randint(1000000, 99999999)} packets output, {rng.randint(100000000, 9999999999)} bytes",
        ])

    lines.extend([
        f"!",
        f"end",
    ])

    return "\n".join(lines)


# =====================================================
# 並列修復処理
# =====================================================
def run_remediation_parallel_v2(
    device_id: str,
    device_info: dict,
    scenario: str,
    environment: RemediationEnvironment = RemediationEnvironment.DEMO,
    timeout_per_step: int = 30
) -> Dict[str, RemediationResult]:
    """修復ステップを並列実行"""

    def backup_step():
        time.sleep(1)
        return RemediationResult("Backup", "success", f"Backup created for {device_id}")

    def apply_step():
        time.sleep(2)
        return RemediationResult("Apply", "success", "Applied remediation")

    def verify_step():
        time.sleep(1)
        return RemediationResult("Verify", "success", {"overall": "HEALTHY"})

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(backup_step): "Backup",
            executor.submit(apply_step): "Apply",
            executor.submit(verify_step): "Verify",
        }

        results = {}
        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result(timeout=timeout_per_step)
            except Exception as e:
                results[name] = RemediationResult(name, "failed", error=str(e))

    return results
