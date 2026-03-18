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


def _compute_fixed_positions(zones: dict, topology: dict) -> dict:
    """zones の rows/grid 定義 + トポロジーのラベル行数から固定座標を算出する。

    レイアウト規則:
      1. 各ノードの上方向・下方向エクステントを非対称に推定
         - box/ellipse: (x,y) = ノード全体の中心 → 上下対称
         - hexagon/diamond/star: (x,y) = 図形の中心、ラベルは下 → 非対称
      2. ゾーン内の各行は、前行の下端 + edge_gap + 次行の上端 で累積計算
      3. ゾーングリッドの行オフセットは、同一行の最大ゾーン高さから算出
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
    H_GAP = cfg.get("node_h_gap", 150)
    FONT_SZ = cfg.get("font_size", 12)
    EDGE_GAP = cfg.get("edge_gap", 22)
    ZONE_GAP = cfg.get("zone_gap", 30)
    PAD_TOP = 55
    PAD_BOTTOM = 65

    # vis.js の形状のうち、ラベルを図形の「下」に描画するもの
    # (box/ellipse/database は内部描画 → 上下対称)
    _LABEL_BELOW_SHAPES = frozenset({
        "hexagon", "diamond", "star", "triangle",
        "triangleDown", "dot", "square",
    })
    _SHAPE_RADIUS = 30   # vis.js デフォルト size=25 + margin
    _SHAPE_LABEL_GAP = 8  # 図形とラベル間の隙間
    _VIS_MARGIN = 8       # vis.js nodes.margin (top/bottom) — graph.py options で設定

    def _est_extents(nid: str):
        """ノードの (x,y) からの上方向・下方向エクステントを推定。

        vis.js は hexagon/diamond/star で (x,y) = 図形中心にラベルを下に描画。
        box/ellipse は (x,y) = ノード全体の中心にラベルを内部描画。
        いずれもノード margin (8px top/bottom) が描画に加算されるため考慮する。
        Returns: (above, below) — y座標からの上方向・下方向の広がり(px)
        """
        node = topology.get(nid) if topology else None
        if not isinstance(node, dict):
            node = {}
        n_lines = 2  # ID行 + (type/role)行
        meta = node.get("metadata", {})
        if isinstance(meta, dict):
            if meta.get("redundancy_type"):
                n_lines += 1
            if meta.get("vendor"):
                n_lines += 1
        n_lines += 1  # ステータスタグ行を保守的に考慮
        line_h = FONT_SZ + 5
        text_h = n_lines * line_h + 24

        node_type = node.get("type", "UNKNOWN")
        visual = _DEVICE_TYPE_VISUALS.get(node_type) or _get_visual(node_type)
        shape = visual.get("shape", "box")

        if shape in _LABEL_BELOW_SHAPES:
            # (x,y) = 図形中心。上方向は図形半径、下方向は図形半径+gap+テキスト全高
            above = _SHAPE_RADIUS + _VIS_MARGIN
            below = _SHAPE_RADIUS + _SHAPE_LABEL_GAP + text_h + _VIS_MARGIN
        else:
            # (x,y) = ノード全体の中心。上下対称
            h = max(48, text_h) + _VIS_MARGIN * 2
            above = h / 2
            below = h / 2

        return above, below

    # --- Pass 1: 各ゾーンの行エクステント & 内部全高を計算 ---
    zone_info = {}
    for zk, zv in zones.items():
        if zk.startswith("_") or not isinstance(zv, dict):
            continue
        rows = zv.get("rows")
        if not rows:
            continue
        g = zv.get("grid", [0, 0, 1, 1])
        gc, gr = g[0], g[1]
        colspan = g[2] if len(g) > 2 else 1
        rowspan = g[3] if len(g) > 3 else 1

        row_aboves = []
        row_belows = []
        for row in rows:
            extents = [_est_extents(nid) for nid in row]
            row_aboves.append(max(a for a, _ in extents))
            row_belows.append(max(b for _, b in extents))

        # 内部全高 = 最初行の above + 各行間距離 + 最終行の below
        internal_h = row_aboves[0] + row_belows[-1]
        for ri in range(len(rows) - 1):
            internal_h += row_belows[ri] + EDGE_GAP + row_aboves[ri + 1]

        zone_info[zk] = {
            "gc": gc, "gr": gr, "colspan": colspan, "rowspan": rowspan,
            "rows": rows,
            "row_aboves": row_aboves, "row_belows": row_belows,
            "internal_h": internal_h,
            "cx": gc * COL_W + colspan * COL_W / 2,
        }

    # --- Pass 2: ゾーングリッドの行ごとの最大高さ → Y オフセット ---
    grid_row_max: dict = {}
    for zi in zone_info.values():
        total_h = zi["internal_h"] + PAD_TOP + PAD_BOTTOM
        per_row = total_h / zi["rowspan"]
        for r in range(zi["gr"], zi["gr"] + zi["rowspan"]):
            grid_row_max[r] = max(grid_row_max.get(r, 0), per_row)

    grid_row_y: dict = {}
    y_cursor = 0.0
    for r in sorted(grid_row_max.keys()):
        grid_row_y[r] = y_cursor
        y_cursor += grid_row_max[r] + ZONE_GAP

    # --- Pass 3: 各ノードの (x, y) を算出（非対称エクステント使用） ---
    positions = {}
    for zi in zone_info.values():
        zone_y0 = grid_row_y[zi["gr"]] + PAD_TOP
        y = zone_y0 + zi["row_aboves"][0]
        for ri, row in enumerate(zi["rows"]):
            for ci, nid in enumerate(row):
                x = zi["cx"] + (ci - (len(row) - 1) / 2) * H_GAP
                positions[nid] = {"x": x, "y": y}
            if ri < len(zi["rows"]) - 1:
                y += (zi["row_belows"][ri]
                      + EDGE_GAP
                      + zi["row_aboves"][ri + 1])

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
    fixed_positions = _compute_fixed_positions(zones, topology)
    _use_fixed = bool(fixed_positions)

    # --- ノード生成 ---
    _n_nodes = len(topology)
    _font_size = 12
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
            "widthConstraint": {"minimum": 110, "maximum": 180},
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

    # --- レイアウト設定 ---
    # 初期レイアウトのパラメータ（リフローが最終調整するため、大まかな値で十分）
    _level_sep, _node_sp, _tree_sp = 200, 150, 150
    if _use_fixed:
        _max_y = max(p["y"] for p in fixed_positions.values())
        _canvas_h = int(_max_y + 250)
    else:
        # キャンバス高さはリフロー後に network.fit() で自動調整される
        _canvas_h = max(700, _n_nodes * 80)

    # --- vis.js レイアウトオプション組み立て ---
    if _use_fixed:
        _layout_js = "layout: { hierarchical: false }"
        _edge_smooth_js = ("smooth: { type: 'cubicBezier', "
                           "forceDirection: 'vertical', roundness: 0.15 }")
    else:
        _layout_js = (
            f"layout: {{ hierarchical: {{ enabled: true, direction: 'UD', "
            f"sortMethod: 'directed', levelSeparation: {_level_sep}, "
            f"nodeSpacing: {_node_sp}, treeSpacing: {_tree_sp}, "
            f"blockShifting: true, edgeMinimization: true, "
            f"parentCentralization: true }} }}"
        )
        _edge_smooth_js = "smooth: false"

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
  #fs-btn, #zoom-btn {{
    position:absolute; z-index:20;
    background:rgba(255,255,255,0.92); border:1px solid #ccc;
    border-radius:6px; padding:6px 12px;
    font:13px/1 Arial,sans-serif; color:#444; cursor:pointer;
    transition:background 0.2s; user-select:none;
  }}
  #fs-btn {{ top:8px; right:8px; }}
  #zoom-btn {{ top:8px; right:120px; }}
  #fs-btn:hover, #zoom-btn:hover {{ background:#e3f2fd; border-color:#90caf9; }}
  #zoom-btn.active {{ background:#e3f2fd; border-color:#1976d2; color:#1976d2; }}
  #topo-wrap:fullscreen,
  #topo-wrap:-webkit-full-screen {{
    width:100vw; height:100vh; background:#fff;
  }}
</style>
</head>
<body>
<div id="topo-wrap">
  <button id="zoom-btn" title="クリックでマウスホイールズームの有効/無効を切替">&#x1F50D; ホイールズーム</button>
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
        zoomView: false,
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

/* ── ゾーンボックス描画 (beforeDrawing) ──
 * 全拠点共通アルゴリズム:
 *   1. 各ゾーンのメンバーノード getBoundingBox から矩形を算出
 *   2. パディング適用後、隣接ゾーン間の重なりを検出・解消
 *   3. 重なり解消は中間点スナップ（両者均等に譲り合い）
 *   4. 調整済み矩形で描画
 * ─────────────────────────────────────── */
network.on('beforeDrawing', function(ctx) {{
  var ZONE_PAD = 25;
  var ZONE_PAD_TOP = 30;
  var ZONE_MIN_GAP = 6;  /* ゾーン間の最小ギャップ */

  /* ── 第1パス: 全ゾーン矩形を算出 ── */
  var zoneRects = [];
  var zoneKeys = [];
  for (var zk in zones) {{
    if (zk.charAt(0) === '_') continue;
    var z = zones[zk];
    var memberNodes = z.nodes || [];
    var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    var found = false;
    for (var i = 0; i < memberNodes.length; i++) {{
      var nid = memberNodes[i];
      try {{
        var bb = network.getBoundingBox(nid);
        if (bb) {{
          if (bb.left < minX) minX = bb.left;
          if (bb.right > maxX) maxX = bb.right;
          if (bb.top < minY) minY = bb.top;
          if (bb.bottom > maxY) maxY = bb.bottom;
          found = true;
        }}
      }} catch(e) {{
        var pp = network.getPositions([nid]);
        if (pp[nid]) {{
          var px = pp[nid].x, py = pp[nid].y;
          if (px - 80 < minX) minX = px - 80;
          if (px + 80 > maxX) maxX = px + 80;
          if (py - 80 < minY) minY = py - 80;
          if (py + 80 > maxY) maxY = py + 80;
          found = true;
        }}
      }}
    }}
    if (!found) continue;
    zoneRects.push({{
      key: zk, zone: z,
      x1: minX - ZONE_PAD, y1: minY - ZONE_PAD_TOP,
      x2: maxX + ZONE_PAD, y2: maxY + ZONE_PAD
    }});
    zoneKeys.push(zk);
  }}

  /* ── 第1.5パス: ゾーンごとのノード包含下限を記録 ──
   * 第2パスの midpoint snapping がゾーン矩形を縮めすぎて
   * ノードが枠外に飛び出すのを防ぐ。
   * origBounds[i] = {{x1,y1,x2,y2}} はノード群の getBoundingBox
   * から算出した「これ以上縮めてはならない下限」。 */
  var origBounds = [];
  for (var i = 0; i < zoneRects.length; i++) {{
    origBounds.push({{
      x1: zoneRects[i].x1, y1: zoneRects[i].y1,
      x2: zoneRects[i].x2, y2: zoneRects[i].y2
    }});
  }}

  /* ── 第2パス: ゾーン間の重なり解消 ──
   * 水平・垂直それぞれで、重なりがあれば中間点にスナップ。
   * ★ 改善点:
   *   1) 完全包含（A が B を含む or 逆）も検出・解消
   *   2) midpoint 結果が origBounds を割り込む場合はクランプ
   *      （ノードが枠外に飛び出すのを防止） */
  for (var i = 0; i < zoneRects.length; i++) {{
    for (var j = i + 1; j < zoneRects.length; j++) {{
      var a = zoneRects[i], b = zoneRects[j];
      /* 垂直範囲が重なるか */
      var yOverlap = a.y1 < b.y2 && b.y1 < a.y2;
      /* 水平範囲が重なるか */
      var xOverlap = a.x1 < b.x2 && b.x1 < a.x2;

      if (yOverlap && xOverlap) {{
        /* 両軸で重なっている → 重なりが浅い軸で解消する */
        var xDepth = Math.min(a.x2, b.x2) - Math.max(a.x1, b.x1);
        var yDepth = Math.min(a.y2, b.y2) - Math.max(a.y1, b.y1);

        if (xDepth <= yDepth) {{
          /* 水平方向で解消（完全包含含む: 中心の左右で判定） */
          var aCx = (a.x1 + a.x2) / 2, bCx = (b.x1 + b.x2) / 2;
          if (aCx <= bCx) {{
            var mid = (Math.min(a.x2, b.x2) + Math.max(a.x1, b.x1)) / 2;
            a.x2 = mid - ZONE_MIN_GAP / 2;
            b.x1 = mid + ZONE_MIN_GAP / 2;
          }} else {{
            var mid = (Math.min(a.x2, b.x2) + Math.max(a.x1, b.x1)) / 2;
            b.x2 = mid - ZONE_MIN_GAP / 2;
            a.x1 = mid + ZONE_MIN_GAP / 2;
          }}
        }} else {{
          /* 垂直方向で解消 */
          var aCy = (a.y1 + a.y2) / 2, bCy = (b.y1 + b.y2) / 2;
          if (aCy <= bCy) {{
            var mid = (Math.min(a.y2, b.y2) + Math.max(a.y1, b.y1)) / 2;
            a.y2 = mid - ZONE_MIN_GAP / 2;
            b.y1 = mid + ZONE_MIN_GAP / 2;
          }} else {{
            var mid = (Math.min(a.y2, b.y2) + Math.max(a.y1, b.y1)) / 2;
            b.y2 = mid - ZONE_MIN_GAP / 2;
            a.y1 = mid + ZONE_MIN_GAP / 2;
          }}
        }}
      }} else if (yOverlap) {{
        /* 水平方向の重なり解消（Y軸のみ重複 = 横並びで接近） */
        var aCx = (a.x1 + a.x2) / 2, bCx = (b.x1 + b.x2) / 2;
        if (aCx <= bCx) {{
          var mid = (a.x2 + b.x1) / 2;
          a.x2 = mid - ZONE_MIN_GAP / 2;
          b.x1 = mid + ZONE_MIN_GAP / 2;
        }} else {{
          var mid = (b.x2 + a.x1) / 2;
          b.x2 = mid - ZONE_MIN_GAP / 2;
          a.x1 = mid + ZONE_MIN_GAP / 2;
        }}
      }} else if (xOverlap) {{
        /* 垂直方向の重なり解消（X軸のみ重複 = 上下で接近） */
        var aCy = (a.y1 + a.y2) / 2, bCy = (b.y1 + b.y2) / 2;
        if (aCy <= bCy) {{
          var mid = (a.y2 + b.y1) / 2;
          a.y2 = mid - ZONE_MIN_GAP / 2;
          b.y1 = mid + ZONE_MIN_GAP / 2;
        }} else {{
          var mid = (b.y2 + a.y1) / 2;
          b.y2 = mid - ZONE_MIN_GAP / 2;
          a.y1 = mid + ZONE_MIN_GAP / 2;
        }}
      }}
    }}
  }}

  /* ── 第2.5パス: ノード包含下限のクランプ ──
   * midpoint snapping でゾーン矩形がノードの外に縮んだ場合、
   * 元のノード包含境界まで復元する。 */
  for (var i = 0; i < zoneRects.length; i++) {{
    var z = zoneRects[i], ob = origBounds[i];
    if (z.x1 > ob.x1) z.x1 = ob.x1;
    if (z.y1 > ob.y1) z.y1 = ob.y1;
    if (z.x2 < ob.x2) z.x2 = ob.x2;
    if (z.y2 < ob.y2) z.y2 = ob.y2;
  }}

  /* ── 第3パス: エンベロープ矩形算出 + 重なり解消 ──
   * _envelopes: 複数の子ゾーンを包む親枠（データセンター等）
   * エンベロープは子ゾーンの和集合 + パディング。
   * ★ 重なり解消の原則:
   *   - エンベロープ vs 外部ゾーン → エンベロープ側のみ縮小
   *   - 子ゾーン包含境界以下には絶対に縮めない（過剰縮小ガード）
   *   - エンベロープ vs エンベロープ → midpoint snapping（双方が譲る） */
  var envelopes = zones._envelopes || {{}};
  var ENV_PAD = 18;
  var envRects = [];
  for (var ek in envelopes) {{
    var env = envelopes[ek];
    var childKeys = env.children || [];
    var childSet = {{}};
    for (var ci = 0; ci < childKeys.length; ci++) childSet[childKeys[ci]] = true;
    var eMinX = Infinity, eMinY = Infinity, eMaxX = -Infinity, eMaxY = -Infinity;
    var eFound = false;
    for (var ci = 0; ci < childKeys.length; ci++) {{
      for (var ri = 0; ri < zoneRects.length; ri++) {{
        if (zoneRects[ri].key === childKeys[ci]) {{
          var cr = zoneRects[ri];
          if (cr.x1 < eMinX) eMinX = cr.x1;
          if (cr.y1 < eMinY) eMinY = cr.y1;
          if (cr.x2 > eMaxX) eMaxX = cr.x2;
          if (cr.y2 > eMaxY) eMaxY = cr.y2;
          eFound = true;
        }}
      }}
    }}
    if (!eFound) continue;
    eMinX -= ENV_PAD; eMinY -= 22; eMaxX += ENV_PAD; eMaxY += ENV_PAD;
    envRects.push({{
      key: ek, env: env, childSet: childSet,
      x1: eMinX, y1: eMinY, x2: eMaxX, y2: eMaxY
    }});
  }}

  /* エンベロープの子ゾーン下限を記録（過剰縮小ガード用）
   * エンベロープが外部ゾーンとの重なり解消で縮みすぎて
   * 子ゾーンをカバーしなくなるのを防ぐ。 */
  var envChildBounds = [];
  for (var ei = 0; ei < envRects.length; ei++) {{
    var cbMinX = Infinity, cbMinY = Infinity, cbMaxX = -Infinity, cbMaxY = -Infinity;
    for (var ri = 0; ri < zoneRects.length; ri++) {{
      if (envRects[ei].childSet[zoneRects[ri].key]) {{
        var cr = zoneRects[ri];
        if (cr.x1 < cbMinX) cbMinX = cr.x1;
        if (cr.y1 < cbMinY) cbMinY = cr.y1;
        if (cr.x2 > cbMaxX) cbMaxX = cr.x2;
        if (cr.y2 > cbMaxY) cbMaxY = cr.y2;
      }}
    }}
    envChildBounds.push({{ x1: cbMinX, y1: cbMinY, x2: cbMaxX, y2: cbMaxY }});
  }}

  /* エンベロープ vs 非子ゾーンの重なり解消
   * ★ エンベロープ側のみ縮小し、ゾーン矩形は一切変更しない。
   * ★ ただし子ゾーンの包含境界以下には縮めない（過剰縮小ガード）。 */
  for (var ei = 0; ei < envRects.length; ei++) {{
    var erc = envRects[ei];
    var ecb = envChildBounds[ei];
    for (var zi = 0; zi < zoneRects.length; zi++) {{
      var zr = zoneRects[zi];
      if (erc.childSet[zr.key]) continue;  /* 子ゾーンはスキップ */
      var yOvl = erc.y1 < zr.y2 && zr.y1 < erc.y2;
      var xOvl = erc.x1 < zr.x2 && zr.x1 < erc.x2;
      if (yOvl) {{
        /* 水平方向: エンベロープ側のみ縮小（中心で左右判定） */
        var eCx = (erc.x1 + erc.x2) / 2, zCx = (zr.x1 + zr.x2) / 2;
        if (eCx <= zCx) {{
          var nx2 = zr.x1 - ZONE_MIN_GAP;
          erc.x2 = Math.max(nx2, ecb.x2);  /* 子ゾーン下限でクランプ */
        }} else {{
          var nx1 = zr.x2 + ZONE_MIN_GAP;
          erc.x1 = Math.min(nx1, ecb.x1);  /* 子ゾーン下限でクランプ */
        }}
      }}
      if (xOvl) {{
        /* 垂直方向: エンベロープ側のみ縮小 */
        var eCy = (erc.y1 + erc.y2) / 2, zCy = (zr.y1 + zr.y2) / 2;
        if (eCy <= zCy) {{
          var ny2 = zr.y1 - ZONE_MIN_GAP;
          erc.y2 = Math.max(ny2, ecb.y2);  /* 子ゾーン下限でクランプ */
        }} else {{
          var ny1 = zr.y2 + ZONE_MIN_GAP;
          erc.y1 = Math.min(ny1, ecb.y1);  /* 子ゾーン下限でクランプ */
        }}
      }}
    }}
  }}

  /* エンベロープ同士の重なり解消（midpoint snapping）
   * 複数エンベロープ定義時に互いが重ならないようにする。 */
  for (var ei = 0; ei < envRects.length; ei++) {{
    for (var ej = ei + 1; ej < envRects.length; ej++) {{
      var ea = envRects[ei], eb = envRects[ej];
      var yOvl = ea.y1 < eb.y2 && eb.y1 < ea.y2;
      var xOvl = ea.x1 < eb.x2 && eb.x1 < ea.x2;
      if (yOvl && xOvl) {{
        var xD = Math.min(ea.x2, eb.x2) - Math.max(ea.x1, eb.x1);
        var yD = Math.min(ea.y2, eb.y2) - Math.max(ea.y1, eb.y1);
        if (xD <= yD) {{
          var aCx = (ea.x1 + ea.x2) / 2, bCx = (eb.x1 + eb.x2) / 2;
          var mid = (Math.min(ea.x2, eb.x2) + Math.max(ea.x1, eb.x1)) / 2;
          if (aCx <= bCx) {{ ea.x2 = mid - ZONE_MIN_GAP / 2; eb.x1 = mid + ZONE_MIN_GAP / 2; }}
          else {{ eb.x2 = mid - ZONE_MIN_GAP / 2; ea.x1 = mid + ZONE_MIN_GAP / 2; }}
        }} else {{
          var aCy = (ea.y1 + ea.y2) / 2, bCy = (eb.y1 + eb.y2) / 2;
          var mid = (Math.min(ea.y2, eb.y2) + Math.max(ea.y1, eb.y1)) / 2;
          if (aCy <= bCy) {{ ea.y2 = mid - ZONE_MIN_GAP / 2; eb.y1 = mid + ZONE_MIN_GAP / 2; }}
          else {{ eb.y2 = mid - ZONE_MIN_GAP / 2; ea.y1 = mid + ZONE_MIN_GAP / 2; }}
        }}
      }}
    }}
  }}

  /* エンベロープ描画 */
  for (var ei = 0; ei < envRects.length; ei++) {{
    var eR = envRects[ei];
    var env = eR.env;
    ctx.fillStyle = env.color || 'rgba(0,0,0,0.03)';
    ctx.strokeStyle = env.border || '#bdbdbd';
    ctx.lineWidth = 1.8;
    if (env.border_style === 'dashed') ctx.setLineDash([8, 5]);
    ctx.beginPath();
    var eRad = 12;
    ctx.moveTo(eR.x1 + eRad, eR.y1);
    ctx.lineTo(eR.x2 - eRad, eR.y1);
    ctx.arcTo(eR.x2, eR.y1, eR.x2, eR.y1 + eRad, eRad);
    ctx.lineTo(eR.x2, eR.y2 - eRad);
    ctx.arcTo(eR.x2, eR.y2, eR.x2 - eRad, eR.y2, eRad);
    ctx.lineTo(eR.x1 + eRad, eR.y2);
    ctx.arcTo(eR.x1, eR.y2, eR.x1, eR.y2 - eRad, eRad);
    ctx.lineTo(eR.x1, eR.y1 + eRad);
    ctx.arcTo(eR.x1, eR.y1, eR.x1 + eRad, eR.y1, eRad);
    ctx.closePath();
    ctx.fill();
    ctx.stroke();
    ctx.setLineDash([]);
    if (env.label) {{
      ctx.font = 'bold 12px Arial, sans-serif';
      ctx.fillStyle = env.border || '#999';
      ctx.textAlign = 'left';
      ctx.textBaseline = 'bottom';
      ctx.fillText(env.label, eR.x1 + 12, eR.y1 - 6);
    }}
  }}

  /* ── 第4パス: 通常ゾーン矩形を描画 ── */
  for (var i = 0; i < zoneRects.length; i++) {{
    var zr = zoneRects[i];
    var z = zr.zone;
    /* 背景ボックス（角丸） */
    ctx.fillStyle = z.color || 'rgba(200,200,200,0.15)';
    ctx.strokeStyle = z.border || '#ccc';
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    var r = 8;
    ctx.moveTo(zr.x1 + r, zr.y1);
    ctx.lineTo(zr.x2 - r, zr.y1);
    ctx.arcTo(zr.x2, zr.y1, zr.x2, zr.y1 + r, r);
    ctx.lineTo(zr.x2, zr.y2 - r);
    ctx.arcTo(zr.x2, zr.y2, zr.x2 - r, zr.y2, r);
    ctx.lineTo(zr.x1 + r, zr.y2);
    ctx.arcTo(zr.x1, zr.y2, zr.x1, zr.y2 - r, r);
    ctx.lineTo(zr.x1, zr.y1 + r);
    ctx.arcTo(zr.x1, zr.y1, zr.x1 + r, zr.y1, r);
    ctx.closePath();
    ctx.fill();
    ctx.stroke();
    /* ゾーンラベル（ボックス上部） */
    if (z.label) {{
      ctx.font = '11px Arial, sans-serif';
      ctx.fillStyle = z.border || '#888';
      ctx.textAlign = 'left';
      ctx.textBaseline = 'bottom';
      ctx.fillText(z.label, zr.x1 + 8, zr.y1 - 4);
    }}
  }}
}});

/* ══════════════════════════════════════════════════════════════
 * Universal Measure & Reflow — 全拠点共通レイアウトアルゴリズム
 * ──────────────────────────────────────────────────────────────
 * 学術的基盤:
 *   Sugiyama et al. (1981) — 階層グラフ描画フレームワーク
 *   Brandes & Köpf (2001)  — 座標割り当てアルゴリズム (dagre で採用)
 *
 * dagre との違い:
 *   dagre: 描画前にサイズを推定 → レイアウト計算 → 描画
 *   本実装: 描画 → 実サイズ測定 → リフロー（ブラウザ layout engine と同原理）
 *   利点: 推定誤差ゼロ。形状変化（ROOT CAUSE ellipse 等）にも自動対応。
 *
 * アルゴリズム:
 *   Phase 1: vis.js 初期描画（hierarchical or fixed positions）
 *   Phase 2: getBoundingBox() で全ノードの実サイズを測定
 *   Phase 3: BFS でレベルツリーを構築（Y近接フォールバック付き）
 *   Phase 4: レベル単位で Y 座標を再計算（非対称エクステント使用）
 *   Phase 5: 同一レベル内の水平重なりを解消
 *   Phase 6: 座標適用 & fit
 * ══════════════════════════════════════════════════════════════ */
network.once('afterDrawing', function() {{
  var allIds = nodes.getIds();
  if (allIds.length === 0) {{ network.fit({{padding:50}}); return; }}

  /* ── Phase 2: 実サイズ測定 ── */
  var pos = network.getPositions();
  var bb = {{}};
  allIds.forEach(function(id) {{
    try {{ bb[id] = network.getBoundingBox(id); }} catch(e) {{}}
  }});

  /* ── Phase 3: BFS レベル検出 ── */
  var children = {{}};
  var inDeg = {{}};
  allIds.forEach(function(id) {{ children[id] = []; inDeg[id] = 0; }});
  edges.forEach(function(e) {{
    if (children[e.from]) children[e.from].push(e.to);
    inDeg[e.to] = (inDeg[e.to] || 0) + 1;
  }});
  var levels = {{}};
  var queue = [];
  allIds.forEach(function(id) {{
    if (!inDeg[id] || inDeg[id] === 0) {{ levels[id] = 0; queue.push(id); }}
  }});
  var head = 0;
  while (head < queue.length) {{
    var cur = queue[head++];
    children[cur].forEach(function(ch) {{
      var nl = levels[cur] + 1;
      if (levels[ch] === undefined || levels[ch] < nl) {{
        levels[ch] = nl;
        queue.push(ch);
      }}
    }});
  }}
  /* 孤立ノード: レベル 0 に割り当て */
  allIds.forEach(function(id) {{
    if (levels[id] === undefined) levels[id] = 0;
  }});

  /* レベル → ノードリスト（X座標順でソート） */
  var lvGroups = {{}};
  allIds.forEach(function(id) {{
    var lv = levels[id];
    if (!lvGroups[lv]) lvGroups[lv] = [];
    lvGroups[lv].push(id);
  }});
  var sortedLvs = Object.keys(lvGroups).map(Number).sort(function(a,b){{return a-b;}});
  sortedLvs.forEach(function(lv) {{
    lvGroups[lv].sort(function(a,b){{ return pos[a].x - pos[b].x; }});
  }});

  /* ── Y近接フォールバック ──
   * 固定座標レイアウト（ゾーングリッド）では BFS レベルと
   * 視覚的な行が一致しない場合がある（例: AWS_DX は BFS level 1
   * だがゾーン内では最上行）。初期 Y が大きく異なるノードが
   * 同一 BFS レベルに入る場合は Y 近接で再分割する。 */
  var finalRows = [];
  sortedLvs.forEach(function(lv) {{
    var grp = lvGroups[lv];
    grp.sort(function(a,b){{ return pos[a].y - pos[b].y; }});
    var subRow = [grp[0]];
    for (var i = 1; i < grp.length; i++) {{
      if (Math.abs(pos[grp[i]].y - pos[subRow[0]].y) < 50) {{
        subRow.push(grp[i]);
      }} else {{
        finalRows.push(subRow);
        subRow = [grp[i]];
      }}
    }}
    finalRows.push(subRow);
  }});
  /* Y の昇順でソート */
  finalRows.sort(function(a,b) {{
    var ya = 0, yb = 0;
    a.forEach(function(id){{ ya += pos[id].y; }});
    b.forEach(function(id){{ yb += pos[id].y; }});
    return ya/a.length - yb/b.length;
  }});

  /* ── Phase 4: 縦方向リフロー（レベル単位、非対称エクステント） ──
   * 水平方向に重ならない行間（例: dc_core と aws_cloud）には
   * 縦ギャップを強制しない。重なりのある直近の先行行を探索して
   * そこからのギャップのみ確保する（ゾーン間の不要な引き延ばしを防止）。 */
  var MIN_V_GAP = 40;
  var rd = finalRows.map(function(row) {{
    var cy = 0;
    row.forEach(function(id){{ cy += pos[id].y; }});
    cy /= row.length;
    var mxA = 0, mxB = 0;
    var xL = Infinity, xR = -Infinity;
    row.forEach(function(id) {{
      var b = bb[id];
      if (b) {{
        mxA = Math.max(mxA, pos[id].y - b.top);
        mxB = Math.max(mxB, b.bottom - pos[id].y);
        if (b.left < xL) xL = b.left;
        if (b.right > xR) xR = b.right;
      }}
    }});
    return {{row:row, cy:cy, above:Math.max(mxA,20), below:Math.max(mxB,20), xL:xL, xR:xR}};
  }});

  var newCY = [rd[0].cy];
  for (var r = 1; r < rd.length; r++) {{
    /* 水平方向に重なりのある直近の先行行を探索 */
    var bestNeeded = rd[r].cy;
    for (var p = r - 1; p >= 0; p--) {{
      /* X範囲が重なるか判定（20pxマージン） */
      if (rd[p].xR + 20 > rd[r].xL && rd[r].xR + 20 > rd[p].xL) {{
        var needed = newCY[p] + rd[p].below + MIN_V_GAP + rd[r].above;
        bestNeeded = Math.max(bestNeeded, needed);
        break;
      }}
    }}
    newCY.push(bestNeeded);
  }}

  /* ── Phase 5: 横方向リフロー（重なり部分のみ拡張） ──
   * dagre の Brandes-Köpf と異なり、vis.js の初期 X 順序を尊重しつつ
   * 実測幅に基づき重なりノード間のみギャップを確保する。
   * 行全体の再センタリングは親子アラインメントを壊すため行わない。 */
  var MIN_H_GAP = 20;
  var xNew = {{}};
  finalRows.forEach(function(row) {{
    if (row.length < 2) return;
    row.sort(function(a,b){{ return pos[a].x - pos[b].x; }});
    /* 左→右に走査し、重なりがあれば右側を押し出す */
    var shifts = new Array(row.length);
    shifts[0] = 0;
    for (var j = 1; j < row.length; j++) {{
      var prevId = row[j-1], currId = row[j];
      var prevRight = (bb[prevId] ? bb[prevId].right : pos[prevId].x + 80) + shifts[j-1];
      var currLeft = bb[currId] ? bb[currId].left : pos[currId].x - 80;
      var overlap = prevRight + MIN_H_GAP - currLeft;
      shifts[j] = (overlap > 0) ? shifts[j-1] + overlap : shifts[j-1];
    }}
    /* 総シフト量の半分を左方向に戻す（行の重心を保持） */
    var totalShift = shifts[row.length-1];
    if (totalShift > 0) {{
      var halfShift = totalShift / 2;
      for (var j = 0; j < row.length; j++) {{
        var dx = shifts[j] - halfShift;
        if (Math.abs(dx) > 1) xNew[row[j]] = pos[row[j]].x + dx;
      }}
    }}
  }});

  /* ── Phase 6: 座標適用 ── */
  for (var r = 0; r < finalRows.length; r++) {{
    var dy = newCY[r] - rd[r].cy;
    finalRows[r].forEach(function(id) {{
      var nx = (xNew[id] !== undefined) ? xNew[id] : pos[id].x;
      var ny = pos[id].y + dy;
      if (Math.abs(ny - pos[id].y) > 1 || Math.abs(nx - pos[id].x) > 1) {{
        network.moveNode(id, nx, ny);
      }}
    }});
  }}
  setTimeout(function(){{ network.fit({{padding:50, animation:false}}); }}, 100);
}});

/* ── ホイールズーム制御ボタン（トグル方式） ──
 * デフォルトは zoomView:false でブラウザスクロールを阻害しない。
 * クリックで ON/OFF を切り替える。 */
var zoomBtn = document.getElementById('zoom-btn');
var _zoomActive = false;
zoomBtn.addEventListener('click', function(e) {{
  e.preventDefault();
  _zoomActive = !_zoomActive;
  network.setOptions({{ interaction: {{ zoomView: _zoomActive }} }});
  zoomBtn.classList.toggle('active', _zoomActive);
  zoomBtn.innerHTML = _zoomActive
    ? '&#x1F50D; ズーム有効'
    : '&#x1F50D; ホイールズーム';
}});

/* ── 全画面トグル ── */
var fsBtn = document.getElementById('fs-btn');
var topoWrap = document.getElementById('topo-wrap');
fsBtn.addEventListener('click', function() {{
  if (!document.fullscreenElement && !document.webkitFullscreenElement) {{
    (topoWrap.requestFullscreen || topoWrap.webkitRequestFullscreen).call(topoWrap);
  }} else {{
    (document.exitFullscreen || document.webkitExitFullscreen).call(document);
  }}
}});
function onFsChange() {{
  var isFull = !!(document.fullscreenElement || document.webkitFullscreenElement);
  fsBtn.innerHTML = isFull ? '&#x2716; 戻る' : '&#x26F6; 全画面';
  setTimeout(function() {{
    network.redraw();
    network.fit({{ padding: 40, animation: true }});
  }}, 300);
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
    interaction: {{ hover: true, zoomView: false, dragView: true, dragNodes: false }},
    nodes: {{
        font: {{ size: 12, face: 'Arial' }},
        margin: {{ top: 6, bottom: 6, left: 8, right: 8 }}
    }},
    edges: {{
        smooth: {{ type: 'cubicBezier', forceDirection: 'vertical', roundness: 0.3 }}
    }}
}};
var network = new vis.Network(document.getElementById('impact-net'), data, options);

/* ── Measure & Reflow: 同一レベル内の水平重なり解消 ──
 * vis.js hierarchical layout は nodeSpacing をノード中心間距離で適用するため、
 * ノード幅が大きいと重なりが発生する。描画後に実サイズを測定し、
 * 重なりを検出・解消する。 */
network.once('afterDrawing', function() {{
  var allIds = nodes.getIds();
  if (allIds.length === 0) return;
  var pos = network.getPositions();
  var bb = {{}};
  allIds.forEach(function(id) {{
    try {{ bb[id] = network.getBoundingBox(id); }} catch(e) {{}}
  }});

  /* レベル別にノードをグループ化 */
  var levelMap = {{}};
  nodes.forEach(function(n) {{
    var lv = n.level != null ? n.level : 0;
    if (!levelMap[lv]) levelMap[lv] = [];
    levelMap[lv].push(n.id);
  }});

  var HGAP = 12; /* 最小水平ギャップ(px) */
  var changed = false;

  for (var lv in levelMap) {{
    var ids = levelMap[lv];
    if (ids.length < 2) continue;
    /* X座標でソート */
    ids.sort(function(a, b) {{ return (pos[a] ? pos[a].x : 0) - (pos[b] ? pos[b].x : 0); }});
    /* 重なり検出 & 解消 */
    for (var i = 0; i < ids.length - 1; i++) {{
      var aId = ids[i], bId = ids[i + 1];
      var aBB = bb[aId], bBB = bb[bId];
      if (!aBB || !bBB) continue;
      var overlap = aBB.right + HGAP - bBB.left;
      if (overlap > 0) {{
        /* a を左に、b を右に均等シフト */
        var shift = overlap / 2 + 1;
        pos[aId].x -= shift;
        pos[bId].x += shift;
        /* 後続ノードへの波及: シフト量を連鎖 */
        for (var j = i + 2; j < ids.length; j++) {{
          pos[ids[j]].x += shift;
        }}
        /* BB更新 */
        aBB.left -= shift; aBB.right -= shift;
        bBB.left += shift; bBB.right += shift;
        changed = true;
      }}
    }}
  }}

  if (changed) {{
    /* 調整後の座標を適用 */
    var updates = [];
    allIds.forEach(function(id) {{
      if (pos[id]) updates.push({{ id: id, x: pos[id].x, y: pos[id].y }});
    }});
    nodes.update(updates);
    network.fit({{ padding: 30, animation: false }});
  }} else {{
    network.fit({{ padding: 30 }});
  }}
}});
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
