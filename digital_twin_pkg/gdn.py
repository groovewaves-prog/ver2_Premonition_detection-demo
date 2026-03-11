# digital_twin_pkg/gdn.py  ―  Phase 3: Graph Deviation Network
"""
GDN (Graph Deviation Network) モジュール。

GNN の課題「合成データのみの学習」を解決するため、
ベースラインモデリング + 偏差検出 のアプローチを採用。

主要コンポーネント:
  1. DeviceBaselineTracker: デバイスごとの正常時統計を蓄積・管理
  2. GraphDeviationScorer: マルチメトリクス偏差スコアリング
  3. GDNPredictor: GNN を補完する偏差ベースの異常検知

理論的背景:
  - GDN (Deng & Hooi, AAAI 2021): グラフ構造学習による多変量時系列異常検知
  - 正常時のセンサー間関係を学習し、逸脱を検知
  - 本実装では EscalationRule のメトリクス + アラーム特徴量を「センサー」として扱う
"""
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------
BASELINE_MIN_SAMPLES = 10       # ベースライン有効化に必要な最小サンプル数
BASELINE_WINDOW_HOURS = 168     # ベースライン学習ウィンドウ (7日)
DEVIATION_SIGMA_THRESHOLD = 2.0 # 偏差検出の σ 閾値
MAX_FEATURES_PER_DEVICE = 16    # デバイスあたりの最大特徴量次元


# ---------------------------------------------------------------------------
# データ構造
# ---------------------------------------------------------------------------
@dataclass
class DeviceBaseline:
    """デバイスの正常時統計ベースライン。"""
    device_id: str
    feature_names: List[str] = field(default_factory=list)
    mean: Optional[np.ndarray] = None       # 特徴量の平均ベクトル
    std: Optional[np.ndarray] = None        # 特徴量の標準偏差ベクトル
    sample_count: int = 0
    last_updated: float = 0.0
    is_valid: bool = False                  # min_samples に達したか


@dataclass
class DeviationResult:
    """偏差検出の結果。"""
    device_id: str
    overall_score: float = 0.0              # 総合偏差スコア (0.0-1.0)
    feature_scores: Dict[str, float] = field(default_factory=dict)
    top_deviations: List[Tuple[str, float]] = field(default_factory=list)
    baseline_valid: bool = False
    anomaly_detected: bool = False
    confidence_boost: float = 0.0
    summary: str = ""


@dataclass
class GraphDeviationSummary:
    """グラフ全体の偏差サマリ。"""
    total_devices: int = 0
    devices_with_baseline: int = 0
    devices_with_anomaly: int = 0
    avg_deviation_score: float = 0.0
    max_deviation_device: str = ""
    max_deviation_score: float = 0.0
    device_scores: Dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# DeviceBaselineTracker: デバイスごとの正常時統計を蓄積
