# ui/graph.py  ―  vis.js インタラクティブトポロジー描画
#   色優先順位・予兆アンバーハイライト・3分類対応
#   ★ Streamlit Custom Component 移行済み: iframe 再生成なし → 白いベール根治
import json
import streamlit as st
import streamlit.components.v1 as components
from alarm_generator import NodeColor, Alarm
from typing import List, Dict, Any, Tuple
from components.topology_graph import topology_graph as _topology_component
from components.topology_graph import impact_graph as _impact_component


# デバイスタイプ別のデフォルト形状・色定義（configs/device_types.json から取得）
# アラーム状態（赤/黄/アンバー等）はこれを上書きする
from configs.device_registry import get_all_visuals as _get_all_visuals, get_visual as _get_visual

_DEVICE_TYPE_VISUALS = _get_all_visuals()


# =====================================================
# ゾーン描画定数（レイアウト計算 & 描画の両レイヤーで共有）
# ★ _compute_fixed_positions() と beforeDrawing コールバックの
#    両方がこれらの値を参照する。片方だけ変えると不整合になるため
#    必ずここで一元管理する。
# =====================================================
ZONE_PAD = 25        # ゾーン矩形の左右パディング (px)
ZONE_PAD_TOP = 30    # ゾーン矩形の上パディング (px, ラベル用に広め)
ZONE_MIN_GAP = 6     # 隣接ゾーン間の最小ギャップ (px)
ENV_PAD = 18         # エンベロープの追加パディング (px)
ENV_PAD_TOP = 22     # エンベロープの上パディング (px, ラベル用)
# ゾーン同士が重ならないために必要な最小の grid ZONE_GAP:
#   左ゾーンの右パディング + ギャップ + 右ゾーンの左パディング
MIN_ZONE_GAP = ZONE_PAD * 2 + ZONE_MIN_GAP  # = 56px

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

# vis.js の形状のうち、ラベルを図形の「下」に描画するもの
# (box/ellipse/database は内部描画 → 上下対称)
_LABEL_BELOW_SHAPES = frozenset({
    "hexagon", "diamond", "star", "triangle",
    "triangleDown", "dot", "square",
})

# ノード寸法推定の共通定数
# ★ _node_extents() が唯一の寸法計算ソース。_compute_fixed_positions と
#    _est_node_size の両方がこれを参照する。片方だけ変えると不整合になるため
#    必ずここで一元管理する。
_SHAPE_RADIUS = 30    # vis.js デフォルト size=25 + margin
_SHAPE_LABEL_GAP = 8  # 図形とラベル間の隙間
_VIS_MARGIN = 8       # vis.js nodes.margin (top/bottom) — graph.py options で設定


