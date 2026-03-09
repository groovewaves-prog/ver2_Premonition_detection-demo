# ui/explanation_panel.py
# Phase 6a: explanation_payload UI コンポーネント
#
# render_explanation_panel(pred_item, dt_engine=None)
#   → AI証拠パネル（異常種別バッジ・LLM narrative・レーダーチャート・ベイズ証拠）

from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional

import streamlit as st


def _st_html(html: str, height: int = 0) -> None:
    """SVG/HTMLをStreamlitで描画する。

    height > 0: st.components.v1.html() で明示的高さ指定（SVG用）。
    height == 0: st.markdown(unsafe_allow_html=True)（通常HTML用）。
    """
    if height > 0:
        import streamlit.components.v1 as components
        components.html(html, height=height, scrolling=False)
    else:
        st.markdown(html, unsafe_allow_html=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 定数
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ANOMALY_TYPE_LABELS = {
    "point":      {"label": "ポイント異常",   "color": "#1565C0", "desc": "単一機器・単一時点の局所的異常"},
    "contextual": {"label": "コンテキスト異常", "color": "#6A1B9A", "desc": "正常範囲内だが文脈上異常"},
    "collective": {"label": "集合的異常",     "color": "#BF360C", "desc": "複数機器で同種シグナルが同時発生"},
    "cascading":  {"label": "カスケード障害",   "color": "#880E4F", "desc": "上流から下流へ連鎖する障害"},
}

_SCORE_LABELS = {
    "semantic":      "ログ内容の深刻度",
    "trend":         "劣化速度",
    "volatility":    "不安定さ",
    "history":       "発生頻度",
    "interaction":   "複合危険度",
    "change_impact": "構成変更影響",  # ★Phase 6c* 統合
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SVG レーダーチャート
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def build_radar_svg(score_breakdown: Dict[str, float], size: int = 440) -> str:
    # ★ 6次元レーダーチャート（拡大版: 440px — ラベル余白確保）
    keys = ["semantic", "trend", "volatility", "history", "interaction", "change_impact"]
    values = [float(score_breakdown.get(k, 0.5)) for k in keys]
    n = len(keys)
    cx, cy = size / 2, size / 2 - 8   # ★ 上部余白確保のため少し上にオフセット
    r = size * 0.28                     # ★ 半径を少し小さくしてラベル余白確保

    def polar(i, val, scale=1.0):
        angle = math.pi / 2 - (2 * math.pi * i / n)
        rv = r * val * scale
        return cx + rv * math.cos(angle), cy - rv * math.sin(angle)

    # 背景グリッド
    grid_lines = ""
    for lv in [0.25, 0.50, 0.75, 1.0]:
        pts = " ".join(f"{polar(i, lv)[0]:.1f},{polar(i, lv)[1]:.1f}" for i in range(n))
        color = "#FFCDD2" if lv == 1.0 else "#ECEFF1"
        grid_lines += f'<polygon points="{pts}" fill="none" stroke="{color}" stroke-width="1"/>'

    # 軸
    axes = "".join(
        f'<line x1="{cx:.1f}" y1="{cy:.1f}" x2="{polar(i,1)[0]:.1f}" y2="{polar(i,1)[1]:.1f}" stroke="#CFD8DC" stroke-width="1"/>'
        for i in range(n)
    )

    # データ多角形
    pts_data = " ".join(f"{polar(i, values[i])[0]:.1f},{polar(i, values[i])[1]:.1f}" for i in range(n))

    # ラベル（★ 位置調整: ラベル距離拡大 + 下部ラベルの y オフセット強化）
    labels = ""
    for i, k in enumerate(keys):
        lx, ly = polar(i, 1.50)         # ★ 1.42 → 1.50 に拡大
        lbl = _SCORE_LABELS.get(k, k)
        val = values[i]
        color = "#B71C1C" if val >= 0.8 else "#1565C0"
        # ★ 下部のラベル（history=i3, interaction=i4）は y を大きく下げてクリッピング防止
        if i == 3:      # 発生頻度 (下部中央)
            _extra_y = 14
        elif i == 4:    # 複合危険度 (左下)
            _extra_y = 6
        elif i == 0:    # 意味的深刻度 (上部) — 上に少し寄せる
            _extra_y = -4
        else:
            _extra_y = 0
        labels += (
            f'<text x="{lx:.1f}" y="{ly + _extra_y:.1f}" font-size="12" text-anchor="middle" '
            f'fill="{color}" font-family="sans-serif">{lbl}</text>'
            f'<text x="{lx:.1f}" y="{ly + _extra_y + 15:.1f}" font-size="12" text-anchor="middle" '
            f'fill="{color}" font-family="sans-serif" font-weight="bold">{val:.2f}</text>'
        )

    return (
        f'<svg width="{size}" height="{size + 20}" xmlns="http://www.w3.org/2000/svg">'
        f'{grid_lines}{axes}'
        f'<polygon points="{pts_data}" fill="rgba(21,101,192,0.25)" stroke="#1565C0" stroke-width="1.5"/>'
        f'{labels}'
        f'</svg>'
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# メイン証拠パネル
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def render_explanation_panel(
    pred_item: Dict[str, Any],
    expanded: bool = False,
    dt_engine: Any = None,
) -> None:
    """
    予兆予測1件の explanation を証拠パネルとして描画する。

    Args:
        pred_item:  predict() が返す dict（explanation キーを含む）
        expanded:   expander の初期展開状態
        dt_engine:  DigitalTwinEngine（類似インシデント検索用・省略可）
    """
    explanation = pred_item.get("explanation") or {}
    if not explanation:
        return

    anomaly_type    = explanation.get("anomaly_type", "point")
    narrative       = explanation.get("narrative") or pred_item.get("llm_narrative", "")
    llm_error       = explanation.get("llm_error")
    score_breakdown = explanation.get("score_breakdown") or {}
    vendor_context  = explanation.get("vendor_context") or pred_item.get("vendor_context")

    atype_info  = ANOMALY_TYPE_LABELS.get(anomaly_type, ANOMALY_TYPE_LABELS["point"])
    badge_color = atype_info["color"]
    badge_label = atype_info["label"]

    with st.expander(f"🔬 AI証拠パネル  [{badge_label}]", expanded=expanded):

        # ── 異常種別バッジ ──
        _st_html(
            f"<div style='margin-bottom:8px;'>"
            f"<span style='background:{badge_color};color:white;padding:3px 10px;"
            f"border-radius:12px;font-size:11px;font-weight:bold;'>"
            f"{badge_label}</span>"
            f"<span style='color:#757575;font-size:11px;margin-left:8px;'>"
            f"{atype_info['desc']}</span>"
            + (f"<br><span style='color:#9E9E9E;font-size:10px;'>"
               f"⚠️ LLM利用不可（フォールバックスコア）</span>" if llm_error else "")
            + "</div>"
        )

        # ── LLM narrative ──
        if narrative:
            st.info(f"💬 {narrative}")

        # ── ベンダーコンテキスト ──
        if vendor_context:
            st.caption(f"🔧 機器情報: {vendor_context}")

        # ── レーダーチャート（★拡大版: 440px）──
        if score_breakdown:
            svg = build_radar_svg(score_breakdown, size=440)
            _st_html(svg, height=460)

        # ── ChiGAD スペクトル分析（ウェーブレットフィルタ） ──
        spectral = explanation.get("spectral_scores")
        if spectral:
            _render_spectral_bar(spectral)

        # ── 類似インシデント（ChromaDB があれば） ──
        alarm_text = pred_item.get("alarm_text", "")
        if dt_engine is not None and alarm_text:
            _render_similar_incidents(dt_engine, alarm_text)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ChiGAD スペクトル分析バー
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _render_spectral_bar(spectral: Dict[str, float]) -> None:
    """ChiGADウェーブレットフィルタによる周波数帯域別の異常スコアを可視化"""
    anomaly_score = spectral.get("anomaly_spectral_score", 0.5)
    low_e = spectral.get("low_freq_energy", 0.0)
    high_e = spectral.get("high_freq_energy", 0.0)
    total = low_e + high_e if (low_e + high_e) > 0 else 1.0
    low_pct = low_e / total * 100
    high_pct = high_e / total * 100

    # 異常度に応じた色
    if anomaly_score >= 0.7:
        bar_color = "#B71C1C"
        status = "高周波優勢 — 異常信号が強い"
    elif anomaly_score >= 0.4:
        bar_color = "#E65100"
        status = "混在 — 要注意"
    else:
        bar_color = "#1B5E20"
        status = "低周波優勢 — 正常パターン"

    _st_html(
        f"<div style='margin:8px 0;padding:8px 12px;background:#F5F5F5;border-radius:6px;"
        f"border-left:3px solid {bar_color};'>"
        f"<div style='font-size:11px;color:#616161;margin-bottom:4px;'>"
        f"📊 ChiGAD スペクトル分析（ウェーブレットフィルタ）</div>"
        # スタックバー
        f"<div style='display:flex;height:18px;border-radius:9px;overflow:hidden;"
        f"background:#E0E0E0;margin-bottom:4px;'>"
        f"<div style='width:{low_pct:.0f}%;background:#42A5F5;'></div>"
        f"<div style='width:{high_pct:.0f}%;background:{bar_color};'></div>"
        f"</div>"
        f"<div style='display:flex;justify-content:space-between;font-size:10px;'>"
        f"<span style='color:#42A5F5;'>低周波（正常パターン）{low_pct:.0f}%</span>"
        f"<span style='color:{bar_color};'>高周波（異常信号）{high_pct:.0f}%</span>"
        f"</div>"
        f"<div style='font-size:11px;margin-top:4px;color:{bar_color};font-weight:bold;'>"
        f"異常スペクトルスコア: {anomaly_score:.2f} — {status}</div>"
        f"</div>"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 類似インシデント検索パネル
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_OUTCOME_BADGES = {
    "confirmed":   ("#B71C1C", "✅ 実障害(TP)"),
    "mitigated":   ("#1B5E20", "🛡️ 予防済(TP)"),
    "false_alarm": ("#E65100", "⚠️ 誤検知(FP)"),
    "pending":     ("#616161", "⏳ 評価中"),
}


def _render_similar_incidents(dt_engine: Any, alarm_text: str) -> None:
    """ChromaDB で類似インシデントを検索して表示する。結果をキャッシュして高速化。"""
    try:
        vs = getattr(dt_engine, "vector_store", None)
        if vs is None:
            return
        
        # ★ 高速化: 同一アラームテキストの検索結果をキャッシュ
        import hashlib
        _cache_key = f"similar_{hashlib.md5(alarm_text[:200].encode()).hexdigest()}"
        
        # Streamlit session_state にキャッシュ（有効期限60秒）
        import streamlit as _st
        _cache = _st.session_state.get("_similar_cache", {})
        _cached = _cache.get(_cache_key)
        if _cached and (time.time() - _cached["ts"]) < 60:
            results = _cached["results"]
        else:
            results = vs.search_similar_alarms(alarm_text=alarm_text, n_results=5)
            _cache[_cache_key] = {"ts": time.time(), "results": results}
            # キャッシュサイズ制限
            if len(_cache) > 20:
                _oldest = sorted(_cache.items(), key=lambda x: x[1]["ts"])[:10]
                for k, _ in _oldest:
                    _cache.pop(k, None)
            _st.session_state["_similar_cache"] = _cache
        
        if not results:
            return
            return

        with st.expander(
            f"🔍 類似インシデント（{len(results)} 件 · ChromaDB）", expanded=False
        ):
            st.caption("意味的に近い過去の予兆履歴。類似度が高いほど構造が一致しています。")
            for r in results:
                color, badge = _OUTCOME_BADGES.get(
                    r.get("outcome", "pending"), _OUTCOME_BADGES["pending"]
                )
                sim_pct = int(r.get("similarity", 0) * 100)
                ts      = r.get("created_at", 0)
                ts_str  = (
                    time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "不明"
                )
                vc = r.get("vendor_context", "")
                text = r.get("text", "")
                _st_html(
                    f"<div style='border-left:3px solid {color};padding:6px 10px;"
                    f"margin-bottom:6px;background:#FAFAFA;border-radius:0 4px 4px 0;font-size:12px;'>"
                    f"<span style='background:{color};color:white;padding:1px 6px;"
                    f"border-radius:8px;font-size:11px;'>{badge}</span>"
                    f"<span style='color:#616161;margin-left:8px;'>類似度: <b>{sim_pct}%</b></span>"
                    f"<span style='color:#9E9E9E;margin-left:8px;font-size:11px;'>{ts_str}</span>"
                    + (f"<br><span style='color:#78909C;font-size:11px;'>🔧 {vc}</span>" if vc else "")
                    + f"<br><span style='color:#424242;margin-top:3px;display:block;'>"
                    f"{text[:120]}{'...' if len(text) > 120 else ''}</span>"
                    f"</div>"
                )
    except Exception as e:
        import logging
        logging.getLogger(__name__).debug(f"_render_similar_incidents: {e}")
