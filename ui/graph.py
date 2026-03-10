# ui/graph.py  ―  vis.js インタラクティブトポロジー描画
#   色優先順位・予兆アンバーハイライト・3分類対応
import json
import streamlit.components.v1 as components
from alarm_generator import NodeColor, Alarm
from typing import List


def render_topology_graph(topology: dict, alarms: List[Alarm], analysis_results: List[dict]):
    """
    vis.js でインタラクティブなトポロジーグラフを描画し、
    Streamlit の components.html() で埋め込む。

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

    # --- ノード生成 ---
    nodes = []
    for node_id, node in topology.items():
        if isinstance(node, dict):
            node_type = node.get('type', 'UNKNOWN')
            metadata = node.get('metadata', {})
            redundancy_type = metadata.get('redundancy_type') if isinstance(metadata, dict) else None
        else:
            node_type = getattr(node, 'type', 'UNKNOWN')
            metadata = getattr(node, 'metadata', {})
            redundancy_type = (metadata.get('redundancy_type')
                               if isinstance(metadata, dict)
                               else getattr(metadata, 'redundancy_type', None))

        # デフォルト（正常）
        bg_color = NodeColor.NORMAL
        border_color = NodeColor.NORMAL
        border_width = 1
        font_color = "#333"
        shape = "box"
        font_bg = None
        label_parts = [node_id, f"({node_type})"]
        status_tag = ""

        if redundancy_type:
            label_parts.append(f"[{redundancy_type}]")

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
                elif info['max_severity'] == 'CRITICAL':
                    bg_color = NodeColor.ROOT_CAUSE_CRITICAL
                    border_color = "#C62828"
                    border_width = 3
                    shape = "ellipse"
                    font_color = "#B71C1C"
                    status_tag = "ROOT CAUSE"
                else:
                    bg_color = NodeColor.ROOT_CAUSE_WARNING
                    border_color = "#F9A825"
                    border_width = 2
                    status_tag = "WARNING"
            else:
                # 非root_cause のアラーム
                if node_id in predicted_ids_real:
                    bg_color = "#FFB300"
                    border_color = "#E65100"
                    border_width = 4
                    font_color = "#E65100"
                    status_tag = "PREDICTION"
                elif node_id in predicted_ids_sim:
                    bg_color = "#FFE082"
                    border_color = "#BF360C"
                    border_width = 3
                    font_color = "#BF360C"
                    status_tag = "SIM-PRED"
                else:
                    # 3分類: symptom vs unrelated
                    cls = classification_map.get(node_id, "")
                    if cls == "symptom":
                        bg_color = "#FFE0B2"  # オレンジ系（派生）
                        border_color = "#E65100"
                        font_color = "#BF360C"
                        status_tag = "Symptom"
                    elif cls == "unrelated":
                        bg_color = "#E1BEE7"  # 薄紫（ノイズ）
                        border_color = "#7B1FA2"
                        shape = "diamond"
                        font_color = "#4A148C"
                        font_bg = "rgba(255,255,255,0.9)"
                        status_tag = "Unrelated"
                    else:
                        bg_color = NodeColor.UNREACHABLE
                        border_color = "#78909C"
                        font_color = "#546e7a"
                        status_tag = "Unreachable"

        # 2. 予兆ハイライト（アラームなし）
        elif node_id in predicted_ids_real:
            bg_color = "#FFB300"
            border_color = "#E65100"
            border_width = 4
            font_color = "#E65100"
            status_tag = "PREDICTION"
        elif node_id in predicted_ids_sim:
            bg_color = "#FFE082"
            border_color = "#BF360C"
            border_width = 3
            font_color = "#BF360C"
            status_tag = "SIM-PRED"

        # ラベル構築
        if status_tag:
            label_parts.append(f"[{status_tag}]")
        label_text = "\\n".join(label_parts)

        font_config = {
            "color": font_color,
            "size": 13,
            "face": "Arial",
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
            "widthConstraint": {"minimum": 100, "maximum": 180},
            "heightConstraint": {"minimum": 35},
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

    # --- 凡例HTML ---
    legend_html = """
    <div style="position:absolute;top:10px;right:10px;background:rgba(255,255,255,0.95);
                padding:10px 14px;border:1px solid #ccc;border-radius:6px;font-size:11px;
                font-family:Arial,sans-serif;line-height:1.8;z-index:100;">
        <div style="font-weight:bold;margin-bottom:4px;">Legend</div>
        <div><span style="display:inline-block;width:14px;height:14px;background:#ffcdd2;
             border:2px solid #C62828;border-radius:50%;vertical-align:middle;margin-right:5px;"></span>Root Cause (真因)</div>
        <div><span style="display:inline-block;width:14px;height:14px;background:#fff9c4;
             border:2px solid #F9A825;vertical-align:middle;margin-right:5px;"></span>Warning (警告)</div>
        <div><span style="display:inline-block;width:14px;height:14px;background:#e1bee7;
             border:2px solid #9C27B0;border-radius:50%;vertical-align:middle;margin-right:5px;"></span>Silent Suspect</div>
        <div><span style="display:inline-block;width:14px;height:14px;background:#FFB300;
             border:2px solid #E65100;vertical-align:middle;margin-right:5px;"></span>Prediction (予兆)</div>
        <div><span style="display:inline-block;width:14px;height:14px;background:#FFE0B2;
             border:2px solid #E65100;vertical-align:middle;margin-right:5px;"></span>Symptom (派生)</div>
        <div><span style="display:inline-block;width:14px;height:14px;background:#E1BEE7;
             border:2px solid #7B1FA2;transform:rotate(45deg);vertical-align:middle;margin-right:5px;"></span>Unrelated (ノイズ)</div>
        <div><span style="display:inline-block;width:14px;height:14px;background:#cfd8dc;
             border:1px solid #78909C;vertical-align:middle;margin-right:5px;"></span>Unreachable</div>
        <div><span style="display:inline-block;width:14px;height:14px;background:#e8f5e9;
             border:1px solid #a5d6a7;vertical-align:middle;margin-right:5px;"></span>Normal (正常)</div>
    </div>
    """

    # --- vis.js HTML ---
    nodes_json = json.dumps(nodes, ensure_ascii=False)
    edges_json = json.dumps(edges, ensure_ascii=False)

    html = f"""
<html><head>
<script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
<style>
  body {{ margin:0; padding:0; overflow:hidden; }}
  #container {{ position:relative; width:100%; height:600px; }}
  #mynetwork {{ height:600px; border:1px solid #e0e0e0; border-radius:4px; }}
</style>
</head>
<body>
<div id="container">
    <div id="mynetwork"></div>
    {legend_html}
</div>
<script>
var nodes = new vis.DataSet({nodes_json});
var edges = new vis.DataSet({edges_json});
var data = {{ nodes: nodes, edges: edges }};
var options = {{
    layout: {{
        hierarchical: {{
            enabled: true,
            direction: "DU",
            sortMethod: "directed",
            levelSeparation: 120,
            nodeSpacing: 220,
            treeSpacing: 250,
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
        font: {{ size: 13, face: 'Arial' }},
        margin: {{ top: 8, bottom: 8, left: 10, right: 10 }}
    }},
    edges: {{
        smooth: {{ type: 'cubicBezier', forceDirection: 'vertical', roundness: 0.4 }}
    }}
}};
var network = new vis.Network(document.getElementById('mynetwork'), data, options);
network.fit({{ padding: 40 }});
</script></body></html>
"""
    components.html(html, height=620)
