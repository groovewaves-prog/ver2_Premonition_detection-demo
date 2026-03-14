# ui/components/remediation.py — Remediation & Execute セクション
import time
import logging
import streamlit as st
from typing import Optional

logger = logging.getLogger(__name__)


def _record_ai_feedback(alert_text: str, is_positive: bool):
    """AIナレッジベースにフィードバックを自律記録（原則4準拠）。"""
    try:
        from inference_engine import InferenceEngine
        engine = InferenceEngine.__new__(InferenceEngine)
        if hasattr(engine, '_ai_severity_store'):
            engine._ai_severity_store.record_feedback(alert_text, is_positive=is_positive)
        else:
            # フォールバック: 直接ストアをインスタンス化
            from inference_engine import _AISeverityStore
            store = _AISeverityStore()
            store.record_feedback(alert_text, is_positive=is_positive)
    except Exception as e:
        logger.debug("AI feedback recording skipped: %s", e)

from network_ops import (
    generate_remediation_commands_streaming,
    run_remediation_parallel_v2,
    RemediationEnvironment,
)
from .helpers import st_html, hash_text
from .report_builders import build_prevention_plan_scenario
from .command_popup import (
    simulate_command_execution,
    format_triage_results_for_llm,
)

try:
    import google.generativeai as genai
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False

logger = logging.getLogger(__name__)


def render_remediation(
    selected_incident_candidate: Optional[dict],
    topology: dict,
    scenario: str,
    site_id: str,
    api_key: Optional[str],
    dt_engine,
):
    """Remediation & Chat セクション（Execute/Cancel含む）を描画"""
    st.markdown("---")
    st.subheader("🤖 Remediation & Chat")

    # サイレント障害はprob値に関わらず復旧プラン生成を許可
    _is_silent = (
        selected_incident_candidate
        and "silent" in selected_incident_candidate.get("label", "").lower()
    )
    if not selected_incident_candidate or (
        selected_incident_candidate["prob"] <= 0.6 and not _is_silent
    ):
        _render_low_risk_banner(selected_incident_candidate)
        return

    is_pred_rem = selected_incident_candidate.get('is_prediction')

    # ステータスバナー
    if is_pred_rem:
        _render_prediction_banner(selected_incident_candidate)
    elif _is_silent:
        _render_silent_failure_banner(selected_incident_candidate)
    else:
        _render_incident_banner(selected_incident_candidate)

    # ★ Generate Fix ボタン（remediation_plan 未生成時のみ表示）
    if st.session_state.remediation_plan is None:
        _render_generate_fix_button(selected_incident_candidate, topology, scenario, site_id, api_key, is_pred_rem)

    # ★ 復旧手順表示 + Execute / Cancel ボタン（remediation_plan 生成済み時）
    if st.session_state.remediation_plan is not None:
        _render_execute_section(selected_incident_candidate, topology, scenario, api_key, dt_engine, is_pred_rem)

    # ★ Phase1: 予兆ステータス履歴
    if dt_engine and selected_incident_candidate:
        _render_prediction_history(selected_incident_candidate, dt_engine, api_key)


