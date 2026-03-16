# ui/components/unified_pipeline.py — 統合診断パイプライン ②→③→④
#
# ①初期確認 は root_cause_table.py が担当（ステップ①ラベル付き）
# 本コンポーネントは ②AI診断 → ③確認手順書 → ④予防措置プラン を
# 1つのパネルに統合し、シーケンシャルに実行する。
import json
import streamlit as st
import logging
from typing import Optional

from utils.helpers import load_config_by_id
from network_ops import (
    generate_analyst_report_streaming,
    generate_remediation_commands_streaming,
)
from .helpers import st_html, hash_text, build_ci_context_for_chat
from .report_builders import build_prediction_report_scenario, build_prevention_plan_scenario
from .command_popup import format_triage_results_for_llm
from ui.service_tier import tier_has_access, TIER_PHM, TIER_FULL
from ui.autonomous_diagnostic import (
    get_thought_log_for_llm,
    get_diagnostic_session,
    run_autonomous_diagnostic,
    _render_thought_log,
)

logger = logging.getLogger(__name__)


# =====================================================
# ステップ進行状態の判定
# =====================================================

def _step_status(cand: dict, device_id: str):
    """各ステップの完了状態を返す。"""
    # ① 初期確認
    has_triage = bool(cand.get('recommended_actions'))
    if not has_triage:
        _triage_ck = f"_triage_incident_{device_id}_{hash(cand.get('label', '')[:200])}"
        has_triage = bool(st.session_state.get(_triage_ck))

    # ② AI診断
    diag_session = get_diagnostic_session(device_id)
    has_diag = diag_session is not None and diag_session.is_complete

    # ③ 確認手順書
    has_report = st.session_state.get("generated_report") is not None

    # ④ 予防措置プラン
    has_prevention = st.session_state.get("remediation_plan") is not None

    return has_triage, has_diag, has_report, has_prevention


def _render_step_progress(has_triage, has_diag, has_report, has_prevention):
    """ステップ進行バーを描画する。"""
    steps = [
        ("①", "初期確認", has_triage),
        ("②", "AI診断", has_diag),
        ("③", "確認手順書", has_report),
        ("④", "予防措置プラン", has_prevention),
    ]

    parts = []
    for num, label, done in steps:
        if done:
            parts.append(
                f'<span style="background:#4CAF50;color:white;padding:3px 10px;'
                f'border-radius:12px;font-size:13px;font-weight:600;">'
                f'{num} {label} ✓</span>'
            )
        else:
            parts.append(
                f'<span style="background:#E0E0E0;color:#666;padding:3px 10px;'
                f'border-radius:12px;font-size:13px;">'
                f'{num} {label}</span>'
            )

    arrow = ' <span style="color:#999;font-size:16px;">→</span> '
    st.markdown(
        f'<div style="display:flex;align-items:center;gap:4px;flex-wrap:wrap;margin-bottom:12px;">'
        f'{arrow.join(parts)}</div>',
        unsafe_allow_html=True,
    )


# =====================================================
# メインレンダラー
# =====================================================

def render_unified_pipeline(
    selected_incident_candidate: Optional[dict],
    topology: dict,
    scenario: str,
    site_id: str,
    api_key: Optional[str],
):
    """統合診断パイプライン: ②AI診断 → ③確認手順書 → ④予防措置プラン

    ①初期確認 は root_cause_table.py が「ステップ①」として描画済み。
    本関数は残りの②③④を順にガイドする。
    """
    st.subheader("📋 診断パイプライン")

    if not selected_incident_candidate:
        st.caption("根本原因候補を選択すると、診断パイプラインが開始されます。")
        return

    cand = selected_incident_candidate
    device_id = cand.get('id', '')
    is_pred = cand.get('is_prediction', False)

    if device_id == "SYSTEM":
        st.caption("正常稼働中のため診断は不要です。")
        return

    # 進行状態
    has_triage, has_diag, has_report, has_prevention = _step_status(cand, device_id)
    _render_step_progress(has_triage, has_diag, has_report, has_prevention)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 高度分析結果（トレンド / Granger / GDN / GrayScope）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    _render_advanced_analysis(cand, is_pred)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # ステップ②: AI診断（コマンド実行 + 結果分析）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    _render_step2_diagnosis(cand, topology, scenario, device_id, has_diag)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # ステップ③: 確認手順書（LLM生成）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    _render_step3_report(cand, topology, scenario, site_id, api_key, device_id, is_pred, has_diag, has_report)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # ステップ④: 予防措置プラン
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    _render_step4_prevention(cand, topology, scenario, site_id, api_key, device_id, is_pred, has_report, has_prevention)


