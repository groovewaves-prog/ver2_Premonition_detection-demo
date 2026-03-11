# ui/components/future_radar.py — 予兆専用表示エリア（AIOps Future Radar）
import streamlit as st
from typing import List
from .helpers import st_html
from .command_popup import (
    extract_cli_commands,
    simulate_command_execution,
    render_command_result_popup,
    show_command_popup_if_pending,
)


def render_future_radar(prediction_candidates: List[dict]):
    """予兆候補の表示エリア。prediction_candidatesが空なら何も表示しない。"""
    if not prediction_candidates:
        return

    # ★ 拡張B: 保留中のトリアージコマンド実行結果ポップアップを表示
    show_command_popup_if_pending()

    st.markdown("### 🔮 AIOps Future Radar")
    with st.container(border=True):
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

            # ★ 拡張B: 初動トリアージ（旧カードスタイル + バッジボタン化）
            rec_actions = pc.get('recommended_actions', [])
            if rec_actions:
                _render_triage_cards(rec_actions, _pred_device, pc_idx)


def _render_triage_cards(rec_actions: list, device_id: str, card_idx: int):
    """★ 拡張B: 初動トリアージを旧スタイルのカード表示で描画。

    各カードの「最優先」「推奨」「補助」バッジをボタンにし、
    押下時にカード内の手順からCLIコマンドのみを抽出して実行、
    結果をポップアップで表示する。
    """
    # プライオリティ別ソート
    _priority_order = {"最優先": 0, "high": 0, "推奨": 1, "medium": 1, "補助": 2, "low": 2}
    sorted_actions = sorted(
        rec_actions,
        key=lambda x: _priority_order.get(str(x.get("priority", "")).lower(), 3),
    )

    with st.expander("🛠 初動トリアージ（推奨アクション）", expanded=True):
        st.caption("🕐 最初の5分: 状況把握のためのshowコマンドです。"
                   "詳細診断 → 「確認手順を生成」 / 予防措置 → 「予防措置プランを生成」")

        for act_idx, ra in enumerate(sorted_actions):
            _title     = ra.get("title", "")
            _effect    = ra.get("effect", "")
            _rationale = ra.get("rationale", "")
            _priority  = ra.get("priority", "")
            _steps     = ra.get("steps", ra.get("command", ra.get("action", "")))

            # プライオリティ判定
            _pri_lower = str(_priority).lower()
            if _pri_lower in ("最優先", "high"):
                _pri_label = "最優先"
                _pri_bg = "#D32F2F"
            elif _pri_lower in ("推奨", "medium"):
                _pri_label = "推奨"
                _pri_bg = "#FF9800"
            else:
                _pri_label = "補助"
                _pri_bg = "#558B2F"

            # 手順テキストをフォーマット
            _steps_display = _steps.replace("\\n", "\n")
            _steps_lines = [line.strip() for line in _steps_display.split("\n") if line.strip()]
            _steps_numbered = "\n".join(
                f"{i+1}. {line}" if not line[0].isdigit() else line
                for i, line in enumerate(_steps_lines)
            )

            # CLIコマンド抽出（ボタン押下時に実行する対象）
            _cli_cmds = extract_cli_commands(_steps)

            # カード描画: 旧スタイルのレイアウト
            # ヘッダ行（番号 + タイトル + ボタン）
            _col_info, _col_btn = st.columns([4, 1])
            with _col_info:
                st.markdown(
                    f"**🔴 {act_idx + 1}. ⚠ {_title}**" if _pri_label == "最優先"
                    else f"**🟠 {act_idx + 1}. ⚠ {_title}**" if _pri_label == "推奨"
                    else f"**🟢 {act_idx + 1}. {_title}**"
                )
            with _col_btn:
                _btn_key = f"triage_{card_idx}_{act_idx}_{device_id}"
                if _cli_cmds:
                    if st.button(
                        _pri_label,
                        key=_btn_key,
                        type="primary" if _pri_label == "最優先" else "secondary",
                        use_container_width=True,
                    ):
                        _results = [
                            simulate_command_execution(cmd, device_id)
                            for cmd in _cli_cmds
                        ]
                        render_command_result_popup(
                            f"🔧 {_title}: {device_id}",
                            _results,
                        )
                        st.rerun()
                else:
                    # CLIコマンドがない場合はバッジのみ表示（ボタンにしない）
                    st.markdown(
                        f'<span style="background:{_pri_bg};color:#fff;padding:4px 12px;'
                        f'border-radius:4px;font-size:13px;font-weight:700;">{_pri_label}</span>',
                        unsafe_allow_html=True,
                    )

            # 効果・根拠
            if _effect:
                st.caption(f"💡 効果: {_effect}")
            if _rationale:
                st.caption(f"⭐ 根拠: {_rationale}")

            # 手順（コードブロック風）
            if _steps_lines:
                st.markdown(
                    f'<div style="background:#f8f8f8;border:1px solid #e8e8e8;border-radius:4px;'
                    f'padding:8px 12px;font-size:13px;font-family:monospace;margin:4px 0 12px 0;'
                    f'white-space:pre-wrap;line-height:1.6;">📋 手順:\n{_steps_numbered}</div>',
                    unsafe_allow_html=True,
                )
