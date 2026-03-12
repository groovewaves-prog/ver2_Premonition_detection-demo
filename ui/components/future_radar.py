# ui/components/future_radar.py — 予兆専用表示エリア（AIOps Future Radar）
import json as _json
import re as _re
import logging
import streamlit as st
from typing import List
from .helpers import st_html, build_ci_context_for_chat
from .command_popup import render_triage_cards
from ui.service_tier import render_tier_gated, TIER_PHM

logger = logging.getLogger(__name__)


def _generate_prediction_triage_lazy(pc: dict, topology: dict) -> list:
    """予兆候補に対してオンデマンドでトリアージを生成する。

    cockpit.py での一括生成を廃止し、Future Radar 表示時に
    遅延生成することで描画を高速化する（2-8秒の遅延を解消）。
    結果は session_state にキャッシュされ、次回以降は即座に返却される。
    """
    _dev_id = pc.get('id', '')
    _scenario = pc.get('predicted_state', pc.get('label', ''))
    _reasons = pc.get('reasons', [])
    _combined_msg = "\n".join(_reasons) if _reasons else pc.get('label', '')

    # ★ キャッシュキーはデバイス+シナリオ+レベルベース（msg非依存で高HIT率）
    _injected = st.session_state.get("injected_weak_signal", {})
    _level = _injected.get("level", 0)
    _triage_cache_key = f"_triage_pred_{_dev_id}_{_scenario}_{_level}"

    _cached = st.session_state.get(_triage_cache_key)
    if _cached is not None:
        return _cached

    api_key = st.session_state.get("api_key")
    if not api_key:
        return []

    _genai_key = f"_genai_model_{api_key[:8]}"
    _genai_model = st.session_state.get(_genai_key)
    if not _genai_model:
        return []

    ci = build_ci_context_for_chat(topology or {}, _dev_id)
    vendor = ci.get("vendor", "Unknown")
    os_type = ci.get("os", "Unknown")
    model_name = ci.get("model", "Unknown")

    _prompt = f"""あなたは熟練のネットワークAIOpsエンジニアです。
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
・機種名: {model_name}

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
    try:
        with st.spinner(f"🔄 {_dev_id} の初動トリアージを生成中..."):
            _response = _genai_model.generate_content(_prompt)
            _match = _re.search(r'\[\s*\{.*?\}\s*\]', _response.text, _re.DOTALL)

            if _match:
                _dynamic_actions = _json.loads(_match.group(0))
                if isinstance(_dynamic_actions, list) and len(_dynamic_actions) > 0:
                    _result = _dynamic_actions[:3]
                    _priority_map = {"high": 0, "medium": 1, "low": 2}
                    _result.sort(
                        key=lambda x: _priority_map.get(
                            str(x.get("priority", "")).lower(), 3
                        )
                    )
                    st.session_state[_triage_cache_key] = _result
                    return _result
    except Exception as e:
        logger.warning(f"Prediction triage lazy generation failed for {_dev_id}: {e}")

    return []


def render_future_radar(prediction_candidates: List[dict], topology: dict = None):
    """予兆候補の表示エリア。prediction_candidatesが空なら何も表示しない。"""
    if not prediction_candidates:
        return

    st.markdown("### 🔮 AIOps Future Radar")
    with render_tier_gated(TIER_PHM, "予兆検知 (Future Radar)"), st.container(border=True):
        injected_info = st.session_state.get("injected_weak_signal")
        if injected_info:
            level = injected_info.get("level", 0)
            _sim_scenario = injected_info.get("scenario", "不明")
            _sim_device   = injected_info.get("device_id", "不明")
            st_html(
                f'<div style="font-size:12px;color:#E65100;background:#FFF3E0;'
                f'padding:6px 12px;border-radius:4px;border:1px solid #FFE0B2;margin-bottom:8px;">'
                f'📡 シミュレーション: <b>{_sim_scenario}</b> → {_sim_device} '
                f'(劣化レベル: {level}/5)</div>'
            )

        for pc_idx, pc in enumerate(prediction_candidates):
            _pred_device = pc.get('id', '')
            _pred_prob   = pc.get('prob', 0)
            _pred_label  = pc.get('label', '')
            _pred_ttf    = pc.get('prediction_time_to_failure_hours', 0)
            _pred_aff    = pc.get('prediction_affected_count', 0)
            _pred_timeline = pc.get('prediction_timeline', '')
            _pred_failure_dt = pc.get('prediction_failure_datetime', '')
            _pred_early_hours = pc.get('prediction_early_warning_hours', 0)

            # RUL 表示
            if _pred_ttf >= 24:
                _rul_display = f"推定 {_pred_ttf // 24}日後"
                if _pred_failure_dt:
                    _rul_display += f" ({_pred_failure_dt})"
            elif _pred_ttf > 0:
                _rul_display = f"推定 {_pred_ttf}時間後"
                if _pred_failure_dt:
                    _rul_display += f" ({_pred_failure_dt})"
            else:
                _rul_display = "障害切迫"

            # 予兆時間
            if _pred_early_hours >= 24:
                _early_str = f"(予兆: {_pred_early_hours // 24}日前〜)"
            elif _pred_early_hours > 0:
                _early_str = f"(予兆: {_pred_early_hours}時間前〜)"
            else:
                _early_str = ""

            # Signal details
            _signal_html = ""
            _signal_details = pc.get('prediction_signal_details', [])
            if _signal_details:
                _sig_items = []
                for sd in _signal_details[:3]:
                    _sig_items.append(
                        f'<div style="font-size:11px;color:#666;padding:2px 0;">'
                        f'・{sd}</div>'
                    )
                _signal_html = (
                    f'<div style="margin-top:6px;">'
                    f'{"".join(_sig_items)}'
                    f'</div>'
                )

            # ヘッダーカード（RUL・影響台数等）
            card_html = f"""
            <div style="background:#fff;border:1px solid #FFE0B2;border-left:4px solid #FF9800;
                        border-radius:6px;padding:12px 16px;margin-bottom:4px;">
                <div style="display:flex;justify-content:space-between;align-items:center;">
                    <div>
                        <span style="font-size:15px;font-weight:700;color:#E65100;">🔮 {_pred_device}</span>
                        <span style="font-size:12px;color:#666;margin-left:8px;">{_pred_label}</span>
                    </div>
                    <div style="font-size:20px;font-weight:700;color:#E65100;">{_pred_prob*100:.0f}%</div>
                </div>
                <div style="display:flex;gap:20px;margin-top:8px;font-size:12px;color:#555;">
                    <span>⏱ RUL: <b>{_rul_display}</b></span>
                    <span>⚡ 急性期: <b>{_pred_timeline}</b> {_early_str}</span>
                    <span>🌐 影響: <b>{_pred_aff}台</b></span>
                </div>
                {_signal_html}
            </div>
            """
            st_html(card_html)

            # ★ 初動トリアージ: 遅延ロードで表示時にのみ LLM 呼出
            rec_actions = pc.get('recommended_actions', [])
            if not rec_actions:
                rec_actions = _generate_prediction_triage_lazy(pc, topology)
            if rec_actions:
                with st.expander("🛠 初動トリアージ（推奨アクション）", expanded=True):
                    st.caption(
                        "🕐 最初の5分: 状況把握のためのshowコマンドです。"
                        "「▶ 全コマンド一括実行」で全 show を一度に実行できます。"
                        "🔧マークは人手作業です。"
                    )
                    render_triage_cards(rec_actions, _pred_device, pc_idx)
