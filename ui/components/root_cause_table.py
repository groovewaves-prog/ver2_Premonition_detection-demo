# ui/components/root_cause_table.py — 根本原因候補テーブル + 派生/ノイズ一覧
import streamlit as st
import pandas as pd
import logging
from typing import List, Tuple, Optional
from .command_popup import render_triage_cards

logger = logging.getLogger(__name__)


def render_root_cause_table(
    root_cause_candidates: List[dict],
    symptom_devices: List[dict],
    unrelated_devices: List[dict],
    alarms: list,
    topology: dict = None,
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

        # ★ 相互検証ステータス
        _v = cand.get('verification', {})
        _v_agreement = _v.get('agreement', '')
        if _v.get('escalation_required'):
            verify_text = "⚠️ 要確認"
        elif _v_agreement == 'consistent':
            verify_text = "✅ 一致"
        elif _v_agreement == 'divergent':
            verify_text = "🔶 不一致"
        elif _v_agreement == 'single_source':
            verify_text = "—"
        else:
            verify_text = "—"

        df_data.append({
            "順位": rank,
            "ステータス": status_text,
            "デバイス": device_id,
            "原因": cand.get('label', ''),
            "確信度": f"{prob*100:.0f}%",
            "検証": verify_text,
            "推奨アクション": action,
            "_id": device_id,
            "_prob": prob
        })

    df = pd.DataFrame(df_data)

    st.markdown("#### 🎯 根本原因候補")
    # 候補データの構成が変わった際にUIを強制リセットするための動的キー
    _table_key = f"rc_table_{hash(str([c['id'] for c in root_cause_candidates]))}"
    event = st.dataframe(
        df[["順位", "ステータス", "デバイス", "原因", "確信度", "検証", "推奨アクション"]],
        use_container_width=True,
        hide_index=True,
        selection_mode="single-row",
        on_select="rerun",
        key=_table_key
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

    # ★ 相互検証の詳細表示（エスカレーション対象の場合）
    if selected_incident_candidate:
        _v = selected_incident_candidate.get('verification', {})
        if _v.get('escalation_required') or _v.get('agreement') == 'divergent':
            _render_verification_detail(selected_incident_candidate)

    # ★ 障害時初期確認: フラグメント化でボタン操作を部分再描画に
    if selected_incident_candidate:
        _is_pred = selected_incident_candidate.get('is_prediction', False)
        _rc_dev = selected_incident_candidate.get('id', '')
        if not _is_pred and _rc_dev != 'SYSTEM':
            _render_incident_triage(selected_incident_candidate, topology or {})

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

    # 生アラーム一覧（全アラームの詳細）
    if alarms:
        _sev_icon = {'CRITICAL': '🔴', 'WARNING': '🟡', 'INFO': '🔵'}
        with st.expander(f"📋 生アラーム一覧: {len(alarms)}件", expanded=False):
            alarm_df = pd.DataFrame([
                {
                    "No": i + 1,
                    "Severity": f"{_sev_icon.get(a.severity, '⚪')} {a.severity}",
                    "デバイス": a.device_id,
                    "メッセージ": a.message,
                    "Root Cause": "✅" if getattr(a, 'is_root_cause', False) else "",
                    "Silent疑い": "🟣" if getattr(a, 'is_silent_suspect', False) else "",
                }
                for i, a in enumerate(alarms)
            ])
            st.dataframe(alarm_df, use_container_width=True, hide_index=True)

    return selected_incident_candidate, target_device_id


def _render_incident_triage(cand: dict, topology: dict):
    """障害時トリアージを描画。st.fragment/st.rerunを使わず、その場で描画を完結させる。"""
    _rc_dev = cand.get('id', '')
    _rc_actions = cand.get('recommended_actions', [])

    # キャッシュ済みならそのまま使う（LLM呼出なし）
    if not _rc_actions:
        _rc_label = cand.get('label', '')
        _triage_ck = f"_triage_incident_{_rc_dev}_{hash(_rc_label[:200])}"
        _rc_actions = st.session_state.get(_triage_ck, [])

    if _rc_actions:
        with st.expander(f"🛠 初期確認: {_rc_dev}", expanded=True):
            st.caption(
                "🕐 ステップ①: 最初の5分: 状況把握のためのshowコマンドです。"
                "「▶ 全コマンド一括実行」で全 show を一度に実行できます。"
                "🔧マークは人手作業です。"
            )
            render_triage_cards(_rc_actions, _rc_dev, card_idx=f"incident_{_rc_dev}")
    else:
        # ★ 障害時は即座にトリアージを自動生成（ボタン待ち不要）
        _rc_actions = _generate_incident_triage_lazy(cand, topology)
        if not _rc_actions:
            # LLM が利用不可の場合、静的フォールバックトリアージを提供
            _rc_actions = _get_fallback_triage_actions(cand, topology)
        if _rc_actions:
            cand['recommended_actions'] = _rc_actions
            _auto_execute_incident_triage(_rc_actions, _rc_dev)
            # ★ st.rerun() を廃止し、その場で描画を完結させる
            with st.expander(f"🛠 初期確認: {_rc_dev}", expanded=True):
                st.caption(
                    "🕐 ステップ①: 最初の5分: 状況把握のためのshowコマンドです。"
                    "「▶ 全コマンド一括実行」で全 show を一度に実行できます。"
                    "🔧マークは人手作業です。"
                )
                render_triage_cards(_rc_actions, _rc_dev, card_idx=f"incident_{_rc_dev}")


def _get_fallback_triage_actions(cand: dict, topology: dict) -> list:
    """LLM が利用不可の場合に使用する静的フォールバックトリアージ。

    デバイスのベンダー情報からプラットフォームに適したshowコマンドを返す。
    """
    from .helpers import build_ci_context_for_chat
    _dev_id = cand.get('id', '')
    ci = build_ci_context_for_chat(topology, _dev_id)
    vendor = ci.get("vendor", "Unknown").lower()

    if "juniper" in vendor:
        return [
            {
                "title": "インターフェース状態の確認",
                "effect": "全インターフェースのリンク状態・エラーカウンタを一覧表示",
                "priority": "high",
                "rationale": "障害範囲を最初に特定するため",
                "steps": "show interfaces terse\nshow interfaces detail | match \"error|drop\"",
            },
            {
                "title": "ログの確認",
                "effect": "直近のシステムイベント・障害メッセージを確認",
                "priority": "high",
                "rationale": "障害発生時刻とトリガーを特定するため",
                "steps": "show log messages | last 20",
            },
            {
                "title": "ルーティング・隣接状態の確認",
                "effect": "BGP/OSPF ネイバーの状態を確認",
                "priority": "medium",
                "rationale": "プロトコルレベルの断を検出するため",
                "steps": "show bgp summary\nshow ospf neighbor",
            },
        ]
    else:
        # Cisco IOS/IOS-XE デフォルト
        return [
            {
                "title": "インターフェース状態の確認",
                "effect": "全インターフェースのリンク状態・エラーカウンタを一覧表示",
                "priority": "high",
                "rationale": "障害範囲を最初に特定するため",
                "steps": "show ip interface brief\nshow interfaces | include errors|drops",
            },
            {
                "title": "ログの確認",
                "effect": "直近のシステムイベント・障害メッセージを確認",
                "priority": "high",
                "rationale": "障害発生時刻とトリガーを特定するため",
                "steps": "show logging | last 20",
            },
            {
                "title": "ルーティング・隣接状態の確認",
                "effect": "BGP/OSPF ネイバーの状態を確認",
                "priority": "medium",
                "rationale": "プロトコルレベルの断を検出するため",
                "steps": "show ip bgp summary\nshow ip ospf neighbor",
            },
        ]


def _auto_execute_incident_triage(rec_actions: list, device_id: str):
    """障害トリアージ生成と同時に全showコマンドを自動実行する。"""
    from .command_popup import extract_cli_commands, simulate_command_execution
    _inline_key = f"_triage_inline_incident_{device_id}_{device_id}"
    _results = {}
    for ra in rec_actions:
        _steps = ra.get("steps", ra.get("command", ra.get("action", "")))
        for cmd in extract_cli_commands(_steps):
            if cmd not in _results:
                _results[cmd] = simulate_command_execution(cmd, device_id)
    if _results:
        st.session_state[_inline_key] = _results


def _generate_incident_triage_lazy(cand: dict, topology: dict) -> list:
    """選択された障害候補に対してのみ、オンデマンドでトリアージを生成する。

    cockpit.py での全候補一括生成を廃止し、表示時に1件だけ生成することで
    シナリオ切替直後の描画を高速化する。
    結果は session_state にキャッシュされ、次回以降は即座に返却される。
    """
    _dev_id = cand.get('id', '')
    _label = cand.get('label', '')
    _triage_cache_key = f"_triage_incident_{_dev_id}_{hash(_label[:200])}"

    # キャッシュヒット
    _cached = st.session_state.get(_triage_cache_key)
    if _cached is not None:
        return _cached

    # API キー & モデルの取得
    api_key = st.session_state.get("api_key")
    if not api_key:
        return []

    _genai_key = f"_genai_model_{api_key[:8]}"
    _genai_model = st.session_state.get(_genai_key)
    if not _genai_model:
        return []

    from .helpers import build_ci_context_for_chat

    from utils.sanitizer import sanitize_for_llm, sanitize_device_id

    ci = build_ci_context_for_chat(topology, _dev_id)
    vendor = ci.get("vendor", "Unknown")
    os_type = ci.get("os", "Unknown")
    model_name = ci.get("model", "Unknown")

    import json as _json
    import re as _re

    # ★ サニタイズ: デバイスIDとアラームラベルをマスキング
    _safe_dev_id = sanitize_device_id(_dev_id)
    _safe_label = sanitize_for_llm(_label, max_length=1000)

    _prompt = f"""あなたは熟練のネットワークAIOpsエンジニアです。
現在、以下の【対象機器】で障害アラームが発報されました。
運用者が【最初の5分以内】にCLIで実行すべき「初期確認」コマンドを、重要度順に【最大3つまで】JSON形式で出力してください。

【★ 初期確認の定義（厳守）】
・目的: 「現状の把握」のみ。状態確認（show系）コマンドだけを提示する
・禁止: config系コマンド（設定変更・復旧措置）は絶対に含めない
・禁止: 詳細な診断手順や判定基準の解説は不要（それは別レポートの役割）
・各コマンドは「何を確認するか」を1行で添え、効果は「この値が分かる」程度に留める

【対象機器の情報】
・ホスト名: {_safe_dev_id}
・メーカー: {vendor}
・OS: {os_type}
・機種名: {model_name}

【⚠️ 厳守事項：プラットフォームの限定】
・対象は上記の「ネットワーク専用機器」です。汎用Linuxサーバではありません。
・必ず {vendor} ({os_type}) の正規コマンド（例: {vendor}がCiscoなら 'show ~', Juniperなら 'show ~' や 'request ~'）を使用してください。
・Linux用のコマンド（top, ps, grep, kill, systemctl等）は【絶対に含めないでください】。
・監視ツール（Zabbix等）は導入済みのため、「監視設定の強化」等の提案は不要です。

【発報アラーム】
{_safe_label}

【出力JSONフォーマット】
必ず以下のキー構造のJSON配列（リスト）のみを出力してください。
[
  {{
    "title": "確認項目のタイトル（例: メモリ使用状況の確認）",
    "effect": "このコマンドで分かること（1行）",
    "priority": "high",
    "rationale": "なぜ最初にこれを確認すべきか（1行）",
    "steps": "show系コマンドのみ (改行は \\n を使用)"
  }}
]
"""
    try:
        from rate_limiter import GlobalRateLimiter
        _rl = GlobalRateLimiter()
        if not _rl.wait_for_slot(timeout=10, model_id="gemma-3-4b-it"):
            logger.warning(f"Rate limit reached for gemma-3-4b-it, skipping triage for {_dev_id}")
            return []
        _rl.record_request(model_id="gemma-3-4b-it")

        with st.spinner(f"🔄 {_dev_id} の初期確認を生成中..."):
            _response = _genai_model.generate_content(_prompt)
            _match = _re.search(r'\[\s*\{.*?\}\s*\]', _response.text, _re.DOTALL)

            if _match:
                _dynamic_actions = _json.loads(_match.group(0))
                if isinstance(_dynamic_actions, list) and len(_dynamic_actions) > 0:
                    _result = _dynamic_actions[:3]
                    st.session_state[_triage_cache_key] = _result
                    return _result
    except Exception as e:
        logger.warning(f"Incident triage lazy generation failed for {_dev_id}: {e}")

    return []


def _render_verification_detail(cand: dict):
    """相互検証の詳細を描画する。エージェント間で不一致がある場合に表示。"""
    _v = cand.get('verification', {})
    if not _v:
        return

    _dev_id = cand.get('id', '')
    _agreement = _v.get('agreement', '')
    _topo = _v.get('topology_score')
    _embed = _v.get('embedding_score')
    _gap = _v.get('confidence_gap', 0)
    _escalation = _v.get('escalation_required', False)

    if _escalation:
        _border_color = "#D32F2F"
        _bg_color = "#FFEBEE"
        _icon = "⚠️"
        _title = "エージェント間で大きな不一致 — 人間による確認を推奨"
    else:
        _border_color = "#F57C00"
        _bg_color = "#FFF3E0"
        _icon = "🔶"
        _title = "エージェント間で不一致を検出"

    _topo_str = f"{_topo*100:.0f}%" if _topo is not None else "N/A"
    _embed_str = f"{_embed*100:.0f}%" if _embed is not None else "N/A"

    with st.container(border=True):
        st.markdown(
            f"**{_icon} 相互検証: {_title}** — `{_dev_id}`"
        )
        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("Agent 1 (BFS/トポロジー)", _topo_str)
        with col2:
            st.metric("Agent 2 (Embedding/LLM)", _embed_str)
        with col3:
            st.metric("スコア差", f"{_gap*100:.0f}%")
        if _escalation:
            st.warning(
                "2つの独立した診断エージェントの評価が大きく乖離しています。"
                "物理シミュレーションとパターン照合で異なる結論が出ているため、"
                "人間による追加調査を推奨します。"
            )


