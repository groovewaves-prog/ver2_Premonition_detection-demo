# digital_twin_pkg/trend.py  ―  Phase 1: メトリクス時系列トレンド検出
"""
アラームメッセージからメトリクス値を抽出し、SQLite に蓄積。
蓄積データに対して線形回帰（STL 簡易版）でトレンドを検出し、
信頼度ブーストと障害到達時刻（TTF）を推定する。

統合先:
  - engine.py predict() から呼び出される
  - rules.py の requires_trend / trend_metric_regex / trend_min_slope を活用
"""
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# データ構造
# ---------------------------------------------------------------------------
@dataclass
class TrendResult:
    """トレンド分析の結果を格納する。"""
    detected: bool = False                # トレンドが検出されたか
    slope: float = 0.0                    # 傾き (単位/時間)
    slope_normalized: float = 0.0         # 正規化傾き (0.0-1.0, 劣化方向で正)
    r_squared: float = 0.0               # 決定係数 (フィット品質)
    data_points: int = 0                  # 使用したデータ点数
    window_hours: int = 24                # 分析ウィンドウ
    latest_value: Optional[float] = None  # 最新のメトリクス値
    estimated_ttf_hours: Optional[float] = None  # 閾値到達推定時間
    confidence_boost: float = 0.0         # 信頼度に加算するブースト値
    trend_direction: str = "stable"       # "degrading" | "improving" | "stable"
    summary: str = ""                     # 人間向けの要約テキスト


# ---------------------------------------------------------------------------
# メトリクス抽出
# ---------------------------------------------------------------------------
def extract_metric_from_message(
    message: str,
    metric_regex: Optional[re.Pattern],
) -> Optional[float]:
    """アラームメッセージからメトリクス値を正規表現で抽出する。

    Args:
        message: アラームメッセージ文字列
        metric_regex: コンパイル済み正規表現 (キャプチャグループ1つ)

    Returns:
        抽出した数値、またはマッチしなければ None
    """
    if not metric_regex or not message:
        return None
    try:
        m = metric_regex.search(message)
        if m:
            return float(m.group(1))
    except (ValueError, IndexError):
        pass
    return None


# ---------------------------------------------------------------------------
# トレンド分析コア
# ---------------------------------------------------------------------------
def analyze_trend(
    history: List[Tuple[float, float]],
    min_slope: float,
    failure_value: Optional[float] = None,
    normal_value: Optional[float] = None,
    window_hours: int = 24,
) -> TrendResult:
    """時系列メトリクスデータのトレンドを分析する。

    Args:
        history: [(timestamp, value), ...] タイムスタンプ昇順
        min_slope: 劣化判定の最小傾き (rules.py で定義)
        failure_value: 障害閾値 (到達したらCRITICAL)
        normal_value: 正常時のベースライン値
        window_hours: 分析ウィンドウ (hours)

    Returns:
        TrendResult
    """
    result = TrendResult(window_hours=window_hours)

    if len(history) < 3:
        result.summary = f"データ不足 ({len(history)}点 < 3点)"
        result.data_points = len(history)
        return result

    ts_arr = np.array([h[0] for h in history])
    val_arr = np.array([h[1] for h in history])

    result.data_points = len(history)
    result.latest_value = float(val_arr[-1])

    # 時間軸を「時間 (hours)」に変換
    t_hours = (ts_arr - ts_arr[0]) / 3600.0

    # 時間幅が短すぎる場合はスキップ
    if t_hours[-1] - t_hours[0] < 0.1:  # 6分未満
        result.summary = "時間幅不足 (< 6分)"
        return result

    # 線形回帰: y = slope * t + intercept
    try:
        coeffs = np.polyfit(t_hours, val_arr, 1)
        slope = float(coeffs[0])
        intercept = float(coeffs[1])
    except (np.linalg.LinAlgError, ValueError):
        result.summary = "回帰計算失敗"
        return result

    result.slope = slope

    # 決定係数 R²
    y_pred = np.polyval(coeffs, t_hours)
    ss_res = np.sum((val_arr - y_pred) ** 2)
    ss_tot = np.sum((val_arr - np.mean(val_arr)) ** 2)
    result.r_squared = float(1.0 - ss_res / ss_tot) if ss_tot > 1e-10 else 0.0

    # 劣化方向の判定
    # min_slope が負 → 値が下がる方向が劣化 (例: 光パワー -0.05)
    # min_slope が正 → 値が上がる方向が劣化 (例: メモリ使用率 +1.0)
    if min_slope < 0:
        # 負の傾き = 劣化 (光パワー減衰など)
        is_degrading = slope <= min_slope  # slope < min_slope (both negative)
        degradation_magnitude = abs(slope / min_slope) if abs(min_slope) > 1e-10 else 0.0
    else:
        # 正の傾き = 劣化 (メモリ使用率増加など)
        is_degrading = slope >= min_slope
        degradation_magnitude = abs(slope / min_slope) if abs(min_slope) > 1e-10 else 0.0

    result.detected = is_degrading and result.r_squared >= 0.3

    if is_degrading:
        result.trend_direction = "degrading"
    elif abs(slope) < abs(min_slope) * 0.3:
        result.trend_direction = "stable"
    else:
        result.trend_direction = "improving"

    # 正規化傾き: 劣化方向で 0.0-1.0 にクランプ
    result.slope_normalized = min(1.0, max(0.0, degradation_magnitude))

    # 障害閾値到達時刻 (TTF) の推定
    if result.detected and failure_value is not None:
        current_t = t_hours[-1]
        current_predicted = slope * current_t + intercept
        if abs(slope) > 1e-10:
            t_failure = (failure_value - intercept) / slope
            ttf = t_failure - current_t
            if ttf > 0:
                result.estimated_ttf_hours = float(ttf)

    # 信頼度ブースト計算
    if result.detected:
        # ブースト = 傾き強度 × フィット品質 × 重み
        # 最大 0.15 (15%) のブースト
        quality_factor = min(1.0, result.r_squared / 0.8)  # R² >= 0.8 で最大
        magnitude_factor = min(1.0, degradation_magnitude / 2.0)  # 閾値の2倍で最大
        result.confidence_boost = round(
            min(0.15, 0.15 * quality_factor * magnitude_factor), 3
        )

    # サマリ生成
    result.summary = _build_summary(result, min_slope, failure_value, normal_value)

    return result


