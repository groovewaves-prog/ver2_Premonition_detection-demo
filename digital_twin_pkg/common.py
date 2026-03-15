# digital_twin_pkg/common.py
# ====================================================
# 共通ユーティリティ: BFS影響伝搬・トポロジー操作・分類ロジック
#
# 以下の重複実装を統合:
#   - digital_twin.py:  _get_downstream_impact()
#   - alarm_generator.py: _get_all_downstream_devices(), _get_downstream_of_single_device()
#   - inference_engine.py: analyze() 内の BFS symptom injection
#   - ui/cockpit.py: _compute_downstream_fallback()
# ====================================================

from typing import Any, Dict, List, Optional, Set, Tuple


# ====================================================
# トポロジーヘルパー
# ====================================================

def get_node_attr(node: Any, attr: str, default: Any = None) -> Any:
    """dict/object 両対応でノード属性を取得"""
    if isinstance(node, dict):
        return node.get(attr, default)
    return getattr(node, attr, default)


def get_metadata(node: Any) -> dict:
    """ノードの metadata を dict として取得"""
    md = get_node_attr(node, 'metadata', {})
    if isinstance(md, dict):
        return md
    try:
        return vars(md)
    except Exception:
        return {}


def build_children_map(topology: dict) -> Dict[str, List[str]]:
    """トポロジーから parent→children のマッピングを構築"""
    children: Dict[str, List[str]] = {}
    for dev_id, node in topology.items():
        pid = get_node_attr(node, 'parent_id')
        if pid:
            children.setdefault(pid, []).append(dev_id)
    return children


def get_redundancy_group(topology: dict, node_id: str) -> Optional[str]:
    """ノードが属する冗長グループを取得"""
    node = topology.get(node_id)
    if not node:
        return None
    return get_node_attr(node, 'redundancy_group')


# ====================================================
# BFS 配下デバイス探索（統合版）
# ====================================================

def get_downstream_devices(
    topology: dict,
    root_id: str,
    max_hops: int = 0,
    include_hop_distance: bool = False,
    consider_redundancy: bool = False,
    children_map: Optional[Dict[str, List[str]]] = None,
) -> list:
    """
    BFS で root_id の配下デバイスを取得する統合関数。

    Args:
        topology: トポロジー辞書
        root_id: 起点デバイスID
        max_hops: 最大ホップ数 (0=無制限)
        include_hop_distance: True → List[Tuple[str, int]], False → List[str]
        consider_redundancy: True → 冗長グループメンバーの配下も含める
        children_map: 事前構築済み children_map (省略時は内部で構築)

    Returns:
        include_hop_distance=True:  [(device_id, hop_distance), ...]
        include_hop_distance=False: [device_id, ...]
    """
    if children_map is None:
        children_map = build_children_map(topology)

    # 冗長グループ考慮: root_id と同一 RG のメンバーも起点に含める
    start_ids = {root_id}
    if consider_redundancy:
        root_rg = get_redundancy_group(topology, root_id)
        if root_rg:
            for dev_id in topology:
                if get_redundancy_group(topology, dev_id) == root_rg:
                    start_ids.add(dev_id)

    results: list = []
    visited: Set[str] = set(start_ids)
    # (current_id, hop_distance)
    queue: List[Tuple[str, int]] = [(sid, 0) for sid in start_ids]

    while queue:
        current, hop = queue.pop(0)
        if current not in start_ids:
            if include_hop_distance:
                results.append((current, hop))
            else:
                results.append(current)
        next_hop = hop + 1
        if max_hops > 0 and next_hop > max_hops:
            continue
        for child in children_map.get(current, []):
            if child not in visited:
                visited.add(child)
                queue.append((child, next_hop))

    return results


def get_downstream_with_hops(
    topology: dict,
    root_id: str,
    max_hops: int = 3,
    children_map: Optional[Dict[str, List[str]]] = None,
) -> List[Tuple[str, int]]:
    """
    BFS で配下デバイスと hop 距離を返す（ショートカット）。
    影響伝搬グラフやDT Engine から呼ばれる。
    """
    return get_downstream_devices(
        topology, root_id,
        max_hops=max_hops,
        include_hop_distance=True,
        children_map=children_map,
    )


def get_all_downstream(
    topology: dict,
    root_ids: List[str],
    consider_redundancy: bool = True,
) -> List[str]:
    """
    複数起点のBFS配下デバイスリスト（冗長グループ考慮対応）。
    alarm_generator から呼ばれる。
    """
    children_map = build_children_map(topology)
    all_downstream: Set[str] = set()
    for rid in root_ids:
        devs = get_downstream_devices(
            topology, rid,
            consider_redundancy=consider_redundancy,
            children_map=children_map,
        )
        all_downstream.update(devs)
    return list(all_downstream)


