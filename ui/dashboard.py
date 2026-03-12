import streamlit as st
import pandas as pd
from typing import List
from dataclasses import dataclass

from registry import list_sites, get_paths, load_topology, get_display_name
from alarm_generator import generate_alarms_for_scenario, get_alarm_summary
from utils.helpers import get_status_from_alarms, get_status_icon


@dataclass
class SiteStatus:
    site_id: str
    display_name: str
    scenario: str
    status: str
    alarm_count: int
    critical_count: int
    warning_count: int
    affected_devices: List[str]
    is_maintenance: bool
    affected_count: int          # 影響を受けるデバイスの実数


def build_site_statuses() -> List[SiteStatus]:
    """全拠点の状態を構築（シナリオ/メンテ変更時のみ再計算）"""
    # ★ 高速化: シナリオ・メンテフラグが変わらなければキャッシュを返す
    _scenarios_sig = hash(tuple(sorted(st.session_state.site_scenarios.items())))
    _maint_sig = hash(tuple(sorted(st.session_state.maint_flags.items())))
    _maint_dev_sig = hash(tuple(
        (k, tuple(sorted(v))) for k, v in sorted(st.session_state.get("maint_devices", {}).items())
    ))
    _cache_key = "_site_statuses_cache"
    _cached = st.session_state.get(_cache_key)
    if _cached and _cached.get("sig") == (_scenarios_sig, _maint_sig, _maint_dev_sig):
        return _cached["data"]

    sites = list_sites()
    statuses = []
    for site_id in sites:
        scenario = st.session_state.site_scenarios.get(site_id, "正常稼働")
        paths = get_paths(site_id)
        topology = load_topology(paths.topology_path)
        alarms = generate_alarms_for_scenario(topology, scenario)
        summary = get_alarm_summary(alarms)
        status = get_status_from_alarms(scenario, alarms)
        is_maint = st.session_state.maint_flags.get(site_id, False)

        statuses.append(SiteStatus(
            site_id=site_id,
            display_name=get_display_name(site_id),
            scenario=scenario,
            status=status,
            alarm_count=summary['total'],
            critical_count=summary['critical'],
            warning_count=summary['warning'],
            affected_devices=summary['devices'],
            is_maintenance=is_maint,
            affected_count=len(summary['devices']),
        ))

    priority = {"停止": 0, "要対応": 1, "注意": 2, "正常": 3}
    statuses.sort(key=lambda s: (priority.get(s.status, 4), -s.alarm_count))
    st.session_state[_cache_key] = {"sig": (_scenarios_sig, _maint_sig, _maint_dev_sig), "data": statuses}
    return statuses


def render_site_status_board():
    """拠点状態ボード"""
    st.subheader("🏢 拠点状態ボード")
    statuses = build_site_statuses()

    cols = st.columns(4)
    cols[0].metric("🔴 障害発生", f"{sum(1 for s in statuses if s.status == '停止')}拠点")
    cols[1].metric("🟠 要対応",   f"{sum(1 for s in statuses if s.status == '要対応')}拠点")
    cols[2].metric("🟡 注意",     f"{sum(1 for s in statuses if s.status == '注意')}拠点")
    cols[3].metric("🟢 正常",     f"{sum(1 for s in statuses if s.status == '正常')}拠点")

    st.divider()

    cols_per_row = 2
    for i in range(0, len(statuses), cols_per_row):
        row_cols = st.columns(cols_per_row)
        for j, col in enumerate(row_cols):
            if i + j < len(statuses):
                site = statuses[i + j]
                with col.container(border=True):
                    _render_site_card(site)


