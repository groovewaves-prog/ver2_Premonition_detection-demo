# ui/stream/kpi_panel.py — ストリームダッシュボードのKPIパネル
#
# 6つのKPIカードをグリッド表示:
#   現在レベル / 障害予測 / 重要度 / イベント数 / シミュ残 / ステージ

from digital_twin_pkg.alarm_stream import (
    SCENARIO_BASE_TTF_HOURS,
    _DETERMINISTIC_DECAY,
)


def render_kpi_html(
    current_level: int,
    severity: str,
    elapsed: float,
    remaining: float,
    latest_stage: str,
    event_count: int,
    pattern: str,
) -> str:
    """KPIパネルのHTMLを生成"""
    # RUL計算
    _base_ttf = SCENARIO_BASE_TTF_HOURS.get(pattern, 336)
    _decay = _DETERMINISTIC_DECAY.get(current_level, 1.0)
    _rul_hours = max(1, int(_base_ttf * _decay))
    ttf_display = f"{_rul_hours // 24}日後" if _rul_hours >= 24 else f"{_rul_hours}時間後"

    # レベル色
    if current_level >= 4:
        lvl_bg, lvl_border, lvl_text = "#FDE8E8", "#D32F2F", "#B71C1C"
    elif current_level >= 2:
        lvl_bg, lvl_border, lvl_text = "#FFF3E0", "#FF9800", "#E65100"
    else:
        lvl_bg, lvl_border, lvl_text = "#E8F5E9", "#4CAF50", "#1B5E20"

    # 障害予測色
    if _rul_hours <= 6:
        ttf_bg, ttf_border, ttf_text = "#FDE8E8", "#D32F2F", "#B71C1C"
    elif _rul_hours <= 24:
        ttf_bg, ttf_border, ttf_text = "#FFF3E0", "#FF9800", "#E65100"
    else:
        ttf_bg, ttf_border, ttf_text = "#E3F2FD", "#1976D2", "#0D47A1"

    # 重要度色
    if severity == "CRITICAL":
        sev_bg, sev_border, sev_text = "#FDE8E8", "#D32F2F", "#B71C1C"
        sev_label = "CRITICAL"
    elif severity == "WARNING":
        sev_bg, sev_border, sev_text = "#FFF3E0", "#FF9800", "#E65100"
        sev_label = "WARNING"
    else:
        sev_bg, sev_border, sev_text = "#E8F5E9", "#4CAF50", "#1B5E20"
        sev_label = "NORMAL"

    # ステージ色
    _stage_critical = any(k in latest_stage for k in ["障害直前", "障害", "Critical", "Failure"])
    if _stage_critical:
        stg_bg, stg_border, stg_text = "#FDE8E8", "#D32F2F", "#B71C1C"
    else:
        stg_bg, stg_border, stg_text = "#F3E5F5", "#7B1FA2", "#4A148C"

    # パルスアニメーション判定
    def _pulse_cls(flag: bool) -> str:
        return " kpi-pulse" if flag else ""

    _pulse_level = current_level >= 4
    _pulse_ttf = _rul_hours <= 6
    _pulse_severity = severity == "CRITICAL"
    _pulse_stage = _stage_critical

    return f"""
    <style>
      @keyframes kpiPulse {{
        0%   {{ box-shadow: 0 0 0 0 rgba(211,47,47,0.4); }}
        70%  {{ box-shadow: 0 0 0 8px rgba(211,47,47,0); }}
        100% {{ box-shadow: 0 0 0 0 rgba(211,47,47,0); }}
      }}
      .kpi-grid {{
        display: grid;
        grid-template-columns: repeat(3, 1fr);
        gap: 8px;
        padding: 4px 0;
      }}
      .kpi-card {{
        border-radius: 8px;
        padding: 10px 14px;
        border-left: 4px solid;
        min-height: 62px;
        display: flex;
        flex-direction: column;
        justify-content: center;
        overflow: visible;
      }}
      .kpi-card.kpi-pulse {{
        animation: kpiPulse 1.8s ease-in-out infinite;
      }}
      .kpi-label {{
        font-size: 12px;
        font-weight: 600;
        letter-spacing: 0.5px;
        margin-bottom: 3px;
        line-height: 1.3;
      }}
      .kpi-value {{
        font-size: 20px;
        font-weight: 700;
        line-height: 1.3;
      }}
    </style>
    <div class="kpi-grid">
      <!-- Row 1 -->
      <div class="kpi-card{_pulse_cls(_pulse_level)}"
           style="background:{lvl_bg};border-color:{lvl_border};">
        <div class="kpi-label" style="color:{lvl_text};">現在レベル</div>
        <div class="kpi-value" style="color:{lvl_text};">{current_level}/5</div>
      </div>
      <div class="kpi-card{_pulse_cls(_pulse_ttf)}"
           style="background:{ttf_bg};border-color:{ttf_border};">
        <div class="kpi-label" style="color:{ttf_text};">障害予測</div>
        <div class="kpi-value" style="color:{ttf_text};">{ttf_display}</div>
      </div>
      <div class="kpi-card{_pulse_cls(_pulse_severity)}"
           style="background:{sev_bg};border-color:{sev_border};">
        <div class="kpi-label" style="color:{sev_text};">重要度</div>
        <div class="kpi-value" style="color:{sev_text};">
          <span style="color:{sev_border};font-size:14px;">&#11044;</span> {sev_label}
        </div>
      </div>
      <!-- Row 2 -->
      <div class="kpi-card"
           style="background:#F5F5F5;border-color:#9E9E9E;">
        <div class="kpi-label" style="color:#616161;">イベント数</div>
        <div class="kpi-value" style="color:#424242;">{event_count}</div>
      </div>
      <div class="kpi-card"
           style="background:#F5F5F5;border-color:#9E9E9E;">
        <div class="kpi-label" style="color:#616161;">シミュ残</div>
        <div class="kpi-value" style="color:#424242;">{remaining:.0f}s</div>
      </div>
      <div class="kpi-card{_pulse_cls(_pulse_stage)}"
           style="background:{stg_bg};border-color:{stg_border};">
        <div class="kpi-label" style="color:{stg_text};">ステージ</div>
        <div class="kpi-value" style="color:{stg_text};">{latest_stage}</div>
      </div>
    </div>
    """
