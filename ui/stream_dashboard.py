# ui/stream_dashboard.py — 連続劣化ストリームダッシュボード（オーケストレータ）
#
# コンポーネント:
#   ui/stream/helpers.py        — 共通ヘルパー（HTML描画, SVGキャッシュ, セッションステート）
#   ui/stream/svg_charts.py     — SVGチャート生成（ゲージ, タイムライン, 劣化曲線）
#   ui/stream/kpi_panel.py      — KPIパネル（6カード）
#   ui/stream/event_timeline.py — イベントカード型タイムライン

import streamlit as st
import logging
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

def auto_start_stream(target_device: str, scenario_key: str, start_level: int):
    """
    劣化進行度が変化した時に自動でストリームを開始/再開始する。

    サイドバーの _render_weak_signal_injection から呼ばれる。
    既存のストリームが異なるレベルで完了済みの場合は、クリアして再開始する。
    """
    sim = _get_simulator()

    # 既に実行中（未完了）なら何もしない
    if sim is not None and sim.is_started and not sim.is_complete:
        return

    # 完了済みで同一 start_level → 再開始不要
    if (sim is not None and sim.is_started and sim.is_complete
            and getattr(sim, 'start_level', 1) == start_level
            and sim.device_id == target_device
            and sim.sequence.pattern == scenario_key):
        return

    # 既存のストリームをクリア（レベル/デバイス/シナリオ変更時）
    if sim is not None:
        _clear_simulator()
        st.session_state.pop("stream_completion_result", None)

    speed = 10.0  # 固定速度（倍速）
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
    logger.info("Auto-started stream: %s on %s (L%d)", scenario_key, target_device, start_level)


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
        st.session_state["_stream_needs_refresh"] = False
        return False  # ストリーム非実行

    seq = sim.sequence
    all_events = sim.get_all_events()       # 全イベント（完了まで）
    live_events = sim.get_all_events_until_now()  # 現在までのイベント
    current_level = sim.get_current_level()
    progress = sim.current_progress_pct
    is_complete = sim.is_complete
    start_lvl = getattr(sim, 'start_level', 1)

    # ── レベル別の elapsed_sec マップ（各レベルの最初のイベント） ──
    level_elapsed_map = {}
    for ev in all_events:
        if ev.level not in level_elapsed_map:
            level_elapsed_map[ev.level] = ev.elapsed_sec

    # ── レベル探索スライダーの値を先に取得 ──
    _EXPLORE_LABELS = {
        1: "L1: 初期劣化", 2: "L2: 劣化進行", 3: "L3: 警戒域",
        4: "L4: 危険域", 5: "L5: 障害直前",
    }
    _all_levels = list(range(start_lvl, 6))
    # 完了時のみ探索可能（実行中は current_level をそのまま使用）
    explore_level = current_level
    if is_complete and _all_levels:
        _default = st.session_state.get("stream_explore_level", current_level)
        if _default not in _all_levels:
            _default = _all_levels[-1]
        explore_level = _default  # 描画前にデフォルト設定

    # ── 探索レベルに基づいてイベントをフィルタ（表示用） ──
    if is_complete and explore_level < current_level:
        display_events = [e for e in all_events if e.level <= explore_level]
        display_level = explore_level
    else:
        display_events = live_events
        display_level = current_level

    # ── ヘッダー ──
    status_text = "完了" if is_complete else f"Level {display_level}/5"
    status_icon = "✅" if is_complete else "🔴" if display_level >= 4 else "🟠" if display_level >= 2 else "🟢"
    start_info = f" (開始L{start_lvl})" if start_lvl > 1 else ""

    _expander_label = (
        f"📡 連続劣化モニタリング  "
        f"{status_icon} {status_text} — "
        f"{seq.pattern.upper()} | {sim.device_id}{start_info}"
    )
    with st.expander(_expander_label, expanded=True):
        # ── 1. ステージタイムライン ──
        active_stages = [s for s in seq.stages if s.level >= start_lvl]
        stages_info = [{"label": s.label} for s in active_stages]
        relative_level = max(0, display_level - start_lvl + 1) if display_level >= start_lvl else 0
        _tl_cache_key = f"{relative_level}|{int(progress // 5 * 5)}|{explore_level}"
        timeline_svg = _svg_cached("timeline", _tl_cache_key,
                                   render_timeline_svg, relative_level, progress, stages_info)
        st_html(timeline_svg, height=100)

        st.markdown("---")

        # ── 2. メトリクスゲージ + KPI ──
        col_gauge, col_kpi1 = st.columns([1, 2])

        display_metric = display_events[-1].metric_value if display_events else seq.normal_value
        with col_gauge:
            _gauge_cache_key = f"{round(display_metric)}|{seq.normal_value}|{seq.failure_value}"
            gauge_svg = _svg_cached("gauge", _gauge_cache_key,
                                    render_metric_gauge_svg,
                                    current_value=display_metric,
                                    normal_value=seq.normal_value,
                                    failure_value=seq.failure_value,
                                    unit=seq.metric_unit,
                                    label=seq.metric_name)
            st_html(gauge_svg, height=190)

        with col_kpi1:
            severity = display_events[-1].severity if display_events else "NORMAL"
            elapsed = sim.current_elapsed_sec
            remaining = max(0, sim.total_duration_sec - elapsed)
            latest_stage = display_events[-1].stage_label if display_events else "-"

            kpi_html = render_kpi_html(
                current_level=display_level,
                severity=severity,
                elapsed=elapsed,
                remaining=remaining,
                latest_stage=latest_stage,
                event_count=len(display_events),
                pattern=seq.pattern,
            )
            st_html(kpi_html, height=200)

        st.markdown("---")

        # ── 3. 劣化曲線チャート（リニアスケール + レベルマーカー） ──
        # 全イベントの履歴を使用（点線部分も描画するため）
        metric_history = sim.get_metric_history(events=all_events)
        _chart_cache_key = f"{len(all_events)}|{explore_level}|{start_lvl}|{seq.pattern}"
        chart_svg = _svg_cached("degradation", _chart_cache_key,
            render_degradation_chart_svg,
            metric_history=metric_history,
            normal_value=seq.normal_value,
            failure_value=seq.failure_value,
            metric_name=seq.metric_name,
            metric_unit=seq.metric_unit,
            total_duration=sim.total_duration_sec,
            scenario_key=seq.pattern,
            start_level=start_lvl,
            explore_level=explore_level if is_complete else 0,
            level_elapsed_map=level_elapsed_map,
        )
        import streamlit.components.v1 as _components
        _scroll_html = (
            f'<div style="overflow-x:auto;overflow-y:hidden;'
            f'border:1px solid #eee;border-radius:4px;padding:4px;">'
            f'{chart_svg}</div>'
        )
        _components.html(_scroll_html, height=380, scrolling=True)

        # ── 3.5 レベル探索スライダー（グラフ直下） ──
        if is_complete and len(_all_levels) > 1:
            explore_level = st.select_slider(
                "🔍 レベル探索",
                options=_all_levels,
                value=_default,
                format_func=lambda x: _EXPLORE_LABELS.get(x, f"L{x}"),
                help="選択レベルまでを実線、以降を点線で表示。ゲージ・KPI・タイムラインも連動します。",
                key="stream_explore_level",
            )
            # injected_weak_signal を更新して cockpit の分析を連動
            _explore_events = [e for e in all_events if e.level == explore_level]
            if _explore_events:
                _latest_explore = _explore_events[-1]
                st.session_state["injected_weak_signal"] = {
                    "device_id": sim.device_id,
                    "messages": _latest_explore.messages,
                    "message": _latest_explore.messages[0] if _latest_explore.messages else "",
                    "level": explore_level,
                    "scenario": seq.pattern,
                    "source": "stream_explore",
                }

        st.markdown("---")

        # ── 4. イベントログ ──
        st.markdown("**📋 アラームイベントログ**")
        if display_events:
            render_event_timeline(display_events, sim)
            if len(display_events) > 30:
                st.caption(f"直近30件を表示中（全{len(display_events)}件）")
        else:
            st.caption("イベント待機中...")

        # ── 完了時: DB同期結果 ──
        if is_complete:
            _completion_key = "stream_completion_result"
            if _completion_key not in st.session_state:
                _sync_result = _run_completion_sync(sim)
                st.session_state[_completion_key] = _sync_result
            else:
                _sync_result = st.session_state[_completion_key]

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

    # ── 自動リフレッシュ ──
    if not is_complete:
        st.session_state["_stream_needs_refresh"] = True
        return True

    st.session_state["_stream_needs_refresh"] = False
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

    ★ エッセンス4: 同時にバックグラウンドでキャッシュウォーミングを実行し、
      ユーザーが Cockpit タブを開く前に推論結果を準備しておく。
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

    # ★ プロアクティブ・キャッシュウォーミング:
    #   ストリームデータ到着時にバックグラウンドで推論をキックし、
    #   Cockpit タブを開く前にキャッシュを温めておく。
    _warm_stream_cache(sim, latest_msgs, current_level)


