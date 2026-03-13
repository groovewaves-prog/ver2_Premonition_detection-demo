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
from digital_twin_pkg.common import build_children_map, get_downstream_devices
from ui.engine_cache import (
    compute_topo_hash, get_cached_dt_engine, get_cached_logical_rca,
    get_topo_hash_cached,
)
from ui.async_inference import (
    submit_rca_task, get_rca_result, is_any_analyzing,
)

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
from ui.autonomous_diagnostic import render_autonomous_diagnostic_panel


# =====================================================
# ヘルパー関数（後方互換）
# =====================================================
def _compute_topo_hash(topology: dict) -> str:
    """後方互換ラッパー → engine_cache.compute_topo_hash に委譲"""
    return compute_topo_hash(topology)


# =====================================================
# キャッシュ済みエンジン取得（★ エッセンス1: 軽量キーのみ）
# =====================================================
def _get_cached_logical_rca_by_site(site_id: str, topo_hash: str):
    """engine_cache の軽量キー版に委譲"""
    return get_cached_logical_rca(site_id, topo_hash)

def _get_cached_dt_engine_by_site(site_id: str, topo_hash: str):
    """engine_cache の軽量キー版に委譲"""
    return get_cached_dt_engine(site_id, topo_hash)


# =====================================================
# run_diagnostic の後方互換エクスポート
# =====================================================
from ui.components.diagnostic import run_diagnostic  # noqa: F401


# =====================================================
# エンジン事前ウォームアップ（ダッシュボード表示時に呼出）
# =====================================================
def prewarm_engines():
    """ダッシュボード表示中にエンジンを事前初期化する。

    「詳細」ボタン押下時のコールドスタート遅延を解消するため、
    拠点状態ボード表示時に @st.cache_resource をウォームアップしておく。
    2回目以降はキャッシュヒットで即座に返る。
    """
    _warmup_key = "_engines_prewarmed"
    if st.session_state.get(_warmup_key):
        return  # 既にウォームアップ済み

    try:
        from registry import list_sites
        with st.spinner("🧠 AI分析エンジンを事前ロード中...（初回のみ）"):
            for site_id in list_sites():
                # ★ エッセンス1: 軽量な文字列キーのみでウォームアップ
                #   トポロジーの読み込みはキャッシュ関数内部で実行される
                topo_hash = get_topo_hash_cached(site_id)
                if not topo_hash:
                    continue
                get_cached_logical_rca(site_id, topo_hash)
                get_cached_dt_engine(site_id, topo_hash)
    except Exception as e:
        logger.warning(f"Engine prewarm failed: {e}")

    st.session_state[_warmup_key] = True


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


