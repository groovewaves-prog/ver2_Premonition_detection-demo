# -*- coding: utf-8 -*-
"""
AIOps Agent - Global Rate Limiter Module (v3 - Complete)
=========================================================
gemma-3-12b-it の制限:
- 30 RPM / 14,400 RPD
- 128,000 入力トークン / 8,192 出力トークン
"""

import time
import threading
import logging
from typing import Optional, Dict, Any, Callable
from collections import deque
from dataclasses import dataclass
from functools import wraps

logger = logging.getLogger(__name__)


@dataclass
class RateLimitConfig:
    """レート制限の設定"""
    rpm: int = 30
    rpd: int = 14400
    input_tokens: int = 128000
    output_tokens: int = 8192
    safety_margin: float = 0.9
    cache_ttl: int = 3600


class _ModelBucket:
    """モデル別のレート制限バケット"""

    def __init__(self, config: RateLimitConfig):
        self.config = config
        self._request_times: deque = deque(maxlen=config.rpm * 2)
        self._daily_count: int = 0
        self._daily_reset_time: float = time.time()

    def clean_old_requests(self):
        now = time.time()
        while self._request_times and now - self._request_times[0] > 60:
            self._request_times.popleft()

    def check_limits(self) -> tuple:
        now = time.time()
        if now - self._daily_reset_time > 86400:
            self._daily_count = 0
            self._daily_reset_time = now
        self.clean_old_requests()
        rpm_limit = int(self.config.rpm * self.config.safety_margin)
        rpd_limit = int(self.config.rpd * self.config.safety_margin)
        current_rpm = len(self._request_times)
        if self._daily_count >= rpd_limit:
            return False, 3600
        if current_rpm < rpm_limit:
            return True, 0
        if self._request_times:
            oldest = self._request_times[0]
            wait_time = max(0.1, 60 - (now - oldest) + 0.1)
            return False, wait_time
        return True, 0

    def record(self):
        self._request_times.append(time.time())
        self._daily_count += 1

    def stats(self) -> Dict[str, Any]:
        self.clean_old_requests()
        return {
            'requests_last_minute': len(self._request_times),
            'rpm_limit': self.config.rpm,
            'daily_count': self._daily_count,
        }


# モデル別のデフォルトRPM設定（Google AI Studio 無料枠に準拠）
# ★ gemini-2.0-flash-exp: RPM=10, RPD=1500 と非常に厳しいため使用停止
# ★ gemma-3 シリーズ: RPM=30, RPD=14400（Google API サービス利用時）
MODEL_RATE_CONFIGS: Dict[str, RateLimitConfig] = {
    "gemma-3-12b-it":     RateLimitConfig(rpm=30,  rpd=14400),
    "gemma-3-4b-it":      RateLimitConfig(rpm=30,  rpd=14400),
    "gemini-2.0-flash":   RateLimitConfig(rpm=10,  rpd=1500),
    "gemini-2.0-flash-exp": RateLimitConfig(rpm=10, rpd=1500),
}