# ---------------------------------------------------------------------------
class DeviceBaselineTracker:
    """デバイスの正常時統計ベースラインを管理する。

    使い方:
        tracker = DeviceBaselineTracker(storage)
        tracker.update(device_id, feature_vector, is_normal=True)
        baseline = tracker.get_baseline(device_id)
    """

    def __init__(self, storage=None):
        self.storage = storage
        self._baselines: Dict[str, DeviceBaseline] = {}
        # オンライン統計用のバッファ (Welford のアルゴリズム)
        self._running_sum: Dict[str, np.ndarray] = {}
        self._running_sq_sum: Dict[str, np.ndarray] = {}
        self._sample_counts: Dict[str, int] = defaultdict(int)

    def update(
        self,
        device_id: str,
        features: np.ndarray,
        feature_names: Optional[List[str]] = None,
        is_normal: bool = True,
    ):
        """ベースラインにサンプルを追加する。

        Args:
            device_id: デバイスID
            features: 特徴量ベクトル (1D)
            feature_names: 特徴量名のリスト
            is_normal: 正常状態のサンプルか (異常時は蓄積しない)
        """
        if not is_normal:
            return

        features = np.asarray(features, dtype=np.float64).flatten()
        dim = len(features)
        if dim == 0:
            return

        # オンライン集計 (Welford)
        if device_id not in self._running_sum:
            self._running_sum[device_id] = np.zeros(dim)
            self._running_sq_sum[device_id] = np.zeros(dim)
        elif len(self._running_sum[device_id]) != dim:
            # 次元変更時はリセット
            self._running_sum[device_id] = np.zeros(dim)
            self._running_sq_sum[device_id] = np.zeros(dim)
            self._sample_counts[device_id] = 0

        self._running_sum[device_id] += features
        self._running_sq_sum[device_id] += features ** 2
        self._sample_counts[device_id] += 1

        n = self._sample_counts[device_id]
        mean = self._running_sum[device_id] / n
        variance = (self._running_sq_sum[device_id] / n) - (mean ** 2)
        std = np.sqrt(np.maximum(variance, 1e-10))

        baseline = DeviceBaseline(
            device_id=device_id,
            feature_names=feature_names or [f"f{i}" for i in range(dim)],
            mean=mean,
            std=std,
            sample_count=n,
            last_updated=time.time(),
            is_valid=n >= BASELINE_MIN_SAMPLES,
        )
        self._baselines[device_id] = baseline

        # DB に永続化
        if self.storage and n % 10 == 0:  # 10サンプルごとに保存
            try:
                self.storage.save_state_sqlite(
                    f"gdn_baseline_{device_id}",
                    {
                        "mean": mean.tolist(),
                        "std": std.tolist(),
                        "count": n,
                        "names": baseline.feature_names,
                        "ts": baseline.last_updated,
                    },
                )
            except Exception:
                pass

    def get_baseline(self, device_id: str) -> Optional[DeviceBaseline]:
        """デバイスのベースラインを取得する。"""
        baseline = self._baselines.get(device_id)
        if baseline and baseline.is_valid:
            return baseline

        # DB からロード (メモリに無い場合)
        if self.storage and device_id not in self._baselines:
            try:
                data = self.storage.load_state_sqlite(
                    f"gdn_baseline_{device_id}", None
                )
                if data and data.get("count", 0) >= BASELINE_MIN_SAMPLES:
                    baseline = DeviceBaseline(
                        device_id=device_id,
                        feature_names=data.get("names", []),
                        mean=np.array(data["mean"]),
                        std=np.array(data["std"]),
                        sample_count=data["count"],
                        last_updated=data.get("ts", 0),
                        is_valid=True,
                    )
                    self._baselines[device_id] = baseline
                    # オンライン集計も復元
                    n = data["count"]
                    mean = np.array(data["mean"])
                    std = np.array(data["std"])
                    self._running_sum[device_id] = mean * n
                    self._running_sq_sum[device_id] = (std ** 2 + mean ** 2) * n
                    self._sample_counts[device_id] = n
                    return baseline
            except Exception:
                pass

        return baseline if (baseline and baseline.is_valid) else None

    def get_all_baselines(self) -> Dict[str, DeviceBaseline]:
        """有効なベースラインを全て返す。"""
        return {
            k: v for k, v in self._baselines.items() if v.is_valid
        }


# ---------------------------------------------------------------------------
# マルチメトリクス特徴量ビルダー
# ---------------------------------------------------------------------------
def build_device_features(
    device_id: str,
    alarm_embedding: Optional[np.ndarray] = None,
    metrics: Optional[Dict[str, float]] = None,
    alarm_count: int = 0,
    severity_score: float = 0.0,
    trend_slope: float = 0.0,
    causality_weight: float = 0.0,
) -> Tuple[np.ndarray, List[str]]:
    """デバイスのマルチメトリクス特徴量ベクトルを構築する。

    GNN のノード特徴量として使用。BERT 埋め込み（768次元）は含めず、
    補助的なメトリクス情報のみ（最大16次元）。

    Returns:
        (feature_vector, feature_names)
    """
    features = []
    names = []

    # 1. アラーム統計
    features.append(float(alarm_count))
    names.append("alarm_count")

    features.append(float(severity_score))
    names.append("severity_score")

    # 2. メトリクス値 (Phase 1 trend.py からの抽出値)
    if metrics:
        for k, v in sorted(metrics.items())[:8]:  # 最大8メトリクス
            features.append(float(v))
            names.append(f"metric_{k}")

    # 3. トレンド情報 (Phase 1)
    features.append(float(trend_slope))
    names.append("trend_slope")

    # 4. 因果情報 (Phase 2)
    features.append(float(causality_weight))
    names.append("causality_weight")

    # 5. アラーム埋め込みの統計量 (768次元を圧縮)
    if alarm_embedding is not None and len(alarm_embedding) > 0:
        emb = np.asarray(alarm_embedding)
        features.append(float(np.mean(emb)))
        names.append("emb_mean")
        features.append(float(np.std(emb)))
        names.append("emb_std")
        features.append(float(np.max(emb)))
        names.append("emb_max")
        features.append(float(np.linalg.norm(emb)))
        names.append("emb_norm")
    else:
        features.extend([0.0, 0.0, 0.0, 0.0])
        names.extend(["emb_mean", "emb_std", "emb_max", "emb_norm"])

    # パディング / トランケート
    if len(features) > MAX_FEATURES_PER_DEVICE:
        features = features[:MAX_FEATURES_PER_DEVICE]
        names = names[:MAX_FEATURES_PER_DEVICE]

    return np.array(features, dtype=np.float64), names