# ====================================================
# 派生アラート（Symptom）自動補完
# ====================================================

def inject_downstream_symptoms(
    topology: dict,
    analysis_results: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    root_cause デバイスの配下でまだ分析されていないデバイスに
    symptom エントリを追加する。

    analysis_results を直接変更し、追加分も返す。
    """
    analyzed_ids = {r["id"] for r in analysis_results}
    rc_ids = {r["id"] for r in analysis_results if r.get("classification") == "root_cause"}
    if not rc_ids:
        return []

    children_map = build_children_map(topology)
    added = []

    for rc_id in list(rc_ids):
        downstream = get_downstream_devices(
            topology, rc_id, children_map=children_map,
        )
        for dev_id in downstream:
            if dev_id not in analyzed_ids:
                entry = {
                    "id": dev_id,
                    "label": f"上流障害 ({rc_id}) の影響",
                    "prob": 0.5,
                    "type": "Network/Downstream",
                    "tier": 2,
                    "reason": f"上流デバイス {rc_id} の障害による影響範囲内。通信断の可能性あり。",
                    "status": "YELLOW",
                    "is_prediction": False,
                    "classification": "symptom",
                }
                analysis_results.append(entry)
                analyzed_ids.add(dev_id)
                added.append(entry)

    return added


# ====================================================
# デバイス3分類
# ====================================================

def classify_device(
    device_id: str,
    root_cause_ids: Set[str],
    silent_suspect_ids: Set[str],
    topology: dict,
) -> str:
    """
    デバイスを3分類する:
      root_cause — 真因
      symptom    — 派生（真因の影響で発生）
      unrelated  — 無関係（ノイズ）
    """
    if device_id in silent_suspect_ids:
        return "root_cause"
    if device_id in root_cause_ids:
        return "root_cause"

    # 親チェーンを遡って root_cause / silent_suspect を探索
    visited: Set[str] = set()
    current = device_id
    while current:
        node = topology.get(current)
        if not node:
            break
        parent_id = get_node_attr(node, 'parent_id')
        if not parent_id or parent_id in visited:
            break
        visited.add(parent_id)
        if parent_id in root_cause_ids or parent_id in silent_suspect_ids:
            return "symptom"
        current = parent_id

    return "unrelated"


# ====================================================
# トラフィック分析ユーティリティ
# ====================================================

DEFAULT_ESTIMATED_USERS = 20  # AP に estimated_users が未定義の場合のデフォルト


def estimate_downstream_users(
    topology: dict,
    root_id: str,
    children_map: Optional[Dict[str, List[str]]] = None,
) -> Dict[str, Any]:
    """BFS で下流の ACCESS_POINT を探索し、推定ユーザー数を集計する。

    Returns:
        {
            "total_users": int,
            "ap_count": int,
            "ap_details": [{"id": str, "location": str, "users": int}, ...],
        }
    """
    downstream = get_downstream_devices(
        topology, root_id, children_map=children_map,
    )
    ap_details: List[Dict[str, Any]] = []
    total = 0
    for dev_id in downstream:
        node = topology.get(dev_id)
        if not node:
            continue
        dev_type = get_node_attr(node, 'type', '')
        if dev_type != 'ACCESS_POINT':
            continue
        md = get_metadata(node)
        users = md.get('estimated_users', DEFAULT_ESTIMATED_USERS)
        location = md.get('location', '')
        ap_details.append({"id": dev_id, "location": location, "users": users})
        total += users

    return {
        "total_users": total,
        "ap_count": len(ap_details),
        "ap_details": ap_details,
    }


def get_interface_to(
    topology: dict,
    from_id: str,
    to_id: str,
) -> Optional[Dict[str, Any]]:
    """from_id のインターフェースのうち to_id に接続されているものを返す。

    Returns:
        {"name": str, "bandwidth_mbps": int, "link_type": str, ...} or None
    """
    node = topology.get(from_id)
    if not node:
        return None
    interfaces = get_node_attr(node, 'interfaces', [])
    for iface in interfaces:
        if isinstance(iface, dict) and iface.get('connected_to') == to_id:
            return iface
    return None


def get_link_capacity_mbps(
    topology: dict,
    from_id: str,
    to_id: str,
) -> int:
    """2ノード間のリンク帯域 (Mbps) を取得。見つからない場合は 0。"""
    iface = get_interface_to(topology, from_id, to_id)
    if iface:
        return iface.get('bandwidth_mbps', 0)
    # 逆方向もチェック
    iface_rev = get_interface_to(topology, to_id, from_id)
    if iface_rev:
        return iface_rev.get('bandwidth_mbps', 0)
    return 0