# =====================================================
# 高度分析結果（analyst_report.py から移植）
# =====================================================

def _render_advanced_analysis(cand: dict, is_pred: bool):
    """トレンド / Granger因果 / GDN偏差 / GrayScope の情報を描画。"""
    has_any = False

    # トレンド検出
    _trend_info = cand.get('trend_info')
    if is_pred and _trend_info and _trend_info.get('detected') and tier_has_access(TIER_PHM):
        has_any = True

    # 高度分析
    if tier_has_access(TIER_FULL):
        if (cand.get('causality_children') or cand.get('causality_parents') or
            (cand.get('gdn_deviation') and cand['gdn_deviation'].get('anomaly')) or
            (cand.get('grayscope_info') and cand['grayscope_info'].get('score', 0) >= 0.3) or
            cand.get('grayscope_correlations') or cand.get('grayscope_evidence')):
            has_any = True

    if not has_any:
        return

    with st.expander("📊 高度分析結果", expanded=False):
        # トレンド検出
        if is_pred and _trend_info and _trend_info.get('detected') and tier_has_access(TIER_PHM):
            _t_parts = [f"傾き: {_trend_info.get('slope', 0):+.4f}/h",
                        f"R²: {_trend_info.get('r_squared', 0):.2f}",
                        f"データ: {_trend_info.get('data_points', 0)}点"]
            _t_latest = _trend_info.get('latest_value')
            if _t_latest is not None:
                _t_parts.append(f"最新値: {_t_latest:.1f}")
            _t_ttf = _trend_info.get('estimated_ttf_hours')
            if _t_ttf is not None:
                _t_parts.append(f"閾値到達: {_t_ttf * 60:.0f}分後" if _t_ttf < 1 else f"閾値到達: {_t_ttf:.1f}時間後")
            if _trend_info.get('confidence_boost', 0) > 0:
                _t_parts.append(f"信頼度ブースト: +{_trend_info['confidence_boost']:.1%}")
            st.warning(f"📈 **劣化トレンド検出**: {' | '.join(_t_parts)}")

        if tier_has_access(TIER_FULL):
            # Granger因果
            for key, label, icon in [('causality_children', '因果的影響先', '🔗'),
                                     ('causality_parents', '因果的影響元', '🔗')]:
                items = cand.get(key)
                if items:
                    desc = ", ".join(f"{c['device']}({c['weight']:.0%})" for c in items[:3])
                    st.info(f"{icon} **{label}**: {desc}")

            # GDN偏差
            _gdn = cand.get('gdn_deviation')
            if _gdn and _gdn.get('anomaly'):
                _gdn_devs = _gdn.get('top_deviations', [])
                _gdn_desc = ", ".join(f"{n}({v:.1f}σ)" for n, v in _gdn_devs[:3]) if _gdn_devs else ""
                _gdn_text = f"スコア: {_gdn['score']:.2f}"
                if _gdn_desc:
                    _gdn_text += f" | 逸脱: {_gdn_desc}"
                if _gdn.get('boost', 0) > 0:
                    _gdn_text += f" | 信頼度+{_gdn['boost']:.1%}"
                st.warning(f"🔬 **ベースライン偏差検出**: {_gdn_text}")

            # GrayScope
            _gs = cand.get('grayscope_info')
            if _gs and _gs.get('score', 0) >= 0.3:
                _gs_parts = [f"スコア: {_gs['score']:.0%}"]
                if _gs.get('affected_ratio', 0) > 0:
                    _gs_parts.append(f"配下影響: {_gs['affected_ratio']:.0%}")
                _gs_sigs = _gs.get('implicit_signals', [])
                if _gs_sigs:
                    _gs_parts.append(f"兆候: {', '.join(_gs_sigs[:2])}")
                st.error(f"🔍 **GrayScope サイレント障害分析**: {' | '.join(_gs_parts)}")
                if _gs.get('recommendation'):
                    st.caption(f"💡 推奨: {_gs['recommendation']}")

            _gs_corrs = cand.get('grayscope_correlations')
            if _gs_corrs:
                _corr_desc = ", ".join(
                    f"{c['source']}↔{c['target']}({c['correlation']:+.2f})" for c in _gs_corrs[:3]
                )
                st.info(f"📊 **メトリクス相関**: {_corr_desc}")

            _gs_ev = cand.get('grayscope_evidence')
            if _gs_ev and not _gs:
                _ev_parts = []
                if _gs_ev.get('child_alarm_ratio', 0) > 0:
                    _ev_parts.append(f"配下アラーム: {_gs_ev['child_alarm_ratio']:.0%}")
                if _gs_ev.get('granger_causality', 0) > 0:
                    _ev_parts.append(f"因果: {_gs_ev['granger_causality']:.2f}")
                if _gs_ev.get('trend_degradation', 0) > 0:
                    _ev_parts.append(f"トレンド: {_gs_ev['trend_degradation']:.2f}")
                if _gs_ev.get('gdn_deviation', 0) > 0:
                    _ev_parts.append(f"GDN: {_gs_ev['gdn_deviation']:.2f}")
                if _ev_parts:
                    st.error(f"🔍 **GrayScope サイレント障害検出**: {' | '.join(_ev_parts)}")
                _gs_rec = cand.get('grayscope_recommendation', '')
                if _gs_rec:
                    st.caption(f"💡 推奨: {_gs_rec}")


