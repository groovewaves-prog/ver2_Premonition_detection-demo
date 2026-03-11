# ui/cockpit.py  ―  AIOps インシデント・コックピット（リファクタリング版: オーケストレータ）
#
# UI描画ロジックは ui/components/ に分割:
#   helpers.py         — 共通ヘルパー
#   report_builders.py — LLMプロンプト構築
#   kpi_banner.py      — KPI + ステータスバナー
#   future_radar.py    — 予兆専用表示エリア
#   root_cause_table.py — 根本原因候補テーブル
#   topology_panel.py  — 左カラム（トポロジー + 診断）
#   analyst_report.py  — AI Analyst Report
#   remediation.py     — Remediation & Execute
#   chat_panel.py      — Chat with AI Agent
#   diagnostic.py      — Auto-Diagnostics 実行
import streamlit as st
import time
import logging
from typing import Optional, List

logger = logging.getLogger(__name__)

try:
    import google.generativeai as genai
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False

from registry import get_paths, load_topology, get_display_name
from alarm_generator import generate_alarms_for_scenario, Alarm
from inference_engine import LogicalRCA
from utils.helpers import get_status_from_alarms
from ui.engine_cache import compute_topo_hash, get_cached_dt_engine

# コンポーネントインポート
from ui.components.helpers import build_ci_context_for_chat
from ui.components.kpi_banner import render_kpi_banner
from ui.components.future_radar import render_future_radar
from ui.components.root_cause_table import render_root_cause_table
from ui.components.topology_panel import render_topology_panel
from ui.components.analyst_report import render_analyst_report
from ui.components.remediation import render_remediation
from ui.components.chat_panel import render_chat_panel


# =====================================================
# ヘルパー関数（後方互換）
# =====================================================
def _compute_topo_hash(topology: dict) -> str:
    """後方互換ラッパー → engine_cache.compute_topo_hash に委譲"""
    return compute_topo_hash(topology)


# =====================================================
# キャッシュ済みエンジン取得
# =====================================================
@st.cache_resource
def _get_cached_logical_rca(_topology):
    return LogicalRCA(_topology)

def _get_cached_dt_engine(site_id: str, topo_hash: str, _topology):
    return get_cached_dt_engine(site_id, topo_hash, _topology)


# =====================================================
# run_diagnostic の後方互換エクスポート
# =====================================================
from ui.components.diagnostic import run_diagnostic  # noqa: F401


