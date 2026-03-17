# ui/graph.py  ―  vis.js インタラクティブトポロジー描画
#   色優先順位・予兆アンバーハイライト・3分類対応
import json
import streamlit as st
import streamlit.components.v1 as components
from alarm_generator import NodeColor, Alarm
from typing import List, Dict, Any, Tuple


# デバイスタイプ別のデフォルト形状・色定義（configs/device_types.json から取得）
# アラーム状態（赤/黄/アンバー等）はこれを上書きする
from configs.device_registry import get_all_visuals as _get_all_visuals, get_visual as _get_visual

_DEVICE_TYPE_VISUALS = _get_all_visuals()


_ZONE_AUTO_PALETTE = [
    {"color": "rgba(200,230,201,0.18)", "border": "#a5d6a7"},
    {"color": "rgba(187,222,251,0.18)", "border": "#90caf9"},
    {"color": "rgba(255,224,178,0.18)", "border": "#ffcc80"},
    {"color": "rgba(225,190,231,0.18)", "border": "#ce93d8"},
    {"color": "rgba(255,205,210,0.18)", "border": "#ef9a9a"},
    {"color": "rgba(178,235,242,0.18)", "border": "#80deea"},
    {"color": "rgba(237,231,246,0.22)", "border": "#b39ddb"},
    {"color": "rgba(220,237,200,0.18)", "border": "#aed581"},
]


def _load_zones_for_site(topology: dict) -> dict:
    """現在のサイトのトポロジーJSONから _zones を読み込む。
    _zones が未定義の場合は metadata.location からゾーンを自動生成する。
    """
    from pathlib import Path
    site_id = st.session_state.get("active_site", "A")
    topo_path = Path(__file__).parent.parent / "topologies" / f"topology_{site_id.lower()}.json"
    if not topo_path.exists():
        return {}
    try:
        import json as _json
        with open(topo_path, 'r', encoding='utf-8') as f:
            raw = _json.load(f)
    except Exception:
        return {}

    # 明示的 _zones 定義があればそれを使用
    if "_zones" in raw:
        return raw["_zones"]

    # metadata.location からゾーンを自動生成
    location_groups: dict = {}
    for node_id, node_data in topology.items():
        if not isinstance(node_data, dict):
            continue
        metadata = node_data.get("metadata", {})
        if not isinstance(metadata, dict):
            continue
        loc = metadata.get("location")
        if loc:
            location_groups.setdefault(loc, []).append(node_id)

    zones = {}
    for i, (loc, node_ids) in enumerate(location_groups.items()):
        palette = _ZONE_AUTO_PALETTE[i % len(_ZONE_AUTO_PALETTE)]
        zone_key = f"auto_{i}"
        zones[zone_key] = {
            "label": loc,
            "color": palette["color"],
            "border": palette["border"],
            "nodes": node_ids,
        }
    return zones


