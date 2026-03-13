# digital_twin_pkg/grayscope.py  ―  Phase 4: GrayScope型メトリクス因果監視
"""
GrayScope (NSDI 2023) にインスパイアされたメトリクス因果監視モジュール。

既存のサイレント障害検出（50%ヒューリスティック）を確率的スコアリングに置換し、
Phase 1-3 の全シグナルを統合した総合的な障害検出を実現する。

主要コンポーネント:
  1. MetricCrossCorrelator: デバイス間のメトリクス相関を検出
  2. ImplicitFeedbackDetector: 直接アラームなしの暗黙的障害兆候を検出
  3. MultiHopPropagationTracer: 多段ホップの障害伝搬パスを追跡
  4. SilentFailureScorer: 全シグナルを統合した確率的サイレント障害スコア
"""
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# データ構造
# ---------------------------------------------------------------------------
@dataclass
class MetricCorrelation:
    """デバイス間のメトリクス相関。"""
    source_device: str
    target_device: str
    source_metric: str
    target_metric: str
    correlation: float = 0.0        # ピアソン相関係数 (-1.0 to 1.0)
    lag_bins: int = 0               # 最適ラグ (ビン数)
    significant: bool = False
    sample_count: int = 0


@dataclass
class PropagationPath:
    """障害伝搬パス。"""
    path: List[str] = field(default_factory=list)       # [root, hop1, hop2, ...]
    total_weight: float = 0.0
    hop_weights: List[float] = field(default_factory=list)
    estimated_delay_hours: float = 0.0
    evidence_type: str = "topology"  # "topology" | "granger" | "metric_correlation"


@dataclass
class SilentFailureCandidate:
    """サイレント障害候補。"""
    device_id: str
    score: float = 0.0              # 総合スコア (0.0-1.0)
    affected_children: List[str] = field(default_factory=list)
    affected_ratio: float = 0.0
    evidence: Dict[str, float] = field(default_factory=dict)
    propagation_paths: List[PropagationPath] = field(default_factory=list)
    implicit_signals: List[str] = field(default_factory=list)
    recommendation: str = ""


@dataclass
class GrayScopeResult:
    """GrayScope分析の全体結果。"""
    silent_candidates: List[SilentFailureCandidate] = field(default_factory=list)
    metric_correlations: List[MetricCorrelation] = field(default_factory=list)
    propagation_paths: List[PropagationPath] = field(default_factory=list)
    total_devices_analyzed: int = 0
    implicit_anomalies_found: int = 0
    summary: str = ""


