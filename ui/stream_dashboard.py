# ui/stream_dashboard.py
# アラームストリーム・リアルタイムダッシュボード
#
# 視覚的に劣化進行を表示:
#   - タイムライン: ステージ遷移の横方向プログレスバー
#   - メトリクスゲージ: 現在値・閾値・危険域の視覚的表示
#   - 劣化曲線チャート: SVGによる時系列グラフ
#   - イベントログ: 色分けされたアラーム履歴

import streamlit as st
import time
import math
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Tuple
from digital_twin_pkg.alarm_stream import (
    AlarmStreamSimulator,
    DEGRADATION_SEQUENCES,
    SCENARIO_BASE_TTF_HOURS,
    _DETERMINISTIC_DECAY,
    get_available_scenarios,
    get_default_interfaces,
    StreamEvent,
)

logger = logging.getLogger(__name__)


def _st_html(html: str, height: int = 0) -> None:
    """SVG/HTMLをStreamlitで描画する。

    height > 0 の場合: st.components.v1.html() で明示的高さを指定（SVG用）。
    height == 0 の場合: st.markdown(unsafe_allow_html=True) を使用（通常HTML用）。

    st.html() は iframe でSVG高さが自動計算されないため使用しない。
    """
    if height > 0:
        import streamlit.components.v1 as components
        components.html(html, height=height, scrolling=False)
    else:
        st.markdown(html, unsafe_allow_html=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# セッションステート管理
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_STREAM_STATE_KEY = "alarm_stream_sim"
_STREAM_EVENTS_KEY = "alarm_stream_events"


def _get_simulator() -> Optional[AlarmStreamSimulator]:
    state = st.session_state.get(_STREAM_STATE_KEY)
    if state is None:
        return None
    return AlarmStreamSimulator.from_state_dict(state)


def _save_simulator(sim: AlarmStreamSimulator):
    st.session_state[_STREAM_STATE_KEY] = sim.to_state_dict()


def _clear_simulator():
    st.session_state.pop(_STREAM_STATE_KEY, None)
    st.session_state.pop(_STREAM_EVENTS_KEY, None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SVG チャート生成
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _render_metric_gauge_svg(
    current_value: float,
    normal_value: float,
    failure_value: float,
    unit: str,
    label: str,
    width: int = 300,
    height: int = 180,
) -> str:
    """SVGでメトリクスゲージを描画（三角ポインタ方式）"""
    # 値の正規化 (0=正常, 1=障害)
    val_range = abs(failure_value - normal_value)
    if val_range < 0.001:
        val_range = 1.0
    if failure_value > normal_value:
        normalized = (current_value - normal_value) / val_range
    else:
        normalized = (normal_value - current_value) / val_range
    normalized = max(0.0, min(1.0, normalized))

    # 色の決定
    if normalized < 0.3:
        color = "#4CAF50"  # 緑
        status = "正常"
    elif normalized < 0.6:
        color = "#FF9800"  # オレンジ
        status = "注意"
    elif normalized < 0.85:
        color = "#FF5722"  # 赤オレンジ
        status = "警戒"
    else:
        color = "#D32F2F"  # 赤
        status = "危険"

    # アーク角度計算 (半円: 左端=π=正常, 右端=0=障害)
    rad = math.pi * (1.0 - normalized)
    cx, cy = width / 2, height - 30
    radius = min(width, height) * 0.45
    needle_len = radius * 0.82
    needle_tip_x = cx + needle_len * math.cos(rad)
    needle_tip_y = cy - needle_len * math.sin(rad)

    # 針の根元（三角形の底辺2点）
    perp_rad = rad + math.pi / 2
    base_half = 4
    base_x1 = cx + base_half * math.cos(perp_rad)
    base_y1 = cy - base_half * math.sin(perp_rad)
    base_x2 = cx - base_half * math.cos(perp_rad)
    base_y2 = cy + base_half * math.sin(perp_rad)

    # アーク描画パラメータ
    arc_r = radius * 0.85

    svg = f"""<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg"
         viewBox="0 0 {width} {height}">
  <!-- 背景アーク (灰色) -->
  <path d="M {cx - arc_r} {cy} A {arc_r} {arc_r} 0 0 1 {cx + arc_r} {cy}"
        fill="none" stroke="#E0E0E0" stroke-width="18" stroke-linecap="round"/>
  <!-- 正常域 (緑) -->
  <path d="M {cx - arc_r} {cy} A {arc_r} {arc_r} 0 0 1 {cx - arc_r * 0.5} {cy - arc_r * 0.866}"
        fill="none" stroke="#C8E6C9" stroke-width="18" stroke-linecap="round"/>
  <!-- 注意域 (黄) -->
  <path d="M {cx - arc_r * 0.5} {cy - arc_r * 0.866} A {arc_r} {arc_r} 0 0 1 {cx + arc_r * 0.5} {cy - arc_r * 0.866}"
        fill="none" stroke="#FFF9C4" stroke-width="18" stroke-linecap="round"/>
  <!-- 危険域 (赤) -->
  <path d="M {cx + arc_r * 0.5} {cy - arc_r * 0.866} A {arc_r} {arc_r} 0 0 1 {cx + arc_r} {cy}"
        fill="none" stroke="#FFCDD2" stroke-width="18" stroke-linecap="round"/>
  <!-- 針（三角ポインタ） -->
  <polygon points="{needle_tip_x},{needle_tip_y} {base_x1},{base_y1} {base_x2},{base_y2}"
           fill="{color}" stroke="{color}" stroke-width="1"/>
  <!-- 中心円 -->
  <circle cx="{cx}" cy="{cy}" r="7" fill="{color}"/>
  <circle cx="{cx}" cy="{cy}" r="3" fill="white"/>
  <!-- 値表示 -->
  <text x="{cx}" y="{cy - 15}" text-anchor="middle"
        font-size="28" font-weight="bold" fill="{color}">{current_value:.1f}</text>
  <text x="{cx}" y="{cy + 2}" text-anchor="middle"
        font-size="12" fill="#666">{unit}</text>
  <!-- ラベル -->
  <text x="{cx}" y="{height - 5}" text-anchor="middle"
        font-size="11" fill="#999">{label}</text>
  <!-- ステータス -->
  <rect x="{cx - 25}" y="{cy - radius * 0.5 - 8}" width="50" height="18" rx="9"
        fill="{color}" opacity="0.15"/>
  <text x="{cx}" y="{cy - radius * 0.5 + 5}" text-anchor="middle"
        font-size="11" font-weight="bold" fill="{color}">{status}</text>
  <!-- 範囲ラベル -->
  <text x="{cx - arc_r - 5}" y="{cy + 15}" text-anchor="end"
        font-size="10" fill="#999">{normal_value:.1f}</text>
  <text x="{cx + arc_r + 5}" y="{cy + 15}" text-anchor="start"
        font-size="10" fill="#999">{failure_value:.1f}</text>
</svg>"""
    return svg


def _render_timeline_svg(
    current_level: int,
    progress_pct: float,
    stages_info: list,
    width: int = 700,
    height: int = 90,
) -> str:
    """ステージ遷移タイムラインをSVGで描画"""
    margin = 40
    bar_width = width - margin * 2
    bar_y = 35
    bar_height = 12

    svg_parts = [
        f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">',
        f'<rect x="{margin}" y="{bar_y}" width="{bar_width}" height="{bar_height}" '
        f'rx="6" fill="#E0E0E0"/>',
    ]

    # プログレスバー
    fill_width = bar_width * (progress_pct / 100.0)
    if current_level <= 2:
        bar_color = "#FFC107"
    elif current_level <= 3:
        bar_color = "#FF9800"
    elif current_level <= 4:
        bar_color = "#FF5722"
    else:
        bar_color = "#D32F2F"

    svg_parts.append(
        f'<rect x="{margin}" y="{bar_y}" width="{fill_width}" height="{bar_height}" '
        f'rx="6" fill="{bar_color}"/>'
    )

    # ステージマーカー
    num_stages = len(stages_info)
    for i, stage in enumerate(stages_info):
        x = margin + (bar_width / num_stages) * (i + 0.5)
        is_active = (i + 1) == current_level
        is_past = (i + 1) < current_level

        if is_active:
            circle_fill = bar_color
            circle_r = 10
            stroke = f'stroke="{bar_color}" stroke-width="3"'
            text_weight = "bold"
            text_color = bar_color
        elif is_past:
            circle_fill = "#4CAF50"
            circle_r = 8
            stroke = 'stroke="none"'
            text_weight = "normal"
            text_color = "#4CAF50"
        else:
            circle_fill = "#BDBDBD"
            circle_r = 7
            stroke = 'stroke="none"'
            text_weight = "normal"
            text_color = "#999"

        svg_parts.append(
            f'<circle cx="{x}" cy="{bar_y + bar_height / 2}" r="{circle_r}" '
            f'fill="{"white" if is_active else circle_fill}" {stroke}/>'
        )
        if is_active:
            svg_parts.append(
                f'<circle cx="{x}" cy="{bar_y + bar_height / 2}" r="5" fill="{bar_color}"/>'
            )

        # ステージ番号
        svg_parts.append(
            f'<text x="{x}" y="{bar_y - 8}" text-anchor="middle" '
            f'font-size="11" font-weight="{text_weight}" fill="{text_color}">L{i + 1}</text>'
        )
        # ステージラベル
        svg_parts.append(
            f'<text x="{x}" y="{bar_y + bar_height + 18}" text-anchor="middle" '
            f'font-size="10" fill="{text_color}">{stage["label"]}</text>'
        )

    # 進行率テキスト
    svg_parts.append(
        f'<text x="{width - 10}" y="{bar_y + bar_height + 18}" text-anchor="end" '
        f'font-size="11" font-weight="bold" fill="{bar_color}">{progress_pct:.0f}%</text>'
    )

    svg_parts.append('</svg>')
    return '\n'.join(svg_parts)


def _render_degradation_chart_svg(
    metric_history: list,
    normal_value: float,
    failure_value: float,
    metric_name: str,
    metric_unit: str,
    total_duration: float,
    width: int = 700,
    height: int = 250,
    *,
    realtime_history: Optional[List[Tuple[float, float]]] = None,
    realtime_x_start: float = 0.0,
    realtime_x_end: float = 0.0,
    scenario_key: str = "",
    start_level: int = 1,
    sim_start_dt: Optional[datetime] = None,
) -> str:
    """劣化曲線チャートをSVGで描画。

    realtime_history が指定された場合、X軸を実時間（日時）で描画する。
    X軸の範囲は realtime_x_start ～ realtime_x_end (時間) に限定される。
    """
    # 実時間モードかどうか
    x_range_hours = realtime_x_end - realtime_x_start
    use_realtime = realtime_history is not None and x_range_hours > 0
    history = realtime_history if use_realtime else metric_history

    # 実時間モードでは表示範囲に応じてチャート幅を調整
    if use_realtime and x_range_hours >= 48:
        width = max(700, min(1400, int(x_range_hours / 24 * 120)))

    margin_left = 60
    margin_right = 30
    margin_top = 25
    margin_bottom = 50 if use_realtime else 35
    chart_w = width - margin_left - margin_right
    chart_h = height - margin_top - margin_bottom

    # Y軸レンジ: normal_value ～ failure_value を基準に 8% パディング
    base_min = min(normal_value, failure_value)
    base_max = max(normal_value, failure_value)
    base_range = base_max - base_min if abs(base_max - base_min) > 0.001 else 1.0
    padding = base_range * 0.08
    y_min = base_min - padding
    y_max = base_max + padding
    y_range = y_max - y_min

    if use_realtime:
        # X軸: realtime_x_start ～ realtime_x_end の範囲にマッピング
        def to_svg_x(t):
            return margin_left + ((t - realtime_x_start) / max(x_range_hours, 0.1)) * chart_w
    else:
        def to_svg_x(t):
            return margin_left + (t / max(total_duration, 0.1)) * chart_w

    def to_svg_y(v):
        return margin_top + chart_h - ((v - y_min) / y_range) * chart_h

    svg_parts = [
        f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#FAFAFA" rx="4"/>',
    ]

    # グリッド (Y軸)
    for i in range(5):
        gy = margin_top + (chart_h / 4) * i
        gv = y_max - (y_range / 4) * i
        svg_parts.append(
            f'<line x1="{margin_left}" y1="{gy}" x2="{width - margin_right}" y2="{gy}" '
            f'stroke="#E0E0E0" stroke-width="1" stroke-dasharray="4,4"/>'
        )
        svg_parts.append(
            f'<text x="{margin_left - 5}" y="{gy + 4}" text-anchor="end" '
            f'font-size="10" fill="#999">{gv:.1f}</text>'
        )

    # --- X軸: 実時間モードではレベル到達日時を目盛りに表示 ---
    if use_realtime and sim_start_dt:
        base_ttf = SCENARIO_BASE_TTF_HOURS.get(scenario_key, 336)
        tick_levels = list(range(start_level, 6))
        for lvl in tick_levels:
            decay = _DETERMINISTIC_DECAY.get(lvl, 0.50)
            real_h = base_ttf * (1.0 - decay)
            # 表示範囲外はスキップ
            if real_h < realtime_x_start - 0.01:
                continue
            sx = to_svg_x(real_h)
            tick_dt = sim_start_dt + timedelta(hours=real_h)
            label = f"L{lvl}"
            # 表示範囲が短い場合（< 48h）は時刻も表示
            if x_range_hours < 48:
                dt_str = tick_dt.strftime("%-m/%-d %H:%M")
            else:
                dt_str = tick_dt.strftime("%-m/%-d")
            svg_parts.append(
                f'<line x1="{sx}" y1="{margin_top}" x2="{sx}" y2="{margin_top + chart_h}" '
                f'stroke="#E0E0E0" stroke-width="1" stroke-dasharray="3,3"/>'
            )
            svg_parts.append(
                f'<text x="{sx}" y="{margin_top + chart_h + 13}" text-anchor="middle" '
                f'font-size="9" font-weight="bold" fill="#666">{label}</text>'
            )
            svg_parts.append(
                f'<text x="{sx}" y="{margin_top + chart_h + 25}" text-anchor="middle" '
                f'font-size="9" fill="#999">{dt_str}</text>'
            )
        # 障害発生線 (X軸右端)
        fx = to_svg_x(base_ttf)
        fail_dt = sim_start_dt + timedelta(hours=base_ttf)
        if x_range_hours < 48:
            fail_dt_str = fail_dt.strftime("%-m/%-d %H:%M")
        else:
            fail_dt_str = fail_dt.strftime("%-m/%-d %H:%M")
        svg_parts.append(
            f'<line x1="{fx}" y1="{margin_top}" x2="{fx}" y2="{margin_top + chart_h}" '
            f'stroke="#D32F2F" stroke-width="1.5" stroke-dasharray="4,2"/>'
        )
        svg_parts.append(
            f'<text x="{fx}" y="{margin_top + chart_h + 13}" text-anchor="middle" '
            f'font-size="9" font-weight="bold" fill="#D32F2F">障害</text>'
        )
        svg_parts.append(
            f'<text x="{fx}" y="{margin_top + chart_h + 25}" text-anchor="middle" '
            f'font-size="9" fill="#D32F2F">{fail_dt_str}</text>'
        )
        # 現在時刻マーカー
        if history:
            now_h = history[-1][0]
            now_sx = to_svg_x(now_h)
            svg_parts.append(
                f'<line x1="{now_sx}" y1="{margin_top}" x2="{now_sx}" y2="{margin_top + chart_h}" '
                f'stroke="#1565C0" stroke-width="1" stroke-dasharray="2,2"/>'
            )

    # 正常ライン
    ny = to_svg_y(normal_value)
    svg_parts.append(
        f'<line x1="{margin_left}" y1="{ny}" x2="{width - margin_right}" y2="{ny}" '
        f'stroke="#4CAF50" stroke-width="1.5" stroke-dasharray="6,3"/>'
    )
    svg_parts.append(
        f'<text x="{width - margin_right + 2}" y="{ny + 3}" font-size="9" fill="#4CAF50">正常</text>'
    )

    # 障害ライン (Y)
    fy = to_svg_y(failure_value)
    svg_parts.append(
        f'<line x1="{margin_left}" y1="{fy}" x2="{width - margin_right}" y2="{fy}" '
        f'stroke="#D32F2F" stroke-width="1.5" stroke-dasharray="6,3"/>'
    )
    svg_parts.append(
        f'<text x="{width - margin_right + 2}" y="{fy + 3}" font-size="9" fill="#D32F2F">障害</text>'
    )

    # 危険域の塗りつぶし（障害値付近の15%帯）
    danger_band = abs(failure_value - normal_value) * 0.15
    if failure_value > normal_value:
        danger_y1 = to_svg_y(failure_value)
        danger_y2 = to_svg_y(failure_value - danger_band)
    else:
        danger_y1 = to_svg_y(failure_value + danger_band)
        danger_y2 = to_svg_y(failure_value)
    svg_parts.append(
        f'<rect x="{margin_left}" y="{min(danger_y1, danger_y2)}" '
        f'width="{chart_w}" height="{abs(danger_y2 - danger_y1)}" '
        f'fill="#FFCDD2" opacity="0.3"/>'
    )

    # データポイント + ライン
    if len(history) > 1:
        points_line = []
        for t, v in history:
            sx = to_svg_x(t)
            sy = to_svg_y(v)
            points_line.append(f"{sx},{sy}")

        svg_parts.append(
            f'<polyline points="{" ".join(points_line)}" '
            f'fill="none" stroke="#1565C0" stroke-width="2.5" stroke-linejoin="round"/>'
        )

        for i, (t, v) in enumerate(history):
            sx = to_svg_x(t)
            sy = to_svg_y(v)
            r = 5 if i == len(history) - 1 else 3
            color = "#D32F2F" if i == len(history) - 1 else "#1565C0"
            svg_parts.append(
                f'<circle cx="{sx}" cy="{sy}" r="{r}" fill="{color}" '
                f'stroke="white" stroke-width="1.5"/>'
            )

        # 最新値のラベル
        if history:
            last_t, last_v = history[-1]
            lx = to_svg_x(last_t)
            ly = to_svg_y(last_v)
            svg_parts.append(
                f'<text x="{lx + 8}" y="{ly - 8}" font-size="12" '
                f'font-weight="bold" fill="#D32F2F">{last_v:.1f} {metric_unit}</text>'
            )

    # X軸ラベル
    if use_realtime:
        svg_parts.append(
            f'<text x="{width / 2}" y="{height - 3}" text-anchor="middle" '
            f'font-size="10" fill="#999">予測タイムライン（日付）</text>'
        )
    else:
        svg_parts.append(
            f'<text x="{width / 2}" y="{height - 5}" text-anchor="middle" '
            f'font-size="10" fill="#999">経過時間 (秒)</text>'
        )
    # Y軸ラベル
    svg_parts.append(
        f'<text x="12" y="{height / 2}" text-anchor="middle" '
        f'font-size="10" fill="#999" transform="rotate(-90, 12, {height / 2})">'
        f'{metric_name} ({metric_unit})</text>'
    )

    svg_parts.append('</svg>')
    return '\n'.join(svg_parts)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# メイン描画関数
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def render_stream_controls(target_device: str, scenario_key: str, site_id: str):
    """
    サイドバーにストリーム制御UIを描画。

    対象デバイスとシナリオは共通設定から受け取る。
    開始レベルと速度はストリーム固有の設定。
    """
    from ui.shared_sim_config import scenario_key_to_display

    sim = _get_simulator()
    is_running = sim is not None and sim.is_started and not sim.is_complete

    with st.expander("📡 連続劣化ストリーム", expanded=True):
        st.caption(
            "時間経過に伴う段階的な劣化進行をシミュレートします。"
            "RULトレンド予測とGNN学習データの蓄積に活用されます。"
        )

        if is_running:
            start_lvl = getattr(sim, 'start_level', 1)
            st.warning(
                f"🔄 ストリーム実行中: {sim.sequence.pattern} on {sim.device_id}"
                f" (開始L{start_lvl})"
            )
            col_stop, col_info = st.columns([1, 2])
            with col_stop:
                if st.button("⏹ 停止", key="stream_stop", type="primary"):
                    _clear_simulator()
                    st.rerun()
            with col_info:
                elapsed = sim.current_elapsed_sec
                st.caption(f"経過: {elapsed:.0f}s / {sim.total_duration_sec:.0f}s")
            return

        # --- 共通設定の参照表示 ---
        scenario_display = scenario_key_to_display(scenario_key)
        st.info(f"🎯 **{target_device}** | {scenario_display}")

        # --- 開始レベルスライダー（連続劣化ストリーム固有） ---
        _LEVEL_OPTIONS = [1, 2, 3, 4, 5]
        _LEVEL_LABELS = {
            1: "L1: 初期劣化",
            2: "L2: 劣化進行",
            3: "L3: 警戒域",
            4: "L4: 危険域",
            5: "L5: 障害直前",
        }
        start_level = st.select_slider(
            "開始レベル",
            options=_LEVEL_OPTIONS,
            value=1,
            format_func=lambda x: _LEVEL_LABELS.get(x, f"L{x}"),
            help="どのレベルからストリームを開始するかを指定します。"
                 "予兆シミュレーションで確認したレベルから開始すると効果的です。",
            key="stream_start_level",
        )

        speed = st.select_slider(
            "速度",
            options=[0.5, 1.0, 2.0, 3.0, 5.0],
            value=2.0,
            format_func=lambda x: f"{x}x",
            key="stream_speed",
            help="シミュレーション速度。2x = 実時間の2倍速"
        )

        # プレビュー情報
        seq = DEGRADATION_SEQUENCES[scenario_key]
        active_stages = [s for s in seq.stages if s.level >= start_level]
        total_sec = sum(s.duration_sec / speed for s in active_stages)
        st.info(
            f"📊 **{seq.metric_name}**: {seq.normal_value} → {seq.failure_value} {seq.metric_unit}  \n"
            f"⏱ L{start_level}→L5: **{total_sec:.0f}秒**（{len(active_stages)}ステージ）"
        )

        if st.button("▶ ストリーム開始", key="stream_start", type="primary"):
            interfaces = get_default_interfaces(target_device, scenario_key)
            sim = AlarmStreamSimulator(
                scenario_key=scenario_key,
                device_id=target_device,
                interfaces=interfaces,
                speed_multiplier=speed,
                start_level=start_level,
            )
            sim.start()
            _save_simulator(sim)
            # 既存のワンショットシミュレーションをクリア
            st.session_state["injected_weak_signal"] = None
            st.session_state.pop("dt_prediction_cache", None)
            st.rerun()


def render_stream_dashboard():
    """
    メインエリアに連続劣化ダッシュボードを描画。

    誰でも状況を判断できる視覚的UIを提供:
      1. ステージタイムライン（横方向プログレス）
      2. メトリクスゲージ（半円ゲージ）
      3. 劣化曲線チャート（時系列SVG）
      4. イベントログ（色分けされた履歴）
    """
    sim = _get_simulator()
    if sim is None or not sim.is_started:
        return False  # ストリーム非実行

    seq = sim.sequence
    events = sim.get_all_events_until_now()
    current_level = sim.get_current_level()
    progress = sim.current_progress_pct
    is_complete = sim.is_complete

    # ── ヘッダー ──
    start_lvl = getattr(sim, 'start_level', 1)
    status_color = "#D32F2F" if current_level >= 4 else "#FF9800" if current_level >= 2 else "#4CAF50"
    status_text = "完了" if is_complete else f"Level {current_level}/5"
    status_icon = "✅" if is_complete else "🔴" if current_level >= 4 else "🟠" if current_level >= 2 else "🟢"
    start_info = f" (開始L{start_lvl})" if start_lvl > 1 else ""

    _st_html(
        f"<h3 style='margin:0 0 8px 0;'>📡 連続劣化モニタリング</h3>"
        f"<span style='background:{status_color};color:white;padding:2px 10px;"
        f"border-radius:10px;font-size:13px;'>"
        f"{status_icon} {status_text}</span>"
        f"<span style='color:#666;font-size:13px;margin-left:12px;'>"
        f"{seq.pattern.upper()} | {sim.device_id}{start_info}</span>"
    )

    with st.container(border=True):
        # ── 1. ステージタイムライン（アクティブステージのみ表示）──
        active_stages = [s for s in seq.stages if s.level >= start_lvl]
        stages_info = [{"label": s.label} for s in active_stages]
        # current_level を active_stages 内での相対位置に変換
        relative_level = max(0, current_level - start_lvl + 1) if current_level >= start_lvl else 0
        timeline_svg = _render_timeline_svg(relative_level, progress, stages_info)
        _st_html(timeline_svg, height=100)

        st.markdown("---")

        # ── 2. メトリクスゲージ + KPI ──
        col_gauge, col_kpi1, col_kpi2, col_kpi3 = st.columns([2, 1, 1, 1])

        current_metric = events[-1].metric_value if events else seq.normal_value
        with col_gauge:
            gauge_svg = _render_metric_gauge_svg(
                current_value=current_metric,
                normal_value=seq.normal_value,
                failure_value=seq.failure_value,
                unit=seq.metric_unit,
                label=seq.metric_name,
            )
            _st_html(gauge_svg, height=190)

        with col_kpi1:
            st.metric(
                "現在レベル",
                f"{current_level}/5",
                delta=f"+{current_level - (events[-2].level if len(events) >= 2 else 0)}" if len(events) >= 2 and events[-1].level != events[-2].level else None,
                delta_color="inverse",
            )
            st.metric("イベント数", f"{len(events)}")

        with col_kpi2:
            # 実時間ベースの予測情報を表示
            _base_ttf = SCENARIO_BASE_TTF_HOURS.get(seq.pattern, 336)
            _decay = _DETERMINISTIC_DECAY.get(current_level, 1.0)
            _rul_hours = max(1, int(_base_ttf * _decay))
            if _rul_hours >= 24:
                st.metric("障害予測", f"{_rul_hours // 24}日後")
            else:
                st.metric("障害予測", f"{_rul_hours}時間後")
            elapsed = sim.current_elapsed_sec
            remaining = max(0, sim.total_duration_sec - elapsed)
            st.metric("シミュ残", f"{remaining:.0f}s")

        with col_kpi3:
            severity = events[-1].severity if events else "NORMAL"
            severity_display = "🔴 CRITICAL" if severity == "CRITICAL" else "🟡 WARNING" if severity == "WARNING" else "🟢 NORMAL"
            st.metric("重要度", severity_display)
            latest_stage = events[-1].stage_label if events else "-"
            st.metric("ステージ", latest_stage)

        st.markdown("---")

        # ── 3. 劣化曲線チャート（実時間軸） ──
        metric_history = sim.get_metric_history()
        realtime_history, rt_x_start, rt_x_end = sim.get_realtime_metric_history()
        _sim_start_dt = datetime.fromtimestamp(sim._start_time) if sim._start_time else datetime.now()
        chart_svg = _render_degradation_chart_svg(
            metric_history=metric_history,
            normal_value=seq.normal_value,
            failure_value=seq.failure_value,
            metric_name=seq.metric_name,
            metric_unit=seq.metric_unit,
            total_duration=sim.total_duration_sec,
            realtime_history=realtime_history,
            realtime_x_start=rt_x_start,
            realtime_x_end=rt_x_end,
            scenario_key=seq.pattern,
            start_level=start_lvl,
            sim_start_dt=_sim_start_dt,
        )
        # 横スクロール対応ラッパー
        import streamlit.components.v1 as _components
        _scroll_html = (
            f'<div style="overflow-x:auto;overflow-y:hidden;'
            f'border:1px solid #eee;border-radius:4px;padding:4px;">'
            f'{chart_svg}</div>'
        )
        _components.html(_scroll_html, height=280, scrolling=True)

        st.markdown("---")

        # ── 4. イベントログ ──
        st.markdown("**📋 アラームイベントログ**")
        if events:
            # 最新5件を表示（新しい順）
            for ev in reversed(events[-5:]):
                border_color = ev.color
                severity_badge = (
                    f"<span style='background:#D32F2F;color:white;padding:1px 6px;"
                    f"border-radius:3px;font-size:10px;'>CRITICAL</span>"
                    if ev.severity == "CRITICAL"
                    else f"<span style='background:#FF9800;color:white;padding:1px 6px;"
                    f"border-radius:3px;font-size:10px;'>WARNING</span>"
                )
                time_display = f"{ev.elapsed_sec:.1f}s"
                msg_display = ev.messages[0][:100] + ("..." if len(ev.messages[0]) > 100 else "")

                extra_line = ""
                if len(ev.messages) > 1:
                    extra_count = len(ev.messages) - 1
                    extra_line = f"<br><span style='color:#999;font-size:10px;'>+ {extra_count} more alerts</span>"

                _st_html(
                    f"<div style='border-left:3px solid {border_color};padding:4px 8px;"
                    f"margin:3px 0;font-size:12px;background:#FAFAFA;border-radius:2px;'>"
                    f"<span style='color:#999;'>[{time_display}]</span> "
                    f"{severity_badge} "
                    f"<span style='color:#333;'>L{ev.level}</span> "
                    f"<code style='font-size:11px;'>{msg_display}</code>"
                    f"{extra_line}"
                    f"</div>"
                )

            if len(events) > 5:
                st.caption(f"...他 {len(events) - 5} 件のイベント")
        else:
            st.caption("イベント待機中...")

    # ── 自動リフレッシュ ──
    if not is_complete:
        # ストリーム実行中は2秒ごとに更新
        # Streamlitの自動リフレッシュ (st.rerun) のため、
        # 呼び出し元で time.sleep + st.rerun を実行
        return True  # "需要リフレッシュ"

    # 完了時: DB同期（ChromaDB + GNN学習データエクスポート）
    _completion_key = "stream_completion_result"
    if _completion_key not in st.session_state:
        _sync_result = _run_completion_sync(sim)
        st.session_state[_completion_key] = _sync_result
    else:
        _sync_result = st.session_state[_completion_key]

    # 結果表示
    _chromadb_n = _sync_result.get("chromadb_added", 0)
    _gnn_path = _sync_result.get("gnn_session_path")
    _sync_errors = _sync_result.get("errors", [])

    _summary_parts = ["forecast_ledgerに記録済み"]
    if _chromadb_n > 0:
        _summary_parts.append(f"ChromaDB +{_chromadb_n}件")
    if _gnn_path:
        _summary_parts.append("GNN学習データ保存済み")

    st.success(f"✅ 劣化シミュレーション完了。{' / '.join(_summary_parts)}")

    if _sync_errors:
        st.caption(f"⚠ 一部エラー: {', '.join(_sync_errors)}")

    col_end, col_spacer = st.columns([1, 3])
    with col_end:
        if st.button("🏁 試験終了", key="stream_end", type="primary"):
            st.session_state.pop(_completion_key, None)
            _clear_simulator()
            st.rerun()

    return False


def _run_completion_sync(sim: AlarmStreamSimulator) -> dict:
    """ストリーム完了時のDB同期を実行"""
    try:
        from digital_twin_pkg.stream_completion_handler import handle_stream_completion
        from registry import load_topology

        # cockpit.py の @st.cache_resource と同じエンジンを再利用
        engine = _get_shared_dt_engine()
        topology = None
        active_site = st.session_state.get("active_site")
        if active_site:
            try:
                topology = load_topology(active_site)
            except Exception:
                pass

        return handle_stream_completion(
            sim=sim,
            engine=engine,
            topology=topology,
        )
    except Exception as e:
        logger.warning("Stream completion sync failed: %s", e)
        return {"chromadb_added": 0, "gnn_session_path": None, "errors": [str(e)]}


def _get_shared_dt_engine():
    """共通キャッシュ (engine_cache) 経由で DigitalTwinEngine を取得する。"""
    try:
        from ui.engine_cache import get_dt_engine_for_site
        return get_dt_engine_for_site()
    except Exception as e:
        logger.warning("Failed to get shared DT engine: %s", e)
        return None


def inject_stream_alarms_to_session(sim: AlarmStreamSimulator):
    """
    ストリームの最新アラームを session_state["injected_weak_signal"] に注入。
    cockpit.py が既存のフローで処理できるようにする。
    """
    if sim is None or not sim.is_started:
        return

    current_level = sim.get_current_level()
    if current_level == 0:
        return

    latest_msgs = sim.get_latest_messages()
    if not latest_msgs:
        return

    scenario_display = get_available_scenarios().get(sim.sequence.pattern, sim.sequence.pattern)

    st.session_state["injected_weak_signal"] = {
        "device_id": sim.device_id,
        "messages": latest_msgs,
        "message": latest_msgs[0],
        "level": current_level,
        "scenario": scenario_display,
        "source": "stream",  # ストリーム由来であることを示す
    }