def _compute_fixed_positions(zones: dict) -> dict:
    """zones の rows/grid 定義からノードの固定座標を算出する。

    レイアウト規則:
      - 各ゾーンは grid=[col, row, colspan, rowspan] でゾーングリッド上に配置
      - rows=[[node_ids], ...] でゾーン内のノード配列を行単位で定義
      - _grid 設定で列幅・行高・ノード間隔を制御
    定義がなければ空辞書を返す（→ vis.js 自動レイアウトへフォールバック）。
    """
    has_layout = any(
        isinstance(v, dict) and "rows" in v
        for k, v in zones.items() if not k.startswith("_")
    )
    if not has_layout:
        return {}

    cfg = zones.get("_grid", {})
    COL_W = cfg.get("col_width", 340)
    ROW_H = cfg.get("row_height", 340)
    H_GAP = cfg.get("node_h_gap", 150)
    V_GAP = cfg.get("node_v_gap", 100)

    positions = {}
    for zk, zv in zones.items():
        if zk.startswith("_") or not isinstance(zv, dict):
            continue
        rows = zv.get("rows")
        if not rows:
            continue
        g = zv.get("grid", [0, 0, 1, 1])
        gc, gr = g[0], g[1]
        colspan = g[2] if len(g) > 2 else 1
        cx = gc * COL_W + colspan * COL_W / 2
        sy = gr * ROW_H + 50

        for ri, row in enumerate(rows):
            for ci, nid in enumerate(row):
                x = cx + (ci - (len(row) - 1) / 2) * H_GAP
                y = sy + ri * V_GAP
                positions[nid] = {"x": x, "y": y}

    return positions


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
    # ★ トポロジーグラフHTML キャッシュ（入力が同一なら再構築スキップ）
    _topo_cache_key = "_topo_graph_cache"
    _alarm_sig = tuple(sorted((a.device_id, a.severity, a.is_root_cause) for a in alarms))
    _analysis_sig = tuple(sorted((r.get("id", ""), r.get("status", ""), r.get("prob", 0)) for r in analysis_results))
    _maint_sig = tuple(sorted(st.session_state.get("maint_devices", {}).get(
        st.session_state.get("active_site", ""), set())))
    _zone_sig = tuple(sorted(st.session_state.get("active_site", "A")))
    _cache_sig = hash((_alarm_sig, _analysis_sig, len(topology), _maint_sig, _zone_sig))
    _cached = st.session_state.get(_topo_cache_key)
    if _cached and _cached.get("sig") == _cache_sig:
        # キャッシュヒット: HTML描画のみ（凡例はHTML内に含まれる）
        components.html(_cached["html"], height=_cached.get("canvas_h", 720))
        return

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

    # --- ゾーン定義の読み込み & 固定座標の計算 ---
    zones = _load_zones_for_site(topology)
    fixed_positions = _compute_fixed_positions(zones)
    _use_fixed = bool(fixed_positions)

    # --- ノード生成 ---
    _n_nodes = len(topology)
    _font_size = 12 if _n_nodes > 14 else 14
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
        _role = (metadata.get('role') if isinstance(metadata, dict)
                 else getattr(metadata, 'role', None))

        # デフォルト（正常）— デバイスタイプ別の形状・色
        _type_visual = _DEVICE_TYPE_VISUALS.get(node_type) or _get_visual(node_type)
        bg_color = _type_visual["bg"]
        border_color = _type_visual["border"]
        border_width = 3
        font_color = "#333"
        shape = _type_visual["shape"]
        font_bg = None
        _type_icon = _type_visual.get("icon", "")
        # SERVER/CLOUD はロール情報を優先表示
        if _role and node_type in ("SERVER", "CLOUD_GATEWAY", "CLOUD_RESOURCE"):
            _type_display = _role.split("(")[0].strip()  # e.g. "Web Frontend"
            label_parts = [f"{_type_icon} {node_id}" if _type_icon else node_id,
                           f"({_type_display})"]
        else:
            label_parts = [f"{_type_icon} {node_id}" if _type_icon else node_id,
                           f"({node_type})"]
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
                    border_color = "#6B4878"
                    border_width = 4
                    shape = "ellipse"
                    status_tag = "SILENT SUSPECT"
                    state_key = "silent"
                elif info['max_severity'] == 'CRITICAL':
                    bg_color = NodeColor.ROOT_CAUSE_CRITICAL
                    border_color = "#8B3030"
                    border_width = 4
                    shape = "ellipse"
                    font_color = "#8B4444"
                    status_tag = "ROOT CAUSE"
                    state_key = "root_cause"
                else:
                    bg_color = NodeColor.ROOT_CAUSE_WARNING
                    border_color = "#A07820"
                    border_width = 4
                    status_tag = "WARNING"
                    state_key = "warning"
            else:
                # 非root_cause のアラーム
                if node_id in predicted_ids_real:
                    bg_color = "#FFB300"
                    border_color = "#8C6030"
                    border_width = 4
                    font_color = "#8C6030"
                    status_tag = "PREDICTION"
                    state_key = "prediction"
                elif node_id in predicted_ids_sim:
                    bg_color = "#FFE082"
                    border_color = "#806030"
                    border_width = 4
                    font_color = "#8C6030"
                    status_tag = "SIM-PRED"
                    state_key = "prediction"
                else:
                    # 3分類: symptom vs unrelated
                    cls = classification_map.get(node_id, "")
                    if cls == "symptom":
                        bg_color = "#FFE0B2"
                        border_color = "#906040"
                        border_width = 3
                        font_color = "#8C5C3C"
                        status_tag = "Symptom"
                        state_key = "symptom"
                    elif cls == "unrelated":
                        bg_color = "#E1BEE7"
                        border_color = "#604878"
                        border_width = 3
                        shape = "diamond"
                        font_color = "#5C4070"
                        font_bg = "rgba(255,255,255,0.9)"
                        status_tag = "Unrelated"
                        state_key = "unrelated"
                    else:
                        bg_color = NodeColor.UNREACHABLE
                        border_color = "#6A7A84"
                        border_width = 3
                        font_color = "#607078"
                        status_tag = "Unreachable"
                        state_key = "unreachable"

        # 1.5 メンテナンスモード（アラーム抑制中）
        elif node_id in st.session_state.get("maint_devices", {}).get(
            st.session_state.get("active_site", ""), set()
        ):
            bg_color = "#B0BEC5"
            border_color = "#78909C"
            border_width = 3
            font_color = "#546E7A"
            status_tag = "MAINTENANCE"
            state_key = "maintenance"

        # 2. 予兆ハイライト（アラームなし）
        elif node_id in predicted_ids_real:
            bg_color = "#FFB300"
            border_color = "#8C6030"
            border_width = 4
            font_color = "#8C6030"
            status_tag = "PREDICTION"
            state_key = "prediction"
        elif node_id in predicted_ids_sim:
            bg_color = "#FFE082"
            border_color = "#806030"
            border_width = 4
            font_color = "#8C6030"
            status_tag = "SIM-PRED"
            state_key = "prediction"

        used_states.add(state_key)

        # ラベル構築 — "\n" (実際の改行文字) で結合して vis.js が改行描画する
        if status_tag:
            label_parts.append(f"[{status_tag}]")
        label_text = "\n".join(label_parts)

        font_config = {
            "color": font_color,
            "size": _font_size,
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
            "widthConstraint": {"minimum": 110, "maximum": 165} if _use_fixed else {"minimum": 120, "maximum": 180},
            "heightConstraint": {"minimum": 40},
        }
        if node_id in fixed_positions:
            node_obj["x"] = fixed_positions[node_id]["x"]
            node_obj["y"] = fixed_positions[node_id]["y"]
            node_obj["fixed"] = {"x": True, "y": True}
        nodes.append(node_obj)

    # --- 冗長グループインデックスを事前構築 O(n) ---
    _rg_index: Dict[str, List[str]] = {}
    for _nid, _n in topology.items():
        _rg = _n.get('redundancy_group') if isinstance(_n, dict) else getattr(_n, 'redundancy_group', None)
        if _rg:
            _rg_index.setdefault(_rg, []).append(_nid)

    # --- エッジ生成 ---
    edges = []
    added_edges = set()
    for node_id, node in topology.items():
        parent_id = node.get('parent_id') if isinstance(node, dict) else getattr(node, 'parent_id', None)
        if parent_id:
            edge_key = (parent_id, node_id)
            if edge_key not in added_edges:
                edges.append({"from": parent_id, "to": node_id, "arrows": "to", "color": "#777"})
                added_edges.add(edge_key)

            # 冗長ペア（O(1)ルックアップ）
            p_node = topology.get(parent_id)
            if p_node:
                rg = p_node.get('redundancy_group') if isinstance(p_node, dict) else getattr(p_node, 'redundancy_group', None)
                if rg and rg in _rg_index:
                    for peer_id in _rg_index[rg]:
                        if peer_id != parent_id:
                            edge_key2 = (peer_id, node_id)
                            if edge_key2 not in added_edges:
                                edges.append({
                                    "from": peer_id, "to": node_id,
                                    "arrows": "to",
                                    "color": {"color": "#B0BEC5", "opacity": 0.6},
                                    "dashes": True,
                                })
                                added_edges.add(edge_key2)

    # --- レイアウト設定（固定座標 or vis.js 自動階層） ---
    if _use_fixed:
        _max_y = max(p["y"] for p in fixed_positions.values())
        _canvas_h = int(_max_y + 200)
    else:
        if _n_nodes > 14:
            _level_sep, _node_sp, _tree_sp = 100, 130, 130
            _canvas_h = 820
        elif _n_nodes > 10:
            _level_sep, _node_sp, _tree_sp = 110, 150, 150
            _canvas_h = 740
        else:
            _level_sep, _node_sp, _tree_sp = 130, 180, 180
            _canvas_h = 700

    # --- vis.js レイアウトオプション組み立て ---
    if _use_fixed:
        _layout_js = "layout: { hierarchical: false }"
        _edge_smooth_js = ("smooth: { type: 'cubicBezier', "
                           "forceDirection: 'vertical', roundness: 0.15 }")
        _pad_x, _pad_top, _pad_bottom = 85, 55, 65
    else:
        _layout_js = (
            f"layout: {{ hierarchical: {{ enabled: true, direction: 'UD', "
            f"sortMethod: 'directed', levelSeparation: {_level_sep}, "
            f"nodeSpacing: {_node_sp}, treeSpacing: {_tree_sp}, "
            f"blockShifting: true, edgeMinimization: true, "
            f"parentCentralization: true }} }}"
        )
        _edge_smooth_js = "smooth: false"
        _pad_x, _pad_top, _pad_bottom = 60, 40, 30

    # --- vis.js HTML (凡例はキャンバス内にオーバーレイ表示) ---
    nodes_json = json.dumps(nodes, ensure_ascii=False)
    edges_json = json.dumps(edges, ensure_ascii=False)
    zones_json = json.dumps(zones, ensure_ascii=False)
    legend_html = _build_legend_html(used_states)

    html = f"""
<html><head>
<script src="https://unpkg.com/vis-network@9.1.6/standalone/umd/vis-network.min.js"></script>
<style>
  body {{ margin:0; padding:0; overflow:hidden; }}
  #topo-wrap {{ position:relative; width:100%; height:{_canvas_h}px; }}
  #mynetwork {{ width:100%; height:100%; border:1px solid #e0e0e0; border-radius:4px; }}
  #legend-bar {{
    position:absolute; bottom:6px; left:6px; right:6px;
    background:rgba(250,250,250,0.92); border:1px solid #e0e0e0;
    border-radius:4px; padding:5px 12px;
    font:12px/1.4 Arial,sans-serif; color:#444;
    pointer-events:none; z-index:10;
  }}
  .lg-swatch {{
    display:inline-block; width:12px; height:12px;
    vertical-align:middle; margin-right:5px;
  }}
  .lg-item {{ margin-right:14px; white-space:nowrap; }}
  #fs-btn {{
    position:absolute; top:8px; right:8px; z-index:20;
    background:rgba(255,255,255,0.92); border:1px solid #ccc;
    border-radius:6px; padding:6px 12px;
    font:13px/1 Arial,sans-serif; color:#444; cursor:pointer;
    transition:background 0.2s;
  }}
  #fs-btn:hover {{ background:#e3f2fd; border-color:#90caf9; }}
  :fullscreen #topo-wrap,
  :-webkit-full-screen #topo-wrap {{
    width:100vw; height:100vh; background:#fff;
  }}
</style>
</head>
<body>
<div id="topo-wrap">
  <button id="fs-btn" title="全画面表示 / 戻る">&#x26F6; 全画面</button>
  <div id="mynetwork"></div>
  <div id="legend-bar">{legend_html}</div>
</div>
<script>
var nodes = new vis.DataSet({nodes_json});
var edges = new vis.DataSet({edges_json});
var zones = {zones_json};
var data = {{ nodes: nodes, edges: edges }};
var options = {{
    {_layout_js},
    physics: {{ enabled: false }},
    interaction: {{
        hover: true,
        tooltipDelay: 100,
        zoomView: true,
        dragView: true,
        dragNodes: false
    }},
    nodes: {{
        font: {{ size: {_font_size}, face: 'Arial, sans-serif', multi: false }},
        margin: {{ top: 8, bottom: 8, left: 10, right: 10 }},
        shapeProperties: {{ borderRadius: 8 }}
    }},
    edges: {{
        {_edge_smooth_js}
    }}
}};
var network = new vis.Network(document.getElementById('mynetwork'), data, options);

/* ── ゾーンボックス描画 (beforeDrawing) ── */
network.on('beforeDrawing', function(ctx) {{
  var positions = network.getPositions();
  var PAD_X = {_pad_x}, PAD_TOP = {_pad_top}, PAD_BOTTOM = {_pad_bottom};
  for (var zk in zones) {{
    if (zk.charAt(0) === '_') continue;
    var z = zones[zk];
    var memberNodes = z.nodes || [];
    var xs = [], ys = [];
    for (var i = 0; i < memberNodes.length; i++) {{
      var p = positions[memberNodes[i]];
      if (p) {{ xs.push(p.x); ys.push(p.y); }}
    }}
    if (xs.length === 0) continue;
    var minX = Math.min.apply(null, xs) - PAD_X;
    var maxX = Math.max.apply(null, xs) + PAD_X;
    var minY = Math.min.apply(null, ys) - PAD_TOP;
    var maxY = Math.max.apply(null, ys) + PAD_BOTTOM;
    /* 背景ボックス */
    ctx.fillStyle = z.color || 'rgba(200,200,200,0.15)';
    ctx.strokeStyle = z.border || '#ccc';
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    var r = 8;
    ctx.moveTo(minX + r, minY);
    ctx.lineTo(maxX - r, minY);
    ctx.arcTo(maxX, minY, maxX, minY + r, r);
    ctx.lineTo(maxX, maxY - r);
    ctx.arcTo(maxX, maxY, maxX - r, maxY, r);
    ctx.lineTo(minX + r, maxY);
    ctx.arcTo(minX, maxY, minX, maxY - r, r);
    ctx.lineTo(minX, minY + r);
    ctx.arcTo(minX, minY, minX + r, minY, r);
    ctx.closePath();
    ctx.fill();
    ctx.stroke();
    /* ゾーンラベル（ボックス上部） */
    if (z.label) {{
      ctx.font = '11px Arial, sans-serif';
      ctx.fillStyle = z.border || '#888';
      ctx.textAlign = 'left';
      ctx.textBaseline = 'bottom';
      ctx.fillText(z.label, minX + 8, minY - 4);
    }}
  }}
}});

network.once('afterDrawing', function() {{ network.fit({{ padding: 50, animation: false }}); }});

/* ── 全画面トグル ── */
var fsBtn = document.getElementById('fs-btn');
fsBtn.addEventListener('click', function() {{
  var wrap = document.documentElement;
  if (!document.fullscreenElement && !document.webkitFullscreenElement) {{
    (wrap.requestFullscreen || wrap.webkitRequestFullscreen).call(wrap);
  }} else {{
    (document.exitFullscreen || document.webkitExitFullscreen).call(document);
  }}
}});
function onFsChange() {{
  var isFull = !!(document.fullscreenElement || document.webkitFullscreenElement);
  fsBtn.innerHTML = isFull ? '&#x2716; 戻る' : '&#x26F6; 全画面';
  setTimeout(function() {{ network.fit({{ padding: 40, animation: true }}); }}, 200);
}}
document.addEventListener('fullscreenchange', onFsChange);
document.addEventListener('webkitfullscreenchange', onFsChange);
</script></body></html>
"""
    # ★ キャッシュに保存（次回rerunで再利用）
    st.session_state[_topo_cache_key] = {"sig": _cache_sig, "html": html, "used_states": used_states, "canvas_h": _canvas_h}
    components.html(html, height=_canvas_h)