def _node_extents(nid: str, topology: dict, font_size: int = 12) -> tuple:
    """ノードの (x,y) からの上方向・下方向エクステントを推定。

    vis.js は hexagon/diamond/star で (x,y) = 図形中心にラベルを下に描画。
    box/ellipse は (x,y) = ノード全体の中心にラベルを内部描画。
    いずれもノード margin (8px top/bottom) が描画に加算されるため考慮する。
    Returns: (above, below) — y座標からの上方向・下方向の広がり(px)
    """
    node = topology.get(nid) if topology else None
    # dict と NetworkNode オブジェクトの両方を処理
    if node is None:
        node_dict: dict = {}
    elif isinstance(node, dict):
        node_dict = node
    else:
        # NetworkNode オブジェクト → getattr で属性取得
        node_dict = {
            "type": getattr(node, "type", "UNKNOWN"),
            "metadata": getattr(node, "metadata", {}),
        }
    n_lines = 2  # ID行 + (type/role)行
    meta = node_dict.get("metadata", {})
    if isinstance(meta, dict):
        if meta.get("redundancy_type"):
            n_lines += 1
        if meta.get("vendor"):
            n_lines += 1
    n_lines += 1  # ステータスタグ行を保守的に考慮
    line_h = font_size + 5
    text_h = n_lines * line_h + 24

    node_type = node_dict.get("type", "UNKNOWN")
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

    # _zones が未定義 → hierarchical レイアウトにフォールバック
    # （小規模トポロジー Site A/B ではゾーン枠は不要）
    return {}


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
    COL_W_HINT = cfg.get("col_width", 340)
    H_GAP = cfg.get("node_h_gap", 150)
    FONT_SZ = cfg.get("font_size", 12)
    EDGE_GAP = cfg.get("edge_gap", 22)
    NODE_MAX_W = 180  # vis.js widthConstraint maximum
    # ★ ZONE_GAP を描画パディングから算出した最小値で保証。
    #   JSON に指定された値がこれより小さい場合は自動で引き上げる。
    #   エンベロープがある場合は ENV_PAD 分の追加マージンも加算。
    _has_envelopes = "_envelopes" in zones
    _min_gap = MIN_ZONE_GAP + (ENV_PAD * 2 if _has_envelopes else 0)
    ZONE_GAP_CFG = cfg.get("zone_gap", 30)
    ZONE_GAP_VAL = max(ZONE_GAP_CFG, _min_gap)
    PAD_TOP = 40      # ゾーンラベル(18px) + 余白: 過大な値は _canvas_h を膨張させる
    PAD_BOTTOM = 45

    # ★ 寸法推定はモジュールレベルの _node_extents() を使用（重複排除）
    def _est_extents(nid: str):
        return _node_extents(nid, topology, FONT_SZ)

    # --- Pass 1: 各ゾーンの行エクステント & 内部全高 & 必要幅を計算 ---
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

        # ★ 必要幅 = 最も広い行のノード配置幅 + パディング
        #   各行の幅: (n-1) * H_GAP (ノード間距離) + NODE_MAX_W (左右端のノード幅)
        max_row_len = max(len(row) for row in rows)
        content_w = (max_row_len - 1) * H_GAP + NODE_MAX_W
        required_w = content_w + ZONE_PAD * 2

        zone_info[zk] = {
            "gc": gc, "gr": gr, "colspan": colspan, "rowspan": rowspan,
            "rows": rows,
            "row_aboves": row_aboves, "row_belows": row_belows,
            "internal_h": internal_h,
            "required_w": required_w,
        }

    # --- Pass 1.5: 列幅の動的計算 → ゾーン中心X座標 ---
    # 各グリッド列に必要な最小幅を、その列に属するゾーンから算出する。
    # colspan > 1 のゾーンは幅を均等按分して各列の下限に反映する。
    col_min_w: dict = {}
    for zi in zone_info.values():
        per_col = zi["required_w"] / zi["colspan"]
        for c in range(zi["gc"], zi["gc"] + zi["colspan"]):
            col_min_w[c] = max(col_min_w.get(c, 0), per_col)
    # JSON の col_width ヒントを最低幅として適用
    for c in col_min_w:
        col_min_w[c] = max(col_min_w[c], COL_W_HINT)

    # 列の開始X座標を累積計算（列間に ZONE_GAP_VAL を確保）
    col_x_start: dict = {}
    x_cursor = 0.0
    for c in sorted(col_min_w.keys()):
        col_x_start[c] = x_cursor
        x_cursor += col_min_w[c] + ZONE_GAP_VAL

    # 各ゾーンの中心X = そのゾーンが占める列範囲の中央
    for zi in zone_info.values():
        first_col = zi["gc"]
        last_col = zi["gc"] + zi["colspan"] - 1
        x_left = col_x_start[first_col]
        x_right = col_x_start[last_col] + col_min_w[last_col]
        zi["cx"] = (x_left + x_right) / 2

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
        y_cursor += grid_row_max[r] + ZONE_GAP_VAL

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

    # --- Pass 4: グリッドセル境界メタデータを zones に注入 ---
    # beforeDrawing でゾーン矩形をトップダウン算出するため
    # (GML grid-based layout: グリッドセル境界が矩形を決定 → 非重複保証)
    zones["_col_bounds"] = {
        str(c): {"x_start": col_x_start[c], "width": col_min_w[c]}
        for c in sorted(col_min_w.keys())
    }
    zones["_row_bounds"] = {
        str(r): {"y_start": grid_row_y[r], "height": grid_row_max[r]}
        for r in sorted(grid_row_y.keys())
    }

    return positions