def _build_alarm_based_fallback(alarms: list) -> list:
    """アラーム情報のみで即席のanalysis_resultsを生成する（LLM呼び出しなし）。

    非同期RCA推論が完了するまでの間、UIが空（SYSTEM/正常稼働）にならないよう
    アラームのseverityとis_root_causeフラグから即座に分類結果を構築する。
    """
    if not alarms:
        return []

    _sev_order = {'CRITICAL': 3, 'WARNING': 2, 'INFO': 1}
    device_map: dict = {}
    for a in alarms:
        dev_id = a.device_id
        if dev_id not in device_map:
            device_map[dev_id] = {
                'messages': [], 'severity': 'INFO', 'is_root_cause': False,
            }
        device_map[dev_id]['messages'].append(a.message)
        if _sev_order.get(a.severity, 0) > _sev_order.get(device_map[dev_id]['severity'], 0):
            device_map[dev_id]['severity'] = a.severity
        if getattr(a, 'is_root_cause', False):
            device_map[dev_id]['is_root_cause'] = True

    results = []
    for dev_id, info in device_map.items():
        sev = info['severity']
        if sev == 'INFO' and not info['is_root_cause']:
            continue  # INFOのみのデバイスはスキップ

        if info['is_root_cause']:
            # is_root_causeフラグが明示的にTrueの場合のみ根本原因
            cls = 'root_cause'
            prob = 0.95
            status = 'RED'
        elif sev == 'CRITICAL' or sev == 'WARNING':
            # CRITICALでもis_root_cause=Falseなら派生アラート（巻き添え）
            cls = 'symptom'
            prob = 0.5
            status = 'YELLOW' if sev == 'WARNING' else 'RED'
        else:
            cls = 'unrelated'
            prob = 0.2
            status = 'GREEN'

        results.append({
            'id': dev_id,
            'label': ' / '.join(info['messages'][:3]),
            'prob': prob,
            'type': 'AlarmBased',
            'tier': 1 if cls == 'root_cause' else (2 if cls == 'symptom' else 3),
            'reason': f"アラーム severity={sev} に基づく即席分類",
            'status': status,
            'is_prediction': False,
            'classification': cls,
        })
    return results


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
        alarms = list(st.session_state[_alarm_cache_key])  # ★ コピー: キャッシュ汚染防止
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

    # ★ エッセンス1+3: 軽量キーでエンジン取得 & 推論結果キャッシュ
    current_topo_hash = get_topo_hash_cached(site_id)
    engine = _get_cached_logical_rca_by_site(site_id, current_topo_hash)

    # ★ エッセンス4: 非同期推論（ゼロ・ウェイティング）
    #   バックグラウンドで RCA 分析をキックし、結果はキャッシュから即座に取得。
    #   計算中はアラーム情報から即席分類を生成し、完了後に最新結果に切り替わる。
    submit_rca_task(site_id, current_topo_hash, alarms)

    # 軽量フォールバック: アラーム情報のみで即席分類を生成（LLM呼び出しなし）
    # 非同期タスク完了までの間、UIが空にならないようにする。
    _fallback = _build_alarm_based_fallback(alarms)

    analysis_results, _is_analyzing = get_rca_result(
        site_id, alarms, fallback_results=_fallback
    )

    # =====================================================
    # DigitalTwinEngine 初期化
    # =====================================================
    dt_err_key = f"dt_engine_error_{site_id}"
    dt_engine  = None

    if not st.session_state.get(dt_err_key):
        try:
            # ★ エッセンス1: 軽量キーのみでエンジン取得（トポロジーはキャッシュ関数内部で読み込み）
            dt_engine = _get_cached_dt_engine_by_site(site_id, current_topo_hash)
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
    # ★ 高速化: シミュレーション操作中（スライダー変更）は
    #   auto_tuning と auto_confirm をスキップ（DB I/O 削減）
    _sim_active = bool(st.session_state.get("injected_weak_signal"))
    if dt_engine and not _sim_active:
        dt_engine.maybe_run_auto_tuning()

    if dt_engine and scenario != "正常稼働" and not _sim_active:
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
        elif cls == 'root_cause' or device_id in _rc_device_ids:
            # ★ is_root_cause=Trueのデバイスは強制的に根本原因へ昇格
            cand['classification'] = 'root_cause'
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

    # =====================================================
    # ★ トポロジーマップを用いた機械的RCA（ノイズ除去）
    # 根本原因候補の中に、他の候補の「下流（downstream）」ノードが含まれている場合、
    # 上流の障害による波及（ノイズ）と判断し「派生アラート（Symptom）」へ強制降格する。
    # ただし、自身がis_root_cause=Trueのアラームを持つデバイスは独立した
    # 障害源と見なし、降格対象から除外する。
    # =====================================================
    _rc_ids = [c['id'] for c in root_cause_candidates
               if not c.get('is_prediction') and c.get('id') != 'SYSTEM']

    if _rc_ids and topology:
        _children_map = build_children_map(topology)
        _downstream_set: set = set()
        for rid in _rc_ids:
            _ds = get_downstream_devices(topology, rid, max_hops=0,
                                         children_map=_children_map)
            _downstream_set.update(_ds)

        _filtered_rc = []
        for cand in root_cause_candidates:
            cid = cand['id']
            is_downstream = cid in _downstream_set
            is_prediction = cand.get('is_prediction')
            has_own_root_cause = cid in _rc_device_ids
            if is_downstream and not is_prediction and not has_own_root_cause:
                cand['classification'] = 'symptom'
                symptom_devices.append(cand)
            else:
                _filtered_rc.append(cand)
        root_cause_candidates = _filtered_rc

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

    # 0.9 AI分析中インジケーター
    if _is_analyzing or is_any_analyzing(site_id):
        st.info("🧠 **AI分析中...** バックグラウンドで推論を実行しています。完了次第、結果が更新されます。")

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
            symptom_devices=symptom_devices,
        )

        # ★ エッセンス5: AI自律診断パネル
        render_autonomous_diagnostic_panel(
            selected_incident_candidate, topology, scenario,
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
