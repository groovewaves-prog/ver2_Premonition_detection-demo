# app.py (Refactored Entry Point)
import streamlit as st
import time
from utils.state import init_session_state
from ui.sidebar import render_sidebar
from ui.dashboard import render_site_status_board, render_triage_center
from ui.cockpit import render_incident_cockpit, prewarm_engines
from ui.tuning import render_tuning_dashboard
from ui.stream_dashboard import _get_simulator

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
        # ストリーム状態の確認（描画は cockpit 内 Future Radar 下に移動）
        sim = _get_simulator()
        stream_running = sim is not None and sim.is_started and not sim.is_complete

        # ★ エッセンス2: タブ切り替え時の「空回し」完全排除
        #   session_state でアクティブタブを追跡し、選択中のタブのみ重い計算を実行。
        #   Streamlit の仕様上 with tab: ブロック内も裏で実行されるため、
        #   if ガードで処理の発火そのものを制御する。
        _TAB_NAMES = ["🚀 Incident Cockpit", "🔧 Digital Twin Tuning"]
        _tab_key = "_active_ops_tab"
        if _tab_key not in st.session_state:
            st.session_state[_tab_key] = _TAB_NAMES[0]

        tab_ops, tab_tune = st.tabs(_TAB_NAMES)
        with tab_ops:
            if st.session_state[_tab_key] == _TAB_NAMES[0]:
                render_incident_cockpit(active_site, api_key)
            else:
                st.info("🚀 「Cockpit を表示」を押すとインシデント分析を実行します。")
                if st.button("🚀 Cockpit を表示", key="_activate_cockpit", type="primary"):
                    st.session_state[_tab_key] = _TAB_NAMES[0]
                    st.rerun()

        with tab_tune:
            if st.session_state[_tab_key] == _TAB_NAMES[1]:
                render_tuning_dashboard(active_site)
            else:
                st.info("🔧 「チューニング開始」を押すと Digital Twin の詳細分析が実行されます。")
                if st.button("🔧 チューニング開始", key="_activate_tuning", type="primary"):
                    st.session_state[_tab_key] = _TAB_NAMES[1]
                    st.rerun()

        # ストリーム実行中: 自動リフレッシュ（0.3s間隔で約2-3秒で全描画完了）
        _stream_needs_refresh = st.session_state.get("_stream_needs_refresh", False)
        if stream_running and _stream_needs_refresh:
            time.sleep(0.3)
            st.rerun()
    else:
        # ★ エンジン事前ウォームアップ: ダッシュボード表示中にバックグラウンドで
        #   LogicalRCA / DigitalTwinEngine を初期化し、「詳細」押下時の待ち時間を解消
        prewarm_engines()
        tab1, tab2 = st.tabs(["📊 拠点状態ボード", "🚨 トリアージ"])
        with tab1: render_site_status_board()
        with tab2: render_triage_center()

if __name__ == "__main__":
    main()
