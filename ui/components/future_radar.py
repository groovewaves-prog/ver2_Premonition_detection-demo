# ui/components/future_radar.py — 予兆専用表示エリア（AIOps Future Radar）
import streamlit as st
from typing import List
from .helpers import st_html
from .command_popup import (
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

            # ★ 拡張B: 推奨アクションはHTMLカード内に概要のみ表示（ボタンは下に配置）
            rec_actions = pc.get('recommended_actions', [])
            _triage_summary_html = ""
            if rec_actions:
                _triage_summary_html = (
                    f'<div style="margin-top:8px;font-size:12px;font-weight:600;color:#333;">'
                    f'🔧 初動トリアージ: {len(rec_actions)}件の確認コマンド'
                    f'</div>'
                )

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
                {_triage_summary_html}
                {_signal_html}
            </div>
            """
            st_html(card_html)

            # ★ 拡張B: トリアージコマンドをボタン化（HTMLカード外にStreamlitボタンで配置）
            if rec_actions:
                _render_triage_buttons(rec_actions, _pred_device, pc_idx)


def _render_triage_buttons(rec_actions: list, device_id: str, card_idx: int):
    """★ 拡張B: 初動トリアージコマンドをボタンとして描画。

    各コマンドに対して実行ボタンを表示し、押下でコマンドを実行して結果をポップアップ表示する。
    「一括実行」ボタンで全コマンドをまとめて実行することも可能。
    """
    # プライオリティ別ソート
    _priority_order = {"最優先": 0, "high": 0, "推奨": 1, "medium": 1, "low": 2}
    sorted_actions = sorted(
        rec_actions,
        key=lambda x: _priority_order.get(str(x.get("priority", "")).lower(), 3),
    )

    # 一括実行ボタン + 個別ボタンのレイアウト
    with st.container():
        # 一括実行ボタン
        _bulk_key = f"triage_bulk_{card_idx}_{device_id}"
        if st.button(
            f"▶ 全 {len(sorted_actions)} コマンドを一括実行",
            key=_bulk_key,
            type="secondary",
            use_container_width=True,
        ):
            _results = []
            for ra in sorted_actions:
                _steps = ra.get("steps", ra.get("command", ra.get("action", "")))
                # steps に改行区切りで複数コマンドが含まれる場合、各行を実行
                _cmds = [
                    line.strip()
                    for line in _steps.replace("\\n", "\n").split("\n")
                    if line.strip()
                ]
                for _cmd in _cmds:
                    _results.append(simulate_command_execution(_cmd, device_id))
            render_command_result_popup(
                f"🔧 初動トリアージ結果: {device_id}",
                _results,
            )
            st.rerun()

        # 個別コマンドボタン
        for act_idx, ra in enumerate(sorted_actions):
            _title = ra.get("title", "")
            _steps = ra.get("steps", ra.get("command", ra.get("action", "")))
            _effect = ra.get("effect", "")
            _priority = ra.get("priority", "")
            _rationale = ra.get("rationale", "")

            # プライオリティバッジ
            _pri_lower = str(_priority).lower()
            if _pri_lower in ("最優先", "high"):
                _badge = "🔴"
                _btn_type = "primary"
            elif _pri_lower in ("推奨", "medium"):
                _badge = "🟠"
                _btn_type = "secondary"
            else:
                _badge = "🔵"
                _btn_type = "secondary"

            # コマンド行を抽出
            _cmds = [
                line.strip()
                for line in _steps.replace("\\n", "\n").split("\n")
                if line.strip()
            ]
            _cmd_display = _cmds[0] if _cmds else _steps
            if len(_cmds) > 1:
                _cmd_display += f" (+{len(_cmds)-1})"

            _btn_key = f"triage_{card_idx}_{act_idx}_{device_id}"
            _col_btn, _col_info = st.columns([1, 2])
            with _col_btn:
                if st.button(
                    f"{_badge} {_cmd_display}",
                    key=_btn_key,
                    type=_btn_type,
                    use_container_width=True,
                ):
                    _results = [
                        simulate_command_execution(_cmd, device_id)
                        for _cmd in _cmds
                    ]
                    render_command_result_popup(
                        f"🔧 {_title or _cmd_display}",
                        _results,
                    )
                    st.rerun()
            with _col_info:
                _info_parts = []
                if _title:
                    _info_parts.append(f"**{_title}**")
                if _effect:
                    _info_parts.append(_effect)
                if _rationale:
                    _info_parts.append(f"_({_rationale})_")
                st.caption(" — ".join(_info_parts) if _info_parts else _steps)
