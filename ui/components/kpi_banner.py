# ui/components/kpi_banner.py — KPIメトリクス + ステータスバナー
import streamlit as st
import streamlit.components.v1 as components
from typing import List


def render_kpi_banner(
    analysis_results: List[dict],
    alarms: list,
    root_cause_candidates: List[dict],
    symptom_devices: List[dict],
    unrelated_devices: List[dict],
):
    """KPI メトリクスとステータスバナーを描画。返り値: (prediction_count, noise_reduction)"""
    total_alarms = len(alarms)
    root_cause_count = len([c for c in root_cause_candidates if c.get('id') != 'SYSTEM'])
    symptom_count = len(symptom_devices)
    unrelated_count = len(unrelated_devices)
    prediction_results = [r for r in analysis_results if r.get('is_prediction')]
    prediction_count = len(prediction_results)
    noise_reduction = ((total_alarms - root_cause_count) / total_alarms * 100) if total_alarms > 0 else 0.0

    # --- ステータス色・テキスト決定 ---
    _has_critical_status = any(
        r.get('status') in ('RED', 'CRITICAL')
        for r in analysis_results if not r.get('is_prediction')
    )
    _has_root_cause = any(
        r.get('classification') == 'root_cause'
        for r in analysis_results if not r.get('is_prediction')
    )
    if _has_critical_status or _has_root_cause:
        _banner_color = "#D32F2F"
        _banner_bg = "#FFEBEE"
        _banner_icon = "&#9888;"
        _banner_text = "インシデント検知"
        _banner_sub = "根本原因を特定しました。対処を推奨します。"
    elif prediction_count > 0:
        _banner_color = "#E65100"
        _banner_bg = "#FFF3E0"
        _banner_icon = "&#128302;"
        _banner_text = "予兆検知"
        _banner_sub = "将来の障害リスクをAIが検出しました。"
    elif total_alarms > 0:
        _banner_color = "#F9A825"
        _banner_bg = "#FFFDE7"
        _banner_icon = "&#9888;"
        _banner_text = "警告あり"
        _banner_sub = "アラートがありますが、重大な障害は検知されていません。"
    else:
        _banner_color = "#2E7D32"
        _banner_bg = "#E8F5E9"
        _banner_icon = "&#10003;"
        _banner_text = "正常稼働"
        _banner_sub = "アラートは検知されていません。"

    # --- 分類サマリー（横棒グラフ風） ---
    _total_classified = root_cause_count + symptom_count + unrelated_count
    _rc_pct = (root_cause_count / _total_classified * 100) if _total_classified > 0 else 0
    _sy_pct = (symptom_count / _total_classified * 100) if _total_classified > 0 else 0
    _ur_pct = (unrelated_count / _total_classified * 100) if _total_classified > 0 else 0

    _bar_html = ""
    if _total_classified > 0:
        _bar_parts = []
        if _rc_pct > 0:
            _bar_parts.append(f'<div style="width:{max(_rc_pct, 8)}%;background:#EF5350;height:100%;border-radius:4px 0 0 4px;" title="Root Cause {root_cause_count}"></div>')
        if _sy_pct > 0:
            _bar_parts.append(f'<div style="width:{max(_sy_pct, 8)}%;background:#FFA726;height:100%;" title="Symptom {symptom_count}"></div>')
        if _ur_pct > 0:
            _bar_parts.append(f'<div style="width:{max(_ur_pct, 8)}%;background:#BDBDBD;height:100%;border-radius:0 4px 4px 0;" title="Unrelated {unrelated_count}"></div>')
        _bar_html = f"""
        <div style="display:flex;height:8px;border-radius:4px;overflow:hidden;background:#eee;margin:8px 0 4px 0;">
            {"".join(_bar_parts)}
        </div>
        <div style="display:flex;gap:16px;font-size:11px;color:#666;">
            <span><span style="display:inline-block;width:8px;height:8px;background:#EF5350;border-radius:2px;margin-right:4px;"></span>Root Cause {root_cause_count}</span>
            <span><span style="display:inline-block;width:8px;height:8px;background:#FFA726;border-radius:2px;margin-right:4px;"></span>Symptom {symptom_count}</span>
            <span><span style="display:inline-block;width:8px;height:8px;background:#BDBDBD;border-radius:2px;margin-right:4px;"></span>Unrelated {unrelated_count}</span>
        </div>"""

    _prediction_chip = ""
    if prediction_count > 0:
        _prediction_chip = (
            f'<div style="display:inline-flex;align-items:center;gap:6px;background:#FFF3E0;'
            f'border:1px solid #FFE0B2;border-radius:16px;padding:4px 12px;font-size:12px;color:#E65100;font-weight:600;">'
            f'&#128302; {prediction_count} Predictions'
            f'</div>'
        )

    _kpi_full_html = f"""
<html><head><style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: transparent; }}
</style></head>
<body>
<div style="border:1px solid #e0e0e0;border-radius:10px;overflow:hidden;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <div style="background:{_banner_bg};border-bottom:2px solid {_banner_color};padding:14px 20px;display:flex;align-items:center;gap:14px;">
    <div style="font-size:28px;color:{_banner_color};line-height:1;">{_banner_icon}</div>
    <div style="flex:1;">
      <div style="font-size:18px;font-weight:700;color:{_banner_color};line-height:1.2;">{_banner_text}</div>
      <div style="font-size:13px;color:#666;margin-top:2px;">{_banner_sub}</div>
    </div>
    {_prediction_chip}
  </div>
  <div style="display:flex;padding:12px 20px;gap:0;background:#fff;">
    <div style="flex:1;text-align:center;border-right:1px solid #eee;">
      <div style="font-size:11px;color:#999;text-transform:uppercase;letter-spacing:0.5px;">Alerts</div>
      <div style="font-size:26px;font-weight:700;color:#333;line-height:1.3;">{total_alarms}</div>
    </div>
    <div style="flex:1;text-align:center;border-right:1px solid #eee;">
      <div style="font-size:11px;color:#999;text-transform:uppercase;letter-spacing:0.5px;">Root Cause</div>
      <div style="font-size:26px;font-weight:700;color:#EF5350;line-height:1.3;">{root_cause_count}</div>
    </div>
    <div style="flex:1;text-align:center;border-right:1px solid #eee;">
      <div style="font-size:11px;color:#999;text-transform:uppercase;letter-spacing:0.5px;">Impact</div>
      <div style="font-size:26px;font-weight:700;color:#FFA726;line-height:1.3;">{symptom_count}</div>
    </div>
    <div style="flex:1;text-align:center;">
      <div style="font-size:11px;color:#999;text-transform:uppercase;letter-spacing:0.5px;">Noise Reduction</div>
      <div style="font-size:26px;font-weight:700;color:#333;line-height:1.3;">{noise_reduction:.0f}%</div>
    </div>
  </div>
  <div style="padding:0 20px 12px 20px;background:#fff;">
    {_bar_html}
  </div>
</div>
</body></html>
"""
    _kpi_height = 200 if _total_classified > 0 else 160
    components.html(_kpi_full_html, height=_kpi_height)

    return prediction_count, noise_reduction