# ---------------------------------------------------------------------------
# MetricCrossCorrelator: デバイス間のメトリクス相関を検出
# ---------------------------------------------------------------------------
class MetricCrossCorrelator:
    """デバイス間のメトリクス時系列の相互相関を計算する。

    Phase 1 trend.py のメトリクスデータを活用し、
    異なるデバイス間でメトリクスの因果的関係を検出する。

    例: Parent の Rx Power 低下 → Child の応答遅延増加
    """

    def __init__(self, storage, max_lag: int = 6, bin_minutes: int = 30):
        self.storage = storage
        self.max_lag = max_lag
        self.bin_minutes = bin_minutes

    def compute_cross_correlation(
        self,
        source_device: str,
        target_device: str,
        source_metric: str = "rx_power_dbm",
        target_metric: str = "rx_power_dbm",
        source_pattern: str = "optical",
        target_pattern: str = "optical",
        window_hours: int = 24,
    ) -> MetricCorrelation:
        """2デバイス間のメトリクス相互相関を計算する。"""
        result = MetricCorrelation(
            source_device=source_device,
            target_device=target_device,
            source_metric=source_metric,
            target_metric=target_metric,
        )

        now = time.time()
        min_ts = now - window_hours * 3600

        # メトリクスデータ取得
        src_data = self.storage.db_fetch_metrics(
            source_device, source_pattern, source_metric, min_ts
        )
        tgt_data = self.storage.db_fetch_metrics(
            target_device, target_pattern, target_metric, min_ts
        )

        if len(src_data) < 5 or len(tgt_data) < 5:
            return result

        # ビン化して等間隔時系列に変換
        src_ts = self._bin_metrics(src_data, min_ts, now, window_hours)
        tgt_ts = self._bin_metrics(tgt_data, min_ts, now, window_hours)

        n = min(len(src_ts), len(tgt_ts))
        if n < 5:
            return result

        src_ts = src_ts[:n]
        tgt_ts = tgt_ts[:n]
        result.sample_count = n

        # 正規化
        src_mean, src_std = np.mean(src_ts), np.std(src_ts)
        tgt_mean, tgt_std = np.mean(tgt_ts), np.std(tgt_ts)

        if src_std < 1e-10 or tgt_std < 1e-10:
            return result

        src_norm = (src_ts - src_mean) / src_std
        tgt_norm = (tgt_ts - tgt_mean) / tgt_std

        # ラグ付き相互相関
        best_corr = 0.0
        best_lag = 0

        for lag in range(0, min(self.max_lag + 1, n - 3)):
            if lag == 0:
                corr = float(np.mean(src_norm * tgt_norm))
            else:
                corr = float(np.mean(src_norm[:-lag] * tgt_norm[lag:]))

            if abs(corr) > abs(best_corr):
                best_corr = corr
                best_lag = lag

        result.correlation = round(best_corr, 4)
        result.lag_bins = best_lag
        result.significant = abs(best_corr) >= 0.5 and n >= 8

        return result

    def _bin_metrics(
        self,
        data: List[Tuple[float, float]],
        start_t: float,
        end_t: float,
        window_hours: int,
    ) -> np.ndarray:
        """メトリクスデータを等間隔ビンに変換する。"""
        bin_sec = self.bin_minutes * 60
        n_bins = int(window_hours * 3600 / bin_sec)
        bins = np.full(n_bins, np.nan)
        counts = np.zeros(n_bins)

        for ts, val in data:
            if ts < start_t or ts > end_t:
                continue
            idx = int((ts - start_t) / bin_sec)
            idx = min(idx, n_bins - 1)
            if np.isnan(bins[idx]):
                bins[idx] = val
            else:
                bins[idx] = (bins[idx] * counts[idx] + val) / (counts[idx] + 1)
            counts[idx] += 1

        # NaN を前方補完
        last_valid = np.nan
        for i in range(n_bins):
            if np.isnan(bins[i]):
                bins[i] = last_valid if not np.isnan(last_valid) else 0.0
            else:
                last_valid = bins[i]

        return bins

    def find_correlated_pairs(
        self,
        device_ids: List[str],
        children_map: Dict[str, List[str]],
        metric_rules: List = None,
        window_hours: int = 24,
    ) -> List[MetricCorrelation]:
        """トポロジー隣接デバイスペアの相関を検索する。"""
        results = []

        # メトリクスルールの設定
        metric_configs = [
            ("optical", "rx_power_dbm"),
            ("memory_leak", "memory_usage_pct"),
        ]

        for parent_id in device_ids:
            children = children_map.get(parent_id, [])
            for child_id in children:
                if child_id not in device_ids:
                    continue
                for pattern, metric in metric_configs:
                    corr = self.compute_cross_correlation(
                        parent_id, child_id, metric, metric,
                        pattern, pattern, window_hours
                    )
                    if corr.significant:
                        results.append(corr)

        return results