def _render_site_card(site: SiteStatus):
    """拠点カードをカスタムHTMLで描画"""
    # 色テーマ
    _STATUS_THEME = {
        "停止":  {"bg": "#FDE8E8", "border": "#D32F2F", "icon": "🔴", "bar": "#D32F2F"},
        "要対応": {"bg": "#FFF3E0", "border": "#FF9800", "icon": "🟠", "bar": "#FF9800"},
        "注意":  {"bg": "#FFF8E1", "border": "#FFC107", "icon": "🟡", "bar": "#FFC107"},
        "正常":  {"bg": "#E8F5E9", "border": "#4CAF50", "icon": "🟢", "bar": "#4CAF50"},
    }
    theme = _STATUS_THEME.get(site.status, _STATUS_THEME["正常"])

    # 深刻度スコア
    severity = min(100, site.critical_count * 30 + site.warning_count * 10) if site.alarm_count > 0 else 0

    # シナリオ表示
    scenario_display = site.scenario.split(". ", 1)[-1] if ". " in site.scenario else site.scenario

    # メンテナンスバッジ（拠点全体 + 機器単位 + ウィンドウ予定）
    _dev_maint = st.session_state.get("maint_devices", {}).get(site.site_id, set())
    if site.is_maintenance:
        maint_badge = '<span style="background:#E3F2FD;color:#1565C0;font-size:11px;padding:2px 6px;border-radius:3px;margin-left:8px;">🛠 メンテ中</span>'
    elif _dev_maint:
        maint_badge = f'<span style="background:#E3F2FD;color:#1565C0;font-size:11px;padding:2px 6px;border-radius:3px;margin-left:8px;">🔧 {len(_dev_maint)}台メンテ中</span>'
    else:
        maint_badge = ""

    # ウィンドウ予定バッジ
    from datetime import datetime as _dt_cls
    _now_ts = _dt_cls.now()
    _site_windows = [
        w for w in st.session_state.get("maint_windows", [])
        if w.get("site_id") == site.site_id and w.get("end") > _now_ts
    ]
    if _site_windows:
        _active_w = sum(1 for w in _site_windows if w.get("start") <= _now_ts)
        _upcoming_w = len(_site_windows) - _active_w
        if _active_w:
            maint_badge += (
                f'<span style="background:#E8F5E9;color:#2E7D32;font-size:11px;'
                f'padding:2px 6px;border-radius:3px;margin-left:4px;">'
                f'📅 {_active_w}件実行中</span>'
            )
        if _upcoming_w:
            maint_badge += (
                f'<span style="background:#FFF3E0;color:#E65100;font-size:11px;'
                f'padding:2px 6px;border-radius:3px;margin-left:4px;">'
                f'📅 {_upcoming_w}件予定</span>'
            )

    # 影響機器テキスト
    if site.affected_devices:
        dev_text = ", ".join(site.affected_devices[:4])
        if len(site.affected_devices) > 4:
            dev_text += f" 他{len(site.affected_devices) - 4}台"
    else:
        dev_text = "-"

    c1, c2 = st.columns([3, 1])
    c1.markdown(f"### {theme['icon']} {site.display_name}{maint_badge}", unsafe_allow_html=True)
    if c2.button("詳細", key=f"board_det_{site.site_id}", type="primary"):
        st.session_state.active_site = site.site_id
        st.session_state.live_result = None
        st.session_state.verification_result = None
        st.session_state.generated_report = None
        st.session_state.remediation_plan = None
        st.session_state.messages = []
        st.session_state.chat_session = None
        st.rerun()

    st.caption(f"📋 {scenario_display}")

    # KPI 3列: カスタムHTML
    st.markdown(f"""<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin:8px 0;">
  <div style="background:{theme['bg']};border-radius:6px;padding:8px 10px;text-align:center;">
    <div style="font-size:11px;color:#666;font-weight:600;">ステータス</div>
    <div style="font-size:22px;font-weight:700;color:{theme['border']};">{site.status}</div>
  </div>
  <div style="background:#F5F5F5;border-radius:6px;padding:8px 10px;text-align:center;">
    <div style="font-size:11px;color:#666;font-weight:600;">アラーム</div>
    <div style="font-size:22px;font-weight:700;">{site.alarm_count}<span style="font-size:13px;color:#888;">件</span></div>
  </div>
  <div style="background:#F5F5F5;border-radius:6px;padding:8px 10px;text-align:center;">
    <div style="font-size:11px;color:#666;font-weight:600;">影響台数</div>
    <div style="font-size:22px;font-weight:700;">{site.affected_count}<span style="font-size:13px;color:#888;">台</span></div>
  </div>
</div>""", unsafe_allow_html=True)

    # 深刻度バー
    if site.alarm_count > 0:
        bar_color = theme['bar']
        st.markdown(f"""<div style="margin:4px 0 2px 0;">
  <div style="font-size:11px;color:#666;font-weight:600;">深刻度: {severity}%</div>
  <div style="background:#E0E0E0;border-radius:4px;height:8px;overflow:hidden;margin-top:2px;">
    <div style="background:{bar_color};width:{severity}%;height:100%;border-radius:4px;transition:width 0.3s;"></div>
  </div>
</div>""", unsafe_allow_html=True)

    # 影響機器
    if site.affected_devices:
        st.markdown(
            f'<div style="font-size:12px;color:#888;margin-top:4px;">影響機器: {dev_text}</div>',
            unsafe_allow_html=True,
        )


