# ui/cockpit.py  ―  AIOps インシデント・コックピット（リファクタリング版: オーケストレータ）
#
# UI描画ロジックは ui/components/ に分割:
#   helpers.py         — 共通ヘルパー
#   report_builders.py — LLMプロンプト構築
#   kpi_banner.py      — KPI + ステータスバナー
#   future_radar.py    — 予兆専用表示エリア
#   root_cause_table.py — 根本原因候補テーブル
#   topology_panel.py  — 左カラム（トポロジー + 診断）
#   analyst_report.py  — AI Analyst Report
#   remediation.py     — Remediation & Execute
#   chat_panel.py      — Chat with AI Agent
#   diagnostic.py      — Auto-Diagnostics 実行
import streamlit as st
import logging
from typing import Optional

logger = logging.getLogger(__name__)

from registry import get_paths, load_topology, get_display_name
from alarm_generator import generate_alarms_for_scenario, Alarm
from inference_engine import LogicalRCA
from utils.helpers import get_status_from_alarms
from ui.engine_cache import compute_topo_hash, get_cached_dt_engine

# コンポーネントインポート
from ui.components.helpers import build_ci_context_for_chat
from ui.components.kpi_banner import render_kpi_banner
from ui.components.future_radar import render_future_radar
from ui.components.root_cause_table import render_root_cause_table
from ui.components.topology_panel import render_topology_panel
from ui.components.analyst_report import render_analyst_report
from ui.components.remediation import render_remediation
from ui.components.chat_panel import render_chat_panel
from ui.components.command_popup import show_command_popup_if_pending
from ui.prediction_pipeline import run_prediction_pipeline


# =====================================================
# ヘルパー関数（後方互換）
# =====================================================
def _compute_topo_hash(topology: dict) -> str:
    """後方互換ラッパー → engine_cache.compute_topo_hash に委譲"""
    return compute_topo_hash(topology)


# =====================================================
# キャッシュ済みエンジン取得
# =====================================================
@st.cache_resource
def _get_cached_logical_rca(_topology):
    return LogicalRCA(_topology)

def _get_cached_dt_engine(site_id: str, topo_hash: str, _topology):
    return get_cached_dt_engine(site_id, topo_hash, _topology)


# =====================================================
# run_diagnostic の後方互換エクスポート
# =====================================================
from ui.components.diagnostic import run_diagnostic  # noqa: F401


# =====================================================
# Phase 2: メンテナンスウィンドウの時間判定
# =====================================================
def _resolve_maint_windows(site_id: str, topology: dict):
    """アクティブなメンテナンスウィンドウの device_ids を maint_devices にマージ。

    終了済みウィンドウは自動クリーンアップし、終了通知バナーを表示する。
    """
    from datetime import datetime
    _now = datetime.now()
    _windows = st.session_state.get("maint_windows", [])
    if not _windows:
        return

    _active_devs = set()
    _expired_labels = []
    _surviving = []

    for _w in _windows:
        if _w.get("site_id") != site_id:
            _surviving.append(_w)
            continue

        _start = _w.get("start")
        _end = _w.get("end")

        if _now > _end:
            # 終了済み → クリーンアップ + 通知ラベル収集
            _label = _w.get("label") or ", ".join(sorted(_w.get("device_ids", set()))) or "拠点全体"
            _expired_labels.append(_label)
            # ★ 終了済みウィンドウを一覧から除去（自動クリーンアップ）
            continue

        _surviving.append(_w)

        if _now >= _start:
            # アクティブ → device_ids をマージ
            _w_devs = _w.get("device_ids", set())
            if _w_devs:
                _active_devs.update(_w_devs)
            else:
                # 空 = 拠点全体 → トポロジーの全デバイスを追加
                _active_devs.update(topology.keys())

    # ウィンドウリストを更新（終了済みを除去）
    if len(_surviving) != len(_windows):
        st.session_state["maint_windows"] = _surviving

    # maint_devices にマージ
    if _active_devs:
        _current = st.session_state.get("maint_devices", {}).get(site_id, set())
        st.session_state["maint_devices"][site_id] = _current | _active_devs

    # 終了通知バナー
    if _expired_labels:
        _labels_str = "、".join(_expired_labels)
        st.success(f"✅ **メンテナンス終了**: {_labels_str} — アラーム抑制を解除しました")