# ---------------------------------------------------------------------------
# GraphDeviationScorer: 偏差スコアリング
# ---------------------------------------------------------------------------
class GraphDeviationScorer:
    """グラフ構造を考慮した偏差スコアリング。

    各デバイスの現在の特徴量をベースラインと比較し、
    z-score ベースの偏差スコアを算出する。
    """

    def __init__(
        self,
        baseline_tracker: DeviceBaselineTracker,
        sigma_threshold: float = DEVIATION_SIGMA_THRESHOLD,
    ):
        self.tracker = baseline_tracker
        self.sigma_threshold = sigma_threshold

    def score_device(
        self,
        device_id: str,
        current_features: np.ndarray,
        feature_names: Optional[List[str]] = None,
    ) -> DeviationResult:
        """単一デバイスの偏差スコアを算出する。"""
        result = DeviationResult(device_id=device_id)
        baseline = self.tracker.get_baseline(device_id)

        if not baseline or not baseline.is_valid:
            result.summary = "ベースライン未確立"
            return result

        result.baseline_valid = True
        current = np.asarray(current_features).flatten()

        # 次元の整合
        dim = min(len(current), len(baseline.mean))
        if dim == 0:
            result.summary = "特徴量なし"
            return result

        current = current[:dim]
        mean = baseline.mean[:dim]
        std = baseline.std[:dim]
        names = (feature_names or baseline.feature_names)[:dim]

        # z-score 計算
        z_scores = np.abs((current - mean) / np.maximum(std, 1e-10))

        # 特徴量ごとのスコア
        for i, name in enumerate(names):
            result.feature_scores[name] = round(float(z_scores[i]), 3)

        # 上位偏差特徴量
        top_indices = np.argsort(z_scores)[::-1][:5]
        result.top_deviations = [
            (names[i], round(float(z_scores[i]), 3))
            for i in top_indices
            if z_scores[i] >= self.sigma_threshold * 0.5
        ]

        # 総合偏差スコア: 閾値超過の割合 + 最大偏差
        n_exceeding = np.sum(z_scores >= self.sigma_threshold)
        max_z = float(np.max(z_scores)) if len(z_scores) > 0 else 0.0

        # スコア正規化: sigmoid(max_z - threshold) * (1 + 0.1 * n_exceeding)
        raw_score = 1.0 / (1.0 + np.exp(-(max_z - self.sigma_threshold)))
        result.overall_score = round(
            min(1.0, raw_score * (1.0 + 0.1 * n_exceeding)), 4
        )

        result.anomaly_detected = result.overall_score >= 0.6
        result.confidence_boost = round(
            min(0.12, result.overall_score * 0.15), 3
        ) if result.anomaly_detected else 0.0

        # サマリ生成
        if result.anomaly_detected:
            top_names = ", ".join(
                f"{n}({z:.1f}σ)" for n, z in result.top_deviations[:3]
            )
            result.summary = (
                f"異常偏差検出 (スコア: {result.overall_score:.2f}) | "
                f"逸脱特徴: {top_names}"
            )
        else:
            result.summary = f"正常範囲内 (スコア: {result.overall_score:.2f})"

        return result

    def score_graph(
        self,
        device_features: Dict[str, Tuple[np.ndarray, List[str]]],
        children_map: Optional[Dict[str, List[str]]] = None,
    ) -> GraphDeviationSummary:
        """グラフ全体の偏差スコアを算出する。

        Args:
            device_features: {device_id: (features, names)}
            children_map: 影響伝搬の重み付けに使用

        Returns:
            GraphDeviationSummary
        """
        summary = GraphDeviationSummary(total_devices=len(device_features))
        scores: Dict[str, float] = {}

        for dev_id, (features, names) in device_features.items():
            result = self.score_device(dev_id, features, names)
            if result.baseline_valid:
                summary.devices_with_baseline += 1
            if result.anomaly_detected:
                summary.devices_with_anomaly += 1
            scores[dev_id] = result.overall_score

        summary.device_scores = scores

        if scores:
            summary.avg_deviation_score = round(
                float(np.mean(list(scores.values()))), 4
            )
            max_dev = max(scores, key=scores.get)
            summary.max_deviation_device = max_dev
            summary.max_deviation_score = scores[max_dev]

        # 影響伝搬の補正: 上流デバイスの偏差が高い場合、下流も引き上げ
        if children_map:
            for parent, children_list in children_map.items():
                parent_score = scores.get(parent, 0.0)
                if parent_score >= 0.5:
                    for child in children_list:
                        if child in scores:
                            # 親の偏差の 30% を伝搬
                            propagated = parent_score * 0.3
                            scores[child] = min(1.0, scores[child] + propagated)
            summary.device_scores = scores

        return summary