# =====================================================
# ステップ②: AI診断
# =====================================================

def _render_step2_diagnosis(cand, topology, scenario, device_id, has_diag):
    """ステップ②: AI診断（コマンド実行 + 結果分析）を描画。"""
    import hashlib

    alarm_label = cand.get("label", "")
    diag_session = get_diagnostic_session(device_id)

    with st.expander("② AI診断（コマンド実行 + 結果分析）", expanded=not has_diag):
        if diag_session and diag_session.is_complete:
            _render_thought_log(diag_session)
            if st.button("🔄 再診断", key=f"rediag_pipe_{device_id}"):
                from ui.autonomous_diagnostic import _get_sessions
                _get_sessions().pop(device_id, None)
                st.rerun()
        else:
            st.caption(
                "AIエージェントがアラーム内容を分析し、診断コマンドを自動で計画・実行・解析します。"
            )
            if st.button(
                "▶ AI診断を開始",
                key=f"start_diag_pipe_{device_id}",
                type="primary",
            ):
                _node = topology.get(device_id)
                _dev_type = ""
                if _node:
                    _dev_type = getattr(_node, "type", "") or (
                        _node.get("type", "") if isinstance(_node, dict) else ""
                    )
                with st.spinner("🤖 AI診断を実行中..."):
                    run_autonomous_diagnostic(
                        device_id=device_id,
                        alarm_label=alarm_label,
                        scenario=scenario,
                        analysis_result=cand,
                        device_type=_dev_type,
                    )
                st.rerun()


# =====================================================
# ステップ③: 確認手順書
# =====================================================

