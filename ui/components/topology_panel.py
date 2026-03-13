# ui/components/topology_panel.py — 左カラム: トポロジー + 影響伝搬 + AI学習ルール + Auto-Diagnostics
import time
import logging
import streamlit as st
from typing import Optional, List

from ui.graph import render_topology_graph, render_impact_graph
from digital_twin_pkg.common import get_downstream_with_hops
from verifier import verify_log_content

logger = logging.getLogger(__name__)


def _compute_downstream_fallback(topology: dict, root_id: str, max_hops: int = 20):
    return get_downstream_with_hops(topology, root_id, max_hops=max_hops)


def render_topology_panel(
    topology: dict,
    alarms: list,
    analysis_results: List[dict],
    selected_incident_candidate: Optional[dict],
    target_device_id: Optional[str],
    dt_engine,
    engine,  # LogicalRCA engine
    scenario: str,
    api_key: Optional[str],
    symptom_devices: Optional[List[dict]] = None,
):
    """左カラム全体を描画: トポロジーマップ + 影響伝搬 + AI学習ルール + Auto-Diagnostics"""

    st.subheader("🌐 Network Topology")
    render_topology_graph(topology, alarms, analysis_results)

    # --- BFS 影響伝搬グラフ ---
    try:
        if (selected_incident_candidate
                and selected_incident_candidate.get('id') != 'SYSTEM'
                and not selected_incident_candidate.get('is_prediction')):
            _impact_rc_id = selected_incident_candidate['id']
            _impact_data = None
            if dt_engine and hasattr(dt_engine, '_get_downstream_impact'):
                try:
                    _impact_data = dt_engine._get_downstream_impact(_impact_rc_id)
                except Exception:
                    pass
            if not _impact_data:
                _impact_data = _compute_downstream_fallback(topology, _impact_rc_id)

            # 派生アラート（symptom）が存在する場合のみ影響伝搬マップを表示
            _has_symptoms = bool(symptom_devices)
            if _impact_data and _has_symptoms:
                with st.expander(f"🌊 影響伝搬マップ: {_impact_rc_id} → {len(_impact_data)}台", expanded=True):
                    render_impact_graph(
                        _impact_rc_id, _impact_data, topology,
                        analysis_results=analysis_results,
                        alarms=alarms,
                    )
            elif _impact_data and not _has_symptoms:
                with st.expander(f"🌊 影響伝搬マップ: {_impact_rc_id}", expanded=False):
                    st.info("✅ 配下デバイスへの影響はありません（冗長系が引き継ぎ中）")
    except Exception as _impact_err:
        logger.warning(f"影響伝搬マップ描画エラー: {_impact_err}")

    # ── AI学習ルール候補 ──
    try:
        _ai_candidates = engine.get_ai_rule_candidates()
        _ai_stats = engine.get_ai_severity_cache_stats()
        _total_learned = _ai_stats.get("total_patterns", 0)

        if _total_learned > 0:
            st.markdown("---")
            _promoted_count = len(_ai_candidates)
            st.subheader("🧠 AI学習ルール")
            st.caption(
                f"学習済みパターン: {_total_learned}件 ｜ "
                f"ルール昇格候補: {_promoted_count}件 "
                f"（同一判定{engine._ai_severity_store.PROMOTION_THRESHOLD}回以上で昇格）"
            )

            if _promoted_count > 0:
                _status_badge = {
                    "RED": "🔴 CRITICAL",
                    "YELLOW": "🟡 WARNING",
                    "GREEN": "🟢 NORMAL",
                }
                _rows = []
                for c in _ai_candidates:
                    _rows.append({
                        "ステータス": _status_badge.get(c["status"], c["status"]),
                        "スコア": f"{c['avg_score']:.2f}",
                        "検出回数": c["hit_count"],
                        "パターン例": c["pattern_sample"][:80],
                        "AI説明": (c.get("narrative") or "")[:60],
                        "初回検出": c.get("first_seen", ""),
                        "最終検出": c.get("last_seen", ""),
                    })
                st.dataframe(_rows, use_container_width=True, hide_index=True)
            else:
                st.info(
                    "AI判定の蓄積中です。同一パターンが"
                    f"{engine._ai_severity_store.PROMOTION_THRESHOLD}回以上"
                    "検出されるとルール候補に昇格します。"
                )
    except Exception as _ai_rule_err:
        logger.debug(f"AI rule candidates display error: {_ai_rule_err}")

    # ── Auto-Diagnostics ──
    st.markdown("---")
    st.subheader("🛠️ Auto-Diagnostics")

    from .diagnostic import run_diagnostic

    if st.button("🚀 診断実行 (Run Diagnostics)", type="primary"):
        if not api_key:
            st.error("API Key Required")
        else:
            with st.status("Agent Operating...", expanded=True) as status_widget:
                st.write("🔌 Connecting to device...")

                _diag_target_id = target_device_id
                if _diag_target_id == "SYSTEM" or not _diag_target_id:
                    _inj = st.session_state.get("injected_weak_signal")
                    if _inj and _inj.get("device_id"):
                        _diag_target_id = _inj.get("device_id")
                    else:
                        _first_router = next((k for k, v in topology.items() if "ROUTER" in k.upper()), None)
                        _diag_target_id = _first_router if _first_router else list(topology.keys())[0]

                target_node_obj = topology.get(_diag_target_id) if _diag_target_id else None

                res = run_diagnostic(scenario, target_node_obj, use_llm=True)
                st.session_state.live_result = res
                if res["status"] == "SUCCESS":
                    st.write("✅ Log Acquired & Sanitized.")
                    status_widget.update(label="Diagnostics Complete!", state="complete", expanded=False)
                    log_content = res.get('sanitized_log', "")
                    st.session_state.verification_result = verify_log_content(log_content)
                    st.session_state.trigger_analysis = True

                    # ★ 結果をポップアップ用データに変換
                    v = st.session_state.verification_result or {}
                    _diag_results = [
                        {
                            "command": "ping / reachability check",
                            "status": "success" if v.get("ping_status") == "OK" else "error",
                            "output": f"Ping Status: {v.get('ping_status', 'N/A')}",
                            "elapsed_sec": 0.5,
                        },
                        {
                            "command": "interface diagnostics",
                            "status": "success" if v.get("interface_status") == "OK" else "error",
                            "output": f"Interface Status: {v.get('interface_status', 'N/A')}",
                            "elapsed_sec": 0.8,
                        },
                        {
                            "command": "hardware health check",
                            "status": "success" if v.get("hardware_status") == "OK" else "error",
                            "output": f"Hardware Status: {v.get('hardware_status', 'N/A')}",
                            "elapsed_sec": 0.3,
                        },
                        {
                            "command": "show logs (sanitized)",
                            "status": "success",
                            "output": res.get("sanitized_log", "No output"),
                            "elapsed_sec": 1.2,
                        },
                    ]
                    from .command_popup import render_command_result_popup
                    render_command_result_popup(
                        f"🛠️ Auto-Diagnostics: {_diag_target_id}",
                        _diag_results,
                    )
                else:
                    st.write("❌ Connection Failed.")
                    status_widget.update(label="Diagnostics Failed", state="error")
            st.rerun()