# ---------------------------------------------------------------------------
# GDNPredictor: GNN を補完する偏差ベースの異常検知
# ---------------------------------------------------------------------------
class GDNPredictor:
    """GDN (Graph Deviation Network) ベースの異常検知器。

    GNN とは独立に動作し、ベースライン偏差から異常を検知する。
    GNN の信頼度を補完・補正するために使用される。

    使い方:
        gdn = GDNPredictor(storage, topology, children_map)

        # 正常時: ベースライン蓄積
        gdn.observe_normal(device_id, features, feature_names)

        # 予測時: 偏差スコアリング
        result = gdn.predict(device_id, features, feature_names)
        boost = result.confidence_boost

        # グラフ全体
        summary = gdn.predict_graph(device_features_dict)
    """

    def __init__(
        self,
        storage=None,
        topology: Optional[Dict] = None,
        children_map: Optional[Dict[str, List[str]]] = None,
        sigma_threshold: float = DEVIATION_SIGMA_THRESHOLD,
    ):
        self.topology = topology or {}
        self.children_map = children_map or {}
        self.tracker = DeviceBaselineTracker(storage)
        self.scorer = GraphDeviationScorer(self.tracker, sigma_threshold)

    def observe_normal(
        self,
        device_id: str,
        features: np.ndarray,
        feature_names: Optional[List[str]] = None,
    ):
        """正常状態のサンプルをベースラインに蓄積する。"""
        self.tracker.update(device_id, features, feature_names, is_normal=True)

    def predict(
        self,
        device_id: str,
        features: np.ndarray,
        feature_names: Optional[List[str]] = None,
    ) -> DeviationResult:
        """単一デバイスの偏差ベース予測を行う。"""
        return self.scorer.score_device(device_id, features, feature_names)

    def predict_graph(
        self,
        device_features: Dict[str, Tuple[np.ndarray, List[str]]],
    ) -> GraphDeviationSummary:
        """グラフ全体の偏差ベース予測を行う。"""
        return self.scorer.score_graph(device_features, self.children_map)

    def get_baseline_coverage(self) -> Dict[str, bool]:
        """各デバイスのベースライン確立状況を返す。"""
        result = {}
        for dev_id in self.topology:
            baseline = self.tracker.get_baseline(dev_id)
            result[dev_id] = baseline is not None and baseline.is_valid
        return result

    def get_baseline_stats(self) -> Dict:
        """ベースライン統計のサマリを返す。"""
        baselines = self.tracker.get_all_baselines()
        return {
            "total_devices": len(self.topology),
            "devices_with_baseline": len(baselines),
            "coverage_pct": round(
                len(baselines) / max(1, len(self.topology)) * 100, 1
            ),
            "avg_sample_count": round(
                np.mean([b.sample_count for b in baselines.values()]), 0
            ) if baselines else 0,
            "details": {
                dev_id: {
                    "samples": b.sample_count,
                    "valid": b.is_valid,
                    "features": len(b.feature_names),
                }
                for dev_id, b in baselines.items()
            },
        }
