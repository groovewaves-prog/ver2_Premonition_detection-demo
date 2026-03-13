# ui/engine_cache.py
# DigitalTwinEngine のキャッシュ管理を一元化
#
# cockpit.py と stream_dashboard.py の両方がこのモジュールを通じて
# 同一のエンジンインスタンスにアクセスする。
# これにより循環インポートを回避し、SQLite/ChromaDB の接続二重化を防ぐ。
#
# ★ エッセンス1: 完全Singleton化
#   - @st.cache_resource の引数は軽量な文字列のみ（site_id, topo_hash）
#   - 重いデータ（Topology）の読み込みはキャッシュ関数内部で実行
#   - キャッシュヒット判定の高速化（巨大辞書のハッシュ計算を排除）
#
# ★ エッセンス3: 推論結果キャッシュ
#   - predict_api / analyze の結果を @st.cache_data でキャッシュ
#   - ネットワーク状態が変わらない（ハッシュ同一）場合は即座に前回結果を返却

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


def _load_topology_for_site(site_id: str):
    """site_id からトポロジーを読み込む内部ヘルパー。
    キャッシュ関数内部からのみ呼ばれる。"""
    from registry import get_paths, load_topology
    paths = get_paths(site_id)
    return load_topology(paths.topology_path)


@st.cache_resource(show_spinner="🧠 Digital Twin Engine (GNN/VectorDB) をロード中...")
def get_cached_dt_engine(site_id: str, topo_hash: str):
    """DigitalTwinEngine のグローバルキャッシュ（@st.cache_resource）。

    ★ エッセンス1: 引数は軽量な文字列のみ。
    site_id + topo_hash でキャッシュキーが決まる。
    トポロジーが変わらない限り同一インスタンスが返る。
    重いトポロジーデータの読み込みはキャッシュミス時のみ実行。
    """
    from digital_twin_pkg import DigitalTwinEngine as _DTE
    _topology = _load_topology_for_site(site_id)
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


@st.cache_resource(show_spinner="🧠 LogicalRCA エンジン (GrayScope/Granger) をロード中...")
def get_cached_logical_rca(site_id: str, topo_hash: str):
    """LogicalRCA のグローバルキャッシュ（@st.cache_resource）。

    ★ エッセンス1: 引数は軽量な文字列のみ。
    site_id + topo_hash でキャッシュキーが決まる。
    重いトポロジーデータの読み込みはキャッシュミス時のみ実行。
    """
    from inference_engine import LogicalRCA
    _topology = _load_topology_for_site(site_id)
    return LogicalRCA(_topology)


def get_topo_hash_cached(site_id: str) -> str:
    """site_id に対する topo_hash を session_state にキャッシュして返す。

    毎回の compute_topo_hash 再計算を回避する。
    """
    _key = f"_topo_hash_{site_id}"
    cached = st.session_state.get(_key)
    if cached:
        return cached
    topology = _load_topology_for_site(site_id)
    h = compute_topo_hash(topology)
    st.session_state[_key] = h
    return h


def get_dt_engine_for_site(site_id: str = None):
    """現在アクティブなサイトの DigitalTwinEngine を取得する便利関数。

    cockpit.py, stream_dashboard.py, tuning.py など全UIから呼べる。
    """
    site_id = site_id or st.session_state.get("active_site")
    if not site_id:
        return None
    try:
        topo_hash = get_topo_hash_cached(site_id)
        return get_cached_dt_engine(site_id, topo_hash)
    except Exception as e:
        logger.warning("Failed to get DT engine for site %s: %s", site_id, e)
        return None


# =====================================================
# ★ エッセンス3: 推論結果キャッシュ層
# =====================================================

def cached_rca_analyze(site_id: str, topo_hash: str, alarms: list) -> list:
    """RCA分析結果をキャッシュする窓口関数。

    アラームのハッシュが同一なら前回の結果を即座に返す。
    ★ INFO アラームは RCA 結果に影響しないためハッシュから除外。
    """
    _alarm_hash = hash(tuple(
        (a.device_id, a.message, a.severity) for a in alarms
        if a.severity != "INFO"
    )) if alarms else 0

    _cache_key = f"_analysis_cache_{site_id}"
    _cached = st.session_state.get(_cache_key)
    if _cached and _cached.get("hash") == _alarm_hash and _cached.get("topo") == topo_hash:
        return _cached["results"]

    engine = get_cached_logical_rca(site_id, topo_hash)
    if alarms:
        results = engine.analyze(alarms)
    else:
        results = [{
            "id": "SYSTEM",
            "label": "正常稼働",
            "prob": 0.0,
            "type": "Normal",
            "tier": 3,
            "reason": "アラームなし"
        }]

    st.session_state[_cache_key] = {
        "hash": _alarm_hash,
        "topo": topo_hash,
        "results": results,
    }
    return results


def cached_predict_api(dt_engine, device_id: str, combined_msg: str,
                       site_id: str, source: str, sim_level: int,
                       signal_count: int, api_key=None) -> list:
    """predict_api の結果をキャッシュする窓口関数。

    device_id + sim_level + メッセージハッシュが同一なら前回結果を即座に返す。
    """
    _cache_key_pred = "dt_prediction_cache"
    if _cache_key_pred not in st.session_state:
        st.session_state[_cache_key_pred] = {}

    _msg_hash = hash(combined_msg[:200])
    _ck = f"v4_{device_id}|{sim_level}|{_msg_hash}"

    _cached = st.session_state[_cache_key_pred].get(_ck)
    if _cached is not None:
        return _cached

    import time as _time
    _resp = dt_engine.predict_api({
        "tenant_id":       site_id,
        "device_id":       device_id,
        "msg":             combined_msg,
        "timestamp":       _time.time(),
        "record_forecast": True,
        "attrs":           {
            "source":            source,
            "degradation_level": sim_level if source == "simulation" else 1,
            "signal_count":      signal_count,
        }
    })

    _preds = _resp.get("predictions", []) if _resp.get("ok") else []

    st.session_state[_cache_key_pred][_ck] = _preds

    # キャッシュサイズ制限
    _cache = st.session_state[_cache_key_pred]
    if len(_cache) > 20:
        _keys = list(_cache.keys())
        for _old_k in _keys[:10]:
            _cache.pop(_old_k, None)

    return _preds