def _render_step3_report(cand, topology, scenario, site_id, api_key, device_id, is_pred, has_diag, has_report):
    """ステップ③: 確認手順書（LLM生成）を描画。"""
    with st.expander("③ 確認手順書（診断ワークブック）", expanded=has_diag and not has_report):
        if not has_diag:
            st.caption("⏳ ステップ②のAI診断が完了すると、確認手順書を生成できます。")
            return

        if has_report:
            # 既に生成済み — 表示
            with st.container(height=400, border=True):
                st.markdown(st.session_state.generated_report)
            if st.button("🔄 手順書を再作成", key=f"regen_report_pipe_{device_id}"):
                st.session_state.generated_report = None
                st.session_state.remediation_plan = None
                st.session_state.report_cache = {}
                st.rerun()
            return

        st.caption(
            "ステップ②の診断結果を踏まえた詳細診断手順を生成します。"
            "出力の読み方・OK/NG判定基準・エスカレーション判断を提示します。"
        )

        if not api_key:
            st.warning("APIキーが設定されていません。サイドバーからAPIキーを入力してください。")
            return

        if scenario == "正常稼働" and not is_pred:
            st.info("正常稼働中のため、確認手順書の生成は不要です。")
            return

        btn_label = "📋 確認手順書を生成" if is_pred else "📋 詳細レポートを作成"
        if st.button(btn_label, key=f"gen_report_pipe_{device_id}", type="primary"):
            _generate_step3_report(cand, topology, scenario, site_id, api_key, device_id, is_pred)


def _generate_step3_report(cand, topology, scenario, site_id, api_key, device_id, is_pred):
    """ステップ③のレポートを実際に生成する。"""
    report_container = st.empty()
    target_conf = load_config_by_id(device_id)
    verification_context = cand.get("verification_log", "特になし")

    # AI診断の思考ログを注入
    _diag_log = get_thought_log_for_llm(device_id)
    if _diag_log:
        verification_context = f"{verification_context}\n\n{_diag_log}"

    t_node = topology.get(device_id)
    t_node_dict = {
        "id": getattr(t_node, "id", None) if t_node else None,
        "type": getattr(t_node, "type", None) if t_node else None,
        "layer": getattr(t_node, "layer", None) if t_node else None,
        "metadata": (getattr(t_node, "metadata", {}) or {}) if t_node else {},
    }
    parent_id = t_node.parent_id if t_node and hasattr(t_node, 'parent_id') else None
    children_ids = [
        nid for nid, n in topology.items()
        if (getattr(n, "parent_id", None) if hasattr(n, 'parent_id')
            else n.get('parent_id')) == device_id
    ]
    topology_context = {
        "node": t_node_dict,
        "parent_id": parent_id,
        "children_ids": children_ids,
    }

    report_scenario = scenario
    if is_pred:
        _sig_count = cand.get('prediction_signal_count', 1)
        report_scenario = build_prediction_report_scenario(cand, _sig_count)

    cache_key = "|".join([
        "analyst", site_id, scenario,
        str(device_id),
        hash_text(json.dumps(topology_context, ensure_ascii=False, sort_keys=True)),
        hash_text(report_scenario),
    ])

    if cache_key in st.session_state.report_cache:
        full_text = st.session_state.report_cache[cache_key]
        report_container.markdown(full_text)
    else:
        try:
            report_container.write("🤖 確認手順書を生成中...")
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
                full_text = f"⚠️ 確認手順書の生成に失敗しました: {full_text}"
                placeholder.markdown(full_text)

            st.session_state.report_cache[cache_key] = full_text
        except Exception as e:
            full_text = f"⚠️ 確認手順書の生成に失敗しました: {type(e).__name__}: {e}"
            report_container.markdown(full_text)

    st.session_state.generated_report = full_text
    st.rerun()


# =====================================================
# ステップ④: 予防措置プラン
# =====================================================

