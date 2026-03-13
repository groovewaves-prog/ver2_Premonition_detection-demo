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
# ★ 推論結果キャッシュ層（@st.cache_data + TTL）
# =====================================================
#
# 設計方針:
#   - @st.cache_data: 計算結果（データ）のキャッシュに使用
#     （@st.cache_resource はエンジン等の重いオブジェクト用）
#   - 入力の「指紋（フィンガープリント）」を軽量な文字列で作成し、
#     キャッシュキーとして使用。入力が変わらなければ計算をスキップ。
#   - TTL を設定し、古い情報が残り続けるのを防止。
#   - エンジンは @st.cache_resource で保持された Singleton を内部で取得。
# =====================================================


def compute_alarm_fingerprint(alarms: list) -> str:
    """アラームリストの指紋（フィンガープリント）を計算する。

    推論関数への入力が前回と同じかどうかを高速に判定するための軽量ハッシュ。
    ★ INFO アラームは RCA 結果に影響しないためハッシュから除外。
    """
    if not alarms:
        return "empty"
    _sig = tuple(
        (a.device_id, a.message, a.severity) for a in alarms
        if a.severity != "INFO"
    )
    return hashlib.md5(str(_sig).encode()).hexdigest()


@st.cache_data(ttl=60, show_spinner=False)
def _rca_analyze_cached(site_id: str, topo_hash: str, alarm_fingerprint: str,
                        _alarm_tuples: tuple) -> list:
    """RCA分析結果を @st.cache_data でキャッシュする内部関数。

    ★ @st.cache_data(ttl=60): 60秒間キャッシュ。
      入力（alarm_fingerprint）が変わらなければ即座に前回結果を返す。
      60秒経過後は自動的に再計算が走り、最新の推論結果に更新される。

    引数は全て hashable な文字列/タプルのみ。
    エンジンは内部で Singleton を取得する。
    """
    engine = get_cached_logical_rca(site_id, topo_hash)

    if not _alarm_tuples:
        return [{
            "id": "SYSTEM",
            "label": "正常稼働",
            "prob": 0.0,
            "type": "Normal",
            "tier": 3,
            "reason": "アラームなし"
        }]

    # Alarm オブジェクトを再構築して analyze に渡す
    from alarm_generator import Alarm
    alarms = [
        Alarm(device_id=t[0], message=t[1], severity=t[2], is_root_cause=t[3])
        for t in _alarm_tuples
    ]
    return engine.analyze(alarms)


def cached_rca_analyze(site_id: str, topo_hash: str, alarms: list) -> list:
    """RCA分析の窓口関数。

    アラームリストを指紋化し、@st.cache_data 経由でキャッシュされた結果を返す。
    呼び出し側はアラームリストをそのまま渡すだけでよい。
    """
    fingerprint = compute_alarm_fingerprint(alarms)
    # Alarm を hashable なタプルに変換（@st.cache_data の引数制約）
    _alarm_tuples = tuple(
        (a.device_id, a.message, a.severity, a.is_root_cause)
        for a in alarms
    ) if alarms else ()
    return _rca_analyze_cached(site_id, topo_hash, fingerprint, _alarm_tuples)


@st.cache_data(ttl=120, show_spinner=False)
def _predict_api_cached(site_id: str, topo_hash: str,
                        device_id: str, msg_fingerprint: str,
                        combined_msg: str, source: str,
                        sim_level: int, signal_count: int) -> list:
    """predict_api 結果を @st.cache_data でキャッシュする内部関数。

    ★ @st.cache_data(ttl=120): 120秒間キャッシュ。
      予測は RCA より変化が少ないため、やや長めの TTL を設定。
      device_id + sim_level + メッセージ指紋が同一なら即座に前回結果を返す。

    引数は全て hashable な文字列/数値のみ。
    エンジンは内部で Singleton を取得する。
    """
    import time as _time
    dt_engine = get_cached_dt_engine(site_id, topo_hash)

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

    return _resp.get("predictions", []) if _resp.get("ok") else []


def cached_predict_api(dt_engine, device_id: str, combined_msg: str,
                       site_id: str, source: str, sim_level: int,
                       signal_count: int, api_key=None) -> list:
    """predict_api の窓口関数。

    メッセージを指紋化し、@st.cache_data 経由でキャッシュされた結果を返す。
    dt_engine 引数は後方互換のため残すが、内部では Singleton を使用する。
    """
    topo_hash = get_topo_hash_cached(site_id)
    msg_fingerprint = hashlib.md5(combined_msg[:500].encode()).hexdigest()
    return _predict_api_cached(
        site_id, topo_hash, device_id, msg_fingerprint,
        combined_msg, source, sim_level, signal_count,
    )
