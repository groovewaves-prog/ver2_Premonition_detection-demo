# ui/sidebar.py  ―  Streamlit サイドバー UI（予兆シミュレーション・シナリオ設定）
#
# 改修: 予兆シミュレーションと連続劣化ストリームで
#       「対象デバイス」「劣化シナリオ」を共通設定として一元管理する。
import streamlit as st
import os
from registry import list_sites, get_display_name, load_topology, get_paths
from utils.const import SCENARIO_MAP
from utils.llm_helper import get_rate_limiter, GENAI_AVAILABLE
from ui.stream_dashboard import render_stream_controls, _get_simulator, inject_stream_alarms_to_session
from ui.service_tier import tier_has_access, get_service_tier, TIER_BASIC, TIER_PHM, TIER_FULL
from ui.shared_sim_config import (
    render_shared_config,
    scenario_key_to_display,
    SIM_DEVICE_KEY,
    SIM_SCENARIO_KEY,
)

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

        st.divider()

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # ★ サービスティア切替（デモ用）
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        _tier_options = {
            TIER_BASIC: "Basic — トポロジー + アラート分析",
            TIER_PHM:   "PHM — + 予兆検知 / RUL予測",
            TIER_FULL:  "Full — 全機能",
        }
        _current_tier = get_service_tier()
        with st.expander("🔑 サービスティア", expanded=False):
            _selected_tier = st.selectbox(
                "SERVICE_TIER",
                options=list(_tier_options.keys()),
                format_func=lambda t: _tier_options[t],
                index=list(_tier_options.keys()).index(_current_tier),
                key="_service_tier_select",
                label_visibility="collapsed",
            )
            if _selected_tier != _current_tier:
                st.session_state["service_tier"] = _selected_tier
                st.rerun()

        st.divider()

        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        # ★ 共通シミュレーション設定（デバイス・シナリオ一元管理）[PHM tier]
        # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        if tier_has_access(TIER_PHM):
            with st.expander("🎯 シミュレーション対象設定", expanded=True):
                target_device, scenario_key = render_shared_config()

            st.divider()

            # --- 予兆シミュレーション (共通設定を参照) ---
            _render_weak_signal_injection(target_device, scenario_key)

            st.divider()

            # --- 連続劣化ストリーム (共通設定を参照) ---
            _render_stream_section(target_device, scenario_key)
        else:
            # PHM未満: デフォルト値を設定（バックエンド側の動作に影響しない）
            target_device = "不明"
            scenario_key = "optical"

        return _render_api_key_input()


def _render_weak_signal_injection(target_device: str, scenario_key: str):
    """
    予兆シミュレーションUI
    AIエンジンが検知可能なリアルなログメッセージを生成する。
    対象デバイスとシナリオは共通設定から受け取る。
    """
    # scenario_key → 旧表示名への変換（cockpit.py 互換）
    scenario_type = scenario_key_to_display(scenario_key)

    with st.expander("🔮 予兆シミュレーション", expanded=True):
        st.caption("共通設定のデバイス・シナリオで予兆検知をデモします。")

        # 現在の共通設定を表示
        st.info(f"🎯 **{target_device}** | {scenario_type}")

        # コックピット側からのリセット要求があれば、スライダー描画前に0に戻す
        if st.session_state.get("reset_pred_level"):
            st.session_state["pred_level"] = 0
            st.session_state["reset_pred_level"] = False

        degradation_level = st.slider(
            "劣化進行度",
            min_value=0, max_value=5, value=0,
            help="0:正常 → 5:障害発生直前。レベルが上がると相関シグナルが増加し、予測精度が向上します。",
            key="pred_level"
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
                dt_key = f"dt_engine_{active_site}"
                if dt_key in st.session_state:
                    dt_engine = st.session_state[dt_key]
                    try:
                        if dt_engine and dt_engine.storage._conn:
                            with dt_engine.storage._db_lock:
                                dt_engine.storage._conn.execute("""
                                    DELETE FROM forecast_ledger
                                    WHERE device_id=? AND status='open' AND source='simulation'
                                """, (target_device,))
                                dt_engine.storage._conn.commit()
                    except Exception as e:
                        pass
                # ★ cockpit キャッシュもクリア（新レベルで再計算させる）
                st.session_state.pop("dt_prediction_cache", None)

            st.session_state["injected_weak_signal"] = {
                "device_id": target_device,
                "messages": log_messages,
                "message": log_messages[0],
                "level": degradation_level,
                "scenario": scenario_type,
            }
            st.info(f"💉 **{len(log_messages)}件のシグナル注入中** (Level {degradation_level}/5)")
            for i, msg in enumerate(log_messages, 1):
                disp_msg = f"{msg[:80]}..." if len(msg) > 80 else msg
                st.caption(f"{i}. `{disp_msg}`")
        else:
            st.session_state["injected_weak_signal"] = None


def _render_stream_section(target_device: str, scenario_key: str):
    """連続劣化ストリームのサイドバーUI（共通設定を参照）"""
    active = st.session_state.get("active_site")
    site_for_list = active if active else (list_sites()[0] if list_sites() else None)

    render_stream_controls(target_device, scenario_key, site_for_list or "")

    # ストリーム実行中: 最新アラームを session_state に注入
    sim = _get_simulator()
    if sim is not None and sim.is_started and not sim.is_complete:
        inject_stream_alarms_to_session(sim)


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
#   レポート・推奨アクション → gemini-2.0-flash-exp (engine.py / cockpit.py)