def render_triage_center():
    """
    トリアージ・コマンドセンター（旧UIを完全復元）
    ─ フィルタ（ステータス multiselect + メンテナンス中チェック）
    ─ 各拠点を border付きコンテナ + 5カラムレイアウトで表示
      [アイコン | 拠点名/シナリオ | CRITICAL/WARNING件数 | MTTR | 詳細を確認ボタン]
    """
    st.subheader("🚨 トリアージ・コマンドセンター")

    statuses = build_site_statuses()

    # ── フィルタ行（旧UIと同じ2カラム構成）──
    col1, col2 = st.columns(2)
    with col1:
        filter_status = st.multiselect(
            "ステータスでフィルタ",
            ["停止", "要対応", "注意", "正常"],
            default=["停止", "要対応"],
            key="triage_filter"
        )
    with col2:
        show_maint = st.checkbox("メンテナンス中を含む", value=False, key="triage_maint")

    filtered = [
        s for s in statuses
        if s.status in filter_status
        and (show_maint or not s.is_maintenance)
    ]

    if not filtered:
        st.info("フィルタ条件に該当する拠点はありません。")
        return

    # ── 各拠点カード（旧UIと同じ5カラムレイアウト）──
    for site in filtered:
        with st.container(border=True):
            cols = st.columns([0.5, 2, 1.5, 1, 1.5])

            # col[0]: ステータスアイコン（大）
            with cols[0]:
                st.markdown(f"## {get_status_icon(site.status)}")

            # col[1]: 拠点名 + シナリオ
            with cols[1]:
                st.markdown(f"**{site.display_name}**")
                scenario_short = site.scenario.split(". ", 1)[-1][:30]
                st.caption(scenario_short)

            # col[2]: CRITICAL / WARNING 件数
            with cols[2]:
                if site.critical_count > 0:
                    st.error(f"🔴 {site.critical_count} CRITICAL")
                if site.warning_count > 0:
                    st.warning(f"🟡 {site.warning_count} WARNING")

            # col[3]: 影響台数
            with cols[3]:
                st.metric("影響台数", f"{site.affected_count}台")

            # col[4]: 詳細を確認ボタン
            with cols[4]:
                btn_type = "primary" if site.status in ["停止", "要対応"] else "secondary"
                if st.button("📋 詳細を確認", key=f"triage_detail_{site.site_id}", type=btn_type):
                    st.session_state.active_site = site.site_id
                    # セッション状態をリセット
                    st.session_state.live_result = None
                    st.session_state.verification_result = None
                    st.session_state.generated_report = None
                    st.session_state.remediation_plan = None
                    st.session_state.messages = []
                    st.session_state.chat_session = None
                    st.rerun()
