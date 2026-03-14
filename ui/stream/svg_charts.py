# ui/stream/svg_charts.py — SVGチャート生成
#
# 3つのSVGチャートを生成:
#   - メトリクスゲージ（半円ゲージ）
#   - ステージタイムライン（横方向プログレスバー）
#   - 劣化曲線チャート（時系列グラフ）

import math
from datetime import datetime, timedelta
from typing import Optional, List, Tuple
from digital_twin_pkg.alarm_stream import (
    SCENARIO_BASE_TTF_HOURS,
    _DETERMINISTIC_DECAY,
)


def render_metric_gauge_svg(
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


def render_timeline_svg(
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


def render_degradation_chart_svg(
    metric_history: list,
    normal_value: float,
    failure_value: float,
    metric_name: str,
    metric_unit: str,
    total_duration: float,
    width: int = 900,
    height: int = 320,
    *,
    realtime_history: Optional[List[Tuple[float, float]]] = None,
    realtime_x_start: float = 0.0,
    realtime_x_end: float = 0.0,
    scenario_key: str = "",
    start_level: int = 1,
    sim_start_dt: Optional[datetime] = None,
    explore_level: int = 0,
    level_elapsed_map: Optional[dict] = None,
) -> str:
    """劣化曲線チャートをSVGで描画。

    explore_level > 0 の場合、そのレベルまでを実線、以降を点線で描画する。
    level_elapsed_map: {level: elapsed_sec} — 各レベルの開始時刻（X軸マーカー用）。
    """
    # 実時間モードかどうか
    x_range_hours = realtime_x_end - realtime_x_start
    use_realtime = realtime_history is not None and x_range_hours > 0
    history = realtime_history if use_realtime else metric_history

    margin_left = 60
    margin_right = 80 if use_realtime else 30
    margin_top = 25
    margin_bottom = 50
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
        # 対数スケール: RUL (残り時間) の log で初期を圧縮、後半を拡大
        max_rul = x_range_hours
        log_denom = math.log(max_rul + 1)
        data_chart_w = chart_w * 0.95

        def to_svg_x(t):
            rul = max(realtime_x_end - t, 0)
            pos = 1.0 - math.log(rul + 1) / log_denom
            return margin_left + pos * data_chart_w
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
            f'font-size="11" fill="#999">{gv:.1f}</text>'
        )

    # --- X軸: 実時間モードではレベル到達位置 + 残り日数を表示 ---
    if use_realtime and sim_start_dt:
        base_ttf = SCENARIO_BASE_TTF_HOURS.get(scenario_key, 336)
        tick_levels = list(range(start_level, 6))
        # ティック位置を事前計算し、重なりを防止
        tick_items = []
        for lvl in tick_levels:
            decay = _DETERMINISTIC_DECAY.get(lvl, 0.50)
            real_h = base_ttf * (1.0 - decay)
            if real_h < realtime_x_start - 0.01:
                continue
            sx = to_svg_x(real_h)
            rul_h = max(0, int(base_ttf * decay))
            if rul_h >= 24:
                rul_str = f"{rul_h // 24}日後"
            else:
                rul_str = f"{rul_h}h後"
            tick_items.append((lvl, sx, rul_str))

        # 隣接ティック間が min_gap px 未満の場合、RULラベルを省略
        min_gap = 40
        for idx, (lvl, sx, rul_str) in enumerate(tick_items):
            label = f"L{lvl}"
            svg_parts.append(
                f'<line x1="{sx}" y1="{margin_top}" x2="{sx}" y2="{margin_top + chart_h}" '
                f'stroke="#E0E0E0" stroke-width="1" stroke-dasharray="3,3"/>'
            )
            anchor = "start" if abs(sx - margin_left) < 20 else "middle"
            svg_parts.append(
                f'<text x="{sx}" y="{margin_top + chart_h + 14}" text-anchor="{anchor}" '
                f'font-size="10" font-weight="bold" fill="#666">{label}</text>'
            )
            show_rul = True
            if idx > 0:
                prev_sx = tick_items[idx - 1][1]
                if abs(sx - prev_sx) < min_gap:
                    show_rul = False
            if idx < len(tick_items) - 1:
                next_sx = tick_items[idx + 1][1]
                if abs(next_sx - sx) < min_gap:
                    show_rul = False
            if show_rul:
                svg_parts.append(
                    f'<text x="{sx}" y="{margin_top + chart_h + 27}" text-anchor="{anchor}" '
                    f'font-size="9" fill="#999">({rul_str})</text>'
                )

        # 障害発生線
        fx = to_svg_x(base_ttf)
        fail_dt = sim_start_dt + timedelta(hours=base_ttf)
        fail_dt_str = fail_dt.strftime("%-m/%-d %H:%M")
        svg_parts.append(
            f'<line x1="{fx}" y1="{margin_top}" x2="{fx}" y2="{margin_top + chart_h}" '
            f'stroke="#D32F2F" stroke-width="2" stroke-dasharray="5,3"/>'
        )
        svg_parts.append(
            f'<text x="{fx + 4}" y="{margin_top + chart_h + 14}" text-anchor="start" '
            f'font-size="10" font-weight="bold" fill="#D32F2F">障害</text>'
        )
        svg_parts.append(
            f'<text x="{fx + 4}" y="{margin_top + chart_h + 27}" text-anchor="start" '
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

    # --- X軸: リニアモードではレベル到達位置を表示 ---
    _LEVEL_LABELS = {1: "初期劣化", 2: "劣化進行", 3: "警戒域", 4: "危険域", 5: "障害直前"}
    if not use_realtime and level_elapsed_map:
        for lvl in sorted(level_elapsed_map.keys()):
            t_sec = level_elapsed_map[lvl]
            sx = to_svg_x(t_sec)
            # レベル到達の縦点線
            is_explore = explore_level > 0 and lvl == explore_level
            line_color = "#D32F2F" if is_explore else "#BDBDBD"
            line_width = "2" if is_explore else "1"
            svg_parts.append(
                f'<line x1="{sx}" y1="{margin_top}" x2="{sx}" y2="{margin_top + chart_h}" '
                f'stroke="{line_color}" stroke-width="{line_width}" stroke-dasharray="4,3"/>'
            )
            # ラベル
            label_color = "#D32F2F" if is_explore else "#666"
            svg_parts.append(
                f'<text x="{sx}" y="{margin_top + chart_h + 14}" text-anchor="middle" '
                f'font-size="10" font-weight="bold" fill="{label_color}">L{lvl}</text>'
            )
            svg_parts.append(
                f'<text x="{sx}" y="{margin_top + chart_h + 27}" text-anchor="middle" '
                f'font-size="9" fill="{label_color}">{_LEVEL_LABELS.get(lvl, "")}</text>'
            )

        # 障害発生の赤縦線（最終地点）
        if total_duration > 0:
            fx = to_svg_x(total_duration)
            svg_parts.append(
                f'<line x1="{fx}" y1="{margin_top}" x2="{fx}" y2="{margin_top + chart_h}" '
                f'stroke="#D32F2F" stroke-width="2" stroke-dasharray="5,3"/>'
            )
            svg_parts.append(
                f'<text x="{fx}" y="{margin_top + chart_h + 14}" text-anchor="middle" '
                f'font-size="10" font-weight="bold" fill="#D32F2F">障害</text>'
            )

    # 正常ライン
    ny = to_svg_y(normal_value)
    svg_parts.append(
        f'<line x1="{margin_left}" y1="{ny}" x2="{width - margin_right}" y2="{ny}" '
        f'stroke="#4CAF50" stroke-width="1.5" stroke-dasharray="6,3"/>'
    )
    svg_parts.append(
        f'<text x="{margin_left + 3}" y="{ny - 4}" text-anchor="start" '
        f'font-size="10" fill="#4CAF50">正常 ({normal_value:.1f})</text>'
    )

    # 障害ライン (Y)
    fy = to_svg_y(failure_value)
    svg_parts.append(
        f'<line x1="{margin_left}" y1="{fy}" x2="{width - margin_right}" y2="{fy}" '
        f'stroke="#D32F2F" stroke-width="1.5" stroke-dasharray="6,3"/>'
    )
    svg_parts.append(
        f'<text x="{margin_left + 3}" y="{fy - 4}" text-anchor="start" '
        f'font-size="10" fill="#D32F2F">障害 ({failure_value:.1f})</text>'
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

    # データポイント + ライン（explore_level で実線/点線を分割）
    # explore_level の境界時刻を特定
    _explore_split_t = None
    if explore_level > 0 and level_elapsed_map and not use_realtime:
        # explore_level の次のレベルの開始時刻を分割点にする
        next_lvl = explore_level + 1
        if next_lvl in level_elapsed_map:
            _explore_split_t = level_elapsed_map[next_lvl]
        elif explore_level == 5:
            _explore_split_t = total_duration  # L5 なら全部実線

    if len(history) > 1:
        if _explore_split_t is not None:
            # 実線部分と点線部分に分割
            solid_pts = []
            dashed_pts = []
            for t, v in history:
                sx = to_svg_x(t)
                sy = to_svg_y(v)
                pt = f"{sx},{sy}"
                if t <= _explore_split_t:
                    solid_pts.append(pt)
                else:
                    if not dashed_pts and solid_pts:
                        dashed_pts.append(solid_pts[-1])  # 接続点
                    dashed_pts.append(pt)
            if solid_pts:
                svg_parts.append(
                    f'<polyline points="{" ".join(solid_pts)}" '
                    f'fill="none" stroke="#1565C0" stroke-width="2.5" stroke-linejoin="round"/>'
                )
            if dashed_pts:
                svg_parts.append(
                    f'<polyline points="{" ".join(dashed_pts)}" '
                    f'fill="none" stroke="#90CAF9" stroke-width="2" '
                    f'stroke-dasharray="6,4" stroke-linejoin="round"/>'
                )
        else:
            # 分割なし: 全部実線
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
            is_future = _explore_split_t is not None and t > _explore_split_t
            r = 5 if i == len(history) - 1 else 3
            if is_future:
                color = "#90CAF9"
            elif i == len(history) - 1:
                color = "#D32F2F"
            else:
                color = "#1565C0"
            svg_parts.append(
                f'<circle cx="{sx}" cy="{sy}" r="{r}" fill="{color}" '
                f'stroke="white" stroke-width="1.5"/>'
            )

        # 最新値のラベル
        if history:
            last_t, last_v = history[-1]
            lx = to_svg_x(last_t)
            ly = to_svg_y(last_v)
            near_failure_line = False
            if use_realtime:
                _fail_x = to_svg_x(realtime_x_end)
                near_failure_line = abs(lx - _fail_x) < 100
            near_right_edge = lx > width - margin_right - 80
            if near_failure_line or near_right_edge:
                svg_parts.append(
                    f'<text x="{lx - 10}" y="{ly - 16}" text-anchor="end" font-size="13" '
                    f'font-weight="bold" fill="#D32F2F">{last_v:.1f} {metric_unit}</text>'
                )
            else:
                svg_parts.append(
                    f'<text x="{lx + 8}" y="{ly - 8}" font-size="13" '
                    f'font-weight="bold" fill="#D32F2F">{last_v:.1f} {metric_unit}</text>'
                )

    # X軸ラベル
    if use_realtime:
        svg_parts.append(
            f'<text x="{width / 2}" y="{height - 3}" text-anchor="middle" '
            f'font-size="11" fill="#999">予測タイムライン（対数スケール）</text>'
        )
    else:
        svg_parts.append(
            f'<text x="{width / 2}" y="{height - 5}" text-anchor="middle" '
            f'font-size="11" fill="#999">経過時間 (秒)</text>'
        )
    # Y軸ラベル
    svg_parts.append(
        f'<text x="12" y="{height / 2}" text-anchor="middle" '
        f'font-size="11" fill="#999" transform="rotate(-90, 12, {height / 2})">'
        f'{metric_name} ({metric_unit})</text>'
    )

    svg_parts.append('</svg>')
    return '\n'.join(svg_parts)