def render_topology_graph(topology: dict, alarms: List[Alarm], analysis_results: List[dict]):
    """
    vis.js でインタラクティブなトポロジーグラフを描画する。
    Streamlit Custom Component により iframe を再生成せず
    データ差分のみ更新（白いベール根治）。

    色優先順位（高→低）:
      1. Root Cause CRITICAL（赤）/ WARNING（黄）/ SILENT（紫）
      2. 実予兆 amber / シミュ予兆 薄amber
      3. Symptom (派生) — オレンジ
      4. Unreachable — グレー
      5. Unrelated (ノイズ) — 薄紫ダイヤ
      6. Normal — グリーン
    """
    # ★ Custom Component 移行後: HTML キャッシュ不要
    #   iframe は破棄されず、onRender() でデータ差分のみ更新される

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

    # --- ゾーン定義の読み込み & レイアウト計算 ---
    zones = _load_zones_for_site(topology)
    fixed_positions = _compute_fixed_positions(zones, topology)
    _use_fixed = bool(fixed_positions)
    # ★ 白いベール対策: network.fit() の padding を Python 側で事前計算。
    #   network.fit() はノード BB のみ考慮し、beforeDrawing のゾーン/エンベロープ矩形を
    #   含まない。ゾーン拡張量(world px)の最大値を padding として設定することで
    #   1回の fit で全ゾーンを表示枠内に収める（2パス fit 不要 → 描画サイクル削減）。
    _has_envelopes = "_envelopes" in zones
    _zone_top_ext = ZONE_PAD_TOP + (ENV_PAD_TOP + 20 if _has_envelopes else 0)
    _zone_side_ext = ZONE_PAD + (ENV_PAD if _has_envelopes else 0)
    _fit_pad = max(_zone_top_ext, _zone_side_ext) + 10  # 安全マージン

    # --- ノード生成 ---
    # _zones 等のメタデータキー（_ プレフィックス）をデバイスノードとして処理しない
    _device_items = {k: v for k, v in topology.items() if not k.startswith("_")}
    _n_nodes = len(_device_items)
    _font_size = 12
    nodes = []
    for node_id, node in _device_items.items():
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
    for _nid, _n in _device_items.items():
        _rg = _n.get('redundancy_group') if isinstance(_n, dict) else getattr(_n, 'redundancy_group', None)
        if _rg:
            _rg_index.setdefault(_rg, []).append(_nid)

    # --- エッジ生成 ---
    edges = []
    added_edges = set()
    for node_id, node in _device_items.items():
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
        # グリッドセル境界からキャンバス高さを算出。
        # network.fit() がコンテンツをコンテナ幅に収めるズームを適用するため、
        # Python 側でもズーム後の高さを近似する。
        # layout="wide" 時のメインカラム幅は通常 1000-1200px（サイドバー表示時）。
        _APPROX_CONTAINER_W = 1400  # 近似コンテナ幅 (px) — wide layout 前提（余裕を持たせる）
        _col_bounds = zones.get("_col_bounds", {})
        _row_bounds = zones.get("_row_bounds", {})
        if _col_bounds and _row_bounds:
            _content_w = (
                max(cb["x_start"] + cb["width"] for cb in _col_bounds.values())
                - min(cb["x_start"] for cb in _col_bounds.values())
                + (ENV_PAD + 5) * 2  # エンベロープパディング
            )
            _bottom_pad = (ZONE_PAD + ENV_PAD + 5) if "_envelopes" in zones else (ZONE_PAD + 5)
            _content_h = (
                max(rb["y_start"] + rb["height"] for rb in _row_bounds.values())
                - min(rb["y_start"] for rb in _row_bounds.values())
                + ENV_PAD_TOP + 18 + _bottom_pad  # パディング + ラベル + ゾーン下端
            )
            _approx_zoom = min(_APPROX_CONTAINER_W / _content_w, 1.0) if _content_w > 0 else 1.0
            _canvas_h = int(_content_h * _approx_zoom) + 100  # ゾーン拡張 + 凡例分
            _canvas_h = max(_canvas_h, 500)  # 最低高さ
        elif _row_bounds:
            _max_grid_y = max(rb["y_start"] + rb["height"] for rb in _row_bounds.values())
            _canvas_h = int(_max_grid_y + ENV_PAD_TOP + ENV_PAD + ZONE_PAD + 80)
        else:
            _max_y = max(p["y"] for p in fixed_positions.values())
            _canvas_h = int(_max_y + 250)
    else:
        # キャンバス高さはリフロー後に network.fit() で自動調整される
        _canvas_h = max(700, _n_nodes * 80)

    # --- Custom Component でレンダリング ---
    legend_html = _build_legend_html(used_states)
    _layout_config = {"mode": "fixed"} if _use_fixed else {"mode": "hierarchical"}
    _topology_component(
        nodes=nodes,
        edges=edges,
        zones=zones,
        use_fixed=_use_fixed,
        fit_pad=_fit_pad,
        canvas_h=_canvas_h,
        layout_js=_layout_config,
        legend_html=legend_html,
        font_size=_font_size,
        key=f"topo_{st.session_state.get('active_site', 'A')}",
    )

    # ★ 旧 HTML テンプレートは Custom Component frontend/index.html に移行済み


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

    # ★ Custom Component 移行後: HTML キャッシュ不要

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

    # --- Custom Component で影響伝搬グラフを描画 ---
    _impact_component(
        nodes=nodes,
        edges=edges,
        canvas_h=370,
        key=f"impact_{root_device_id}",
    )

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
    st.markdown(summary_html, unsafe_allow_html=True)
