# ui/stream_dashboard.py — 連続劣化ストリームダッシュボード（オーケストレータ）
#
# コンポーネント:
#   ui/stream/helpers.py        — 共通ヘルパー（HTML描画, SVGキャッシュ, セッションステート）
#   ui/stream/svg_charts.py     — SVGチャート生成（ゲージ, タイムライン, 劣化曲線）
#   ui/stream/kpi_panel.py      — KPIパネル（6カード）
#   ui/stream/event_timeline.py — イベントカード型タイムライン

import streamlit as st
import logging
from datetime import datetime
from typing import Optional
from digital_twin_pkg.alarm_stream import (
    AlarmStreamSimulator,
    DEGRADATION_SEQUENCES,
    get_available_scenarios,
    get_default_interfaces,
)

from ui.stream.helpers import (
    st_html,
    get_simulator as _get_simulator,
    save_simulator as _save_simulator,
    clear_simulator as _clear_simulator,
    svg_cached as _svg_cached,
)
from ui.stream.svg_charts import (
    render_metric_gauge_svg,
    render_timeline_svg,
    render_degradation_chart_svg,
)
from ui.stream.kpi_panel import render_kpi_html
from ui.stream.event_timeline import render_event_timeline

logger = logging.getLogger(__name__)


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

        # --- 開始レベルスライダー ---
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

    4つのビジュアルコンポーネント:
      1. ステージタイムライン（横方向プログレス）
      2. メトリクスゲージ（半円ゲージ）+ KPIパネル
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

    st_html(
        f"<h3 style='margin:0 0 8px 0;'>📡 連続劣化モニタリング</h3>"
        f"<span style='background:{status_color};color:white;padding:2px 10px;"
        f"border-radius:10px;font-size:13px;'>"
        f"{status_icon} {status_text}</span>"
        f"<span style='color:#666;font-size:13px;margin-left:12px;'>"
        f"{seq.pattern.upper()} | {sim.device_id}{start_info}</span>"
    )

    with st.container(border=True):
        # ── 1. ステージタイムライン ──
        active_stages = [s for s in seq.stages if s.level >= start_lvl]
        stages_info = [{"label": s.label} for s in active_stages]
        relative_level = max(0, current_level - start_lvl + 1) if current_level >= start_lvl else 0
        _tl_cache_key = f"{relative_level}|{int(progress // 5 * 5)}"
        timeline_svg = _svg_cached("timeline", _tl_cache_key,
                                   render_timeline_svg, relative_level, progress, stages_info)
        st_html(timeline_svg, height=100)

        st.markdown("---")

        # ── 2. メトリクスゲージ + KPI ──
        col_gauge, col_kpi1 = st.columns([1, 2])

        current_metric = events[-1].metric_value if events else seq.normal_value
        with col_gauge:
            _gauge_cache_key = f"{round(current_metric)}|{seq.normal_value}|{seq.failure_value}"
            gauge_svg = _svg_cached("gauge", _gauge_cache_key,
                                    render_metric_gauge_svg,
                                    current_value=current_metric,
                                    normal_value=seq.normal_value,
                                    failure_value=seq.failure_value,
                                    unit=seq.metric_unit,
                                    label=seq.metric_name)
            st_html(gauge_svg, height=190)

        with col_kpi1:
            severity = events[-1].severity if events else "NORMAL"
            elapsed = sim.current_elapsed_sec
            remaining = max(0, sim.total_duration_sec - elapsed)
            latest_stage = events[-1].stage_label if events else "-"

            kpi_html = render_kpi_html(
                current_level=current_level,
                severity=severity,
                elapsed=elapsed,
                remaining=remaining,
                latest_stage=latest_stage,
                event_count=len(events),
                pattern=seq.pattern,
            )
            st_html(kpi_html, height=200)

        st.markdown("---")

        # ── 3. 劣化曲線チャート（実時間軸） ──
        _chart_cache_key = f"{len(events)}|{current_level}|{start_lvl}|{seq.pattern}"
        metric_history = sim.get_metric_history(events=events)
        realtime_history, rt_x_start, rt_x_end = sim.get_realtime_metric_history(events=events)
        _sim_start_dt = datetime.fromtimestamp(sim._start_time) if sim._start_time else datetime.now()
        chart_svg = _svg_cached("degradation", _chart_cache_key,
            render_degradation_chart_svg,
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
        _components.html(_scroll_html, height=350, scrolling=True)

        st.markdown("---")

        # ── 4. イベントログ ──
        st.markdown("**📋 アラームイベントログ**")
        if events:
            render_event_timeline(events, sim)
            if len(events) > 30:
                st.caption(f"直近30件を表示中（全{len(events)}件）")
        else:
            st.caption("イベント待機中...")

    # ── 自動リフレッシュ ──
    if not is_complete:
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
        "source": "stream",
    }
