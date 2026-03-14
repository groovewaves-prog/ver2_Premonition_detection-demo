# ui/stream/svg_charts.py — SVGチャート生成
#
# 3つのSVGチャートを生成:
#   - メトリクスゲージ（半円ゲージ）
#   - ステージタイムライン（横方向プログレスバー）
#   - 劣化曲線チャート（時系列グラフ）

import math
from typing import List, Tuple


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
    chart_points: List[Tuple[float, float, int]],
    normal_value: float,
    failure_value: float,
    metric_name: str,
    metric_unit: str,
    total_duration: float,
    width: int = 900,
    height: int = 320,
    *,
    explore_level: int = 0,
) -> str:
    """劣化曲線チャートをSVGで描画（レベル対応版）。

    chart_points: [(elapsed_sec, metric_value, level)] — 各点にレベル情報付き。
      level 0 = 初期点, 1-5 = 劣化レベル, 6 = 障害点。
    explore_level: 0 = 分割なし（全部実線）, 1-5 = そのレベルまで実線、以降点線。
    """
    _LEVEL_LABELS = {1: "初期劣化", 2: "劣化進行", 3: "警戒域", 4: "危険域", 5: "障害直前"}

    margin_left = 60
    margin_right = 30
    margin_top = 25
    margin_bottom = 50
    chart_w = width - margin_left - margin_right
    chart_h = height - margin_top - margin_bottom

    # Y軸レンジ
    base_min = min(normal_value, failure_value)
    base_max = max(normal_value, failure_value)
    base_range = base_max - base_min if abs(base_max - base_min) > 0.001 else 1.0
    padding = base_range * 0.08
    y_min = base_min - padding
    y_max = base_max + padding
    y_range = y_max - y_min

    def to_x(t: float) -> float:
        return margin_left + (t / max(total_duration, 0.1)) * chart_w

    def to_y(v: float) -> float:
        return margin_top + chart_h - ((v - y_min) / y_range) * chart_h

    svg = [
        f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#FAFAFA" rx="4"/>',
    ]

    # ── Y軸グリッド ──
    for i in range(5):
        gy = margin_top + (chart_h / 4) * i
        gv = y_max - (y_range / 4) * i
        svg.append(
            f'<line x1="{margin_left}" y1="{gy}" x2="{width - margin_right}" y2="{gy}" '
            f'stroke="#E0E0E0" stroke-width="1" stroke-dasharray="4,4"/>'
        )
        svg.append(
            f'<text x="{margin_left - 5}" y="{gy + 4}" text-anchor="end" '
            f'font-size="11" fill="#999">{gv:.1f}</text>'
        )

    # ── X軸: レベルマーカー（各レベルの最後の点から導出 = 境界位置） ──
    level_last_t: dict = {}
    for t, v, lvl in chart_points:
        if 1 <= lvl <= 5:
            level_last_t[lvl] = t  # 上書きで最終値が残る
    for lvl in sorted(level_last_t.keys()):
        sx = to_x(level_last_t[lvl])
        is_explore_boundary = (explore_level > 0 and lvl == explore_level)
        line_color = "#D32F2F" if is_explore_boundary else "#BDBDBD"
        line_w = "2" if is_explore_boundary else "1"
        label_color = "#D32F2F" if is_explore_boundary else "#666"
        svg.append(
            f'<line x1="{sx}" y1="{margin_top}" x2="{sx}" y2="{margin_top + chart_h}" '
            f'stroke="{line_color}" stroke-width="{line_w}" stroke-dasharray="4,3"/>'
        )
        svg.append(
            f'<text x="{sx}" y="{margin_top + chart_h + 14}" text-anchor="middle" '
            f'font-size="10" font-weight="bold" fill="{label_color}">L{lvl}</text>'
        )
        svg.append(
            f'<text x="{sx}" y="{margin_top + chart_h + 27}" text-anchor="middle" '
            f'font-size="9" fill="{label_color}">{_LEVEL_LABELS.get(lvl, "")}</text>'
        )

    # ── X軸: 障害マーカー（total_duration） ──
    fx = to_x(total_duration)
    svg.append(
        f'<line x1="{fx}" y1="{margin_top}" x2="{fx}" y2="{margin_top + chart_h}" '
        f'stroke="#D32F2F" stroke-width="2" stroke-dasharray="5,3"/>'
    )
    svg.append(
        f'<text x="{fx}" y="{margin_top + chart_h + 14}" text-anchor="middle" '
        f'font-size="10" font-weight="bold" fill="#D32F2F">障害</text>'
    )

    # ── 正常ライン (Y) ──
    ny = to_y(normal_value)
    svg.append(
        f'<line x1="{margin_left}" y1="{ny}" x2="{width - margin_right}" y2="{ny}" '
        f'stroke="#4CAF50" stroke-width="1.5" stroke-dasharray="6,3"/>'
    )
    svg.append(
        f'<text x="{margin_left + 3}" y="{ny - 4}" text-anchor="start" '
        f'font-size="10" fill="#4CAF50">正常 ({normal_value:.1f})</text>'
    )

    # ── 障害ライン (Y) ──
    fy = to_y(failure_value)
    svg.append(
        f'<line x1="{margin_left}" y1="{fy}" x2="{width - margin_right}" y2="{fy}" '
        f'stroke="#D32F2F" stroke-width="1.5" stroke-dasharray="6,3"/>'
    )
    svg.append(
        f'<text x="{margin_left + 3}" y="{fy - 4}" text-anchor="start" '
        f'font-size="10" fill="#D32F2F">障害 ({failure_value:.1f})</text>'
    )

    # ── 危険域の塗りつぶし ──
    danger_band = abs(failure_value - normal_value) * 0.15
    if failure_value > normal_value:
        dy1 = to_y(failure_value)
        dy2 = to_y(failure_value - danger_band)
    else:
        dy1 = to_y(failure_value + danger_band)
        dy2 = to_y(failure_value)
    svg.append(
        f'<rect x="{margin_left}" y="{min(dy1, dy2)}" '
        f'width="{chart_w}" height="{abs(dy2 - dy1)}" '
        f'fill="#FFCDD2" opacity="0.3"/>'
    )

    # ── データライン + ポイント（レベルベースの実線/点線分割） ──
    if len(chart_points) > 1:
        # 分割判定: explore_level > 0 の場合、level <= explore_level を実線
        # 例: explore_level=3 → level 0,1,2,3 = 実線、level 4,5,6 = 点線
        splitting = explore_level > 0

        # セグメントごとに実線/点線を描画
        # 連続する同種（solid/dashed）の点をグループ化してpolyline描画
        solid_pts: List[str] = []
        dashed_pts: List[str] = []

        for i, (t, v, lvl) in enumerate(chart_points):
            sx = to_x(t)
            sy = to_y(v)
            pt = f"{sx},{sy}"

            if not splitting or lvl <= explore_level:
                # 実線側: dashed→solidの切替時に接続点を追加
                if dashed_pts and not solid_pts:
                    solid_pts.append(dashed_pts[-1])
                solid_pts.append(pt)
            else:
                # 点線側: solid→dashedの切替時に接続点を追加
                if solid_pts and not dashed_pts:
                    dashed_pts.append(solid_pts[-1])
                dashed_pts.append(pt)

        if solid_pts:
            svg.append(
                f'<polyline points="{" ".join(solid_pts)}" '
                f'fill="none" stroke="#1565C0" stroke-width="2.5" stroke-linejoin="round"/>'
            )
        if dashed_pts:
            svg.append(
                f'<polyline points="{" ".join(dashed_pts)}" '
                f'fill="none" stroke="#90CAF9" stroke-width="2" '
                f'stroke-dasharray="6,4" stroke-linejoin="round"/>'
            )

        # ── ドット描画 ──
        for i, (t, v, lvl) in enumerate(chart_points):
            sx = to_x(t)
            sy = to_y(v)
            is_future = splitting and lvl > explore_level
            is_last = (i == len(chart_points) - 1)
            r = 5 if is_last else 3

            if is_future:
                color = "#90CAF9"
            elif is_last:
                color = "#D32F2F"
            else:
                color = "#1565C0"
            svg.append(
                f'<circle cx="{sx}" cy="{sy}" r="{r}" fill="{color}" '
                f'stroke="white" stroke-width="1.5"/>'
            )

        # ── 最終値ラベル ──
        last_t, last_v, _ = chart_points[-1]
        lx = to_x(last_t)
        ly = to_y(last_v)
        near_right = lx > width - margin_right - 80
        if near_right:
            svg.append(
                f'<text x="{lx - 10}" y="{ly - 16}" text-anchor="end" font-size="13" '
                f'font-weight="bold" fill="#D32F2F">{last_v:.1f} {metric_unit}</text>'
            )
        else:
            svg.append(
                f'<text x="{lx + 8}" y="{ly - 8}" font-size="13" '
                f'font-weight="bold" fill="#D32F2F">{last_v:.1f} {metric_unit}</text>'
            )

    # ── 軸ラベル ──
    svg.append(
        f'<text x="{width / 2}" y="{height - 5}" text-anchor="middle" '
        f'font-size="11" fill="#999">経過時間 (秒)</text>'
    )
    svg.append(
        f'<text x="12" y="{height / 2}" text-anchor="middle" '
        f'font-size="11" fill="#999" transform="rotate(-90, 12, {height / 2})">'
        f'{metric_name} ({metric_unit})</text>'
    )

    svg.append('</svg>')
    return '\n'.join(svg)