# =====================================================
# メインエントリポイント
# =====================================================
def render_incident_cockpit(site_id: str, api_key: Optional[str]):
    display_name = get_display_name(site_id)
    scenario = getattr(st.session_state, 'site_scenarios', {}).get(site_id, "正常稼働")

    # ヘッダー
    col_header = st.columns([4, 1])
    with col_header[0]:
        st.markdown(f"### 🛡️ AIOps インシデント・コックピット")
    with col_header[1]:
        if st.button(
            "🔙 一覧に戻る",
            key="back_to_list",
            type="primary",
            use_container_width=True,
        ):
            st.session_state.active_site = None
            st.rerun()

    # トポロジー読み込み
    paths = get_paths(site_id)
    topology = load_topology(paths.topology_path)

    if not topology:
        st.error("トポロジーが読み込めませんでした。")
        return

    # アラーム生成（シナリオ不変ならキャッシュ利用）
    _alarm_cache_key = f"_alarm_cache_{site_id}_{scenario}"
    if _alarm_cache_key in st.session_state:
        alarms = st.session_state[_alarm_cache_key]
    else:
        alarms = generate_alarms_for_scenario(topology, scenario)
        st.session_state[_alarm_cache_key] = alarms
    status = get_status_from_alarms(scenario, alarms)

    # 予兆シグナル注入
    injected = st.session_state.get("injected_weak_signal")
    if injected and injected["device_id"] in topology:
        messages = injected.get("messages", [injected.get("message", "")])
        for msg in messages:
            if msg:
                alarms.append(Alarm(
                    device_id=injected["device_id"],
                    message=msg,
                    severity="INFO",
                    is_root_cause=False
                ))

    # LogicalRCA エンジン
    engine = _get_cached_logical_rca(topology)

    # 分析結果キャッシュ
    _analysis_cache_key = f"_analysis_cache_{site_id}_{scenario}"
    _alarm_hash = hash(tuple((a.device_id, a.message, a.severity) for a in alarms)) if alarms else 0
    _cached_analysis = st.session_state.get(_analysis_cache_key)
    if _cached_analysis and _cached_analysis.get("hash") == _alarm_hash:
        analysis_results = _cached_analysis["results"]
    elif alarms:
        analysis_results = engine.analyze(alarms)
        st.session_state[_analysis_cache_key] = {"hash": _alarm_hash, "results": analysis_results}
    else:
        analysis_results = [{
            "id": "SYSTEM",
            "label": "正常稼働",
            "prob": 0.0,
            "type": "Normal",
            "tier": 3,
            "reason": "アラームなし"
        }]
        st.session_state[_analysis_cache_key] = {"hash": _alarm_hash, "results": analysis_results}

    # =====================================================
    # DigitalTwinEngine 初期化
    # =====================================================
    dt_err_key = f"dt_engine_error_{site_id}"
    dt_engine  = None

    if not st.session_state.get(dt_err_key):
        try:
            current_topo_hash = _compute_topo_hash(topology)
            dt_engine = _get_cached_dt_engine(site_id, current_topo_hash, topology)
        except Exception as _dte_err:
            import traceback as _tb
            st.session_state[dt_err_key] = f"{type(_dte_err).__name__}: {_dte_err}\n{_tb.format_exc()}"

    _dte_error = st.session_state.get(dt_err_key)
    if _dte_error and dt_engine is None:
        with st.expander("⚠️ Digital Twin Engine 初期化エラー（予兆検知は無効）", expanded=False):
            st.code(_dte_error, language="text")
            if st.button("🔄 再初期化", key=f"dte_retry_{site_id}"):
                st.session_state.pop(dt_err_key, None)
                st.rerun()

    # 自動チューニング + 自動TP確認
    if dt_engine:
        dt_engine.maybe_run_auto_tuning()

    if dt_engine and scenario != "正常稼働":
        critical_devices = {a.device_id for a in alarms if a.severity == "CRITICAL"}
        for dev_id in critical_devices:
            confirmed_count = dt_engine.forecast_auto_confirm_on_incident(
                dev_id, scenario=scenario, note="障害シナリオ発生により自動確認"
            )
            if confirmed_count > 0:
                logger.info(f"Auto-confirmed {confirmed_count} predictions for {dev_id} on scenario: {scenario}")

    # =====================================================
    # 競合検出 + DT予兆パイプライン
    # =====================================================
    _injected        = st.session_state.get("injected_weak_signal")
    _scenario_active = (scenario != "正常稼働")
    _sim_active      = bool(_injected and _injected.get("device_id") in topology)
    _conflict = _scenario_active and _sim_active

    if _conflict:
        _sim_device     = _injected.get("device_id", "")
        _critical_set   = {a.device_id for a in alarms if a.severity == "CRITICAL"}
        _warning_set    = {a.device_id for a in alarms if a.severity == "WARNING"}
        _conflict_level = ("CRITICAL" if _sim_device in _critical_set
                           else "WARNING" if _sim_device in _warning_set
                           else "OTHER")

        if _conflict_level in ("CRITICAL", "WARNING"):
            st.warning(
                "⚠️ **予兆シミュレーション競合検出**\n\n"
                f"現在の障害シナリオ「**{scenario}**」により `{_sim_device}` は既に "
                f"**{_conflict_level}** 状態です。\n"
                "予兆シミュレーションは **無効化** されています。\n\n"
                "💡 予兆→障害の流れをデモするには:\n"
                "1. シナリオを「正常稼働」に戻す\n"
                "2. 予兆シミュレーションを実行（アンバー色でハイライト）\n"
                "3. 障害シナリオに切り替えて「予兆が的中した」を確認"
            )
        else:
            st.info(
                f"ℹ️ 障害シナリオ「**{scenario}**」実行中です。\n"
                f"`{_sim_device}` への予兆シミュレーションは継続しますが、"
                "forecast_ledger の自動 CONFIRMED 登録は **抑制** されます。"
            )

    # DT予兆生成パイプライン
    dt_predictions: List[dict] = []
    if dt_engine:
        _msg_sources = []

        # A) 予兆シミュレーション注入シグナル
        if _sim_active:
            _sim_dev  = _injected.get("device_id", "")
            _alarm_devices = {a.device_id for a in alarms
                              if a.severity in ("CRITICAL", "WARNING")}
            _disabled = (_conflict and _sim_dev in _alarm_devices)
            if not _disabled:
                _msgs = _injected.get("messages", [_injected.get("message", "")])
                for _m in _msgs:
                    if _m:
                        _msg_sources.append((_sim_dev, _m, "simulation"))

        _sim_level = int((_injected or {}).get("level", 1)) if _sim_active else 1
        _prev_sim_dev_key = f"dt_prev_sim_device_{site_id}"
        _cur_sim_dev = (_injected or {}).get("device_id", "")
        if _cur_sim_dev != st.session_state.get(_prev_sim_dev_key, ""):
            for _k in [k for k in list(st.session_state.report_cache.keys())
                       if "analyst" in k and site_id in k]:
                del st.session_state.report_cache[_k]
            st.session_state.generated_report   = None
            st.session_state.remediation_plan   = None
            st.session_state.verification_log   = None
            st.session_state[_prev_sim_dev_key] = _cur_sim_dev

        # B) 実アラームの WARNING/INFO
        for _a in alarms:
            if _a.severity in ("WARNING", "INFO") and not _a.is_root_cause:
                _msg_sources.append((_a.device_id, _a.message, "real"))

        _signal_count = len(_msg_sources)

        # メッセージ集約
        _grouped: dict = {}
        for _dev_id, _msg, _src in _msg_sources:
            if _dev_id not in _grouped:
                _grouped[_dev_id] = ([], _src)
            _grouped[_dev_id][0].append(_msg)

        # 予測キャッシュ
        _ck_pred_cache = "dt_prediction_cache"
        if _ck_pred_cache not in st.session_state:
            st.session_state[_ck_pred_cache] = {}

        # GenAI モデル初期化（session_state にキャッシュして毎回の再初期化を回避）
        _genai_model = None
        if api_key and GENAI_AVAILABLE:
            _genai_cache_key = f"_genai_model_{api_key[:8]}"
            if _genai_cache_key in st.session_state:
                _genai_model = st.session_state[_genai_cache_key]
            else:
                try:
                    genai.configure(api_key=api_key)
                    _genai_model = genai.GenerativeModel('gemma-3-4b-it')
                    st.session_state[_genai_cache_key] = _genai_model
                except Exception:
                    pass

        for _dev_id, (_msgs_list, _src) in _grouped.items():
            try:
                _combined_msg = "\n".join(_msgs_list)
                _cache_key = f"v4_{_dev_id}|{_sim_level}|{hash(_combined_msg[:200])}"

                _cached = st.session_state[_ck_pred_cache].get(_cache_key)
                if _cached is not None:
                    for _p in _cached:
                        _p["is_prediction"] = True
                        if not any(d.get("id") == _dev_id for d in dt_predictions):
                            dt_predictions.append(_p)
                    continue

                _resp = dt_engine.predict_api({
                    "tenant_id":       site_id,
                    "device_id":       _dev_id,
                    "msg":             _combined_msg,
                    "timestamp":       time.time(),
                    "record_forecast": True,
                    "attrs":           {
                        "source":            _src,
                        "degradation_level": _sim_level if _src == "simulation" else 1,
                        "signal_count":      len(_msgs_list),
                    }
                })

                _preds_returned = _resp.get("predictions", []) if _resp.get("ok") else []

                if not _preds_returned and _src == "simulation":
                    _sim_scenario = _injected.get("scenario", "異常")
                    _preds_returned = [{
                        "label": f"🔮 [予兆] {_sim_scenario} の初期兆候",
                        "predicted_state": _sim_scenario,
                        "prob": min(0.65 + (_sim_level * 0.05), 0.99),
                        "confidence": min(0.65 + (_sim_level * 0.05), 0.99),
                        "prediction_timeline": "1〜3日",
                        "prediction_affected_count": 2,
                        "prediction_time_to_critical_min": 60,
                        "prediction_time_to_failure_hours": max(72 - (_sim_level * 12), 2),
                        "rule_pattern": f"{_sim_scenario}_Auto",
                        "reasons": _msgs_list,
                        "recommended_actions": []
                    }]

                # AI動的トリアージ生成
                if _src == "simulation" and _injected:
                    ci = build_ci_context_for_chat(topology, _dev_id)
                    vendor = ci.get("vendor", "Unknown")
                    os_type = ci.get("os", "Unknown")
                    model = ci.get("model", "Unknown")

                    for _p in _preds_returned:
                        _actions = _p.get("recommended_actions", [])
                        if not _actions or len(_actions) <= 1:
                            if _genai_model is not None:
                                try:
                                    import json as _json
                                    import re as _re

                                    _prompt = f"""
                                    あなたは熟練のネットワークAIOpsエンジニアです。
                                    現在、以下の【対象機器】で予兆シグナルを検知しました。
                                    運用者が【最初の5分以内】にCLIで実行すべき「初動トリアージ」コマンドを、重要度順に【最大3つまで】JSON形式で出力してください。

                                    【★ 初動トリアージの定義（厳守）】
                                    ・目的: 「現状の把握」のみ。状態確認（show系）コマンドだけを提示する
                                    ・禁止: config系コマンド（設定変更・復旧措置）は絶対に含めない
                                    ・禁止: 詳細な診断手順や判定基準の解説は不要（それは別レポートの役割）
                                    ・各コマンドは「何を確認するか」を1行で添え、効果は「この値が分かる」程度に留める

                                    【対象機器の情報】
                                    ・ホスト名: {_dev_id}
                                    ・メーカー: {vendor}
                                    ・OS: {os_type}
                                    ・機種名: {model}

                                    【⚠️ 厳守事項：プラットフォームの限定】
                                    ・対象は上記の「ネットワーク専用機器」です。汎用Linuxサーバではありません。
                                    ・必ず {vendor} ({os_type}) の正規コマンド（例: {vendor}がCiscoなら 'show ~', Juniperなら 'show ~' や 'request ~'）を使用してください。
                                    ・Linux用のコマンド（top, ps, grep, kill, systemctl等）は【絶対に含めないでください】。
                                    ・監視ツール（Zabbix等）は導入済みのため、「監視設定の強化」等の提案は不要です。

                                    【対象ログ】
                                    {_combined_msg[:1000]}

                                    【出力JSONフォーマット】
                                    必ず以下のキー構造のJSON配列（リスト）のみを出力してください。
                                    [
                                      {{
                                        "title": "確認項目のタイトル（例: メモリ使用状況の確認）",
                                        "effect": "このコマンドで分かること（1行）",
                                        "priority": "high",
                                        "rationale": "なぜ最初にこれを確認すべきか（1行）",
                                        "steps": "show系コマンドのみ (改行は \\n を使用)"
                                      }}
                                    ]
                                    """

                                    _response = _genai_model.generate_content(_prompt)

                                    _match = _re.search(r'\[\s*\{.*?\}\s*\]', _response.text, _re.DOTALL)

                                    if _match:
                                        _json_str = _match.group(0)
                                        _dynamic_actions = _json.loads(_json_str)
                                        if isinstance(_dynamic_actions, list) and len(_dynamic_actions) > 0:
                                            _p["recommended_actions"] = _dynamic_actions[:3]
                                    else:
                                        raise ValueError("AIの回答からJSONが見つかりませんでした。")

                                except Exception as e:
                                    _err_msg = str(e)
                                    _raw_resp = getattr(_response, "text", "レスポンスなし") if '_response' in locals() else "未実行"
                                    _p["recommended_actions"] = [{
                                        "title": f"⚠️ 動的生成エラー: {type(e).__name__}",
                                        "effect": "システムエラーにより生成中断",
                                        "priority": "high",
                                        "rationale": f"エラー詳細: {_err_msg}",
                                        "steps": f"【AIの生の回答】\n{_raw_resp}"
                                    }]

                        _priority_map = {"high": 0, "medium": 1, "low": 2}
                        _p.get("recommended_actions", []).sort(
                            key=lambda x: _priority_map.get(str(x.get("priority", "")).lower(), 3)
                        )

                _preds_to_cache = []
                for _p in _preds_returned:
                    _p["id"]     = _dev_id
                    _p["source"] = _src
                    _p["prediction_signal_count"] = _signal_count
                    _p["is_prediction"] = True
                    _preds_to_cache.append(_p)

                    if not any(d.get("id") == _dev_id for d in dt_predictions):
                        dt_predictions.append(_p)

                st.session_state[_ck_pred_cache][_cache_key] = _preds_to_cache
                if len(st.session_state[_ck_pred_cache]) > 20:
                    _keys = list(st.session_state[_ck_pred_cache].keys())
                    for _old_k in _keys[:10]:
                        st.session_state[_ck_pred_cache].pop(_old_k, None)

            except Exception as _pred_err:
                logger.warning(f"predict_api failed for {_dev_id}: {_pred_err}")

        # 自動 outcome 登録
        for _rid, _recovered in list(st.session_state.get("recovered_devices", {}).items()):
            if _recovered:
                _auto_key = f"dt_auto_mitigated_{site_id}_{_rid}"
                if not st.session_state.get(_auto_key):
                    dt_engine.forecast_auto_resolve(
                        _rid, "mitigated", note="Execute 成功による自動解消")
                    st.session_state[_auto_key] = True

        if not _conflict:
            _critical_devices = {a.device_id for a in alarms if a.severity == "CRITICAL"}
            for _cd in _critical_devices:
                _auto_key = f"dt_auto_confirmed_{site_id}_{_cd}"
                if not st.session_state.get(_auto_key):
                    dt_engine.forecast_auto_resolve(
                        _cd, "confirmed_incident",
                        note="CRITICAL アラームによる自動確定")
                    st.session_state[_auto_key] = True

    # DT予兆を analysis_results にマージ
    existing_pred_ids = {r.get("id") for r in analysis_results if r.get("is_prediction")}
    for _dp in dt_predictions:
        if _dp.get("id") not in existing_pred_ids:
            analysis_results.append(_dp)

    # シミュレーション状態変更検知 → レポートリセット
    _sim_state_now = f"{_injected.get('device_id')}_{_injected.get('scenario')}_{_injected.get('level')}" if _injected else None
    _sim_state_key = f"dt_last_sim_state_{site_id}"
    _sim_state_prev = st.session_state.get(_sim_state_key)

    if _sim_state_now != _sim_state_prev:
        st.session_state.generated_report   = None
        st.session_state.remediation_plan   = None
        st.session_state.verification_log   = None
        st.session_state.live_result        = None
        st.session_state.verification_result = None
        _keys_to_del = [k for k in st.session_state.get("report_cache", {})
                        if "analyst" in k or "remediation" in k]
        for _k in _keys_to_del:
            st.session_state.report_cache.pop(_k, None)
        st.session_state[_sim_state_key] = _sim_state_now

    # =====================================================
    # 3分類: root_cause / symptom / unrelated
    # =====================================================
    root_cause_candidates = []
    symptom_devices = []
    unrelated_devices = []

    # ★ 高速化: アラームのデバイスIDセットを事前計算（O(n*m) → O(n+m)）
    _rc_device_ids = {a.device_id for a in alarms if a.is_root_cause}
    _non_rc_device_ids = {a.device_id for a in alarms if not a.is_root_cause}

    for cand in analysis_results:
        device_id = cand.get('id', '')
        cls = cand.get('classification', '')

        if cand.get('is_prediction'):
            root_cause_candidates.append(cand)
        elif cls == 'root_cause':
            root_cause_candidates.append(cand)
        elif cls == 'symptom':
            symptom_devices.append(cand)
        elif cls == 'unrelated':
            unrelated_devices.append(cand)
        else:
            if device_id in _rc_device_ids:
                root_cause_candidates.append(cand)
            elif device_id in _non_rc_device_ids:
                symptom_devices.append(cand)
            elif cand.get('prob', 0) > 0.5:
                root_cause_candidates.append(cand)

    if not root_cause_candidates:
        root_cause_candidates = [{
            "id": "SYSTEM", "label": "正常稼働", "prob": 0.0,
            "type": "Normal", "tier": 3, "reason": "異常は検知されていません",
            "classification": "unrelated"
        }]

    # =====================================================
    # UI描画（コンポーネントに委譲）
    # =====================================================

    # 1. KPIバナー
    prediction_count, noise_reduction = render_kpi_banner(
        analysis_results, alarms,
        root_cause_candidates, symptom_devices, unrelated_devices,
    )

    # 2. Future Radar（予兆専用表示）
    prediction_candidates = [c for c in root_cause_candidates if c.get('is_prediction')]
    render_future_radar(prediction_candidates)

    # 3. 根本原因候補テーブル
    selected_incident_candidate, target_device_id = render_root_cause_table(
        root_cause_candidates, symptom_devices, unrelated_devices, alarms,
    )

    # 4. 2カラムレイアウト
    col_map, col_chat = st.columns([1.2, 1])

    # 左カラム: トポロジー + 影響伝搬 + AI学習ルール + Auto-Diagnostics
    with col_map:
        render_topology_panel(
            topology, alarms, analysis_results,
            selected_incident_candidate, target_device_id,
            dt_engine, engine, scenario, api_key,
        )

    # 右カラム: AI Analyst Report + Remediation + Chat
    with col_chat:
        render_analyst_report(
            selected_incident_candidate, topology,
            scenario, site_id, api_key,
        )

        render_remediation(
            selected_incident_candidate, topology,
            scenario, site_id, api_key, dt_engine,
        )

        render_chat_panel(
            selected_incident_candidate, target_device_id,
            topology, api_key,
        )