def _build_legend_html(used_states: set) -> str:
    """凡例を vis.js キャンバス内オーバーレイ用 HTML として生成"""
    _LEGEND_ITEMS = [
        ("root_cause",  "#ffcdd2", "#8B3030", "border-radius:50%", "Root Cause (真因)"),
        ("warning",     "#fff9c4", "#A07820", "",                  "Warning (警告)"),
        ("silent",      "#e1bee7", "#6B4878", "border-radius:50%", "Silent Suspect"),
        ("prediction",  "#FFB300", "#8C6030", "",                  "Prediction (予兆)"),
        ("symptom",     "#FFE0B2", "#906040", "",                  "Symptom (派生)"),
        ("unrelated",   "#E1BEE7", "#604878", "transform:rotate(45deg)", "Unrelated (ノイズ)"),
        ("unreachable", "#cfd8dc", "#6A7A84", "",                  "Unreachable"),
        ("maintenance", "#B0BEC5", "#78909C", "",                  "Maintenance (メンテ中)"),
        ("normal",      "#e8f5e9", "#6B9E72", "",                  "Normal (正常)"),
    ]

    items = []
    for key, bg, border, extra_style, text in _LEGEND_ITEMS:
        if key in used_states:
            swatch = (
                f'<span class="lg-swatch" style="background:{bg};'
                f'border:2px solid {border};{extra_style};"></span>'
            )
            items.append(f'<span class="lg-item">{swatch}{text}</span>')

    if not items:
        return ""
    return " ".join(items)