# ---------------------------------------------------------------------------
# ImplicitFeedbackDetector: 暗黙的障害兆候の検出
# ---------------------------------------------------------------------------
class ImplicitFeedbackDetector:
    """直接アラームなしの暗黙的障害兆候を検出する。

    Phase 1-3 の全シグナルを統合:
      - Phase 1: メトリクストレンド（傾き、R²）
      - Phase 2: Granger因果（重み、p値）
      - Phase 3: GDN偏差（スコア）
    """

    def __init__(
        self,
        trend_analyzer=None,
        granger_analyzer=None,
        gdn_predictor=None,
    ):
        self.trend = trend_analyzer
        self.granger = granger_analyzer
        self.gdn = gdn_predictor

    def detect_implicit_signals(
        self,
        device_id: str,
        alarmed_devices: Set[str],
        children_map: Dict[str, List[str]],
    ) -> Tuple[float, List[str]]:
        """デバイスの暗黙的障害シグナルを検出する。

        デバイス自体はアラームを出していなくても、
        配下デバイスやメトリクスから障害の兆候を推定する。

        Returns:
            (implicit_score 0.0-1.0, signal_descriptions)
        """
        signals = []
        scores = []

        children = children_map.get(device_id, [])
        if not children:
            return 0.0, []

        # 1. 配下デバイスのアラーム集中度
        alarmed_children = [c for c in children if c in alarmed_devices]
        child_alarm_ratio = len(alarmed_children) / max(1, len(children))
        if child_alarm_ratio >= 0.3:
            scores.append(child_alarm_ratio)
            signals.append(
                f"配下{len(alarmed_children)}/{len(children)}台がアラーム中 "
                f"({child_alarm_ratio:.0%})"
            )

        # 2. Phase 2: Granger因果的影響（このデバイスが原因の可能性）
        if self.granger:
            causal_children = self.granger.get_causal_children(
                device_id, min_weight=0.3
            )
            if causal_children:
                causal_weight = max(w for _, w in causal_children)
                scores.append(causal_weight * 0.8)
                signals.append(
                    f"因果的影響先{len(causal_children)}台 "
                    f"(最大重み: {causal_weight:.2f})"
                )

        # 3. Phase 3: GDN偏差スコア
        if self.gdn:
            try:
                from .gdn import build_device_features
                features, names = build_device_features(
                    device_id=device_id,
                    alarm_count=0,
                    severity_score=0.0,
                )
                gdn_result = self.gdn.predict(device_id, features, names)
                if gdn_result.anomaly_detected:
                    scores.append(gdn_result.overall_score * 0.7)
                    signals.append(
                        f"GDN偏差検出 (スコア: {gdn_result.overall_score:.2f})"
                    )
            except Exception:
                pass

        # 4. Phase 1: メトリクストレンド（このデバイスの劣化傾向）
        if self.trend:
            for pattern, metric, slope_dir in [
                ("optical", "rx_power_dbm", -1),
                ("memory_leak", "memory_usage_pct", 1),
            ]:
                try:
                    trend_result = self.trend.analyze(
                        device_id, pattern, metric,
                        min_slope=-0.05 if slope_dir < 0 else 1.0,
                        window_hours=24,
                    )
                    if trend_result.detected:
                        scores.append(trend_result.slope_normalized * 0.6)
                        signals.append(
                            f"トレンド劣化: {metric} "
                            f"(傾き: {trend_result.slope:+.4f}/h)"
                        )
                except Exception:
                    pass

        if not scores:
            return 0.0, []

        # 加重平均 (最大値に重みをかける)
        max_score = max(scores)
        avg_score = np.mean(scores)
        implicit_score = 0.6 * max_score + 0.4 * avg_score

        return round(min(1.0, implicit_score), 4), signals


# ---------------------------------------------------------------------------
# MultiHopPropagationTracer: 多段ホップの障害伝搬パス追跡
# ---------------------------------------------------------------------------
class MultiHopPropagationTracer:
    """障害伝搬パスを多段ホップで追跡する。

    BFS + 因果重み付きのパス探索。
    """

    def __init__(
        self,
        topology: Dict,
        children_map: Dict[str, List[str]],
        granger_analyzer=None,
        max_hops: int = 5,
    ):
        self.topology = topology
        self.children_map = children_map
        self.granger = granger_analyzer
        self.max_hops = max_hops

    def trace_from_root(
        self,
        root_device: str,
        alarmed_devices: Set[str],
    ) -> List[PropagationPath]:
        """root_device から障害が伝搬するパスを追跡する。"""
        paths = []
        visited = set()

        def _dfs(current: str, path: List[str], weights: List[float], depth: int):
            if depth > self.max_hops:
                return
            if current in visited:
                return
            visited.add(current)

            children = self.children_map.get(current, [])
            for child in children:
                # 因果重み
                weight = 0.5  # デフォルト: トポロジーのみ
                evidence = "topology"

                if self.granger:
                    causal_w = self.granger.get_causality_weight(
                        current, child, default=0.0
                    )
                    if causal_w > 0:
                        weight = max(weight, causal_w)
                        evidence = "granger"

                new_path = path + [child]
                new_weights = weights + [weight]

                # 子がアラーム中なら有効なパス
                if child in alarmed_devices:
                    total_w = float(np.mean(new_weights))
                    paths.append(PropagationPath(
                        path=new_path,
                        total_weight=round(total_w, 4),
                        hop_weights=[round(w, 3) for w in new_weights],
                        evidence_type=evidence,
                    ))

                _dfs(child, new_path, new_weights, depth + 1)

        _dfs(root_device, [root_device], [], 0)

        # 重み降順でソート
        paths.sort(key=lambda p: p.total_weight, reverse=True)
        return paths[:10]  # 上位10パス