# =====================================================
# メインエントリポイント
# =====================================================
def render_incident_cockpit(site_id: str, api_key: Optional[str]):
    display_name = get_display_name(site_id)
    scenario = getattr(st.session_state, 'site_scenarios', {}).get(site_id, "正常稼働")

    # ヘッダー
    col_header = st.columns([4, 1])
    with col_header[0]:
        st.markdown(f"### 🛡️ AIOps インシデント・コックピット")
    with col_header[1]:
        if st.button(
            "🔙 一覧に戻る",
            key="back_to_list",
            type="primary",
            use_container_width=True,
        ):
            st.session_state.active_site = None
            st.rerun()

    # トポロジー読み込み
    paths = get_paths(site_id)
    topology = load_topology(paths.topology_path)

    if not topology:
        st.error("トポロジーが読み込めませんでした。")
        return

    # アラーム生成（シナリオ不変ならキャッシュ利用）
    _alarm_cache_key = f"_alarm_cache_{site_id}_{scenario}"
    if _alarm_cache_key in st.session_state:
        alarms = st.session_state[_alarm_cache_key]
    else:
        alarms = generate_alarms_for_scenario(topology, scenario)
        st.session_state[_alarm_cache_key] = alarms

    # ★ Phase 2: メンテナンスウィンドウの時間判定 → maint_devices にマージ
    _resolve_maint_windows(site_id, topology)

    # ★ 機器単位メンテナンスモード: メンテ中デバイスのアラームを抑制
    _maint_devs = st.session_state.get("maint_devices", {}).get(site_id, set())
    _suppressed_count = 0
    if _maint_devs:
        _original_len = len(alarms)
        alarms = [a for a in alarms if a.device_id not in _maint_devs]
        _suppressed_count = _original_len - len(alarms)

    status = get_status_from_alarms(scenario, alarms)

    # 予兆シグナル注入（メンテ中デバイスはスキップ）
    injected = st.session_state.get("injected_weak_signal")
    if injected and injected["device_id"] in topology and injected["device_id"] not in _maint_devs:
        messages = injected.get("messages", [injected.get("message", "")])
        for msg in messages:
            if msg:
                alarms.append(Alarm(
                    device_id=injected["device_id"],
                    message=msg,
                    severity="INFO",
                    is_root_cause=False
                ))

    # LogicalRCA エンジン
    engine = _get_cached_logical_rca(topology)

    # 分析結果キャッシュ
    _analysis_cache_key = f"_analysis_cache_{site_id}_{scenario}"
    _alarm_hash = hash(tuple((a.device_id, a.message, a.severity) for a in alarms)) if alarms else 0
    _cached_analysis = st.session_state.get(_analysis_cache_key)
    if _cached_analysis and _cached_analysis.get("hash") == _alarm_hash:
        analysis_results = _cached_analysis["results"]
    elif alarms:
        analysis_results = engine.analyze(alarms)
        st.session_state[_analysis_cache_key] = {"hash": _alarm_hash, "results": analysis_results}
    else:
        analysis_results = [{
            "id": "SYSTEM",
            "label": "正常稼働",
            "prob": 0.0,
            "type": "Normal",
            "tier": 3,
            "reason": "アラームなし"
        }]
        st.session_state[_analysis_cache_key] = {"hash": _alarm_hash, "results": analysis_results}

    # =====================================================
    # DigitalTwinEngine 初期化
    # =====================================================
    dt_err_key = f"dt_engine_error_{site_id}"
    dt_engine  = None

    if not st.session_state.get(dt_err_key):
        try:
            # ★ 高速化: topo_hash を session_state にキャッシュ（毎回の再計算を回避）
            _topo_hash_key = f"_topo_hash_{site_id}"
            current_topo_hash = st.session_state.get(_topo_hash_key)
            if not current_topo_hash:
                current_topo_hash = _compute_topo_hash(topology)
                st.session_state[_topo_hash_key] = current_topo_hash
            dt_engine = _get_cached_dt_engine(site_id, current_topo_hash, topology)
        except Exception as _dte_err:
            import traceback as _tb
            st.session_state[dt_err_key] = f"{type(_dte_err).__name__}: {_dte_err}\n{_tb.format_exc()}"

    _dte_error = st.session_state.get(dt_err_key)
    if _dte_error and dt_engine is None:
        with st.expander("⚠️ Digital Twin Engine 初期化エラー（予兆検知は無効）", expanded=False):
            st.code(_dte_error, language="text")
            if st.button("🔄 再初期化", key=f"dte_retry_{site_id}"):
                st.session_state.pop(dt_err_key, None)
                st.rerun()

    # 自動チューニング + 自動TP確認
    if dt_engine:
        dt_engine.maybe_run_auto_tuning()

    if dt_engine and scenario != "正常稼働":
        critical_devices = {a.device_id for a in alarms if a.severity == "CRITICAL"}
        for dev_id in critical_devices:
            confirmed_count = dt_engine.forecast_auto_confirm_on_incident(
                dev_id, scenario=scenario, note="障害シナリオ発生により自動確認"
            )
            if confirmed_count > 0:
                logger.info(f"Auto-confirmed {confirmed_count} predictions for {dev_id} on scenario: {scenario}")

    # =====================================================
    # DT予兆パイプライン（prediction_pipeline.py に委譲）
    # =====================================================
    if dt_engine:
        run_prediction_pipeline(
            dt_engine=dt_engine,
            alarms=alarms,
            analysis_results=analysis_results,
            site_id=site_id,
            api_key=api_key,
            topology=topology,
            scenario=scenario,
        )

    # =====================================================
    # 3分類: root_cause / symptom / unrelated
    # =====================================================
    root_cause_candidates = []
    symptom_devices = []
    unrelated_devices = []

    # ★ 高速化: アラームのデバイスIDセットを事前計算（O(n*m) → O(n+m)）
    _rc_device_ids = {a.device_id for a in alarms if a.is_root_cause}
    _non_rc_device_ids = {a.device_id for a in alarms if not a.is_root_cause}

    for cand in analysis_results:
        device_id = cand.get('id', '')
        cls = cand.get('classification', '')

        if cand.get('is_prediction'):
            root_cause_candidates.append(cand)
        elif cls == 'root_cause':
            root_cause_candidates.append(cand)
        elif cls == 'symptom':
            symptom_devices.append(cand)
        elif cls == 'unrelated':
            unrelated_devices.append(cand)
        else:
            if device_id in _rc_device_ids:
                root_cause_candidates.append(cand)
            elif device_id in _non_rc_device_ids:
                symptom_devices.append(cand)
            elif cand.get('prob', 0) > 0.5:
                root_cause_candidates.append(cand)

    if not root_cause_candidates:
        root_cause_candidates = [{
            "id": "SYSTEM", "label": "正常稼働", "prob": 0.0,
            "type": "Normal", "tier": 3, "reason": "異常は検知されていません",
            "classification": "unrelated"
        }]

    # =====================================================
    # 障害時トリアージ: cockpit では生成せず、root_cause_table で
    # 選択された候補のみオンデマンド生成する（高速化）
    # =====================================================

    # =====================================================
    # UI描画（コンポーネントに委譲）
    # =====================================================

    # 0. コマンド実行結果ポップアップ（@st.dialog の重複登録を防ぐため1箇所で呼ぶ）
    show_command_popup_if_pending()

    # 0.5 メンテナンスモード通知バナー
    if _maint_devs:
        _maint_list = ", ".join(sorted(_maint_devs))
        # アクティブなウィンドウ情報を付加
        _active_win_info = ""
        from datetime import datetime as _dt_cls
        _now_ts = _dt_cls.now()
        for _w in st.session_state.get("maint_windows", []):
            if (_w.get("site_id") == site_id
                    and _w.get("start") <= _now_ts <= _w.get("end")):
                _end_str = _w["end"].strftime("%m/%d %H:%M")
                _wlabel = _w.get("label", "")
                _active_win_info += f" | 📅 {_wlabel or 'ウィンドウ'} 〜{_end_str}"
        st.info(
            f"🔧 **メンテナンスモード**: {len(_maint_devs)}台のアラームを抑制中 "
            f"({_maint_list})"
            + (f" — {_suppressed_count}件のアラームを非表示" if _suppressed_count else "")
            + _active_win_info
        )

    # 1. KPIバナー
    prediction_count, noise_reduction = render_kpi_banner(
        analysis_results, alarms,
        root_cause_candidates, symptom_devices, unrelated_devices,
    )

    # 2. Future Radar（予兆専用表示）
    prediction_candidates = [c for c in root_cause_candidates if c.get('is_prediction')]
    render_future_radar(prediction_candidates, topology=topology)

    # 3. 根本原因候補テーブル
    selected_incident_candidate, target_device_id = render_root_cause_table(
        root_cause_candidates, symptom_devices, unrelated_devices, alarms,
        topology=topology,
    )

    # 4. 2カラムレイアウト
    col_map, col_chat = st.columns([1.2, 1])

    # 左カラム: トポロジー + 影響伝搬 + AI学習ルール + Auto-Diagnostics
    with col_map:
        render_topology_panel(
            topology, alarms, analysis_results,
            selected_incident_candidate, target_device_id,
            dt_engine, engine, scenario, api_key,
        )

    # 右カラム: AI Analyst Report + Remediation + Chat
    with col_chat:
        render_analyst_report(
            selected_incident_candidate, topology,
            scenario, site_id, api_key,
        )

        render_remediation(
            selected_incident_candidate, topology,
            scenario, site_id, api_key, dt_engine,
        )

        render_chat_panel(
            selected_incident_candidate, target_device_id,
            topology, api_key,
        )