def _warm_stream_cache(sim: AlarmStreamSimulator, latest_msgs: list, current_level: int):
    """ストリームデータ到着時にバックグラウンドで推論キャッシュを温める。

    Cockpit タブを開く前に推論結果を準備しておくプロアクティブ型。
    """
    try:
        from ui.async_inference import proactive_warm_cache
        from ui.engine_cache import get_topo_hash_cached

        active_site = st.session_state.get("active_site")
        if not active_site:
            return

        topo_hash = get_topo_hash_cached(active_site)

        # ストリームのアラームから Alarm オブジェクトを構築
        from alarm_generator import generate_alarms_for_scenario, Alarm
        scenario = st.session_state.get("site_scenarios", {}).get(active_site, "正常稼働")
        from registry import get_paths, load_topology
        paths = get_paths(active_site)
        topology = load_topology(paths.topology_path)
        alarms = generate_alarms_for_scenario(topology, scenario) if topology else []

        # ストリームの INFO アラームを追加
        for msg in latest_msgs:
            if msg:
                alarms.append(Alarm(
                    device_id=sim.device_id,
                    message=msg,
                    severity="INFO",
                    is_root_cause=False,
                ))

        # predict_api 用のソース情報
        combined_msg = " | ".join(latest_msgs[:5])
        predict_sources = [
            (sim.device_id, combined_msg, "stream", current_level, len(latest_msgs)),
        ]

        proactive_warm_cache(
            site_id=active_site,
            topo_hash=topo_hash,
            alarms=alarms,
            predict_sources=predict_sources,
        )
    except Exception as e:
        logger.debug("Stream cache warming skipped: %s", e)