def _render_step4_prevention(cand, topology, scenario, site_id, api_key, device_id, is_pred, has_report, has_prevention):
    """ステップ④: 予防措置プラン（メンテナンス作業計画書）を描画。"""
    with st.expander("④ 予防措置プラン", expanded=has_report and not has_prevention):
        if not has_report:
            st.caption("⏳ ステップ③の確認手順書が完了すると、予防措置プランを生成できます。")
            return

        if has_prevention:
            with st.container(height=400, border=True):
                st.markdown(st.session_state.remediation_plan)
            if st.button("🔄 プランを再作成", key=f"regen_prev_pipe_{device_id}"):
                st.session_state.remediation_plan = None
                st.session_state.report_cache = {
                    k: v for k, v in st.session_state.report_cache.items()
                    if not k.startswith("remediation")
                }
                st.rerun()
            return

        st.caption(
            "ステップ③の診断結果を踏まえたメンテナンス作業計画書を生成します。"
            "config系の予防コマンドを含み、「復旧実行」ボタンで自動実行できます。"
        )

        # トリアージ結果連携
        _has_triage_results = bool(format_triage_results_for_llm(device_id))
        if _has_triage_results:
            st.caption("✅ 初期確認の実行結果を検出しました。プランに自動反映されます。")

        if not api_key:
            st.warning("APIキーが設定されていません。サイドバーからAPIキーを入力してください。")
            return

        fix_label = "🔮 予防措置プランを生成" if is_pred else "✨ 修復プランを作成"
        if st.button(fix_label, key=f"gen_prev_pipe_{device_id}", type="primary"):
            _generate_step4_prevention(cand, topology, scenario, site_id, api_key, device_id, is_pred)


def _generate_step4_prevention(cand, topology, scenario, site_id, api_key, device_id, is_pred):
    """ステップ④のプランを実際に生成する。"""
    remediation_container = st.empty()
    t_node = topology.get(device_id)

    rem_scenario = scenario
    if is_pred:
        rem_scenario = build_prevention_plan_scenario(cand)

    # トリアージ結果連携
    _base_report = st.session_state.generated_report or ""
    _triage_ctx = format_triage_results_for_llm(device_id)
    if _triage_ctx:
        _analysis_with_triage = (
            f"{_base_report}\n\n"
            f"【初期確認のコマンド実行結果（実機出力）】\n"
            f"以下は運用者が初期確認で実行したコマンドの結果です。\n"
            f"この情報を踏まえて復旧手順を最適化してください。\n\n"
            f"{_triage_ctx}"
        )
    else:
        _analysis_with_triage = _base_report

    cache_key = "|".join([
        "remediation", site_id, scenario,
        str(device_id),
        hash_text(_analysis_with_triage),
    ])

    if cache_key in st.session_state.report_cache:
        remediation_text = st.session_state.report_cache[cache_key]
        remediation_container.markdown(remediation_text)
    else:
        try:
            loading_msg = "🔮 予防措置プラン生成中..." if is_pred else "🤖 修復プラン生成中..."
            remediation_container.write(loading_msg)
            placeholder = remediation_container.empty()
            remediation_text = ""

            for chunk in generate_remediation_commands_streaming(
                scenario=rem_scenario,
                analysis_result=_analysis_with_triage,
                target_node=t_node,
                api_key=api_key,
                max_retries=2,
                backoff=3,
            ):
                remediation_text += chunk
                placeholder.markdown(remediation_text)

            if not remediation_text or remediation_text.startswith("Error"):
                remediation_text = f"⚠️ プラン生成に失敗しました: {remediation_text}"
                placeholder.markdown(remediation_text)

            remediation_text = "\n".join(
                line for line in remediation_text.split("\n")
                if not line.strip().startswith("⏳")
            ).strip()

            st.session_state.report_cache[cache_key] = remediation_text
        except Exception as e:
            remediation_text = f"⚠️ プラン生成に失敗しました: {type(e).__name__}: {e}"
            remediation_container.markdown(remediation_text)

    st.session_state.remediation_plan = remediation_text
    st.rerun()
