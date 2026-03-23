# components/topology_graph/__init__.py
# Streamlit Custom Component: vis.js トポロジーグラフ
# iframe を再生成せずデータ差分のみ更新 → 白いベール根治
import os
import streamlit.components.v1 as components

_COMPONENT_NAME = "topology_graph"
_FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "frontend")

_component_func = components.declare_component(
    _COMPONENT_NAME,
    path=_FRONTEND_DIR,
)


def topology_graph(
    nodes: list,
    edges: list,
    zones: dict,
    use_fixed: bool,
    fit_pad: int,
    canvas_h: int,
    layout_js: dict,
    legend_html: str,
    font_size: int = 12,
    key: str = None,
):
    """vis.js トポロジーグラフを Custom Component として描画。

    iframe は初回のみ生成され、以降は onRender() でデータ差分更新される。
    """
    return _component_func(
        nodes=nodes,
        edges=edges,
        zones=zones,
        useFixed=use_fixed,
        fitPad=fit_pad,
        canvasH=canvas_h,
        layoutJs=layout_js,
        legendHtml=legend_html,
        fontSize=font_size,
        height=canvas_h,
        key=key,
        default=None,
    )


def impact_graph(
    nodes: list,
    edges: list,
    canvas_h: int = 370,
    key: str = None,
):
    """BFS 影響伝搬グラフを Custom Component として描画。"""
    return _component_func(
        nodes=nodes,
        edges=edges,
        zones={},
        useFixed=False,
        fitPad=30,
        canvasH=canvas_h,
        layoutJs={"mode": "impact"},
        legendHtml="",
        fontSize=12,
        height=canvas_h,
        key=key,
        default=None,
    )