def _build_summary(
    result: TrendResult,
    min_slope: float,
    failure_value: Optional[float],
    normal_value: Optional[float],
) -> str:
    """トレンド結果の人間向け要約を生成する。"""
    parts = []

    if result.detected:
        parts.append("⚠️ 劣化トレンド検出")
    else:
        parts.append("トレンド未検出")

    parts.append(f"傾き: {result.slope:+.4f}/h")
    parts.append(f"R²: {result.r_squared:.2f}")
    parts.append(f"データ: {result.data_points}点/{result.window_hours}h")

    if result.latest_value is not None:
        parts.append(f"最新値: {result.latest_value:.1f}")

    if result.estimated_ttf_hours is not None:
        if result.estimated_ttf_hours < 1:
            parts.append(f"閾値到達: {result.estimated_ttf_hours * 60:.0f}分後")
        else:
            parts.append(f"閾値到達: {result.estimated_ttf_hours:.1f}時間後")

    if result.confidence_boost > 0:
        parts.append(f"信頼度ブースト: +{result.confidence_boost:.1%}")

    return " | ".join(parts)


# ---------------------------------------------------------------------------
# TrendAnalyzer: ストレージ連携クラス
# ---------------------------------------------------------------------------
class TrendAnalyzer:
    """メトリクス蓄積 + トレンド分析を統括するクラス。

    engine.py から利用される:
        analyzer = TrendAnalyzer(storage)
        analyzer.ingest(device_id, rule, alarm_messages)
        trend_result = analyzer.analyze(device_id, rule)
    """

    def __init__(self, storage):
        """
        Args:
            storage: digital_twin_pkg.storage.DigitalTwinStorage インスタンス
        """
        self.storage = storage
        # 劣化シナリオ別のメトリクス範囲マップ (pattern → {failure_value, normal_value})
        self._metric_ranges: Dict[str, Dict[str, float]] = {}

    def register_metric_range(
        self, rule_pattern: str, normal_value: float, failure_value: float
    ):
        """劣化シナリオのメトリクス正常値・障害値を登録する。"""
        self._metric_ranges[rule_pattern] = {
            "normal": normal_value,
            "failure": failure_value,
        }

    def ingest(
        self,
        device_id: str,
        rule_pattern: str,
        metric_name: str,
        metric_regex: Optional[re.Pattern],
        messages: List[str],
        timestamp: Optional[float] = None,
    ) -> int:
        """アラームメッセージからメトリクス値を抽出し DB に蓄積する。

        Args:
            device_id: デバイスID
            rule_pattern: ルールパターン名
            metric_name: メトリクス名 (例: "rx_power_dbm")
            metric_regex: コンパイル済み正規表現
            messages: アラームメッセージのリスト
            timestamp: タイムスタンプ (省略時は現在時刻)

        Returns:
            蓄積した件数
        """
        if not metric_regex:
            return 0

        ts = timestamp or time.time()
        count = 0
        seen_values = set()

        for msg in messages:
            val = extract_metric_from_message(msg, metric_regex)
            if val is not None and val not in seen_values:
                self.storage.db_insert_metric(
                    device_id, rule_pattern, metric_name, ts, val
                )
                seen_values.add(val)
                count += 1
                # 同一タイムスタンプに異なるインターフェースの値がある場合
                # 微小なオフセットで区別
                ts += 0.001

        if count > 0:
            logger.debug(
                f"Trend ingest: {device_id}/{rule_pattern} +{count} points"
            )
        return count

    def analyze(
        self,
        device_id: str,
        rule_pattern: str,
        metric_name: str,
        min_slope: float,
        window_hours: int = 24,
    ) -> TrendResult:
        """蓄積済みメトリクスデータのトレンドを分析する。

        Args:
            device_id: デバイスID
            rule_pattern: ルールパターン名
            metric_name: メトリクス名
            min_slope: 劣化判定閾値 (rules.py の trend_min_slope)
            window_hours: 分析ウィンドウ (hours)

        Returns:
            TrendResult
        """
        now = time.time()
        min_ts = now - window_hours * 3600

        history = self.storage.db_fetch_metrics(
            device_id, rule_pattern, metric_name, min_ts
        )

        # メトリクス範囲の取得
        ranges = self._metric_ranges.get(rule_pattern, {})
        failure_value = ranges.get("failure")
        normal_value = ranges.get("normal")

        return analyze_trend(
            history=history,
            min_slope=min_slope,
            failure_value=failure_value,
            normal_value=normal_value,
            window_hours=window_hours,
        )
