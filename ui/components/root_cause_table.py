# ui/components/root_cause_table.py — 根本原因候補テーブル + 派生/ノイズ一覧
import streamlit as st
import pandas as pd
from typing import List, Tuple, Optional


def render_root_cause_table(
    root_cause_candidates: List[dict],
    symptom_devices: List[dict],
    unrelated_devices: List[dict],
    alarms: list,
) -> Tuple[Optional[dict], Optional[str]]:
    """
    根本原因候補テーブルを描画し、選択されたインシデント候補とデバイスIDを返す。

    Returns:
        (selected_incident_candidate, target_device_id)
    """
    selected_incident_candidate = None
    target_device_id = None

    if not root_cause_candidates:
        return None, None

    # アラームのseverityとsilentフラグをデバイスIDでマッピング
    alarm_info_map = {}
    for a in alarms:
        if a.device_id not in alarm_info_map:
            alarm_info_map[a.device_id] = {'severity': 'INFO', 'is_silent': False}
        if a.severity == 'CRITICAL':
            alarm_info_map[a.device_id]['severity'] = 'CRITICAL'
        elif a.severity == 'WARNING' and alarm_info_map[a.device_id]['severity'] != 'CRITICAL':
            alarm_info_map[a.device_id]['severity'] = 'WARNING'
        if hasattr(a, 'is_silent_suspect') and a.is_silent_suspect:
            alarm_info_map[a.device_id]['is_silent'] = True

    df_data = []
    for rank, cand in enumerate(root_cause_candidates, 1):
        prob = cand.get('prob', 0)
        cand_type = cand.get('type', 'UNKNOWN')
        device_id = cand['id']
        alarm_info = alarm_info_map.get(device_id, {'severity': 'INFO', 'is_silent': False})

        if cand.get('is_prediction'):
            _cand_trend = cand.get('trend_info')
            if _cand_trend and _cand_trend.get('detected'):
                status_text = "📈 予兆+トレンド"
            else:
                status_text = "🔮 予兆検知"
            timeline = cand.get('prediction_timeline', '')
            affected = cand.get('prediction_affected_count', 0)
            early_hours = cand.get('prediction_early_warning_hours', 0)
            early_str = (f"(予兆: {early_hours // 24}日前〜)" if early_hours >= 24
                         else (f"(予兆: {early_hours}時間前〜)" if early_hours > 0 else ""))
            if timeline and affected:
                action = f"⚡ 急性期{timeline}以内 {early_str} ({affected}台影響)"
            else:
                action = f"⚡ 予防的対処を推奨 {early_str}"
        elif alarm_info['is_silent'] or "Silent" in cand_type:
            status_text = "🟣 サイレント疑い"
            action = "🔍 上位確認"
        elif alarm_info['severity'] == 'CRITICAL':
            status_text = "🔴 危険 (根本原因)"
            action = "🚀 自動修復が可能"
        elif alarm_info['severity'] == 'WARNING':
            status_text = "🟡 警告"
            action = "🔍 詳細調査"
        elif prob > 0.6:
            status_text = "🟡 被疑箇所"
            action = "🔍 詳細調査"
        else:
            status_text = "⚪ 監視中"
            action = "👁️ 静観"

        df_data.append({
            "順位": rank,
            "ステータス": status_text,
            "デバイス": device_id,
            "原因": cand.get('label', ''),
            "確信度": f"{prob*100:.0f}%",
            "推奨アクション": action,
            "_id": device_id,
            "_prob": prob
        })

    df = pd.DataFrame(df_data)

    st.markdown("#### 🎯 根本原因候補")
    event = st.dataframe(
        df[["順位", "ステータス", "デバイス", "原因", "確信度", "推奨アクション"]],
        use_container_width=True,
        hide_index=True,
        selection_mode="single-row",
        on_select="rerun"
    )

    if event.selection and len(event.selection.rows) > 0:
        sel_row = df.iloc[event.selection.rows[0]]
        for cand in root_cause_candidates:
            if cand['id'] == sel_row['_id']:
                selected_incident_candidate = cand
                target_device_id = cand['id']
                break
    elif root_cause_candidates:
        selected_incident_candidate = root_cause_candidates[0]
        target_device_id = root_cause_candidates[0]['id']

    # 派生アラート（Symptom）一覧
    if symptom_devices:
        with st.expander(f"🔗 派生アラート (Symptom): {len(symptom_devices)}件 - 上流復旧待ち", expanded=False):
            dd_df = pd.DataFrame([
                {"No": i+1, "デバイス": d['id'], "状態": "⚫ 応答なし",
                 "原因": d.get('label', ''), "備考": "上流復旧待ち"}
                for i, d in enumerate(symptom_devices)
            ])
            if len(symptom_devices) >= 10:
                with st.container(height=300):
                    st.dataframe(dd_df, use_container_width=True, hide_index=True)
            else:
                st.dataframe(dd_df, use_container_width=True, hide_index=True)

    # 無関係アラート（Unrelated / ノイズ）一覧
    if unrelated_devices:
        with st.expander(f"📢 無関係アラート (Unrelated): {len(unrelated_devices)}件", expanded=False):
            ur_df = pd.DataFrame([
                {"No": i+1, "デバイス": d['id'],
                 "アラート": d.get('label', ''), "確信度": f"{d.get('prob', 0)*100:.0f}%"}
                for i, d in enumerate(unrelated_devices)
            ])
            st.dataframe(ur_df, use_container_width=True, hide_index=True)

    return selected_incident_candidate, target_device_id