def _render_prediction_banner(cand: dict):
    """予兆検知時のステータスバナー"""
    timeline    = cand.get('prediction_timeline', '不明')
    affected    = cand.get('prediction_affected_count', 0)
    ttf_hours   = cand.get('prediction_time_to_failure_hours', 0)
    failure_dt  = cand.get('prediction_failure_datetime', '')
    ttc_min     = cand.get('prediction_time_to_critical_min', 0)

    if ttf_hours >= 24:
        ttf_display = f"今後 <b>{ttf_hours // 24}日後</b>"
        if failure_dt:
            ttf_display += f" ({failure_dt}頃)"
    elif ttf_hours > 0:
        ttf_display = f"今後 <b>{ttf_hours}時間後</b>"
        if failure_dt:
            ttf_display += f" ({failure_dt}頃)"
    else:
        ttf_display = "<b>障害が切迫</b>"

    ttc_display = f"症状発症後 <b>{ttc_min}分後</b>" if ttc_min > 0 else f"<b>{timeline}</b>"

    st_html(f"""
    <div style="background-color:#fff3e0;padding:10px;border-radius:5px;border:1px solid #ff9800;color:#e65100;margin-bottom:10px;">
        <strong>🔮 Digital Twin 未来予測 (Predictive Maintenance)</strong><br>
        <b>{cand['id']}</b> で障害の兆候を検出しました。<br>
        ・障害発生予測: {ttf_display}<br>
        ・急性期進行: {ttc_display} でサービス断の恐れ<br>
        ・影響範囲: <b>{affected}台</b> のデバイスに影響の可能性<br>
        ・推奨: メンテナンスウィンドウでの予防交換/対応<br>
        (信頼度: <span style="font-size:1.2em;font-weight:bold;">{cand['prob']*100:.0f}%</span>)
    </div>
    """)


def _render_silent_failure_banner(cand: dict):
    """サイレント障害検知時のステータスバナー"""
    st_html(f"""
    <div style="background-color:#f3e5f5;padding:10px;border-radius:5px;border:1px solid #9c27b0;color:#6a1b9a;margin-bottom:10px;">
        <strong>🟣 サイレント障害検知 (Silent Failure Detected)</strong><br>
        <b>{cand['id']}</b> でサイレント障害の疑いを検出しました。<br>
        ハードウェアは正常ですが、配下デバイスへの通信影響が発生しています。<br>
        L2レベル（MAC/STP/VLAN）の調査と復旧対応が必要です。<br>
        (リスクスコア: <span style="font-size:1.2em;font-weight:bold;">{cand['prob']*100:.0f}</span>)
    </div>
    """)


def _render_incident_banner(cand: dict):
    """障害検知時のステータスバナー"""
    st_html(f"""
    <div style="background-color:#e8f5e9;padding:10px;border-radius:5px;border:1px solid #4caf50;color:#2e7d32;margin-bottom:10px;">
        <strong>✅ AI Analysis Completed</strong><br>
        特定された原因 <b>{cand['id']}</b> に対する復旧手順が利用可能です。<br>
        (リスクスコア: <span style="font-size:1.2em;font-weight:bold;">{cand['prob']*100:.0f}</span>)
    </div>
    """)


def _render_low_risk_banner(cand: Optional[dict]):
    """低リスクまたは正常稼働時のバナー"""
    if not cand:
        return
    device_id = cand.get('id', '')
    score = cand['prob'] * 100
    if device_id == "SYSTEM" and score == 0:
        st_html("""
        <div style="background-color:#e8f5e9;padding:10px;border-radius:5px;border:1px solid #4caf50;color:#2e7d32;margin-bottom:10px;">
            <strong>✅ 正常稼働中</strong><br>
            現在、ネットワークは正常に稼働しています。対応が必要なインシデントはありません。
        </div>
        """)
    else:
        st_html(f"""
        <div style="background-color:#fff3e0;padding:10px;border-radius:5px;border:1px solid #ff9800;color:#e65100;margin-bottom:10px;">
            <strong>⚠️ 監視中</strong><br>
            対象: <b>{device_id}</b><br>
            (リスクスコア: {score:.0f} - 60以上で自動修復を推奨)
        </div>
        """)


