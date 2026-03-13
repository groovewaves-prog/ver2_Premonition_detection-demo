# ui/async_inference.py — 非同期推論ワーカー（ゼロ・ウェイティング）
#
# 設計方針:
#   - UIスレッドとは別にバックグラウンドで推論を実行
#   - 結果はスレッドセーフな共有ストアに書き込み
#   - UI層はストアから結果を読み取り、計算中なら前回結果を表示
#   - ストリームシミュレーションの新データ到着時に自動的に推論をキック
#
# 構造:
#   [データ受信] → submit_rca_task() → ThreadPoolExecutor → _bg_store に結果書き込み
#   [UI描画]     → get_rca_result()  → _bg_store から読み取り（即座に返却）

import hashlib
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# =====================================================
# スレッドセーフな結果ストア
# =====================================================

@dataclass
class InferenceResult:
    """推論結果を保持するコンテナ"""
    fingerprint: str           # 入力の指紋
    results: List[Dict]        # 推論結果
    timestamp: float           # 計算完了時刻
    is_stale: bool = False     # TTL 超過フラグ


class _BackgroundStore:
    """バックグラウンド推論結果のスレッドセーフなストア。

    プロセスレベルのシングルトン。Streamlit の session_state とは独立に
    動作するため、バックグラウンドスレッドから安全に書き込める。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._rca_results: Dict[str, InferenceResult] = {}
        self._predict_results: Dict[str, InferenceResult] = {}
        self._analyzing: Dict[str, bool] = {}

    def set_analyzing(self, key: str, value: bool):
        with self._lock:
            self._analyzing[key] = value

    def is_analyzing(self, key: str) -> bool:
        with self._lock:
            return self._analyzing.get(key, False)

    def put_rca(self, site_id: str, fingerprint: str, results: list):
        with self._lock:
            self._rca_results[site_id] = InferenceResult(
                fingerprint=fingerprint,
                results=results,
                timestamp=time.time(),
            )
            self._analyzing.pop(f"rca_{site_id}", None)

    def get_rca(self, site_id: str, ttl: float = 60.0) -> Optional[InferenceResult]:
        with self._lock:
            r = self._rca_results.get(site_id)
            if r and (time.time() - r.timestamp > ttl):
                r.is_stale = True
            return r

    def put_predict(self, cache_key: str, fingerprint: str, results: list):
        with self._lock:
            self._predict_results[cache_key] = InferenceResult(
                fingerprint=fingerprint,
                results=results,
                timestamp=time.time(),
            )
            self._analyzing.pop(f"pred_{cache_key}", None)

    def get_predict(self, cache_key: str, ttl: float = 120.0) -> Optional[InferenceResult]:
        with self._lock:
            r = self._predict_results.get(cache_key)
            if r and (time.time() - r.timestamp > ttl):
                r.is_stale = True
            return r


# プロセスレベルのシングルトン
_bg_store = _BackgroundStore()
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ai_inference")
_pending_futures: Dict[str, Future] = {}
_pending_lock = threading.Lock()


# =====================================================
# バックグラウンド推論タスク
# =====================================================

def _bg_rca_analyze(site_id: str, topo_hash: str, fingerprint: str,
                    alarm_tuples: tuple):
    """バックグラウンドで RCA 分析を実行する。

    UIスレッドとは独立に動作し、結果を _bg_store に書き込む。
    """
    try:
        _bg_store.set_analyzing(f"rca_{site_id}", True)

        from ui.engine_cache import get_cached_logical_rca
        engine = get_cached_logical_rca(site_id, topo_hash)

        if not alarm_tuples:
            results = [{
                "id": "SYSTEM", "label": "正常稼働", "prob": 0.0,
                "type": "Normal", "tier": 3, "reason": "アラームなし"
            }]
        else:
            from alarm_generator import Alarm
            alarms = [
                Alarm(device_id=t[0], message=t[1], severity=t[2], is_root_cause=t[3])
                for t in alarm_tuples
            ]
            results = engine.analyze(alarms)

        _bg_store.put_rca(site_id, fingerprint, results)
        logger.debug("BG RCA analyze complete for %s (fp=%s)", site_id, fingerprint[:8])
    except Exception as e:
        logger.warning("BG RCA analyze failed for %s: %s", site_id, e)
        _bg_store.set_analyzing(f"rca_{site_id}", False)


def _bg_predict_api(site_id: str, topo_hash: str, device_id: str,
                    combined_msg: str, source: str, sim_level: int,
                    signal_count: int, cache_key: str, fingerprint: str):
    """バックグラウンドで predict_api を実行する。"""
    try:
        _bg_store.set_analyzing(f"pred_{cache_key}", True)

        from ui.engine_cache import get_cached_dt_engine
        dt_engine = get_cached_dt_engine(site_id, topo_hash)

        resp = dt_engine.predict_api({
            "tenant_id":       site_id,
            "device_id":       device_id,
            "msg":             combined_msg,
            "timestamp":       time.time(),
            "record_forecast": True,
            "attrs":           {
                "source":            source,
                "degradation_level": sim_level if source == "simulation" else 1,
                "signal_count":      signal_count,
            }
        })

        preds = resp.get("predictions", []) if resp.get("ok") else []
        _bg_store.put_predict(cache_key, fingerprint, preds)
        logger.debug("BG predict complete for %s/%s", site_id, device_id)
    except Exception as e:
        logger.warning("BG predict failed for %s/%s: %s", site_id, device_id, e)
        _bg_store.set_analyzing(f"pred_{cache_key}", False)


# =====================================================
# 公開API: タスク投入
# =====================================================

def submit_rca_task(site_id: str, topo_hash: str, alarms: list):
    """RCA分析をバックグラウンドでキックする。

    同一 fingerprint のタスクが実行中なら重複投入しない。
    """
    from ui.engine_cache import compute_alarm_fingerprint

    fingerprint = compute_alarm_fingerprint(alarms)

    # 既にキャッシュに同一指紋の結果があるなら不要
    cached = _bg_store.get_rca(site_id)
    if cached and cached.fingerprint == fingerprint and not cached.is_stale:
        return

    task_key = f"rca_{site_id}_{fingerprint}"
    with _pending_lock:
        existing = _pending_futures.get(task_key)
        if existing and not existing.done():
            return  # 実行中
        # 完了済みの Future をクリーンアップ
        _cleanup_futures()

    alarm_tuples = tuple(
        (a.device_id, a.message, a.severity, a.is_root_cause)
        for a in alarms
    ) if alarms else ()

    future = _executor.submit(
        _bg_rca_analyze, site_id, topo_hash, fingerprint, alarm_tuples
    )
    with _pending_lock:
        _pending_futures[task_key] = future


def submit_predict_task(site_id: str, topo_hash: str, device_id: str,
                        combined_msg: str, source: str, sim_level: int,
                        signal_count: int):
    """predict_api をバックグラウンドでキックする。"""
    fingerprint = hashlib.md5(combined_msg[:500].encode()).hexdigest()
    cache_key = f"{device_id}|{sim_level}|{fingerprint}"

    cached = _bg_store.get_predict(cache_key)
    if cached and cached.fingerprint == fingerprint and not cached.is_stale:
        return

    task_key = f"pred_{cache_key}"
    with _pending_lock:
        existing = _pending_futures.get(task_key)
        if existing and not existing.done():
            return
        _cleanup_futures()

    future = _executor.submit(
        _bg_predict_api, site_id, topo_hash, device_id,
        combined_msg, source, sim_level, signal_count,
        cache_key, fingerprint,
    )
    with _pending_lock:
        _pending_futures[task_key] = future


# =====================================================
# 公開API: 結果取得
# =====================================================

def get_rca_result(site_id: str, alarms: list,
                   fallback_results: Optional[list] = None) -> Tuple[list, bool]:
    """RCA分析結果を取得する。

    Returns:
        (results, is_analyzing)
        - results: 推論結果（キャッシュヒットまたはフォールバック）
        - is_analyzing: バックグラウンドで分析中かどうか
    """
    from ui.engine_cache import compute_alarm_fingerprint
    fingerprint = compute_alarm_fingerprint(alarms)

    cached = _bg_store.get_rca(site_id)
    is_analyzing = _bg_store.is_analyzing(f"rca_{site_id}")

    if cached and cached.fingerprint == fingerprint and not cached.is_stale:
        return cached.results, False

    # fingerprint不一致（シナリオ切替直後）→ fallback を優先する。
    # 旧シナリオのキャッシュを返すと、障害シナリオなのに「正常稼働」と
    # 表示されるUI同期バグの原因になる。
    if fallback_results:
        return fallback_results, is_analyzing

    # fallback もない場合のみ、stale でも前回結果を返す（空表示よりまし）
    if cached:
        return cached.results, is_analyzing

    return [], is_analyzing


def get_predict_result(device_id: str, combined_msg: str,
                       sim_level: int) -> Tuple[list, bool]:
    """predict_api の結果を取得する。"""
    fingerprint = hashlib.md5(combined_msg[:500].encode()).hexdigest()
    cache_key = f"{device_id}|{sim_level}|{fingerprint}"

    cached = _bg_store.get_predict(cache_key)
    is_analyzing = _bg_store.is_analyzing(f"pred_{cache_key}")

    if cached and cached.fingerprint == fingerprint and not cached.is_stale:
        return cached.results, False

    if cached:
        return cached.results, is_analyzing

    return [], is_analyzing


def is_any_analyzing(site_id: str) -> bool:
    """指定サイトで何らかの推論がバックグラウンド実行中かどうか"""
    return _bg_store.is_analyzing(f"rca_{site_id}")


# =====================================================
# プロアクティブ・キャッシュウォーミング
# =====================================================

def proactive_warm_cache(site_id: str, topo_hash: str, alarms: list,
                         predict_sources: Optional[List[Tuple[str, str, str, int, int]]] = None):
    """ストリームデータ到着時にバックグラウンドでキャッシュを温める。

    stream_dashboard.py から呼ばれ、ユーザーが Cockpit タブを開く前に
    推論結果を準備しておく（プロアクティブ型）。

    Args:
        site_id: サイトID
        topo_hash: トポロジーハッシュ
        alarms: アラームリスト
        predict_sources: [(device_id, combined_msg, source, sim_level, signal_count), ...]
    """
    # RCA 分析をバックグラウンドで先行実行
    submit_rca_task(site_id, topo_hash, alarms)

    # predict_api も先行実行
    if predict_sources:
        for dev_id, msg, src, level, count in predict_sources:
            submit_predict_task(
                site_id, topo_hash, dev_id, msg, src, level, count
            )


# =====================================================
# 内部ヘルパー
# =====================================================

def _cleanup_futures():
    """完了済みの Future をクリーンアップ"""
    done_keys = [k for k, f in _pending_futures.items() if f.done()]
    for k in done_keys:
        _pending_futures.pop(k, None)
