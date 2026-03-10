# ui/engine_cache.py
# DigitalTwinEngine のキャッシュ管理を一元化
#
# cockpit.py と stream_dashboard.py の両方がこのモジュールを通じて
# 同一のエンジンインスタンスにアクセスする。
# これにより循環インポートを回避し、SQLite/ChromaDB の接続二重化を防ぐ。

import hashlib
import logging
import time

import streamlit as st

logger = logging.getLogger(__name__)


def compute_topo_hash(topology: dict) -> str:
    """トポロジーの構成変更を検知するための軽量ハッシュを計算"""
    try:
        keys = sorted(list(topology.keys()))
        state = []
        for k in keys:
            node = topology[k]
            pid = node.get('parent_id') if isinstance(node, dict) else getattr(node, 'parent_id', None)
            rg = node.get('redundancy_group') if isinstance(node, dict) else getattr(node, 'redundancy_group', None)
            state.append(f"{k}|{pid}|{rg}")
        return hashlib.md5(",".join(state).encode()).hexdigest()
    except Exception:
        return str(time.time())


@st.cache_resource(show_spinner="🧠 Digital Twin Engine (GNN/VectorDB) をロード中...")
def get_cached_dt_engine(site_id: str, topo_hash: str, _topology):
    """DigitalTwinEngine のグローバルキャッシュ（@st.cache_resource）。

    site_id + topo_hash でキャッシュキーが決まる。
    トポロジーが変わらない限り同一インスタンスが返る。
    """
    from digital_twin_pkg import DigitalTwinEngine as _DTE
    _children_map: dict = {}
    for _nid, _n in _topology.items():
        _pid = (_n.get('parent_id') if isinstance(_n, dict)
                else getattr(_n, 'parent_id', None))
        if _pid:
            _children_map.setdefault(_pid, []).append(_nid)
    return _DTE(
        topology=_topology,
        children_map=_children_map,
        tenant_id=site_id,
    )


def get_dt_engine_for_site(site_id: str = None):
    """現在アクティブなサイトの DigitalTwinEngine を取得する便利関数。

    cockpit.py, stream_dashboard.py, tuning.py など全UIから呼べる。
    """
    site_id = site_id or st.session_state.get("active_site")
    if not site_id:
        return None
    try:
        from registry import get_paths, load_topology
        paths = get_paths(site_id)
        topology = load_topology(paths.topology_path)
        topo_hash = compute_topo_hash(topology)
        return get_cached_dt_engine(site_id, topo_hash, topology)
    except Exception as e:
        logger.warning("Failed to get DT engine for site %s: %s", site_id, e)
        return None