def _render_generate_fix_button(cand, topology, scenario, site_id, api_key, is_pred_rem):
    """Generate Fix ボタンと remediation_plan 生成"""
    if is_pred_rem:
        fix_label    = "🔮 予防措置プランを生成 (Preventive Measures)"
        report_prereq = "「🔮 予兆の確認手順を生成」"
        st.caption(
            "📋 **ステップ③**: 確認手順の診断結果を踏まえたメンテナンス作業計画書。"
            "config系の予防コマンドを含み、「復旧実行」ボタンで自動実行できます。"
        )
    else:
        fix_label    = "✨ 修復プランを作成 (Generate Fix)"
        report_prereq = "「📝 詳細レポートを作成 (Generate Report)」"

    # ★ トリアージ結果連携のステータス表示
    _has_triage = bool(format_triage_results_for_llm(cand.get("id", "")))
    if _has_triage:
        st.caption("✅ 初動トリアージの実行結果を検出しました。復旧計画に自動反映されます。")

    if st.button(fix_label):
        if st.session_state.generated_report is None:
            st.warning(f"先に{report_prereq}を実行してください。")
        else:
            remediation_container = st.empty()
            t_node = topology.get(cand["id"])

            rem_scenario = scenario
            if is_pred_rem:
                rem_scenario = build_prevention_plan_scenario(cand)

            # ★ トリアージ実行結果をAI復旧計画のコンテキストに自動連携
            _base_report = st.session_state.generated_report or ""
            _triage_ctx = format_triage_results_for_llm(cand.get("id", ""))
            if _triage_ctx:
                _analysis_with_triage = (
                    f"{_base_report}\n\n"
                    f"【初動トリアージのコマンド実行結果（実機出力）】\n"
                    f"以下は運用者が初動トリアージで実行したコマンドの結果です。\n"
                    f"この情報を踏まえて復旧手順を最適化してください。\n\n"
                    f"{_triage_ctx}"
                )
            else:
                _analysis_with_triage = _base_report

            cache_key_rem = "|".join([
                "remediation", site_id, scenario,
                str(cand.get("id")),
                hash_text(_analysis_with_triage),
            ])

            if cache_key_rem in st.session_state.report_cache:
                remediation_text = st.session_state.report_cache[cache_key_rem]
                remediation_container.markdown(remediation_text)
            else:
                try:
                    loading_msg = "🔮 予防措置プラン生成中..." if is_pred_rem else "🤖 復旧プラン生成中..."
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
                        remediation_text = f"⚠️ 復旧プラン生成に失敗しました: {remediation_text}"
                        placeholder.markdown(remediation_text)

                    remediation_text = "\n".join(
                        line for line in remediation_text.split("\n")
                        if not line.strip().startswith("⏳")
                    ).strip()

                    st.session_state.report_cache[cache_key_rem] = remediation_text
                except Exception as e:
                    remediation_text = f"⚠️ 復旧プラン生成に失敗しました: {type(e).__name__}: {e}"
                    remediation_container.markdown(remediation_text)

            st.session_state.remediation_plan = remediation_text
            st.rerun()


def _render_execute_section(cand, topology, scenario, api_key, dt_engine, is_pred_rem):
    """復旧手順表示 + Execute / Cancel ボタン（セーフティガード付き）"""
    with st.container(height=400, border=True):
        st.info("AI Generated Recovery Procedure（復旧手順）")
        st.markdown(st.session_state.remediation_plan)

    # ── 既存の検証セッションがあれば表示 ──
    existing_session = get_verification_session(cand["id"])
    if existing_session and existing_session.status not in ("pending",):
        st.markdown("#### 🛡️ 実行ログと検証ステータス")
        render_verification_panel(existing_session)

        # ロールバックボタン（異常検知時のみ表示）
        if render_rollback_button(existing_session):
            _execute_rollback_flow(existing_session, cand, scenario, dt_engine, is_pred_rem)
            return

        # 復旧確認済みなら追加ボタンは不要
        if existing_session.status == "verified":
            return

    # ── Execute / Cancel ボタン ──
    col_exec1, col_exec2 = st.columns(2)
    exec_clicked   = col_exec1.button("🚀 修復実行 (Execute)", type="primary")
    cancel_clicked = col_exec2.button("キャンセル")

    if cancel_clicked:
        st.session_state.remediation_plan  = None
        st.session_state.verification_log  = None
        st.rerun()

    if exec_clicked:
        if not api_key:
            st.error("API Key Required")
        else:
            _execute_remediation(cand, topology, scenario, dt_engine, is_pred_rem)

    if st.session_state.get("verification_log"):
        st.markdown("#### 🔎 Post-Fix Verification Logs")
        st.code(st.session_state.verification_log, language="text")