# ---------------------------------------------------------------------------
# SilentFailureScorer: 全シグナル統合の確率的サイレント障害スコア
# ---------------------------------------------------------------------------
class SilentFailureScorer:
    """Phase 1-4 の全シグナルを統合し、
    サイレント障害の確率的スコアを算出する。

    既存の50%ヒューリスティックを補完・置換。
    """

    # スコアリング重み
    WEIGHT_CHILD_RATIO = 0.30       # 配下アラーム比率
    WEIGHT_GRANGER = 0.20           # Granger因果
    WEIGHT_IMPLICIT = 0.20          # 暗黙的フィードバック
    WEIGHT_TREND = 0.15             # メトリクストレンド
    WEIGHT_GDN = 0.15               # GDN偏差

    def __init__(
        self,
        topology: Dict,
        children_map: Dict[str, List[str]],
        trend_analyzer=None,
        granger_analyzer=None,
        gdn_predictor=None,
    ):
        self.topology = topology
        self.children_map = children_map
        self.implicit_detector = ImplicitFeedbackDetector(
            trend_analyzer, granger_analyzer, gdn_predictor
        )
        self.propagation_tracer = MultiHopPropagationTracer(
            topology, children_map, granger_analyzer
        )
        self.granger = granger_analyzer
        self.trend = trend_analyzer
        self.gdn = gdn_predictor

    def score_candidates(
        self,
        msg_map: Dict[str, List[str]],
        alarmed_devices: Optional[Set[str]] = None,
    ) -> List[SilentFailureCandidate]:
        """サイレント障害候補をスコアリングする。

        Args:
            msg_map: {device_id: [alarm_messages]}
            alarmed_devices: アラームがあるデバイスのセット

        Returns:
            SilentFailureCandidate のリスト (スコア降順)
        """
        if alarmed_devices is None:
            alarmed_devices = set(msg_map.keys())

        candidates = []

        for dev_id in self.topology:
            children = self.children_map.get(dev_id, [])
            if not children:
                continue

            # アラームがないデバイスのみ候補
            has_own_alarm = dev_id in alarmed_devices
            if has_own_alarm:
                # 自身がアラーム中でも、配下に大量の影響があれば候補に
                own_msgs = msg_map.get(dev_id, [])
                if any("Silent" in m for m in own_msgs):
                    pass  # サイレント疑いは処理続行
                else:
                    continue

            # 配下のアラーム状況
            # ★ alarmed_devices は呼び出し元 (inference_engine.py) で
            #   WARNING/CRITICAL のみにフィルタ済み。
            #   INFO のみのデバイス（予兆シミュレーション等）は含まれない。
            alarmed_children = [c for c in children if c in alarmed_devices]
            if not alarmed_children:
                continue

            child_ratio = len(alarmed_children) / len(children)

            # 各シグナルのスコアを収集
            evidence = {}
            signals = []

            # 1. 配下アラーム比率
            evidence["child_alarm_ratio"] = child_ratio
            if child_ratio >= 0.3:
                signals.append(
                    f"配下{len(alarmed_children)}/{len(children)}台アラーム中"
                )

            # 2. 暗黙的フィードバック
            implicit_score, implicit_signals = \
                self.implicit_detector.detect_implicit_signals(
                    dev_id, alarmed_devices, self.children_map
                )
            evidence["implicit_feedback"] = implicit_score
            signals.extend(implicit_signals)

            # 3. Granger因果スコア
            granger_score = 0.0
            if self.granger:
                causal_children = self.granger.get_causal_children(
                    dev_id, min_weight=0.2
                )
                if causal_children:
                    granger_score = max(w for _, w in causal_children)
                    evidence["granger_causality"] = granger_score

            # 4. トレンドスコア
            trend_score = 0.0
            if self.trend:
                for ptn, metric, min_s in [
                    ("optical", "rx_power_dbm", -0.05),
                    ("memory_leak", "memory_usage_pct", 1.0),
                ]:
                    try:
                        tr = self.trend.analyze(
                            dev_id, ptn, metric, min_s, 24
                        )
                        if tr.detected:
                            trend_score = max(
                                trend_score, tr.slope_normalized
                            )
                    except Exception:
                        pass
                evidence["trend_degradation"] = trend_score

            # 5. GDN偏差スコア
            gdn_score = 0.0
            if self.gdn:
                try:
                    from .gdn import build_device_features
                    gdn_f, gdn_n = build_device_features(
                        device_id=dev_id, alarm_count=0
                    )
                    gdn_result = self.gdn.predict(dev_id, gdn_f, gdn_n)
                    gdn_score = gdn_result.overall_score
                    evidence["gdn_deviation"] = gdn_score
                except Exception:
                    pass

            # 総合スコア算出
            total_score = (
                self.WEIGHT_CHILD_RATIO * child_ratio
                + self.WEIGHT_GRANGER * granger_score
                + self.WEIGHT_IMPLICIT * implicit_score
                + self.WEIGHT_TREND * trend_score
                + self.WEIGHT_GDN * gdn_score
            )

            # 閾値チェック: 最低でも配下の30%がアラーム中
            if child_ratio < 0.3 and total_score < 0.3:
                continue

            # 伝搬パス追跡
            prop_paths = self.propagation_tracer.trace_from_root(
                dev_id, alarmed_devices
            )

            # 推奨アクション
            recommendation = self._generate_recommendation(
                dev_id, total_score, child_ratio, evidence
            )

            candidates.append(SilentFailureCandidate(
                device_id=dev_id,
                score=round(min(1.0, total_score), 4),
                affected_children=alarmed_children,
                affected_ratio=round(child_ratio, 3),
                evidence=evidence,
                propagation_paths=prop_paths,
                implicit_signals=signals,
                recommendation=recommendation,
            ))

        # スコア降順でソート
        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates

    def _generate_recommendation(
        self,
        device_id: str,
        score: float,
        ratio: float,
        evidence: Dict[str, float],
    ) -> str:
        """推奨アクションを生成する。"""
        if score >= 0.7:
            return (
                f"{device_id} のサイレント障害の可能性が高い (スコア: {score:.0%})。"
                "即座に接続確認・リモートアクセスの可否を確認してください。"
            )
        elif score >= 0.4:
            parts = []
            if evidence.get("granger_causality", 0) > 0.3:
                parts.append("因果的裏付けあり")
            if evidence.get("trend_degradation", 0) > 0.3:
                parts.append("メトリクス劣化傾向")
            if evidence.get("gdn_deviation", 0) > 0.5:
                parts.append("ベースライン逸脱")
            detail = "、".join(parts) if parts else "配下デバイスへの影響"
            return (
                f"{device_id} の監視強化を推奨 ({detail})。"
                "SNMP/ICMP での死活確認を実施してください。"
            )
        else:
            return f"{device_id} は低リスク。定期監視を継続。"


