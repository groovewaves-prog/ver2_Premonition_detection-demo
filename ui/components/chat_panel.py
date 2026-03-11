# ui/components/chat_panel.py — Chat with AI Agent セクション
import json
import streamlit as st
from typing import Optional

from utils.llm_helper import generate_content_with_retry
from .helpers import build_ci_context_for_chat

try:
    import google.generativeai as genai
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False


def render_chat_panel(
    selected_incident_candidate: Optional[dict],
    target_device_id: Optional[str],
    topology: dict,
    api_key: Optional[str],
):
    """Chat with AI Agent（Expander形式）を描画"""
    with st.expander("💬 Chat with AI Agent", expanded=False):
        _chat_target_id = ""
        if selected_incident_candidate:
            _chat_target_id = selected_incident_candidate.get("id", "") or ""
        if not _chat_target_id and target_device_id:
            _chat_target_id = target_device_id

        _chat_ci = build_ci_context_for_chat(topology, _chat_target_id) if _chat_target_id else {}
        if _chat_ci:
            _vendor     = _chat_ci.get("vendor", "") or "Unknown"
            _os         = _chat_ci.get("os",     "") or "Unknown"
            _model_name = _chat_ci.get("model",  "") or "Unknown"
            st.caption(f"対象機器: {_chat_target_id}   Vendor: {_vendor}   OS: {_os}   Model: {_model_name}")

        # クイック質問ボタン
        q1, q2, q3 = st.columns(3)
        with q1:
            if st.button("設定バックアップ", use_container_width=True):
                st.session_state.chat_quick_text = "この機器で、現在の設定を安全にバックアップする手順とコマンド例を教えてください。"
        with q2:
            if st.button("ロールバック", use_container_width=True):
                st.session_state.chat_quick_text = "この機器で、変更をロールバックする代表的な手順（候補）と注意点を教えてください。"
        with q3:
            if st.button("確認コマンド", use_container_width=True):
                st.session_state.chat_quick_text = "今回の症状を切り分けるために、まず実行すべき確認コマンド（show/diagnostic）を優先度順に教えてください。"

        if st.session_state.chat_quick_text:
            st.info("クイック質問（コピーして貼り付け）")
            st.code(st.session_state.chat_quick_text)

        if st.session_state.chat_session is None and api_key and GENAI_AVAILABLE:
            genai.configure(api_key=api_key)
            model_obj = genai.GenerativeModel("gemma-3-4b-it")
            st.session_state.chat_session = model_obj.start_chat(history=[])

        tab1, tab2 = st.tabs(["💬 会話", "📝 履歴"])

        with tab1:
            if st.session_state.messages:
                last_msg = st.session_state.messages[-1]
                if last_msg["role"] == "assistant":
                    st.info("🤖 最新の回答")
                    with st.container(height=300):
                        st.markdown(last_msg["content"])

            st.markdown("---")
            prompt = st.text_area(
                "質問を入力してください:",
                height=70,
                placeholder="Ctrl+Enter または 送信ボタンで送信",
                key="chat_textarea"
            )

            col1, col2, col3 = st.columns([3, 1, 1])
            with col2:
                send_button = st.button("送信", type="primary", use_container_width=True)
            with col3:
                if st.button("クリア"):
                    st.session_state.messages = []
                    st.rerun()

            if send_button and prompt:
                st.session_state.messages.append({"role": "user", "content": prompt})
                if st.session_state.chat_session:
                    ci = build_ci_context_for_chat(topology, _chat_target_id) if _chat_target_id else {}
                    ci_prompt = f"""あなたはネットワーク運用（NOC/SRE）の実務者です。
次の CI 情報と Config 抜粋を必ず参照して、具体的に回答してください。一般論だけで終わらせないでください。

【CI (JSON)】
{json.dumps(ci, ensure_ascii=False, indent=2)}

【ユーザーの質問】
{prompt}

回答ルール:
- CI/Config に基づく具体手順・コマンド例を提示する
- 追加確認が必要なら、質問は最小限（1〜2点）に絞る
- 不明な前提は推測せず「CIに無いので確認が必要」と明記する
"""
                    with st.spinner("AI が回答を生成中..."):
                        try:
                            response = generate_content_with_retry(
                                st.session_state.chat_session.model, ci_prompt, stream=False
                            )
                            if response:
                                full_response = response.text if hasattr(response, "text") else str(response)
                                if not full_response.strip():
                                    full_response = "AI応答が空でした。"
                                st.session_state.messages.append({"role": "assistant", "content": full_response})
                            else:
                                st.error("AIからの応答がありませんでした。")
                        except Exception as e:
                            st.error(f"エラーが発生しました: {e}")

        with tab2:
            if st.session_state.messages:
                history_container = st.container(height=400)
                with history_container:
                    for i, msg in enumerate(st.session_state.messages):
                        icon = "🤖" if msg["role"] == "assistant" else "👤"
                        with st.container(border=True):
                            st.markdown(f"{icon} **{msg['role'].upper()}** (メッセージ {i+1})")
                            st.markdown(msg["content"])
            else:
                st.info("会話履歴はまだありません。")