def _execute_remediation(cand, topology, scenario, dt_engine, is_pred_rem):
    """セーフティガード付き修復実行ロジック

    Pre-Check → Snapshot → Execute → Post-Check → Recovery Confirmed / Rollback
    """
    with st.status("🔧 セーフティガード付き修復処理を実行中...", expanded=True) as status_widget:

        # ── Step 1: Pre-Check ──
        st.write("🔍 **Step 1/4: Pre-Check** — 現在の状態を確認中...")
        from .verifier import run_pre_checks, take_config_snapshot, run_post_checks, evaluate_post_checks
        pre_checks = run_pre_checks(cand["id"])

        pre_fails = sum(1 for c in pre_checks if c.status == "fail")
        pre_passes = sum(1 for c in pre_checks if c.status == "pass")
        st.write(f"  Pre-Check完了: {len(pre_checks)}項目 (Pass: {pre_passes}, Fail: {pre_fails})")

        # ── Step 2: Snapshot ──
        st.write("📸 **Step 2/4: Snapshot** — 現在の設定を保存中...")
        snapshot = take_config_snapshot(cand["id"])
        st.write(f"  スナップショット取得完了 (ID: {snapshot.snapshot_id})")

        # ── Step 3: Execute ──
        st.write("⚡ **Step 3/4: Execute** — 修復アクションを実行中...")
        target_node_obj = topology.get(cand["id"])
        device_info = (target_node_obj.metadata
                       if target_node_obj and hasattr(target_node_obj, 'metadata')
                       else {})

        results_rem = run_remediation_parallel_v2(
            device_id=cand["id"],
            device_info=device_info,
            scenario=scenario,
            environment=RemediationEnvironment.DEMO,
            timeout_per_step=30
        )

        all_success = True
        remediation_summary = []
        _popup_results = []
        for step_name in ["Backup", "Apply", "Verify"]:
            result = results_rem.get(step_name)
            if result:
                st.write(str(result))
                remediation_summary.append(str(result))
                _popup_results.append({
                    "status": result.status,
                    "command": step_name,
                    "output": str(result),
                    "device_id": cand["id"],
                    "elapsed_sec": round(time.time() - result.timestamp, 2) if hasattr(result, 'timestamp') else 0.0,
                })
                if result.status != "success":
                    all_success = False

        # 修復後の検証コマンド
        _verify_commands = [
            "show interfaces status",
            "show logging | last 10",
            "ping 8.8.8.8 repeat 5",
        ]
        for _vcmd in _verify_commands:
            _vresult = simulate_command_execution(_vcmd, cand["id"])
            _popup_results.append(_vresult)
            remediation_summary.append(_vresult["output"])

        verification_log = "\n".join(remediation_summary)
        st.session_state.verification_log = verification_log

        # ── Step 4: Post-Check ──
        st.write("🔍 **Step 4/4: Post-Check** — 修復結果を自動検証中...")
        post_checks = run_post_checks(cand["id"])
        verdict = evaluate_post_checks(post_checks)

        post_passes = sum(1 for c in post_checks if c.status == "pass")
        post_fails = sum(1 for c in post_checks if c.status == "fail")
        st.write(f"  Post-Check完了: {len(post_checks)}項目 (Pass: {post_passes}, Fail: {post_fails})")

        # ── 検証セッションを構築・保存 ──
        from .verifier import VerificationSession, _get_sessions
        session = VerificationSession(
            device_id=cand["id"],
            scenario=scenario,
            pre_checks=pre_checks,
            post_checks=post_checks,
            snapshot=snapshot,
            execution_log=_popup_results,
        )

        if verdict == "verified" and all_success:
            session.status = "verified"
            session.conclusion = "Recovery Confirmed: 全検証項目がパスしました。修復は正常に完了しています。"

            st.write("✅ All remediation steps completed successfully.")
            status_widget.update(label="Recovery Confirmed", state="complete", expanded=False)
            st.session_state.recovered_devices[cand["id"]] = True
            st.session_state.recovered_scenario_map[cand["id"]] = scenario

            if is_pred_rem:
                st.session_state["reset_pred_level"] = True
                st.session_state["injected_weak_signal"] = None

                if dt_engine:
                    dt_engine.forecast_auto_resolve(
                        cand["id"],
                        "mitigated",
                        note="セーフティガード付き修復の検証完了により自動解消"
                    )

                ts = time.strftime("%Y-%m-%d %H:%M:%S")
                success_log = (f"[PROBE] ts={ts}\n"
                               f"予防的メンテナンスが完了しました。\n"
                               f"show system alarms\nNo active alarms\n"
                               f"ping 8.8.8.8 repeat 5\nSuccess rate is 100 percent (5/5)")
                st.session_state.live_result = {
                    "status": "SUCCESS",
                    "sanitized_log": success_log,
                    "device_id": cand["id"]
                }
                st.session_state.verification_result = {
                    "ping_status": "OK", "interface_status": "UP", "hardware_status": "NORMAL"
                }

            if not st.session_state.balloons_shown:
                st.balloons()
                st.session_state.balloons_shown = True
            st.success("✅ System Recovered Successfully!")

            _popup_title = "🔮 予防措置 実行結果" if is_pred_rem else "🚀 修復実行 結果"
            from .command_popup import render_command_result_popup
            render_command_result_popup(_popup_title, _popup_results)

            if is_pred_rem:
                time.sleep(0.5)

        elif verdict == "rollback_needed":
            session.status = "rollback_needed"
            session.conclusion = "異常検出: 修復後の検証で異常が検出されました。ロールバックを推奨します。"

            st.write("🔴 Post-Check で異常を検出しました。ロールバックを推奨します。")
            status_widget.update(label="異常検出 — ロールバック推奨", state="error", expanded=True)

            from .command_popup import render_command_result_popup
            render_command_result_popup("🔴 修復実行 結果 — ロールバック推奨", _popup_results)

        else:
            session.status = "warning"
            session.conclusion = "警告: 一部の検証項目で警告があります。手動確認を推奨します。"

            if all_success:
                st.write("🟡 修復は完了しましたが、一部の検証項目で警告があります。")
                status_widget.update(label="Process Finished — 要確認", state="complete", expanded=True)
                st.session_state.recovered_devices[cand["id"]] = True
                st.session_state.recovered_scenario_map[cand["id"]] = scenario
            else:
                st.write("⚠️ Some remediation steps failed. Please review.")
                status_widget.update(label="Process Finished - With Errors", state="error", expanded=True)

            from .command_popup import render_command_result_popup
            render_command_result_popup("⚠️ 修復実行 結果（要確認）", _popup_results)

        # セッション保存
        _get_sessions()[cand["id"]] = session

    # ステータスパネルを即表示
    st.rerun()


