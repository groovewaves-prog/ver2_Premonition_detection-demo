# ui/sidebar.py  ―  Streamlit サイドバー UI（予兆シミュレーション・シナリオ設定）
#
# 改修: 予兆シミュレーションと連続劣化ストリームで
#       「対象デバイス」「劣化シナリオ」を共通設定として一元管理する。
import streamlit as st
import os
from registry import list_sites, get_display_name, load_topology, get_paths
from utils.const import SCENARIO_MAP
from utils.llm_helper import get_rate_limiter, GENAI_AVAILABLE
from ui.stream_dashboard import auto_start_stream, _get_simulator, _clear_simulator, inject_stream_alarms_to_session
from ui.service_tier import (
    tier_has_access, get_service_tier,
    TIER_BASIC, TIER_PHM, TIER_PHM_PREMONITION, TIER_PHM_RUL, TIER_PHM_TRAFFIC, TIER_FULL,
    ALL_TIERS,
)
from ui.shared_sim_config import scenario_key_to_display

def render_sidebar():
    with st.sidebar:
        st.header("⚡ 拠点シナリオ設定")
        st.caption("各拠点で発生させるシナリオを選択")

        sites = list_sites()

        for site_id in sites:
            display_name = get_display_name(site_id)

            with st.expander(f"📍 {display_name}", expanded=True):
                category = st.selectbox(
                    "カテゴリ",
                    list(SCENARIO_MAP.keys()),
                    key=f"cat_{site_id}",
                    label_visibility="collapsed"
                )

                scenarios = SCENARIO_MAP[category]
                current = st.session_state.site_scenarios.get(site_id, "正常稼働")

                default_idx = 0
                for idx, s in enumerate(scenarios):
                    if s == current or current in s:
                        default_idx = idx
                        break

                selected = st.radio(
                    "シナリオ",
                    scenarios,
                    index=default_idx,
                    key=f"scenario_{site_id}",
                    label_visibility="collapsed"
                )

                if selected != current:
                    st.session_state.site_scenarios[site_id] = selected

                    # ★ 予兆シミュレーションとの競合を防ぐため自動クリア
                    injected = st.session_state.get("injected_weak_signal")
                    if injected and selected != "正常稼働":
                        # 障害シナリオ選択時は予兆シミュレーションをクリア
                        st.session_state["injected_weak_signal"] = None
                        # ★ 劣化進行度スライダーも0にリセット
                        st.session_state["reset_pred_level"] = True

                        # ★ 関連するセッションステートキーも完全クリア
                        dt_prev_key = f"dt_prev_sim_device_{site_id}"
                        if dt_prev_key in st.session_state:
                            del st.session_state[dt_prev_key]

                        st.info(
                            f"🔄 障害シナリオ「{selected}」を選択したため、"
                            "予兆シミュレーションを自動的にクリアしました。"
                        )

                    # ==========================================================
                    # ★修正: キャッシュとセッションステートの完全クリア
                    # ==========================================================
                    # 1. レポートキャッシュのクリア
                    keys_to_remove =[k for k in list(st.session_state.report_cache.keys()) if site_id in k]
                    for k in keys_to_remove:
                        del st.session_state.report_cache[k]

                    # 2. 予測APIキャッシュのクリア（古い予兆が画面に残るのを防ぐ）
                    if "dt_prediction_cache" in st.session_state:
                        st.session_state.dt_prediction_cache.clear()

                    # 3. アクティブな画面のステートリセット
                    if st.session_state.get("active_site") == site_id:
                        st.session_state.generated_report = None
                        st.session_state.remediation_plan = None
                        st.session_state.messages =[]
                        st.session_state.chat_session = None
                        st.session_state.live_result = None
                        st.session_state.verification_result = None

                    # ★ 重要：セッションステート変更後は必ず rerun
                    st.rerun()

        st.divider()

        with st.expander("🛠️ メンテナンス設定", expanded=False):
            for site_id in sites:
                display_name = get_display_name(site_id)
                is_maint = st.checkbox(
                    display_name,
                    value=st.session_state.maint_flags.get(site_id, False),
                    key=f"maint_{site_id}"
                )
                st.session_state.maint_flags[site_id] = is_maint

                # ★ Phase 1: 機器単位メンテナンスモード
                if not is_maint:
                    _topo = load_topology(get_paths(site_id).topology_path)
                    if _topo:
                        _dev_ids = sorted(_topo.keys())
                        _current_maint = st.session_state.maint_devices.get(site_id, set())
                        # set → list 変換（multiselect用）
                        _current_list = [d for d in _dev_ids if d in _current_maint]
                        _selected = st.multiselect(
                            f"メンテ中機器 ({display_name})",
                            options=_dev_ids,
                            default=_current_list,
                            key=f"maint_dev_{site_id}",
                            label_visibility="collapsed",
                            placeholder="機器を選択してメンテモードに設定...",
                        )
                        st.session_state.maint_devices[site_id] = set(_selected)
                        if _selected:
                            st.caption(f"🔧 {len(_selected)}台がメンテ中（アラーム抑制）")

            # ── Phase 2: メンテナンスウィンドウ（時間帯指定）──
            st.markdown("---")
            st.markdown("**📅 メンテナンスウィンドウ**")

            # 登録済みウィンドウの一覧表示
            from datetime import datetime, timedelta
            _now = datetime.now()
            _windows = st.session_state.get("maint_windows", [])

            if _windows:
                for _w in list(_windows):
                    _wid = _w.get("id", "")
                    _w_start = _w.get("start")
                    _w_end = _w.get("end")
                    _w_label = _w.get("label", "")
                    _w_site = _w.get("site_id", "")
                    _w_devs = _w.get("device_ids", set())

                    if _now > _w_end:
                        _status = "⏹ 終了"
                        _color = "#9E9E9E"
                    elif _now >= _w_start:
                        _status = "🟢 アクティブ"
                        _color = "#4CAF50"
                    else:
                        _status = "⏳ 予定"
                        _color = "#FF9800"

                    _dev_str = ", ".join(sorted(_w_devs)) if _w_devs else "拠点全体"
                    _start_str = _w_start.strftime("%m/%d %H:%M")
                    _end_str = _w_end.strftime("%m/%d %H:%M")

                    _col_info, _col_del = st.columns([5, 1])
                    with _col_info:
                        st.markdown(
                            f'<div style="font-size:12px;border-left:3px solid {_color};'
                            f'padding:4px 8px;margin:2px 0;">'
                            f'<b>{_status}</b> {_start_str}〜{_end_str}<br>'
                            f'<span style="color:#666;">{_w_label or _dev_str}</span>'
                            f'</div>',
                            unsafe_allow_html=True,
                        )
                    with _col_del:
                        if st.button("✕", key=f"mw_del_{_wid}", help="削除"):
                            st.session_state["maint_windows"] = [
                                w for w in _windows if w.get("id") != _wid
                            ]
                            st.rerun()
            else:
                st.caption("登録なし")

            # ウィンドウ追加フォーム
            with st.popover("+ ウィンドウ追加", use_container_width=True):
                _add_site = st.selectbox(
                    "拠点", options=sites,
                    format_func=get_display_name,
                    key="_mw_add_site",
                )
                _add_label = st.text_input("ラベル", placeholder="例: FW更新メンテ", key="_mw_add_label")

                _col_sd, _col_st = st.columns(2)
                with _col_sd:
                    _add_sdate = st.date_input("開始日", value=_now.date(), key="_mw_add_sdate")
                with _col_st:
                    _add_stime = st.time_input("開始時刻", value=_now.replace(minute=0, second=0).time(), key="_mw_add_stime")

                _col_ed, _col_et = st.columns(2)
                with _col_ed:
                    _add_edate = st.date_input("終了日", value=(_now + timedelta(hours=4)).date(), key="_mw_add_edate")
                with _col_et:
                    _add_etime = st.time_input("終了時刻", value=(_now + timedelta(hours=4)).replace(minute=0, second=0).time(), key="_mw_add_etime")

                # 対象機器（空=拠点全体）
                _add_topo = load_topology(get_paths(_add_site).topology_path)
                _add_dev_opts = sorted(_add_topo.keys()) if _add_topo else []
                _add_devs = st.multiselect(
                    "対象機器（空=拠点全体）",
                    options=_add_dev_opts,
                    key="_mw_add_devs",
                )

                if st.button("登録", key="_mw_add_submit", type="primary", use_container_width=True):
                    _start_dt = datetime.combine(_add_sdate, _add_stime)
                    _end_dt = datetime.combine(_add_edate, _add_etime)
                    if _end_dt <= _start_dt:
                        st.error("終了は開始より後に設定してください")
                    else:
                        _new_window = {
                            "id": f"mw_{int(_start_dt.timestamp())}_{_add_site}",
                            "site_id": _add_site,
                            "device_ids": set(_add_devs),
                            "start": _start_dt,
                            "end": _end_dt,
                            "label": _add_label,
                        }
                        st.session_state["maint_windows"].append(_new_window)
                        st.rerun()

        st.divider()

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # ★ サービスティア切替（デモ用）
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        from ui.service_tier import TIER_DESCRIPTIONS
        _tier_options = {
            TIER_BASIC:           "Basic — トポロジー + アラート分析",
            TIER_PHM_PREMONITION: "PHM: 予兆検知 — Future Radar / シミュレーション",
            TIER_PHM_RUL:         "PHM: RUL予測 — AI自律診断 / 自動復旧",
            TIER_PHM_TRAFFIC:     "PHM: トラフィック — 帯域監視 / 輻輳予測",
            TIER_FULL:            "Full — 全機能",
        }
        # ★ BugFix: selectbox のウィジェットキーを "service_tier" に統一。
        #   旧コードでは "_service_tier_select"（ウィジェット用）と
        #   "service_tier"（アプリロジック用）の2つのキーを同期していたが、
        #   Streamlit の rerun サイクル中に index パラメータがユーザー選択を
        #   上書きし、Full→Basic 切替に2-3回の選択が必要なバグがあった。
        #   ウィジェットキー = アプリキーとすることで同期問題を根本排除。

        # service_tier を session_state に確実に初期化（selectbox が読む前に）
        if "service_tier" not in st.session_state:
            _env_tier = os.environ.get("SERVICE_TIER", "full").lower().strip()
            if _env_tier == "phm":
                _env_tier = TIER_PHM_PREMONITION  # 後方互換
            st.session_state["service_tier"] = (
                _env_tier if _env_tier in set(ALL_TIERS)
                else TIER_FULL
            )

        def _on_tier_change():
            """ティア切替時のステートクリア。on_change は値確定後・rerun 前に発火。
            ウィジェットキーが "service_tier" なので、コールバック時点で
            st.session_state["service_tier"] は既に新しい値になっている。"""
            for _sid in list_sites():
                # アラームキャッシュを全クリア（シナリオ別キーを網羅）
                _keys_to_del = [
                    k for k in list(st.session_state.keys())
                    if k.startswith(f"_alarm_cache_{_sid}")
                ]
                for k in _keys_to_del:
                    del st.session_state[k]

                # レポートキャッシュのクリア
                _rpt_keys = [k for k in list(st.session_state.report_cache.keys()) if _sid in k]
                for k in _rpt_keys:
                    del st.session_state.report_cache[k]

            # 予測APIキャッシュのクリア
            if "dt_prediction_cache" in st.session_state:
                st.session_state.dt_prediction_cache.clear()

            # アクティブ画面のステートリセット
            st.session_state.generated_report = None
            st.session_state.remediation_plan = None
            st.session_state.messages = []
            st.session_state.chat_session = None
            st.session_state.live_result = None
            st.session_state.verification_result = None

        _current_tier = get_service_tier()

        with st.expander("🔑 サービスティア", expanded=False):
            st.selectbox(
                "SERVICE_TIER",
                options=list(_tier_options.keys()),
                format_func=lambda t: _tier_options[t],
                key="service_tier",
                on_change=_on_tier_change,
                label_visibility="collapsed",
            )

            # 現在のティアの含まれる機能一覧
            _current_tier = get_service_tier()
            st.caption(f"**{_tier_options[_current_tier].split(' — ')[0]}** プラン:")
            st.caption(TIER_DESCRIPTIONS.get(_current_tier, ""))
            if _current_tier != TIER_FULL:
                _cur_idx = ALL_TIERS.index(_current_tier) if _current_tier in ALL_TIERS else 0
                if _cur_idx + 1 < len(ALL_TIERS):
                    _next_tier = ALL_TIERS[_cur_idx + 1]
                    _next_label = _tier_options[_next_tier].split(" — ")[0]
                    st.caption(f"⬆️ **{_next_label}** にアップグレードすると: {TIER_DESCRIPTIONS.get(_next_tier, '')}")

        st.divider()

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # ★ 予兆シミュレーション（デバイス・シナリオ・劣化進行度の一元管理）[PHM tier]
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        from ui.service_tier import render_tier_section
        with render_tier_section(
            TIER_PHM, "予兆シミュレーション", icon="🔮",
            description="対象デバイス・劣化シナリオを選択し、劣化進行度を制御して予兆検知のデモを実行できます。",
        ) as _sim_ok:
            if _sim_ok:
                # --- 統合: 予兆シミュレーション（デバイス + シナリオ + 劣化進行度を一体化） ---
                _render_weak_signal_injection()

            else:
                # PHM未満: デフォルト値を設定（バックエンド側の動作に影響しない）
                target_device = "不明"
                scenario_key = "optical"

        return _render_api_key_input()


