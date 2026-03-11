# ui/components/remediation.py — Remediation & Execute セクション
import time
import logging
import streamlit as st
from typing import Optional

from network_ops import (
    generate_remediation_commands_streaming,
    run_remediation_parallel_v2,
    RemediationEnvironment,
)
from .helpers import st_html, hash_text
from .report_builders import build_prevention_plan_scenario
from .command_popup import (
    simulate_command_execution,
    show_command_popup_if_pending,
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
    # ★ 拡張A/C: 保留中のコマンド実行結果ポップアップを表示
    show_command_popup_if_pending()

    st.markdown("---")
    st.subheader("🤖 Remediation & Chat")

    if not selected_incident_candidate or selected_incident_candidate["prob"] <= 0.6:
        _render_low_risk_banner(selected_incident_candidate)
        return

    is_pred_rem = selected_incident_candidate.get('is_prediction')

    # ステータスバナー
    if is_pred_rem:
        _render_prediction_banner(selected_incident_candidate)
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

    if st.button(fix_label):
        if st.session_state.generated_report is None:
            st.warning(f"先に{report_prereq}を実行してください。")
        else:
            remediation_container = st.empty()
            t_node = topology.get(cand["id"])

            rem_scenario = scenario
            if is_pred_rem:
                rem_scenario = build_prevention_plan_scenario(cand)

            cache_key_rem = "|".join([
                "remediation", site_id, scenario,
                str(cand.get("id")),
                hash_text(st.session_state.generated_report or ""),
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
                        analysis_result=st.session_state.generated_report or "",
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
    """復旧手順表示 + Execute / Cancel ボタン"""
    with st.container(height=400, border=True):
        st.info("AI Generated Recovery Procedure（復旧手順）")
        st.markdown(st.session_state.remediation_plan)

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
    """修復実行ロジック — 結果をポップアップで表示（拡張A/C）"""
    with st.status("🔧 修復処理実行中...", expanded=True) as status_widget:
        target_node_obj = topology.get(cand["id"])
        device_info = (target_node_obj.metadata
                       if target_node_obj and hasattr(target_node_obj, 'metadata')
                       else {})

        st.write("🔄 Executing remediation steps in parallel...")
        results_rem = run_remediation_parallel_v2(
            device_id=cand["id"],
            device_info=device_info,
            scenario=scenario,
            environment=RemediationEnvironment.DEMO,
            timeout_per_step=30
        )

        st.write("📋 Remediation steps result:")
        all_success = True
        remediation_summary = []

        # ★ 拡張A/C: 実行結果をポップアップ用データとして収集
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

        # ★ 拡張A/C: 修復後検証コマンドも実行してポップアップに含める
        _verify_commands = [
            f"show interfaces status",
            f"show logging | last 10",
            f"ping 8.8.8.8 repeat 5",
        ]
        for _vcmd in _verify_commands:
            _vresult = simulate_command_execution(_vcmd, cand["id"])
            _popup_results.append(_vresult)
            remediation_summary.append(_vresult["output"])

        verification_log = "\n".join(remediation_summary)
        st.session_state.verification_log = verification_log

        if all_success:
            st.write("✅ All remediation steps completed successfully.")
            status_widget.update(label="Process Finished", state="complete", expanded=False)
            st.session_state.recovered_devices[cand["id"]] = True
            st.session_state.recovered_scenario_map[cand["id"]] = scenario

            if is_pred_rem:
                st.session_state["reset_pred_level"] = True
                st.session_state["injected_weak_signal"] = None

                if dt_engine:
                    dt_engine.forecast_auto_resolve(
                        cand["id"],
                        "mitigated",
                        note="予防措置(Execute)の実行により自動解消"
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

            # ★ 拡張A/C: 実行結果をポップアップで表示
            _popup_title = "🔮 予防措置 実行結果" if is_pred_rem else "🚀 修復実行 結果"
            from .command_popup import render_command_result_popup
            render_command_result_popup(_popup_title, _popup_results)

            if is_pred_rem:
                time.sleep(0.5)
                st.rerun()
        else:
            st.write("⚠️ Some remediation steps failed. Please review.")
            status_widget.update(label="Process Finished - With Errors", state="error", expanded=True)

            # ★ エラー時もポップアップで詳細表示
            from .command_popup import render_command_result_popup
            render_command_result_popup("⚠️ 修復実行 結果（エラーあり）", _popup_results)


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
            if api_key and GENAI_AVAILABLE:
                try:
                    _sample_logs = "\n".join([p.get("message", "") for p in _pred_group[:3]])

                    _prompt = f"""
                    あなたは熟練のネットワークAIOpsエンジニアです。
                    以下のCisco/Juniperのシステムログ（現在 {_group_size}件 同時発生中）から、根本的な原因となる「インシデントタイトル」を命名してください。

                    【条件】
                    ・20文字以内の簡潔な日本語で出力すること。
                    ・「〇〇の疑い」「〇〇の異常」などの表現を含めること。
                    ・ログが複数（3件以上）発生している場合は、単体故障ではなく「共通基板」「電源」「ファブリック」などの上位レイヤーの異常を疑うこと。

                    【対象ログサンプル】
                    {_sample_logs}
                    """

                    _model = genai.GenerativeModel('gemma-3-4b-it')
                    _response = _model.generate_content(_prompt)

                    _learned_title = _response.text.strip()

                    if _learned_title:
                        _learned_title = _learned_title.replace('\n', ' ').replace('"', '').replace("'", "")[:30]
                        st.session_state.auto_learned_rules[_pattern_key] = _learned_title
                        _incident_name = _learned_title
                    else:
                        _incident_name = f"異常シグナル検知 ({_rule_pattern})"

                except Exception as e:
                    logger.warning(f"Auto-Rule generation failed: {e}")
                    _incident_name = f"異常シグナル検知 ({_rule_pattern})"
            else:
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
            if st.button(f"✅ このインシデントを対応済みにする", key=f"bulk_handled_{_rule_pattern}", use_container_width=True):
                _cnt = 0
                for p in _pred_group:
                    r = dt_engine.forecast_register_outcome(p.get("forecast_id", ""), "mitigated", note="インシデント単位で対応済み")
                    if r.get("ok"):
                        _cnt += 1

                st.session_state["reset_pred_level"] = True
                st.session_state["injected_weak_signal"] = None
                st.session_state.live_result = None
                st.session_state.verification_result = None
                st.session_state.generated_report = None
                st.session_state.remediation_plan = None

                st.success(f"✅ {_cnt}件のシグナルをクローズし、システムを正常状態に復旧しました")
                time.sleep(0.3)
                st.rerun()