def _execute_rollback_flow(session, cand, scenario, dt_engine, is_pred_rem):
    """ロールバックを実行し、セッションを更新する。"""
    with st.status("🔄 ロールバック実行中...", expanded=True) as rollback_status:
        st.write(f"📸 スナップショット {session.snapshot.snapshot_id} から復元中...")

        rollback_results = execute_rollback(cand["id"], session.snapshot)

        for r in rollback_results:
            st.write(f"  {r.get('command', 'unknown')}: {r.get('status', '')}")

        session.status = "rolled_back"
        session.conclusion = (
            f"ロールバック完了: スナップショット {session.snapshot.snapshot_id} から"
            f"設定を復元しました。"
        )

        from .verifier import _get_sessions
        _get_sessions()[cand["id"]] = session

        rollback_status.update(label="ロールバック完了", state="complete", expanded=False)
        st.success("🔄 ロールバックが正常に完了しました。")

    st.rerun()


def _render_prediction_history(cand, dt_engine, api_key):
    """予兆ステータス履歴の描画"""
    from collections import defaultdict
    from datetime import datetime

    _oc_device = cand.get("id", "")
    _open_preds = dt_engine.forecast_list_open(device_id=_oc_device)
    if not _open_preds:
        return

    st.markdown("---")
    st.markdown("##### 📜 予兆ステータス履歴")

    _open_preds.sort(key=lambda x: float(x.get("created_at", 0)), reverse=True)
    _target_rule = _open_preds[0].get("rule_pattern", "不明")
    _pred_group = [p for p in _open_preds if p.get("rule_pattern", "不明") == _target_rule]
    _rule_pattern = _target_rule
    _group_size = len(_pred_group)

    # インシデントタイトルの自動学習
    _incident_name = cand.get('predicted_state') or cand.get('label', '').replace('🔮 [予兆] ', '')

    if not _incident_name or _incident_name == '不明':
        if "auto_learned_rules" not in st.session_state:
            st.session_state.auto_learned_rules = {}

        _severity_level = "high" if _group_size > 2 else "low"
        _pattern_key = f"{_rule_pattern}_{_severity_level}"

        if _pattern_key in st.session_state.auto_learned_rules:
            _incident_name = st.session_state.auto_learned_rules[_pattern_key]
        else:
            # ★ LLM呼出を描画パスから排除: デフォルト名を使用
            _incident_name = f"異常シグナル検知 ({_rule_pattern})"

    # 統計情報の計算
    _confidences = [float(p.get("confidence", 0.0)) for p in _pred_group]
    _is_sim = any(p.get("source") == "simulation" for p in _pred_group)
    _display_conf = max(_confidences) if _is_sim else (sum(_confidences) / len(_confidences) if _confidences else 0.0)

    _timestamps = []
    for p in _pred_group:
        try:
            _timestamps.append(float(p.get("created_at", 0)))
        except Exception:
            pass

    if _timestamps:
        _newest_ts = max(_timestamps)
        _elapsed_sec = time.time() - _newest_ts
        if _elapsed_sec < 3600:
            _relative = f"{int(_elapsed_sec / 60)}分前"
        elif _elapsed_sec < 86400:
            _relative = f"{int(_elapsed_sec / 3600)}時間前"
        else:
            _relative = f"{int(_elapsed_sec / 86400)}日前"
    else:
        _relative = "不明"

    # ユニークなログ抽出
    _unique_log_entries = []
    for _fp in _pred_group:
        try:
            _created_ts = float(_fp.get("created_at", 0))
            _dt_str = datetime.fromtimestamp(_created_ts).strftime("%m/%d %H:%M:%S")
        except Exception:
            _dt_str = "不明"

        _raw_msg = _fp.get("message", "ログ内容なし")
        _log_lines = [line.strip() for line in _raw_msg.split('\n') if line.strip()]

        for _line in _log_lines:
            _entry_html = f"<span style='color: #888;'>[{_dt_str}]</span> {_line}"
            if _entry_html not in _unique_log_entries:
                _unique_log_entries.append(_entry_html)

    _total_signals = len(_unique_log_entries) or 1

    # インシデントカード描画
    _expander_title = f"🚨 インシデント：{_incident_name} （信頼度: {_display_conf*100:.0f}% ｜ 影響シグナル: {_total_signals}件）"

    with st.expander(_expander_title, expanded=True):
        st_html(
            f"<div style='margin-bottom: 8px; color: #666; font-size: 0.9em;'>"
            f"最新検知: {_relative}"
            f"</div>"
        )

        st.markdown("**🔍 証拠シグナル一覧（検知ログ詳細）**")

        _box_height = 250 if _total_signals > 4 else None

        if _box_height:
            scroll_container = st.container(height=_box_height, border=True)
        else:
            scroll_container = st.container(border=True)

        with scroll_container:
            for _entry_html in _unique_log_entries:
                st_html(
                    f"<div style='font-family: monospace; font-size: 0.85em; background: #F8F9FA; padding: 4px 8px; margin-bottom: 4px; border-left: 3px solid #FFC107; word-break: break-all;'>"
                    f"{_entry_html}"
                    f"</div>"
                )

        st_html("<div style='margin-top: 12px;'></div>")
        _btn_col1, _btn_col2 = st.columns(2)
        with _btn_col1:
            if st.button(f"✅ 対応済み", key=f"bulk_handled_{_rule_pattern}", use_container_width=True):
                _cnt = 0
                for p in _pred_group:
                    r = dt_engine.forecast_register_outcome(p.get("forecast_id", ""), "mitigated", note="インシデント単位で対応済み")
                    if r.get("ok"):
                        _cnt += 1
                        # 原則4: AIナレッジに正のフィードバックを自律記録
                        _msg = p.get("message", "")
                        if _msg:
                            _record_ai_feedback(_msg, is_positive=True)

                st.session_state["reset_pred_level"] = True
                st.session_state["injected_weak_signal"] = None
                st.session_state.live_result = None
                st.session_state.verification_result = None
                st.session_state.generated_report = None
                st.session_state.remediation_plan = None

                st.success(f"✅ {_cnt}件をクローズし、正常状態に復旧しました")
                time.sleep(0.3)
                st.rerun()
        with _btn_col2:
            if st.button(f"🚫 静観 / キャンセル", key=f"bulk_dismiss_{_rule_pattern}", use_container_width=True):
                _cnt = 0
                for p in _pred_group:
                    r = dt_engine.forecast_register_outcome(p.get("forecast_id", ""), "mitigated", note="静観・ノイズとして却下")
                    if r.get("ok"):
                        _cnt += 1
                        # 原則4: AIナレッジに負のフィードバックを自律記録
                        _msg = p.get("message", "")
                        if _msg:
                            _record_ai_feedback(_msg, is_positive=False)

                st.session_state["reset_pred_level"] = True
                st.session_state["injected_weak_signal"] = None
                st.session_state.live_result = None
                st.session_state.verification_result = None
                st.session_state.generated_report = None
                st.session_state.remediation_plan = None

                st.info(f"🚫 {_cnt}件を静観としてクローズしました")
                time.sleep(0.3)
                st.rerun()

        # ── 詳細フォーカスボタン（History モードへの遷移） ──
        if st.button(
            f"🔍 この予兆にフォーカス（ダッシュボード全体を切替）",
            key=f"focus_context_{_rule_pattern}",
            use_container_width=True,
        ):
            # 代表的な予兆アイテムからコンテキスト情報を構築
            _representative = _pred_group[0]
            _all_messages = []
            for p in _pred_group:
                _m = p.get("message", "")
                if _m and _m not in _all_messages:
                    _all_messages.append(_m)
            st.session_state["active_context_item"] = {
                "device_id": _oc_device,
                "messages": _all_messages,
                "message": _all_messages[0] if _all_messages else "",
                "level": cand.get("level", 0),
                "scenario": _representative.get("rule_pattern", ""),
                "forecast_id": _representative.get("forecast_id", ""),
                "rule_pattern": _rule_pattern,
                "confidence": _display_conf,
                "created_at": max(_timestamps) if _timestamps else 0,
                "source": "history_focus",
                "incident_name": _incident_name,
            }
            # キャッシュクリアで全コンポーネントを再描画させる
            st.session_state.pop("dt_prediction_cache", None)
            st.session_state.pop("generated_report", None)
            st.session_state.pop("report_cache", None)
            st.rerun()
