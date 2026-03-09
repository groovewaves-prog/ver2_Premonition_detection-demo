# app.py (Refactored Entry Point)
import streamlit as st
import time
from utils.state import init_session_state
from ui.sidebar import render_sidebar
from ui.dashboard import render_site_status_board, render_triage_center
from ui.cockpit import render_incident_cockpit
from ui.tuning import render_tuning_dashboard
from ui.stream_dashboard import render_stream_dashboard, _get_simulator

import logging
import warnings
import os

# 不要な警告（Warning）を非表示にする
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)

# おしゃべりなAIライブラリのログ出力を「ERRORのみ」に制限する
logging.getLogger("sentence_transformers").setLevel(logging.ERROR)
logging.getLogger("transformers").setLevel(logging.ERROR)
logging.getLogger("chromadb").setLevel(logging.ERROR)
logging.getLogger("torch").setLevel(logging.ERROR)

# Tokenizerの並列処理に関する不要な警告を抑止
os.environ["TOKENIZERS_PARALLELISM"] = "false"

st.set_page_config(page_title="AIOps Cockpit", page_icon="🛡️", layout="wide")

def main():
    init_session_state()
    api_key = render_sidebar()
    
    st.title("🛡️ AIOps インシデント・コックピット")
    
    active_site = st.session_state.get("active_site")
    
    if active_site:
        # ストリームダッシュボード（実行中のみ表示）
        sim = _get_simulator()
        stream_active = sim is not None and sim.is_started and not sim.is_complete
        if stream_active:
            needs_refresh = render_stream_dashboard()

        tab_ops, tab_tune = st.tabs(["🚀 Incident Cockpit", "🔧 Digital Twin Tuning"])
        with tab_ops:
            render_incident_cockpit(active_site, api_key)
        with tab_tune:
            render_tuning_dashboard(active_site)

        # ストリーム実行中: 自動リフレッシュ
        if stream_active and needs_refresh:
            time.sleep(2)
            st.rerun()
    else:
        tab1, tab2 = st.tabs(["📊 拠点状態ボード", "🚨 トリアージ"])
        with tab1: render_site_status_board()
        with tab2: render_triage_center()

if __name__ == "__main__":
    main()