# =====================================================
# BFS 影響伝搬グラフ
# =====================================================

# トポロジーマップと同じ色定義（状態ベース）
_IMPACT_STATE_COLORS = {
    "root_cause_critical": {"bg": "#ffcdd2", "border": "#A05050", "font": "#8B4444"},
    "root_cause_warning":  {"bg": "#fff9c4", "border": "#C49840", "font": "#444"},
    "silent":              {"bg": "#e1bee7", "border": "#8B6896", "font": "#444"},
    "symptom":             {"bg": "#FFE0B2", "border": "#B07858", "font": "#8C5C3C"},
    "unreachable":         {"bg": "#cfd8dc", "border": "#8A9AA4", "font": "#607078"},
    "normal":              {"bg": "#e8f5e9", "border": "#94B898", "font": "#444"},
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

    # ★ 高速化: 影響伝搬グラフ HTML キャッシュ
    _impact_cache_key = "_impact_graph_cache"
    _impact_sig = hash((
        root_device_id,
        tuple(sorted(downstream_impacts)),
        tuple(sorted((r.get("id", ""), r.get("classification", "")) for r in (analysis_results or []))),
    ))
    _impact_cached = st.session_state.get(_impact_cache_key)
    if _impact_cached and _impact_cached.get("sig") == _impact_sig:
        components.html(_impact_cached["html"], height=370)
        st.markdown(_impact_cached["summary"], unsafe_allow_html=True)
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
            levelSeparation: 85,
            nodeSpacing: 120,
            treeSpacing: 150,
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
    summary_text = f"影響範囲: 計 {total}台&nbsp;&nbsp;|&nbsp;&nbsp;" + "&nbsp;&nbsp;&nbsp;".join(hop_labels)
    summary_html = (
        f'<div style="font-size:12px;font-family:Arial,sans-serif;'
        f'padding:5px 12px;background:#fff3e0;border:1px solid #ffe0b2;'
        f'border-radius:4px;margin-top:4px;">{summary_text}</div>'
    )

    # ★ キャッシュに保存
    st.session_state[_impact_cache_key] = {"sig": _impact_sig, "html": html, "summary": summary_html}
    components.html(html, height=370)
    st.markdown(summary_html, unsafe_allow_html=True)
