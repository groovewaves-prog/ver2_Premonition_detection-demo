# ui/stream/helpers.py — ストリームダッシュボード共通ヘルパー
#
# SVG/HTML描画ユーティリティ、SVGキャッシュ、セッションステート管理

import streamlit as st
from typing import Optional
from digital_twin_pkg.alarm_stream import AlarmStreamSimulator


def st_html(html: str, height: int = 0) -> None:
    """SVG/HTMLをStreamlitで描画する。

    height > 0 の場合: st.components.v1.html() で明示的高さを指定（SVG用）。
    height == 0 の場合: st.markdown(unsafe_allow_html=True) を使用（通常HTML用）。

    st.html() は iframe でSVG高さが自動計算されないため使用しない。
    """
    if height > 0:
        import streamlit.components.v1 as components
        components.html(html, height=height, scrolling=False)
    else:
        st.markdown(html, unsafe_allow_html=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# セッションステート管理
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_STREAM_STATE_KEY = "alarm_stream_sim"
_STREAM_EVENTS_KEY = "alarm_stream_events"


def get_simulator() -> Optional[AlarmStreamSimulator]:
    state = st.session_state.get(_STREAM_STATE_KEY)
    if state is None:
        return None
    return AlarmStreamSimulator.from_state_dict(state)


def save_simulator(sim: AlarmStreamSimulator):
    st.session_state[_STREAM_STATE_KEY] = sim.to_state_dict()


def clear_simulator():
    st.session_state.pop(_STREAM_STATE_KEY, None)
    st.session_state.pop(_STREAM_EVENTS_KEY, None)
    # SVGキャッシュもクリア（古いストリームの描画結果を破棄）
    st.session_state.pop(SVG_CACHE_KEY, None)
    st.session_state.pop("stream_explore_level", None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SVG キャッシュ（同一パラメータなら再生成をスキップ）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SVG_CACHE_KEY = "_stream_svg_cache"


def svg_cached(cache_name: str, cache_key: str, generator, *args, **kwargs) -> str:
    """SVG生成結果をsession_stateにキャッシュ。cache_keyが変わった時だけ再生成。"""
    if SVG_CACHE_KEY not in st.session_state:
        st.session_state[SVG_CACHE_KEY] = {}
    cache = st.session_state[SVG_CACHE_KEY]
    full_key = f"{cache_name}:{cache_key}"
    if full_key in cache:
        return cache[full_key]
    svg = generator(*args, **kwargs)
    cache[full_key] = svg
    return svg
