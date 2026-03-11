# ui/components/future_radar.py — 予兆専用表示エリア（AIOps Future Radar）
import streamlit as st
from typing import List
from .helpers import st_html
from .command_popup import (
    render_triage_cards,
    show_command_popup_if_pending,
)
from ui.service_tier import render_tier_gated, TIER_PHM


def render_future_radar(prediction_candidates: List[dict]):
    """予兆候補の表示エリア。prediction_candidatesが空なら何も表示しない。"""
    if not prediction_candidates:
        return

    # ★ 拡張B: 保留中のトリアージコマンド実行結果ポップアップを表示
    show_command_popup_if_pending()

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

            # ★ 拡張B: 初動トリアージ（共通コンポーネント使用）
            rec_actions = pc.get('recommended_actions', [])
            if rec_actions:
                with st.expander("🛠 初動トリアージ（推奨アクション）", expanded=True):
                    st.caption("🕐 最初の5分: 状況把握のためのshowコマンドです。"
                               "詳細診断 → 「確認手順を生成」 / 予防措置 → 「予防措置プランを生成」")
                    render_triage_cards(rec_actions, _pred_device, pc_idx)
