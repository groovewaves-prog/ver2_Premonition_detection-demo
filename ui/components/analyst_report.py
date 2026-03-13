# ui/components/analyst_report.py — AI Analyst Report セクション
import json
import streamlit as st
from typing import Optional

from utils.helpers import load_config_by_id
from network_ops import generate_analyst_report_streaming
from .helpers import st_html, hash_text
from .report_builders import build_prediction_report_scenario
from ui.service_tier import render_tier_gated, tier_has_access, TIER_PHM, TIER_FULL
from ui.autonomous_diagnostic import get_thought_log_for_llm, get_diagnostic_session


def render_analyst_report(
    selected_incident_candidate: Optional[dict],
    topology: dict,
    scenario: str,
    site_id: str,
    api_key: Optional[str],
):
    """AI Analyst Report セクションを描画"""
    st.subheader("📝 AI Analyst Report")

    if not selected_incident_candidate:
        return

    cand = selected_incident_candidate
    is_pred = cand.get('is_prediction')
    st.info(f"インシデント選択中: **{cand['id']}** ({cand.get('label', '')})")

    # ★ Phase 1: トレンド検出情報の表示 [PHM tier]
    _trend_info = cand.get('trend_info')
    if is_pred and _trend_info and _trend_info.get('detected') and tier_has_access(TIER_PHM):
        _t_slope = _trend_info.get('slope', 0)
        _t_r2 = _trend_info.get('r_squared', 0)
        _t_pts = _trend_info.get('data_points', 0)
        _t_latest = _trend_info.get('latest_value')
        _t_ttf = _trend_info.get('estimated_ttf_hours')
        _t_boost = _trend_info.get('confidence_boost', 0)

        _trend_parts = [f"傾き: {_t_slope:+.4f}/h", f"R\u00b2: {_t_r2:.2f}", f"データ: {_t_pts}点"]
        if _t_latest is not None:
            _trend_parts.append(f"最新値: {_t_latest:.1f}")
        if _t_ttf is not None:
            if _t_ttf < 1:
                _trend_parts.append(f"閾値到達: {_t_ttf * 60:.0f}分後")
            else:
                _trend_parts.append(f"閾値到達: {_t_ttf:.1f}時間後")
        if _t_boost > 0:
            _trend_parts.append(f"信頼度ブースト: +{_t_boost:.1%}")

        st.warning(f"📈 **劣化トレンド検出**: {' | '.join(_trend_parts)}")

    # ★ Phase 2-4: 高度分析（Granger因果 / GDN偏差 / GrayScope）[FULL tier]
    if tier_has_access(TIER_FULL):
        # Phase 2: Granger因果関係の表示
        _causality_children = cand.get('causality_children')
        _causality_parents = cand.get('causality_parents')
        if _causality_children:
            _causal_desc = ", ".join(
                f"{c['device']}({c['weight']:.0%})" for c in _causality_children[:3]
            )
            st.info(f"🔗 **因果的影響先**: {_causal_desc}")
        if _causality_parents:
            _causal_desc = ", ".join(
                f"{p['device']}({p['weight']:.0%})" for p in _causality_parents[:3]
            )
            st.info(f"🔗 **因果的影響元**: {_causal_desc}")

        # Phase 3: GDN偏差検出の表示
        _gdn_info = cand.get('gdn_deviation')
        if _gdn_info and _gdn_info.get('anomaly'):
            _gdn_devs = _gdn_info.get('top_deviations', [])
            _gdn_desc = ", ".join(
                f"{name}({val:.1f}σ)" for name, val in _gdn_devs[:3]
            ) if _gdn_devs else ""
            _gdn_text = f"スコア: {_gdn_info['score']:.2f}"
            if _gdn_desc:
                _gdn_text += f" | 逸脱: {_gdn_desc}"
            if _gdn_info.get('boost', 0) > 0:
                _gdn_text += f" | 信頼度+{_gdn_info['boost']:.1%}"
            st.warning(f"🔬 **ベースライン偏差検出**: {_gdn_text}")

        # Phase 4: GrayScope サイレント障害分析の表示
        _gs_info = cand.get('grayscope_info')
        if _gs_info and _gs_info.get('score', 0) >= 0.3:
            _gs_parts = [f"スコア: {_gs_info['score']:.0%}"]
            if _gs_info.get('affected_ratio', 0) > 0:
                _gs_parts.append(f"配下影響: {_gs_info['affected_ratio']:.0%}")
            _gs_signals = _gs_info.get('implicit_signals', [])
            if _gs_signals:
                _gs_parts.append(f"兆候: {', '.join(_gs_signals[:2])}")
            st.error(f"🔍 **GrayScope サイレント障害分析**: {' | '.join(_gs_parts)}")
            if _gs_info.get('recommendation'):
                st.caption(f"💡 推奨: {_gs_info['recommendation']}")

        # Phase 4: GrayScope メトリクス相関の表示
        _gs_corrs = cand.get('grayscope_correlations')
        if _gs_corrs:
            _corr_desc = ", ".join(
                f"{c['source']}↔{c['target']}({c['correlation']:+.2f})"
                for c in _gs_corrs[:3]
            )
            st.info(f"📊 **メトリクス相関**: {_corr_desc}")

        # Phase 4: GrayScope推奨（inference_engine経由）
        _gs_evidence = cand.get('grayscope_evidence')
        if _gs_evidence and not _gs_info:
            _ev_parts = []
            if _gs_evidence.get('child_alarm_ratio', 0) > 0:
                _ev_parts.append(f"配下アラーム: {_gs_evidence['child_alarm_ratio']:.0%}")
            if _gs_evidence.get('granger_causality', 0) > 0:
                _ev_parts.append(f"因果: {_gs_evidence['granger_causality']:.2f}")
            if _gs_evidence.get('trend_degradation', 0) > 0:
                _ev_parts.append(f"トレンド: {_gs_evidence['trend_degradation']:.2f}")
            if _gs_evidence.get('gdn_deviation', 0) > 0:
                _ev_parts.append(f"GDN: {_gs_evidence['gdn_deviation']:.2f}")
            if _ev_parts:
                st.error(f"🔍 **GrayScope サイレント障害検出**: {' | '.join(_ev_parts)}")
            _gs_rec = cand.get('grayscope_recommendation', '')
            if _gs_rec:
                st.caption(f"💡 推奨: {_gs_rec}")

    # ★ エッセンス6: AI自律診断の思考ログ表示
    _diag_session = get_diagnostic_session(cand.get("id", ""))
    if _diag_session and _diag_session.is_complete:
        with st.expander("🧠 AI診断プロセス（思考ログ）", expanded=False):
            for _step in _diag_session.steps:
                _step_icons = {
                    "plan": "🧠", "execute": "⚡",
                    "analyze": "🔍", "conclude": "📋",
                }
                _icon = _step_icons.get(_step.step_type, "❓")
                st.markdown(
                    f"<div style='font-size:13px;padding:2px 0;'>"
                    f"{_icon} <b>R{_step.round_num}</b>: {_step.description}</div>",
                    unsafe_allow_html=True,
                )
                if _step.insights:
                    for _ins in _step.insights:
                        st.markdown(
                            f"<div style='font-size:12px;color:#555;padding:1px 0 1px 20px;'>{_ins}</div>",
                            unsafe_allow_html=True,
                        )

    if is_pred:
        st.caption(
            "📋 **ステップ②**: 初動トリアージの次に実施する詳細診断。"
            "出力の読み方・OK/NG判定基準・エスカレーション判断を提示します。"
        )

    if st.session_state.generated_report is None:
        if api_key and (scenario != "正常稼働" or is_pred):
            if is_pred:
                btn_label = "🔮 予兆の確認手順を生成 (Predictive Analysis)"
            else:
                btn_label = "📝 詳細レポートを作成 (Generate Report)"

            if st.button(btn_label):
                report_container = st.empty()
                target_conf = load_config_by_id(cand['id'])
                verification_context = cand.get("verification_log", "特になし")

                # ★ 思考ログをレポートコンテキストに注入
                _diag_log = get_thought_log_for_llm(cand["id"])
                if _diag_log:
                    verification_context = f"{verification_context}\n\n{_diag_log}"

                t_node = topology.get(cand["id"])
                t_node_dict = {
                    "id":       getattr(t_node, "id",       None) if t_node else None,
                    "type":     getattr(t_node, "type",     None) if t_node else None,
                    "layer":    getattr(t_node, "layer",    None) if t_node else None,
                    "metadata": (getattr(t_node, "metadata", {}) or {}) if t_node else {},
                }
                parent_id = t_node.parent_id if t_node and hasattr(t_node, 'parent_id') else None
                children_ids = [
                    nid for nid, n in topology.items()
                    if (getattr(n, "parent_id", None) if hasattr(n, 'parent_id')
                        else n.get('parent_id')) == cand["id"]
                ]
                topology_context = {
                    "node": t_node_dict,
                    "parent_id": parent_id,
                    "children_ids": children_ids
                }

                report_scenario = scenario
                if is_pred:
                    _sig_count = cand.get('prediction_signal_count', 1)
                    report_scenario = build_prediction_report_scenario(cand, _sig_count)

                cache_key_analyst = "|".join([
                    "analyst", site_id, scenario,
                    str(cand.get("id")),
                    hash_text(json.dumps(topology_context, ensure_ascii=False, sort_keys=True)),
                    hash_text(report_scenario),
                ])

                if cache_key_analyst in st.session_state.report_cache:
                    full_text = st.session_state.report_cache[cache_key_analyst]
                    report_container.markdown(full_text)
                else:
                    try:
                        report_container.write("🤖 AI 分析中...")
                        placeholder = report_container.empty()
                        full_text = ""

                        for chunk in generate_analyst_report_streaming(
                            scenario=report_scenario,
                            target_node=t_node,
                            topology_context=topology_context,
                            target_conf=target_conf or "なし",
                            verification_context=verification_context,
                            api_key=api_key,
                            max_retries=2,
                            backoff=3,
                            is_prediction=is_pred,
                        ):
                            full_text += chunk
                            placeholder.markdown(full_text)

                        if not full_text or full_text.startswith("Error"):
                            full_text = f"⚠️ 分析レポート生成に失敗しました: {full_text}"
                            placeholder.markdown(full_text)

                        st.session_state.report_cache[cache_key_analyst] = full_text
                    except Exception as e:
                        full_text = f"⚠️ 分析レポート生成に失敗しました: {type(e).__name__}: {e}"
                        report_container.markdown(full_text)

                st.session_state.generated_report = full_text
    else:
        with st.container(height=400, border=True):
            st.markdown(st.session_state.generated_report)
        if st.button("🔄 レポート再作成"):
            st.session_state.generated_report = None
            st.session_state.remediation_plan = None
            st.session_state.report_cache = {}
            st.rerun()
