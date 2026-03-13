# streamlit_cache.py
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 廃止: エンジンキャッシュは ui/engine_cache.py に一本化。
# このファイルは後方互換のリダイレクトのみ提供する。
# 新規コードでは ui.engine_cache.get_dt_engine_for_site() を使用すること。
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
import logging

logger = logging.getLogger(__name__)


def get_digital_twin_engine(topology: dict, children_map: dict):
    """後方互換エントリポイント → ui.engine_cache に委譲。

    ★ 注意: この関数は非推奨です。
    新規コードでは ui.engine_cache.get_dt_engine_for_site(site_id) を使用してください。
    独自の @st.cache_resource を持たず、ui/engine_cache.py の Singleton に委譲するため
    メモリの二重持ちは発生しません。
    """
    try:
        from ui.engine_cache import compute_topo_hash, get_cached_dt_engine
        topo_hash = compute_topo_hash(topology)
        # topology のキーから site_id を推定（後方互換）
        site_id = "default"
        try:
            from registry import list_sites
            sites = list_sites()
            if sites:
                site_id = sites[0]
        except Exception:
            pass
        return get_cached_dt_engine(site_id, topo_hash)
    except Exception as e:
        logger.warning("streamlit_cache fallback: %s", e)
        # フォールバック: 直接生成（キャッシュなし）
        try:
            from digital_twin_pkg import DigitalTwinEngine
            return DigitalTwinEngine(topology, children_map, tenant_id="default")
        except Exception:
            return None
