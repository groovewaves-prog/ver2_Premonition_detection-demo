# ui/graph.py  ―  vis.js インタラクティブトポロジー描画
#   色優先順位・予兆アンバーハイライト・3分類対応
import json
import streamlit as st
import streamlit.components.v1 as components
from alarm_generator import NodeColor, Alarm
from typing import List, Dict, Any, Tuple


def render_topology_graph(topology: dict, alarms: List[Alarm], analysis_results: List[dict]):
    """
    vis.js でインタラクティブなトポロジーグラフを描画し、
    Streamlit の components.html() で埋め込む。
    凡例はマップ外に Streamlit ウィジェットとして表示。

    色優先順位（高→低）:
      1. Root Cause CRITICAL（赤）/ WARNING（黄）/ SILENT（紫）
      2. 実予兆 amber / シミュ予兆 薄amber
      3. Symptom (派生) — オレンジ
      4. Unreachable — グレー
      5. Unrelated (ノイズ) — 薄紫ダイヤ
      6. Normal — グリーン
    """
    # --- アラーム情報をデバイスIDでマッピング ---
    alarm_map = {}
    for a in alarms:
        if a.device_id not in alarm_map:
            alarm_map[a.device_id] = {
                'is_root_cause': False,
                'is_silent_suspect': False,
                'max_severity': 'INFO'
            }
        info = alarm_map[a.device_id]
        if a.is_root_cause:
            info['is_root_cause'] = True
        if a.is_silent_suspect:
            info['is_silent_suspect'] = True
        severity_order = {'CRITICAL': 3, 'WARNING': 2, 'INFO': 1}
        if severity_order.get(a.severity, 0) > severity_order.get(info['max_severity'], 0):
            info['max_severity'] = a.severity

    # --- 予兆検知IDのセット ---
    predicted_ids_real = {r['id'] for r in analysis_results
                         if r.get('is_prediction') and r.get('source') != 'simulation'}
    predicted_ids_sim = {r['id'] for r in analysis_results
                        if r.get('is_prediction') and r.get('source') == 'simulation'}

    # --- 3分類情報 ---
    classification_map = {}
    for r in analysis_results:
        if r.get('classification'):
            classification_map[r['id']] = r['classification']

    # --- 各状態の使用有無を追跡（凡例表示用） ---
    used_states = set()

    # --- ノード生成 ---
    nodes = []
    for node_id, node in topology.items():
        if isinstance(node, dict):
            node_type = node.get('type', 'UNKNOWN')
            metadata = node.get('metadata', {})
            redundancy_type = metadata.get('redundancy_type') if isinstance(metadata, dict) else None
            vendor = metadata.get('vendor') if isinstance(metadata, dict) else None
        else:
            node_type = getattr(node, 'type', 'UNKNOWN')
            metadata = getattr(node, 'metadata', {})
            redundancy_type = (metadata.get('redundancy_type')
                               if isinstance(metadata, dict)
                               else getattr(metadata, 'redundancy_type', None))
            vendor = (metadata.get('vendor')
                      if isinstance(metadata, dict)
                      else getattr(metadata, 'vendor', None))

        # デフォルト（正常）
        bg_color = NodeColor.NORMAL
        border_color = "#a5d6a7"
        border_width = 2
        font_color = "#333"
        shape = "box"
        font_bg = None
        label_parts = [node_id, f"({node_type})"]
        status_tag = ""
        state_key = "normal"

        # 冗長タイプ: "PSU" → "PSU Redundancy"
        if redundancy_type:
            rt_display = f"{redundancy_type} Redundancy" if redundancy_type in ("PSU", "HA", "STACK") else redundancy_type
            label_parts.append(f"[{rt_display}]")
        # ベンダー名
        if vendor:
            label_parts.append(f"[{vendor}]")

        # --- 色決定（優先順位順） ---

        # 1. アラームに基づく色（最優先）
        if node_id in alarm_map:
            info = alarm_map[node_id]
            if info['is_root_cause']:
                if info['is_silent_suspect']:
                    bg_color = NodeColor.SILENT_FAILURE
                    border_color = "#9C27B0"
                    border_width = 3
                    shape = "ellipse"
                    status_tag = "SILENT SUSPECT"
                    state_key = "silent"
                elif info['max_severity'] == 'CRITICAL':
                    bg_color = NodeColor.ROOT_CAUSE_CRITICAL
                    border_color = "#C62828"
                    border_width = 3
                    shape = "ellipse"
                    font_color = "#B71C1C"
                    status_tag = "ROOT CAUSE"
                    state_key = "root_cause"
                else:
                    bg_color = NodeColor.ROOT_CAUSE_WARNING
                    border_color = "#F9A825"
                    border_width = 3
                    status_tag = "WARNING"
                    state_key = "warning"
            else:
                # 非root_cause のアラーム
                if node_id in predicted_ids_real:
                    bg_color = "#FFB300"
                    border_color = "#E65100"
                    border_width = 4
                    font_color = "#E65100"
                    status_tag = "PREDICTION"
                    state_key = "prediction"
                elif node_id in predicted_ids_sim:
                    bg_color = "#FFE082"
                    border_color = "#BF360C"
                    border_width = 3
                    font_color = "#BF360C"
                    status_tag = "SIM-PRED"
                    state_key = "prediction"
                else:
                    # 3分類: symptom vs unrelated
                    cls = classification_map.get(node_id, "")
                    if cls == "symptom":
                        bg_color = "#FFE0B2"
                        border_color = "#E65100"
                        font_color = "#BF360C"
                        status_tag = "Symptom"
                        state_key = "symptom"
                    elif cls == "unrelated":
                        bg_color = "#E1BEE7"
                        border_color = "#7B1FA2"
                        shape = "diamond"
                        font_color = "#4A148C"
                        font_bg = "rgba(255,255,255,0.9)"
                        status_tag = "Unrelated"
                        state_key = "unrelated"
                    else:
                        bg_color = NodeColor.UNREACHABLE
                        border_color = "#78909C"
                        font_color = "#546e7a"
                        status_tag = "Unreachable"
                        state_key = "unreachable"

        # 2. 予兆ハイライト（アラームなし）
        elif node_id in predicted_ids_real:
            bg_color = "#FFB300"
            border_color = "#E65100"
            border_width = 4
            font_color = "#E65100"
            status_tag = "PREDICTION"
            state_key = "prediction"
        elif node_id in predicted_ids_sim:
            bg_color = "#FFE082"
            border_color = "#BF360C"
            border_width = 3
            font_color = "#BF360C"
            status_tag = "SIM-PRED"
            state_key = "prediction"

        used_states.add(state_key)

        # ラベル構築 — "\n" (実際の改行文字) で結合して vis.js が改行描画する
        if status_tag:
            label_parts.append(f"[{status_tag}]")
        label_text = "\n".join(label_parts)

        font_config = {
            "color": font_color,
            "size": 14,
            "face": "Arial, sans-serif",
            "bold": status_tag in ("ROOT CAUSE", "PREDICTION"),
        }
        if font_bg:
            font_config["background"] = font_bg

        node_obj = {
            "id": node_id,
            "label": label_text,
            "color": {"background": bg_color, "border": border_color},
            "shape": shape,
            "borderWidth": border_width,
            "font": font_config,
            "widthConstraint": {"minimum": 150, "maximum": 220},
            "heightConstraint": {"minimum": 50},
        }
        nodes.append(node_obj)

    # --- エッジ生成 ---
    edges = []
    added_edges = set()
    for node_id, node in topology.items():
        parent_id = node.get('parent_id') if isinstance(node, dict) else getattr(node, 'parent_id', None)
        if parent_id:
            edge_key = (parent_id, node_id)
            if edge_key not in added_edges:
                edges.append({"from": parent_id, "to": node_id, "arrows": "to", "color": "#999"})
                added_edges.add(edge_key)

            # 冗長ペア
            p_node = topology.get(parent_id)
            if p_node:
                rg = p_node.get('redundancy_group') if isinstance(p_node, dict) else getattr(p_node, 'redundancy_group', None)
                if rg:
                    for nid, n in topology.items():
                        n_rg = n.get('redundancy_group') if isinstance(n, dict) else getattr(n, 'redundancy_group', None)
                        if n_rg == rg and nid != parent_id:
                            edge_key2 = (nid, node_id)
                            if edge_key2 not in added_edges:
                                edges.append({
                                    "from": nid, "to": node_id,
                                    "arrows": "to",
                                    "color": {"color": "#B0BEC5", "opacity": 0.6},
                                    "dashes": True,
                                })
                                added_edges.add(edge_key2)

    # --- vis.js HTML (凡例なし — マップ外に Streamlit で描画) ---
    nodes_json = json.dumps(nodes, ensure_ascii=False)
    edges_json = json.dumps(edges, ensure_ascii=False)

    html = f"""
<html><head>
<script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
<style>
  body {{ margin:0; padding:0; overflow:hidden; }}
  #mynetwork {{ width:100%; height:600px; border:1px solid #e0e0e0; border-radius:4px; }}
</style>
</head>
<body>
<div id="mynetwork"></div>
<script>
var nodes = new vis.DataSet({nodes_json});
var edges = new vis.DataSet({edges_json});
var data = {{ nodes: nodes, edges: edges }};
var options = {{
    layout: {{
        hierarchical: {{
            enabled: true,
            direction: "UD",
            sortMethod: "directed",
            levelSeparation: 140,
            nodeSpacing: 240,
            treeSpacing: 280,
            blockShifting: true,
            edgeMinimization: true,
            parentCentralization: true
        }}
    }},
    physics: {{ enabled: false }},
    interaction: {{
        hover: true,
        tooltipDelay: 100,
        zoomView: true,
        dragView: true,
        dragNodes: false
    }},
    nodes: {{
        font: {{ size: 14, face: 'Arial, sans-serif', multi: false }},
        margin: {{ top: 10, bottom: 10, left: 14, right: 14 }},
        shapeProperties: {{ borderRadius: 8 }}
    }},
    edges: {{
        smooth: {{ type: 'cubicBezier', forceDirection: 'vertical', roundness: 0.4 }}
    }}
}};
var network = new vis.Network(document.getElementById('mynetwork'), data, options);
network.fit({{ padding: 50 }});
</script></body></html>
"""
    components.html(html, height=680)

    # --- 凡例を Streamlit 側に描画（マップ外・被りなし） ---
    # 現在使用中の状態のみ表示
    _LEGEND_ITEMS = [
        ("root_cause",  "#ffcdd2", "#C62828", "border-radius:50%", "Root Cause (真因)"),
        ("warning",     "#fff9c4", "#F9A825", "",                  "Warning (警告)"),
        ("silent",      "#e1bee7", "#9C27B0", "border-radius:50%", "Silent Suspect"),
        ("prediction",  "#FFB300", "#E65100", "",                  "Prediction (予兆)"),
        ("symptom",     "#FFE0B2", "#E65100", "",                  "Symptom (派生)"),
        ("unrelated",   "#E1BEE7", "#7B1FA2", "transform:rotate(45deg)", "Unrelated (ノイズ)"),
        ("unreachable", "#cfd8dc", "#78909C", "",                  "Unreachable"),
        ("normal",      "#e8f5e9", "#a5d6a7", "",                  "Normal (正常)"),
    ]

    legend_items_html = []
    for key, bg, border, extra_style, text in _LEGEND_ITEMS:
        if key in used_states:
            swatch = (
                f'<span style="display:inline-block;width:13px;height:13px;'
                f'background:{bg};border:2px solid {border};{extra_style};'
                f'vertical-align:middle;margin-right:6px;"></span>'
            )
            legend_items_html.append(f"{swatch} {text}")

    if legend_items_html:
        legend_row = "&nbsp;&nbsp;&nbsp;".join(legend_items_html)
        st.markdown(
            f'<div style="font-size:12px;font-family:Arial,sans-serif;'
            f'padding:6px 12px;background:#fafafa;border:1px solid #e0e0e0;'
            f'border-radius:4px;margin-top:4px;">'
            f'<b>Legend:</b>&nbsp;&nbsp;{legend_row}</div>',
            unsafe_allow_html=True,
        )