class GlobalRateLimiter:
    """
    スレッドセーフなグローバルレートリミッター（モデル別カウンタ版）

    ★改善ポイント:
    - モデル別に独立したRPMカウンタを保持
    - 異なるモデルへのリクエストが互いを圧迫しない
    - 後方互換: model_id 未指定時は "_default" バケットを使用
    """

    _instance: Optional['GlobalRateLimiter'] = None
    _lock = threading.Lock()

    def __new__(cls, config: Optional[RateLimitConfig] = None):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self, config: Optional[RateLimitConfig] = None):
        if getattr(self, '_initialized', False):
            return

        self.config = config or RateLimitConfig()
        self._buckets: Dict[str, _ModelBucket] = {}
        self._bucket_lock = threading.Lock()
        # 後方互換: デフォルトバケット
        self._buckets["_default"] = _ModelBucket(self.config)
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._cache_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._initialized = True

    def _get_bucket(self, model_id: Optional[str] = None) -> _ModelBucket:
        """モデル別バケットを取得（なければ作成）"""
        key = model_id or "_default"
        if key not in self._buckets:
            with self._bucket_lock:
                if key not in self._buckets:
                    cfg = MODEL_RATE_CONFIGS.get(key, self.config)
                    self._buckets[key] = _ModelBucket(cfg)
        return self._buckets[key]

    def _clean_old_requests(self):
        """後方互換: デフォルトバケットの古いリクエストを削除"""
        self._get_bucket().clean_old_requests()

    def _check_limits(self) -> tuple:
        """後方互換: デフォルトバケットの制限チェック"""
        return self._get_bucket().check_limits()

    def wait_for_slot(self, timeout: float = 60.0, model_id: Optional[str] = None) -> bool:
        """
        リクエスト可能になるまで待機（モデル別カウンタ版）

        Args:
            timeout: 最大待機時間（秒）
            model_id: モデル名（例: "gemma-3-12b-it"）。None でデフォルトバケット使用
        """
        bucket = self._get_bucket(model_id)
        start_time = time.time()

        while True:
            with self._request_lock:
                can_proceed, wait_time = bucket.check_limits()
                if can_proceed:
                    return True

            elapsed = time.time() - start_time
            if elapsed >= timeout:
                return False

            actual_wait = min(wait_time, timeout - elapsed, 5.0)
            if actual_wait > 0:
                time.sleep(actual_wait)

    def record_request(self, model_id: Optional[str] = None):
        """リクエストを記録（モデル別）"""
        bucket = self._get_bucket(model_id)
        with self._request_lock:
            bucket.record()

    def get_cache(self, key: str) -> Optional[Any]:
        """キャッシュ取得"""
        with self._cache_lock:
            entry = self._cache.get(key)
            if entry and time.time() - entry['ts'] < self.config.cache_ttl:
                return entry['val']
        return None

    def set_cache(self, key: str, value: Any):
        """キャッシュ設定"""
        with self._cache_lock:
            self._cache[key] = {'val': value, 'ts': time.time()}

    def get_stats(self, model_id: Optional[str] = None) -> Dict[str, Any]:
        """統計取得（モデル別またはデフォルト）"""
        with self._request_lock:
            if model_id:
                bucket = self._get_bucket(model_id)
                stats = bucket.stats()
                stats['model_id'] = model_id
                stats['cache_size'] = len(self._cache)
                return stats
            # 後方互換: 全バケットの合計を返す
            total_rpm = 0
            total_daily = 0
            for b in self._buckets.values():
                b.clean_old_requests()
                total_rpm += len(b._request_times)
                total_daily += b._daily_count
            return {
                'requests_last_minute': total_rpm,
                'rpm_limit': self.config.rpm,
                'daily_count': total_daily,
                'cache_size': len(self._cache),
                'active_models': [k for k in self._buckets if k != "_default"],
            }


# =====================================================
# ユーティリティ関数（inference_engine.py用）
# =====================================================
def estimate_tokens(text: str) -> int:
    """
    テキストのトークン数を概算
    
    日本語は1文字≒1.5トークン、英語は1単語≒1.3トークン
    """
    if not text:
        return 0
    
    # 日本語文字数をカウント
    japanese_chars = sum(1 for c in text if '\u3000' <= c <= '\u9fff' or '\uff00' <= c <= '\uffef')
    # 英語単語数を概算
    english_words = len(text.split()) - japanese_chars // 2
    
    # 概算トークン数
    return int(japanese_chars * 1.5 + max(0, english_words) * 1.3)


def check_input_limit(text: str, limit: int = 100000) -> bool:
    """
    入力テキストがトークン制限内かチェック
    
    Args:
        text: チェック対象テキスト
        limit: トークン上限（デフォルト: 100,000 = 128,000の約80%）
    
    Returns:
        bool: 制限内ならTrue
    """
    return estimate_tokens(text) < limit


def rate_limited_with_retry(max_retries: int = 3, base_delay: float = 2.0,
                           model_id: str = None):
    """
    レート制限とリトライを適用するデコレータ

    Args:
        max_retries: 最大リトライ回数
        base_delay: 基本待機時間（秒）
        model_id: モデル別バケットを使用する場合のモデルID
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            limiter = GlobalRateLimiter()

            for attempt in range(max_retries + 1):
                try:
                    if not limiter.wait_for_slot(timeout=30, model_id=model_id):
                        if attempt < max_retries:
                            time.sleep(base_delay * (attempt + 1))
                            continue
                        raise RuntimeError("Rate limit timeout")

                    limiter.record_request(model_id=model_id)
                    return func(*args, **kwargs)
                    
                except Exception as e:
                    error_msg = str(e).lower()
                    if any(x in error_msg for x in ['429', '503', 'overloaded', 'resource_exhausted']):
                        if attempt < max_retries:
                            wait_time = base_delay * (attempt + 1)
                            logger.warning(f"Rate limit error, retrying in {wait_time}s...")
                            time.sleep(wait_time)
                            continue
                    raise
            
            return None
        return wrapper
    return decorator
