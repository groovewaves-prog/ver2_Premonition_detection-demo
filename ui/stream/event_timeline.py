# ui/stream/event_timeline.py — アラームイベントのカード型タイムライン
#
# 設計方針:
#   - 左に劣化レベル（L1-L5）の色付きインジケーター
#   - 右にアラートの要約（何が・どこで・どの程度）
#   - レベル遷移を明示的な区切り線で表示
#   - 最新のイベントが上

from typing import List


SEVERITY_COLORS = {
    "CRITICAL": "#D32F2F",
    "WARNING":  "#FF9800",
    "NORMAL":   "#4CAF50",
    "INFO":     "#2196F3",
}

LEVEL_COLORS = {
    1: "#43A047",
    2: "#FDD835",
    3: "#FF9800",
    4: "#E53935",
    5: "#B71C1C",
}


def render_event_timeline(events: List, sim) -> None:
    """アラームイベントをカード型タイムラインで描画する。"""
    import streamlit.components.v1 as _components

    display_events = list(reversed(events[-15:]))

    cards_html = ""
    prev_level = None
    for ev in display_events:
        elapsed = ev.elapsed_sec
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)
        time_str = f"{mins}:{secs:02d}" if mins > 0 else f"{secs}s"

        level = ev.level
        lv_color = LEVEL_COLORS.get(level, "#999")
        sev = ev.severity
        sev_color = SEVERITY_COLORS.get(sev, "#999")

        msg_raw = ev.messages[0] if ev.messages else ""
        msg_short = msg_raw
        if "%" in msg_short:
            msg_short = msg_short.split("%", 1)[-1]
        if len(msg_short) > 65:
            msg_short = msg_short[:62] + "..."

        extra_count = len(ev.messages) - 1 if len(ev.messages) > 1 else 0
        extra_html = f'<span class="extra">+{extra_count}</span>' if extra_count > 0 else ""

        level_divider = ""
        if prev_level is not None and level != prev_level:
            direction = "ESCALATED" if level > prev_level else "De-escalated"
            dir_color = "#D32F2F" if level > prev_level else "#43A047"
            level_divider = (
                f'<div class="level-change">'
                f'<span style="color:{dir_color}">&#9654; {direction}: L{prev_level} &rarr; L{level}</span>'
                f'</div>'
            )
        prev_level = level

        sev_badge = ""
        if sev == "CRITICAL":
            sev_badge = '<span class="sev-badge crit">CRITICAL</span>'
        elif sev == "WARNING":
            sev_badge = '<span class="sev-badge warn">WARNING</span>'

        cards_html += f"""{level_divider}
<div class="ev-card">
  <div class="ev-indicator" style="background:{lv_color};">
    <div class="ev-level">L{level}</div>
  </div>
  <div class="ev-body">
    <div class="ev-header">
      <span class="ev-time">{time_str}</span>
      {sev_badge}
      {extra_html}
    </div>
    <div class="ev-msg">{msg_short}</div>
  </div>
</div>"""

    total_events = len(events)
    hidden = max(0, total_events - 15)
    footer = f'<div class="ev-footer">{total_events} events &mdash; showing latest 15</div>' if hidden > 0 else ""

    html = f"""
<html><head><style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: transparent; padding: 0 2px; }}

  .ev-card {{
    display: flex; align-items: stretch;
    margin: 4px 0; border-radius: 6px;
    background: #fff; border: 1px solid #e8e8e8;
    overflow: hidden; transition: box-shadow 0.15s;
  }}
  .ev-card:hover {{ box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}

  .ev-indicator {{
    width: 44px; min-height: 48px; flex-shrink: 0;
    display: flex; align-items: center; justify-content: center;
  }}
  .ev-level {{
    color: #fff; font-weight: 700; font-size: 13px;
    text-shadow: 0 1px 2px rgba(0,0,0,0.3);
  }}

  .ev-body {{
    flex: 1; padding: 8px 12px; min-width: 0;
  }}
  .ev-header {{
    display: flex; align-items: center; gap: 8px;
    margin-bottom: 3px;
  }}
  .ev-time {{
    font-size: 12px; color: #888; font-weight: 600;
    font-variant-numeric: tabular-nums;
  }}
  .sev-badge {{
    font-size: 10px; font-weight: 700; padding: 1px 6px;
    border-radius: 3px; letter-spacing: 0.5px;
  }}
  .sev-badge.crit {{ background: #D32F2F; color: #fff; }}
  .sev-badge.warn {{ background: #FF9800; color: #fff; }}
  .extra {{
    font-size: 10px; color: #999; background: #f0f0f0;
    padding: 1px 5px; border-radius: 8px;
  }}

  .ev-msg {{
    font-size: 13px; color: #333; line-height: 1.4;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }}

  .level-change {{
    text-align: center; font-size: 11px; font-weight: 600;
    padding: 4px 0; margin: 2px 0;
    border-top: 1px dashed #ddd;
  }}

  .ev-footer {{
    text-align: center; font-size: 11px; color: #aaa;
    padding: 6px 0; border-top: 1px solid #eee; margin-top: 4px;
  }}
</style></head>
<body>
{cards_html}
{footer}
</body></html>
"""
    height = min(52 * len(display_events) + 40, 520)
    _components.html(html, height=height, scrolling=True)