# =====================================================
# BFS 影響伝搬グラフ
# =====================================================

# トポロジーマップと同じ色定義（状態ベース）
_IMPACT_STATE_COLORS = {
    "root_cause_critical": {"bg": "#ffcdd2", "border": "#C62828", "font": "#B71C1C"},
    "root_cause_warning":  {"bg": "#fff9c4", "border": "#F9A825", "font": "#333"},
    "silent":              {"bg": "#e1bee7", "border": "#9C27B0", "font": "#333"},
    "symptom":             {"bg": "#FFE0B2", "border": "#E65100", "font": "#BF360C"},
    "unreachable":         {"bg": "#cfd8dc", "border": "#78909C", "font": "#546e7a"},
    "normal":              {"bg": "#e8f5e9", "border": "#a5d6a7", "font": "#333"},
}


def render_impact_graph(
    root_device_id: str,
    downstream_impacts: List[Tuple[str, int]],
    topology: dict,
    analysis_results: List[Dict[str, Any]] = None,
    alarms: list = None,
):
    """
    BFS影響伝搬グラフを vis.js で描画する。
    色はトポロジーマップと統一された状態ベースの配色を使用。

    Args:
        root_device_id: 真因デバイスID
        downstream_impacts: [(device_id, hop_distance), ...] — _get_downstream_impact() の出力
        topology: トポロジー辞書（parent_id 参照用）
        analysis_results: 分析結果（ノード色決定用）
        alarms: アラーム一覧（severity/silent判定用）
    """
    if not downstream_impacts:
        st.caption("影響範囲なし（配下デバイスなし）")
        return

    # --- 状態マップ構築 ---
    classification_map = {}
    severity_map = {}
    if analysis_results:
        for r in analysis_results:
            classification_map[r.get('id', '')] = r.get('classification', '')
            severity_map[r.get('id', '')] = r.get('status', '')

    alarm_info_map = {}
    if alarms:
        for a in alarms:
            if a.device_id not in alarm_info_map:
                alarm_info_map[a.device_id] = {'severity': 'INFO', 'is_silent': False}
            if a.severity == 'CRITICAL':
                alarm_info_map[a.device_id]['severity'] = 'CRITICAL'
            elif a.severity == 'WARNING' and alarm_info_map[a.device_id]['severity'] != 'CRITICAL':
                alarm_info_map[a.device_id]['severity'] = 'WARNING'
            if hasattr(a, 'is_silent_suspect') and a.is_silent_suspect:
                alarm_info_map[a.device_id]['is_silent'] = True

    def _get_node_state(dev_id: str, is_root: bool = False) -> str:
        """トポロジーマップと同じロジックで状態を判定"""
        alarm_info = alarm_info_map.get(dev_id, {})
        cls = classification_map.get(dev_id, '')

        if is_root or cls == 'root_cause':
            if alarm_info.get('is_silent'):
                return "silent"
            elif alarm_info.get('severity') == 'CRITICAL' or severity_map.get(dev_id) in ('RED', 'CRITICAL'):
                return "root_cause_critical"
            else:
                return "root_cause_warning"
        elif cls == 'symptom':
            return "symptom"
        elif alarm_info.get('severity') in ('CRITICAL', 'WARNING'):
            return "symptom"
        else:
            return "unreachable"

    # --- ノード生成 ---
    nodes = []

    def _get_node_type(dev_id: str) -> str:
        node = topology.get(dev_id, {})
        if isinstance(node, dict):
            return node.get('type', 'UNKNOWN')
        return getattr(node, 'type', 'UNKNOWN')

    # Root Cause ノード
    rc_type = _get_node_type(root_device_id)
    rc_state = _get_node_state(root_device_id, is_root=True)
    rc_col = _IMPACT_STATE_COLORS[rc_state]
    nodes.append({
        "id": root_device_id,
        "label": f"{root_device_id}\n({rc_type})\n[ROOT CAUSE]",
        "color": {"background": rc_col["bg"], "border": rc_col["border"]},
        "shape": "ellipse",
        "borderWidth": 3,
        "font": {"color": rc_col["font"], "size": 14, "face": "Arial", "bold": True},
        "widthConstraint": {"minimum": 110, "maximum": 200},
        "level": 0,
    })

    # 影響デバイスノード
    for dev_id, hop in downstream_impacts:
        dev_type = _get_node_type(dev_id)
        dev_state = _get_node_state(dev_id, is_root=False)
        dev_col = _IMPACT_STATE_COLORS[dev_state]
        nodes.append({
            "id": dev_id,
            "label": f"{dev_id}\n({dev_type})\n[{hop}hop]",
            "color": {"background": dev_col["bg"], "border": dev_col["border"]},
            "shape": "box",
            "borderWidth": 2,
            "font": {"color": dev_col["font"], "size": 12, "face": "Arial"},
            "widthConstraint": {"minimum": 100, "maximum": 180},
            "level": hop,
        })

    # --- エッジ生成（トポロジーの parent_id から） ---
    impact_ids = {root_device_id} | {d[0] for d in downstream_impacts}
    edges = []
    added = set()
    for dev_id, hop in downstream_impacts:
        node = topology.get(dev_id, {})
        parent_id = node.get('parent_id') if isinstance(node, dict) else getattr(node, 'parent_id', None)
        if parent_id and parent_id in impact_ids:
            key = (parent_id, dev_id)
            if key not in added:
                width = max(1, 4 - hop)
                edges.append({
                    "from": parent_id, "to": dev_id,
                    "arrows": {"to": {"enabled": True, "scaleFactor": 0.8}},
                    "color": {"color": "#999", "opacity": 0.7},
                    "width": width,
                    "smooth": {"type": "cubicBezier", "forceDirection": "vertical", "roundness": 0.3},
                })
                added.add(key)

    # --- 統計サマリ ---
    hop_counts = {}
    for _, hop in downstream_impacts:
        hop_counts[hop] = hop_counts.get(hop, 0) + 1
    total = len(downstream_impacts)

    nodes_json = json.dumps(nodes, ensure_ascii=False)
    edges_json = json.dumps(edges, ensure_ascii=False)

    html = f"""
<html><head>
<script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
<style>
  body {{ margin:0; padding:0; overflow:hidden; }}
  #impact-net {{ width:100%; height:350px; border:1px solid #e0e0e0; border-radius:4px; }}
</style>
</head>
<body>
<div id="impact-net"></div>
<script>
var nodes = new vis.DataSet({nodes_json});
var edges = new vis.DataSet({edges_json});
var data = {{ nodes: nodes, edges: edges }};
var options = {{
    layout: {{
        hierarchical: {{
            enabled: true,
            direction: "UD",
            sortMethod: "directed",
            levelSeparation: 90,
            nodeSpacing: 180,
            treeSpacing: 200,
            parentCentralization: true
        }}
    }},
    physics: {{ enabled: false }},
    interaction: {{ hover: true, zoomView: true, dragView: true, dragNodes: false }},
    nodes: {{
        font: {{ size: 12, face: 'Arial' }},
        margin: {{ top: 6, bottom: 6, left: 8, right: 8 }}
    }},
    edges: {{
        smooth: {{ type: 'cubicBezier', forceDirection: 'vertical', roundness: 0.3 }}
    }}
}};
var network = new vis.Network(document.getElementById('impact-net'), data, options);
network.fit({{ padding: 30 }});
</script></body></html>
"""
    components.html(html, height=370)

    # ホップ距離内訳バー
    hop_labels = []
    sym_col = _IMPACT_STATE_COLORS["symptom"]
    for h in sorted(hop_counts.keys()):
        hop_labels.append(
            f'<span style="display:inline-block;width:12px;height:12px;'
            f'background:{sym_col["bg"]};border:1px solid {sym_col["border"]};'
            f'vertical-align:middle;margin-right:4px;border-radius:2px;"></span>'
            f'{h}hop: {hop_counts[h]}台'
        )
    summary = f"影響範囲: 計 {total}台&nbsp;&nbsp;|&nbsp;&nbsp;" + "&nbsp;&nbsp;&nbsp;".join(hop_labels)
    st.markdown(
        f'<div style="font-size:12px;font-family:Arial,sans-serif;'
        f'padding:5px 12px;background:#fff3e0;border:1px solid #ffe0b2;'
        f'border-radius:4px;margin-top:4px;">{summary}</div>',
        unsafe_allow_html=True,
    )