def _render_weak_signal_injection():
    """
    予兆シミュレーションUI（統合版）
    対象デバイス・劣化シナリオ・劣化進行度を一体化。
    """
    from ui.shared_sim_config import (
        build_device_options, _get_short_name_map, scenario_display_to_key,
        SIM_DEVICE_KEY, SIM_SCENARIO_KEY,
    )

    with st.expander("🔧 シミュレーション・モード", expanded=False):
        st.caption("⚠️ テスト用: 疑似的なインシデント状態を作り出し、予兆検知をシミュレーションします。")

        # --- 対象デバイス ---
        device_options = build_device_options()
        target_device = st.selectbox(
            "対象デバイス",
            [d[0] for d in device_options],
            format_func=lambda x: next(
                (d[1] for d in device_options if d[0] == x), x
            ),
            key=SIM_DEVICE_KEY,
        )

        # --- 劣化シナリオ ---
        short_names = _get_short_name_map()
        scenario_display_list = list(short_names.values())
        scenario_display = st.selectbox(
            "劣化シナリオ",
            scenario_display_list,
            key=SIM_SCENARIO_KEY,
        )
        scenario_key = scenario_display_to_key(scenario_display)
        scenario_type = scenario_key_to_display(scenario_key)

        # コックピット側からのリセット要求があれば、スライダー描画前に0に戻す
        if st.session_state.get("reset_pred_level"):
            st.session_state["pred_level"] = 0
            st.session_state["reset_pred_level"] = False

        # ★ システム状態: スライダーはシミュレーションの「現実」を制御する
        #   ビュー状態（whatif_phase）とは完全に独立。互いに書き合わない。
        _sim_for_sync = _get_simulator()
        _sim_complete = (_sim_for_sync is not None
                         and _sim_for_sync.is_started
                         and _sim_for_sync.is_complete)

        degradation_level = st.slider(
            "劣化進行度",
            min_value=0, max_value=5, value=0,
            help="0:正常 → 5:障害発生直前。レベルが上がると相関シグナルが増加し、予測精度が向上します。",
            key="pred_level",
        )

        # --- リアルなログメッセージ生成 ---
        # ★ シード固定で同一レベルは同一メッセージを生成
        import random as _rng
        _seed = hash(f"{target_device}_{scenario_key}_{degradation_level}")
        _rng_local = _rng.Random(_seed)

        log_messages = []
        if degradation_level > 0:
            if scenario_key == "optical":
                optical_interfaces = [
                    "Gi0/0/1", "Gi0/0/2", "Gi0/0/3", "Gi0/0/4",
                    "Te1/0/1", "Te1/0/2", "Te1/0/3", "Te1/0/4"
                ]

                num_affected = min(degradation_level + (1 if degradation_level >= 5 else 0), len(optical_interfaces))
                selected_interfaces = _rng_local.sample(optical_interfaces, num_affected)

                dbm = -23.0 - (degradation_level * 0.4)

                for i, intf in enumerate(selected_interfaces):
                    if i == 0 or degradation_level >= 3:
                        _msg = (f"%TRANSCEIVER-4-THRESHOLD_VIOLATION: Rx Power {dbm:.1f} dBm on {intf} "
                               f"(optical signal degrading). transceiver rx power below threshold.")
                        log_messages.append(_msg)

                    if (i == 1 or degradation_level >= 4) and len(log_messages) < degradation_level:
                        _msg = (f"%OPTICAL-3-SIGNAL_WARN: optical signal level degrading on {intf}. "
                               f"light level {dbm+1.5:.1f} dBm. transceiver rx power loss detected.")
                        log_messages.append(_msg)

            elif scenario_key == "microburst":
                data_interfaces = [
                    "Gi0/1/0", "Gi0/1/1", "Gi0/1/2", "Gi0/1/3",
                    "Gi0/1/4", "Gi0/1/5", "Gi0/1/6", "Gi0/1/7"
                ]

                num_affected = min(degradation_level + (0 if degradation_level < 5 else 1), len(data_interfaces))
                selected_interfaces = _rng_local.sample(data_interfaces, num_affected)

                drops = degradation_level * 200

                for i, intf in enumerate(selected_interfaces):
                    if i == 0 or degradation_level >= 3:
                        _msg = (f"%HARDWARE-3-ASIC_ERROR: asic_error queue drops detected on {intf} "
                               f"(Count: {drops}). output drops on burst traffic.")
                        log_messages.append(_msg)

                    if (i == 1 or degradation_level >= 4) and len(log_messages) < degradation_level:
                        _msg = (f"%QOS-4-BUFFER: buffer overflow risk on {intf}. "
                               f"queue drops {drops+100}/sec. output drops increasing.")
                        log_messages.append(_msg)

            elif scenario_key == "memory_leak":
                base_mem = 8192

                while len(log_messages) < degradation_level:
                    i = len(log_messages)
                    free_mem = max(128, base_mem - (degradation_level * 1200) + (i * 100))

                    if i % 3 == 0:
                        _msg = (f"%SYS-4-MEMORY_WARN: High memory usage detected. "
                               f"Processor Pool Free: {free_mem}M. Potential memory leak.")
                    elif i % 3 == 1:
                        _msg = (f"%PLATFORM-3-ELEMENT_WARNING: Used Memory value {80 + degradation_level*2}% "
                               f"exceeds warning threshold. System instability risk.")
                    else:
                        _msg = (f"%SYS-2-MALLOCFAIL: Memory allocation of 65536 bytes failed. "
                               f"Pool: Processor, Free: {free_mem}M.")

                    log_messages.append(_msg)


        # Session State に保存
        if log_messages:
            # ★ 障害シナリオとの競合チェック
            active_site = st.session_state.get("active_site")
            if active_site:
                current_scenario = st.session_state.site_scenarios.get(active_site, "正常稼働")
                if current_scenario != "正常稼働":
                    st.error(
                        f"⛔ **競合エラー**\n\n"
                        f"現在、拠点 `{active_site}` では障害シナリオ「**{current_scenario}**」が実行中です。\n"
                        "予兆シミュレーションは **正常稼働時** にのみ有効です。\n\n"
                        "💡 対処方法:\n"
                        "1. 拠点シナリオ設定を「**正常稼働**」に戻す\n"
                        "2. 予兆シミュレーションを実行\n"
                        "3. （オプション）その後、障害シナリオに切り替えて予兆の的中を確認"
                    )
                    st.session_state["injected_weak_signal"] = None
                    return  # 早期終了

            # ★ 古い予兆をクリア（レベル変更時のみ）
            _prev_injected = st.session_state.get("injected_weak_signal")
            _level_changed = (
                _prev_injected
                and _prev_injected.get("device_id") == target_device
                and _prev_injected.get("level") != degradation_level
            )
            if _level_changed:
                # ★ 高速化: DB DELETE を除去（_forecast_record 内で simulation 行は
                #   自動クリーンアップされるため、ここでの二重削除は不要）
                # cockpit キャッシュのみクリア（新レベルで再計算させる）
                st.session_state.pop("dt_prediction_cache", None)
                # ★ BugFix: トリアージキャッシュ + インライン結果をクリア
                #   （_analysis_cache は意図的にクリアしない:
                #    cockpitのhashはINFO除外のため変わらず、
                #    prediction_pipeline.pyのスライス代入で古い予測は自動除去される。
                #    _analysis_cacheをクリアするとanalyze()が毎回フル実行され
                #    GrayScope/Granger/LLM呼出で数秒の遅延が発生する）
                _keys_to_clear = [k for k in list(st.session_state.keys())
                                  if k.startswith("_triage_pred_")
                                  or k.startswith("_triage_inline_")]
                for k in _keys_to_clear:
                    st.session_state.pop(k, None)

            st.info(f"💉 **{len(log_messages)}件のシグナル準備済み** (Level {degradation_level}/5)")
            for i, msg in enumerate(log_messages, 1):
                disp_msg = f"{msg[:80]}..." if len(msg) > 80 else msg
                st.caption(f"{i}. `{disp_msg}`")

            # ★ 発報ボタン: 押下で初めて予兆ステータス履歴にキューイング
            if st.button("💉 予兆アラートを発報", key="sim_dispatch_btn",
                         type="primary", use_container_width=True):
                import time
                signal_data = {
                    "device_id": target_device,
                    "messages": log_messages,
                    "message": log_messages[0],
                    "level": degradation_level,
                    "scenario": scenario_type,
                    "created_at": time.time(),
                }
                st.session_state["injected_weak_signal"] = signal_data
                # ★ 履歴リスト(インボックス)へのデータ追加
                if "alert_history" not in st.session_state:
                    st.session_state["alert_history"] = []
                st.session_state["alert_history"].insert(0, signal_data)
                # キャッシュクリア（新しい発報で再計算させる）
                st.session_state.pop("dt_prediction_cache", None)
                _keys_to_clear = [k for k in list(st.session_state.keys())
                                  if k.startswith("_triage_pred_")
                                  or k.startswith("_triage_inline_")]
                for k in _keys_to_clear:
                    st.session_state.pop(k, None)
                st.success(f"予兆アラート（Level {degradation_level}）を発報しました")

            # ★ 連続劣化ストリームの自動開始:
            if not _sim_complete:
                auto_start_stream(target_device, scenario_key, start_level=degradation_level)

            # ストリーム実行中: 最新アラームを session_state に注入
            sim = _get_simulator()
            if sim is not None and sim.is_started and not sim.is_complete:
                inject_stream_alarms_to_session(sim)
        else:
            # ★ level=0: ストリームを停止し、予測キャッシュ + forecast_ledger をクリア
            _clear_simulator()
            st.session_state.pop("stream_completion_result", None)

            _prev_injected = st.session_state.get("injected_weak_signal")
            if _prev_injected is not None:
                # 以前シミュレーションが動いていた → クリーンアップ必要
                _prev_device = _prev_injected.get("device_id", "")
                st.session_state.pop("dt_prediction_cache", None)
                # ★ _analysis_cache は意図的にクリアしない
                #   （prediction_pipeline.pyのスライス代入で古い予測を自動除去。
                #    クリアするとanalyze()フル実行で重大な遅延が発生する）

                # forecast_ledger からシミュレーション由来の open 予測を削除
                active_site = st.session_state.get("active_site")
                if active_site and _prev_device:
                    dt_key = f"dt_engine_{active_site}"
                    if dt_key in st.session_state:
                        dt_engine = st.session_state[dt_key]
                        try:
                            if dt_engine and dt_engine.storage._conn:
                                with dt_engine.storage._db_lock:
                                    dt_engine.storage._conn.execute("""
                                        DELETE FROM forecast_ledger
                                        WHERE device_id=? AND status='open'
                                              AND source='simulation'
                                    """, (_prev_device,))
                                    dt_engine.storage._conn.commit()
                        except Exception:
                            pass

                # トリアージキャッシュ + インライン実行結果もクリア
                _keys_to_clean = [
                    k for k in list(st.session_state.keys())
                    if k.startswith("_triage_pred_") or k.startswith("_triage_inline_")
                ]
                for k in _keys_to_clean:
                    st.session_state.pop(k, None)

            st.session_state["injected_weak_signal"] = None


def _render_api_key_input():
    api_key = None
    if GENAI_AVAILABLE:
        if "GOOGLE_API_KEY" in st.secrets:
            api_key = st.secrets["GOOGLE_API_KEY"]
        else:
            api_key = os.environ.get("GOOGLE_API_KEY")

        if api_key:
            st.success("✅ API 接続済み")
            stats = get_rate_limiter().get_stats()
            st.caption(f"📊 API: {stats['requests_last_minute']}/{stats['rpm_limit']} RPM")
        else:
            st.warning("⚠️ API Key未設定")
            user_key = st.text_input("Google API Key", type="password")
            if user_key:
                api_key = user_key
    return api_key


# ─────────────────────────────────────────────────────────
# LLM 自動振り分け（UIなし — 設計ドキュメント）
# ─────────────────────────────────────────────────────────
# 1つの GOOGLE_API_KEY で2モデルを自動振り分け:
#   スコアリング・判定       → gemma-3-12b-it      (llm_client.py)
#   レポート・推奨アクション → gemma-3-12b-it (engine.py / cockpit.py)
