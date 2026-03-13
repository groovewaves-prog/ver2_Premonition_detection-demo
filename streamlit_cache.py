# streamlit_cache.py
# ★ レガシー互換: 新しい ui/engine_cache.py に委譲
import logging

logger = logging.getLogger(__name__)

try:
    import streamlit as st
    HAS_STREAMLIT = True
except ImportError:
    HAS_STREAMLIT = False

try:
    from digital_twin_pkg import DigitalTwinEngine
    HAS_DT = True
except ImportError:
    HAS_DT = False


if HAS_STREAMLIT and HAS_DT:
    @st.cache_resource
    def _load_digital_twin_singleton(_topology_hash: str):
        """
        Streamlit process-level cache for the engine.
        ★ エッセンス1: 引数は軽量な文字列のみ（重い辞書を排除）。
        """
        logger.info("Initializing Digital Twin Engine (V45 - cached)...")
        from registry import get_paths, load_topology, list_sites
        # デフォルトサイトのトポロジーを内部で読み込み
        sites = list_sites()
        site_id = sites[0] if sites else "A"
        paths = get_paths(site_id)
        topology = load_topology(paths.topology_path)
        children_map = {}
        for nid, n in topology.items():
            pid = n.get('parent_id') if isinstance(n, dict) else getattr(n, 'parent_id', None)
            if pid:
                children_map.setdefault(pid, []).append(nid)
        return DigitalTwinEngine(topology, children_map, tenant_id="default")

    def get_digital_twin_engine(topology: dict, children_map: dict):
        """
        Entry point from app.py.
        ★ エッセンス1: トポロジーのハッシュのみをキーとして渡す。
        """
        import hashlib, json
        topo_hash = hashlib.md5(
            json.dumps(sorted(topology.keys())).encode()
        ).hexdigest()
        return _load_digital_twin_singleton(topo_hash)

else:
    def get_digital_twin_engine(topology: dict, children_map: dict):
        if HAS_DT:
            return DigitalTwinEngine(topology, children_map, tenant_id="default")
        return None