# ---------------------------------------------------------------------------
# GrayScopeMonitor: 統合インターフェース
# ---------------------------------------------------------------------------
class GrayScopeMonitor:
    """GrayScope型メトリクス因果監視の統合インターフェース。

    使い方:
        monitor = GrayScopeMonitor(
            storage, topology, children_map,
            trend_analyzer, granger_analyzer, gdn_predictor
        )
        result = monitor.analyze(msg_map, alarmed_devices)
    """

    def __init__(
        self,
        storage,
        topology: Dict,
        children_map: Dict[str, List[str]],
        trend_analyzer=None,
        granger_analyzer=None,
        gdn_predictor=None,
    ):
        self.storage = storage
        self.topology = topology
        self.children_map = children_map

        self.correlator = MetricCrossCorrelator(storage)
        self.scorer = SilentFailureScorer(
            topology, children_map,
            trend_analyzer, granger_analyzer, gdn_predictor,
        )
        self.tracer = MultiHopPropagationTracer(
            topology, children_map, granger_analyzer
        )

    def analyze(
        self,
        msg_map: Dict[str, List[str]],
        alarmed_devices: Optional[Set[str]] = None,
    ) -> GrayScopeResult:
        """GrayScope分析を実行する。

        Args:
            msg_map: {device_id: [alarm_messages]}
            alarmed_devices: アラームがあるデバイスのセット

        Returns:
            GrayScopeResult
        """
        if alarmed_devices is None:
            alarmed_devices = set(msg_map.keys())

        result = GrayScopeResult(
            total_devices_analyzed=len(self.topology)
        )

        # 1. サイレント障害スコアリング
        result.silent_candidates = self.scorer.score_candidates(
            msg_map, alarmed_devices
        )

        # 2. メトリクス相互相関（アラームデバイスの隣接ペア）
        alarm_list = list(alarmed_devices)
        if len(alarm_list) >= 2:
            result.metric_correlations = self.correlator.find_correlated_pairs(
                alarm_list, self.children_map
            )

        # 3. 暗黙的異常の集計
        result.implicit_anomalies_found = sum(
            1 for c in result.silent_candidates if c.score >= 0.4
        )

        # サマリ生成
        n_cands = len(result.silent_candidates)
        n_high = sum(1 for c in result.silent_candidates if c.score >= 0.6)
        n_corrs = len([c for c in result.metric_correlations if c.significant])

        parts = []
        if n_high > 0:
            parts.append(f"高リスクサイレント障害: {n_high}件")
        if n_cands > n_high:
            parts.append(f"監視強化推奨: {n_cands - n_high}件")
        if n_corrs > 0:
            parts.append(f"メトリクス相関: {n_corrs}ペア")
        result.summary = " | ".join(parts) if parts else "サイレント障害なし"

        return result
