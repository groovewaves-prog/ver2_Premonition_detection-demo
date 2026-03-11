# digital_twin_pkg/granger.py  ―  Phase 2: Granger因果テスト + 因果グラフ
"""
デバイス間のアラーム時系列に対して Granger 因果テストを実施し、
因果関係の重み (p-value, F-statistic) を因果グラフに蓄積する。

目的:
  - 静的トポロジー (parent_id) に加え、動的な因果関係をデータドリブンで推定
  - RCA の root_cause / symptom 分類精度を向上
  - 予兆検知の信頼度を因果的裏付けで補強

Granger因果テスト概要:
  帰無仮説 H₀: source の過去値は target の予測に寄与しない
  対立仮説 H₁: source の過去値が target の予測精度を改善する

  Model 1 (Restricted):  target(t) = Σ α_i × target(t-i)           + ε
  Model 2 (Unrestricted): target(t) = Σ α_i × target(t-i) + Σ β_j × source(t-j) + ε

  F = ((RSS_r - RSS_u) / p) / (RSS_u / (n - 2p - 1))
  p-value < α → source Granger-causes target
"""
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# データ構造
# ---------------------------------------------------------------------------
@dataclass
class CausalityResult:
    """Granger因果テストの結果。"""
    source_device: str
    target_device: str
    f_statistic: float = 0.0
    p_value: float = 1.0         # 低いほど因果関係が強い
    lag_hours: float = 0.0       # 最適ラグ (時間)
    direction: str = "none"      # "source→target" | "bidirectional" | "none"
    strength: float = 0.0        # 因果強度 0.0-1.0 (1-p_value をベースに正規化)
    data_points: int = 0
    significant: bool = False    # p_value < significance_level
    topology_consistent: bool = False  # トポロジーと整合するか


@dataclass
class CausalEdge:
    """因果グラフのエッジ。"""
    source: str
    target: str
    weight: float               # 因果強度 (0.0-1.0)
    p_value: float
    lag_hours: float
    last_updated: float         # Unix timestamp
    test_count: int = 1         # テスト回数 (信頼性指標)
    topology_consistent: bool = False


# ---------------------------------------------------------------------------
# Granger 因果テスト (純 numpy 実装)
# ---------------------------------------------------------------------------
def granger_f_test(
    source_series: np.ndarray,
    target_series: np.ndarray,
    max_lag: int = 5,
    significance_level: float = 0.05,
) -> CausalityResult:
    """2つの時系列に対して Granger 因果 F 検定を実行する。

    Args:
        source_series: 原因候補の時系列 (等間隔サンプリング前提)
        target_series: 結果候補の時系列
        max_lag: テストする最大ラグ数
        significance_level: 有意水準

    Returns:
        CausalityResult
    """
    n = len(target_series)
    if n < max_lag * 3 + 5:
        return CausalityResult(
            source_device="", target_device="",
            data_points=n, p_value=1.0
        )

    best_result = None
    best_f = -1.0

    for lag in range(1, max_lag + 1):
        if n - lag < lag * 2 + 2:
            continue

        # Design matrices
        Y = target_series[lag:]
        n_obs = len(Y)

        # Restricted model: target の自己回帰のみ
        X_r = np.column_stack([
            target_series[lag - i - 1: n - i - 1] for i in range(lag)
        ])
        X_r = np.column_stack([np.ones(n_obs), X_r])

        # Unrestricted model: target の自己回帰 + source のラグ
        X_u = np.column_stack([
            X_r,
            *[source_series[lag - i - 1: n - i - 1].reshape(-1, 1) for i in range(lag)]
        ])

        try:
            # OLS: β = (X'X)^(-1) X'Y
            beta_r = np.linalg.lstsq(X_r, Y, rcond=None)[0]
            beta_u = np.linalg.lstsq(X_u, Y, rcond=None)[0]

            resid_r = Y - X_r @ beta_r
            resid_u = Y - X_u @ beta_u

            rss_r = float(np.sum(resid_r ** 2))
            rss_u = float(np.sum(resid_u ** 2))

            if rss_u < 1e-15:
                continue

            p = lag  # 追加パラメータ数
            df_denom = n_obs - 2 * lag - 1
            if df_denom <= 0:
                continue

            f_stat = ((rss_r - rss_u) / p) / (rss_u / df_denom)
            if f_stat < 0:
                f_stat = 0.0

            # p-value: F分布の生存関数 (scipy不使用の近似)
            p_value = _f_survival_approx(f_stat, p, df_denom)

            if f_stat > best_f:
                best_f = f_stat
                best_result = CausalityResult(
                    source_device="",
                    target_device="",
                    f_statistic=round(f_stat, 4),
                    p_value=round(p_value, 6),
                    lag_hours=lag,
                    strength=round(max(0.0, min(1.0, 1.0 - p_value)), 4),
                    data_points=n_obs,
                    significant=p_value < significance_level,
                )
        except (np.linalg.LinAlgError, ValueError):
            continue

    if best_result is None:
        return CausalityResult(
            source_device="", target_device="",
            data_points=n, p_value=1.0
        )
    return best_result


def _f_survival_approx(f_stat: float, df1: int, df2: int) -> float:
    """F分布の生存関数 P(F > f_stat) の近似 (scipy不使用)。

    Abramowitz & Stegun の正規近似を使用。
    """
    if f_stat <= 0 or df1 <= 0 or df2 <= 0:
        return 1.0

    # 正規近似 (Paulson's approximation)
    a = 2.0 / (9.0 * df1)
    b = 2.0 / (9.0 * df2)
    ratio = f_stat ** (1.0 / 3.0)

    z = ((1.0 - b) * ratio - (1.0 - a)) / np.sqrt(a + b * ratio ** 2)

    # 標準正規分布の生存関数 (erfc 近似)
    p_value = 0.5 * _erfc(z / np.sqrt(2.0))
    return float(max(0.0, min(1.0, p_value)))


def _erfc(x: float) -> float:
    """相補誤差関数の近似 (Horner法)。"""
    # Abramowitz & Stegun 7.1.26
    t = 1.0 / (1.0 + 0.3275911 * abs(x))
    poly = t * (0.254829592 + t * (-0.284496736 + t * (
        1.421413741 + t * (-1.453152027 + t * 1.061405429))))
    result = poly * np.exp(-x * x)
    return result if x >= 0 else 2.0 - result


# ---------------------------------------------------------------------------
# アラームイベント → 等間隔時系列への変換
# ---------------------------------------------------------------------------
def alarm_events_to_time_series(
    events: List[Tuple[float, float]],
    bin_minutes: int = 30,
    window_hours: int = 24,
    reference_time: Optional[float] = None,
) -> Tuple[np.ndarray, np.ndarray]:
    """アラームイベント列を等間隔のビン化時系列に変換する。

    Args:
        events: [(timestamp, severity_score), ...] タイムスタンプ昇順
        bin_minutes: ビン幅 (分)
        window_hours: 分析ウィンドウ (時間)
        reference_time: 基準時刻 (省略時は最新イベントの時刻)

    Returns:
        (time_bins, value_bins): 等間隔の時系列
    """
    if not events:
        return np.array([]), np.array([])

    ref_t = reference_time or max(e[0] for e in events)
    start_t = ref_t - window_hours * 3600
    bin_sec = bin_minutes * 60
    n_bins = int(window_hours * 3600 / bin_sec)

    time_bins = np.array([start_t + i * bin_sec for i in range(n_bins)])
    value_bins = np.zeros(n_bins)

    for ts, val in events:
        if ts < start_t or ts > ref_t:
            continue
        idx = int((ts - start_t) / bin_sec)
        idx = min(idx, n_bins - 1)
        value_bins[idx] = max(value_bins[idx], val)  # ビン内の最大値を採用

    return time_bins, value_bins


# ---------------------------------------------------------------------------
# GrangerCausalityAnalyzer: ストレージ連携 + 因果グラフ管理
# ---------------------------------------------------------------------------
class GrangerCausalityAnalyzer:
    """デバイス間の Granger 因果分析を統括するクラス。

    使い方:
        analyzer = GrangerCausalityAnalyzer(storage, topology, children_map)
        analyzer.record_alarm_event(device_id, timestamp, severity_score)
        results = analyzer.run_pairwise_tests(device_ids)
        weight = analyzer.get_causality_weight(source_id, target_id)
    """

    def __init__(
        self,
        storage,
        topology: Dict,
        children_map: Dict[str, List[str]],
        significance_level: float = 0.05,
        bin_minutes: int = 30,
        window_hours: int = 24,
        max_lag: int = 5,
    ):
        self.storage = storage
        self.topology = topology
        self.children_map = children_map
        self.significance_level = significance_level
        self.bin_minutes = bin_minutes
        self.window_hours = window_hours
        self.max_lag = max_lag

        # 因果グラフ: (source, target) → CausalEdge
        self._causal_graph: Dict[Tuple[str, str], CausalEdge] = {}

        # メモリ内イベントバッファ (SQLite テーブルが無い場合のフォールバック)
        self._event_buffer: Dict[str, List[Tuple[float, float]]] = {}

        # キャッシュ TTL
        self._cache_ttl = 300.0  # 5分

    # ----- イベント記録 -----
    def record_alarm_event(
        self,
        device_id: str,
        timestamp: float,
        severity_score: float = 1.0,
    ):
        """アラームイベントを記録する。

        severity_score: 0.0-1.0 (WARNING=0.5, CRITICAL=1.0)
        """
        # メモリバッファに蓄積
        if device_id not in self._event_buffer:
            self._event_buffer[device_id] = []
        self._event_buffer[device_id].append((timestamp, severity_score))

        # DB にも蓄積 (alarm_events テーブル)
        try:
            self.storage.db_insert_alarm_event(device_id, timestamp, severity_score)
        except (AttributeError, Exception):
            pass  # テーブルが未作成の場合はメモリのみ

    # ----- イベント取得 -----
    def _get_device_events(
        self, device_id: str, window_hours: Optional[int] = None
    ) -> List[Tuple[float, float]]:
        """デバイスのアラームイベントを取得する。"""
        wh = window_hours or self.window_hours
        cutoff = time.time() - wh * 3600

        # DB 優先
        try:
            db_events = self.storage.db_fetch_alarm_events(device_id, cutoff)
            if db_events:
                return db_events
        except (AttributeError, Exception):
            pass

        # メモリフォールバック
        events = self._event_buffer.get(device_id, [])
        return [(ts, val) for ts, val in events if ts >= cutoff]

    # ----- ペアワイズ因果テスト -----
    def run_pairwise_tests(
        self,
        device_ids: List[str],
        topology_aware: bool = True,
    ) -> List[CausalityResult]:
        """指定デバイス群のペアワイズ Granger 因果テストを実行する。

        topology_aware=True: トポロジー上の隣接デバイスのみテスト (O(E) vs O(V²))
        """
        results = []
        now = time.time()

        # テスト対象ペアの生成
        pairs = set()
        if topology_aware:
            # トポロジー隣接ペアのみ (parent↔child)
            for dev_id in device_ids:
                children = self.children_map.get(dev_id, [])
                for child in children:
                    if child in device_ids:
                        pairs.add((dev_id, child))
                        pairs.add((child, dev_id))  # 逆方向もテスト
        else:
            # 全ペア (N×(N-1) — 小規模時のみ推奨)
            for i, src in enumerate(device_ids):
                for tgt in device_ids[i + 1:]:
                    pairs.add((src, tgt))
                    pairs.add((tgt, src))

        for src, tgt in pairs:
            # キャッシュ確認
            cached = self._causal_graph.get((src, tgt))
            if cached and (now - cached.last_updated) < self._cache_ttl:
                results.append(CausalityResult(
                    source_device=src, target_device=tgt,
                    f_statistic=cached.weight * 10,
                    p_value=cached.p_value,
                    lag_hours=cached.lag_hours,
                    strength=cached.weight,
                    significant=cached.p_value < self.significance_level,
                    topology_consistent=cached.topology_consistent,
                ))
                continue

            # イベント取得 → 時系列変換
            src_events = self._get_device_events(src)
            tgt_events = self._get_device_events(tgt)

            if len(src_events) < 5 or len(tgt_events) < 5:
                continue

            _, src_ts = alarm_events_to_time_series(
                src_events, self.bin_minutes, self.window_hours
            )
            _, tgt_ts = alarm_events_to_time_series(
                tgt_events, self.bin_minutes, self.window_hours
            )

            if len(src_ts) < self.max_lag * 3 + 5:
                continue

            result = granger_f_test(
                src_ts, tgt_ts,
                max_lag=self.max_lag,
                significance_level=self.significance_level,
            )
            result.source_device = src
            result.target_device = tgt

            # トポロジー整合性チェック
            is_topo_consistent = self._check_topology_consistency(src, tgt)
            result.topology_consistent = is_topo_consistent

            if result.significant:
                result.direction = f"{src}→{tgt}"

            # 因果グラフ更新
            self._update_causal_graph(result, now)

            # DB に保存
            try:
                self.storage.db_insert_causality(
                    src, tgt, result.strength, result.p_value,
                    result.lag_hours, now, is_topo_consistent
                )
            except (AttributeError, Exception):
                pass

            results.append(result)

        return results

    def _check_topology_consistency(self, source: str, target: str) -> bool:
        """因果関係がトポロジーと整合するかチェックする。

        source が target の祖先（parent chain 上）にあれば整合。
        """
        # source → target: source が parent chain 上にあるか
        current = target
        visited = set()
        for _ in range(10):  # 最大10ホップ
            if current in visited:
                break
            visited.add(current)
            node = self.topology.get(current)
            if not node:
                break
            parent = (
                node.get("parent_id")
                if isinstance(node, dict)
                else getattr(node, "parent_id", None)
            )
            if parent == source:
                return True
            if not parent:
                break
            current = parent

        # 逆方向もチェック: target → source
        current = source
        visited = set()
        for _ in range(10):
            if current in visited:
                break
            visited.add(current)
            node = self.topology.get(current)
            if not node:
                break
            parent = (
                node.get("parent_id")
                if isinstance(node, dict)
                else getattr(node, "parent_id", None)
            )
            if parent == target:
                return True
            if not parent:
                break
            current = parent

        return False

    def _update_causal_graph(self, result: CausalityResult, now: float):
        """因果グラフを更新する (EWMA 平滑化)。"""
        key = (result.source_device, result.target_device)
        existing = self._causal_graph.get(key)

        if existing:
            # EWMA で重みを更新 (新しい結果に 0.3 の重み)
            alpha = 0.3
            new_weight = alpha * result.strength + (1 - alpha) * existing.weight
            existing.weight = round(new_weight, 4)
            existing.p_value = round(
                alpha * result.p_value + (1 - alpha) * existing.p_value, 6
            )
            existing.lag_hours = result.lag_hours
            existing.last_updated = now
            existing.test_count += 1
            existing.topology_consistent = result.topology_consistent
        else:
            self._causal_graph[key] = CausalEdge(
                source=result.source_device,
                target=result.target_device,
                weight=result.strength,
                p_value=result.p_value,
                lag_hours=result.lag_hours,
                last_updated=now,
                test_count=1,
                topology_consistent=result.topology_consistent,
            )

    # ----- 因果重み取得 (RCA / 予測で利用) -----
    def get_causality_weight(
        self, source: str, target: str, default: float = 0.0
    ) -> float:
        """source → target の因果重みを取得する。"""
        edge = self._causal_graph.get((source, target))
        if edge:
            return edge.weight
        return default

    def get_causal_parents(
        self, device_id: str, min_weight: float = 0.3
    ) -> List[Tuple[str, float]]:
        """device_id の因果的な親デバイスを取得する。

        Returns:
            [(parent_device_id, causality_weight), ...] weight降順
        """
        parents = []
        for (src, tgt), edge in self._causal_graph.items():
            if tgt == device_id and edge.weight >= min_weight:
                parents.append((src, edge.weight))
        parents.sort(key=lambda x: x[1], reverse=True)
        return parents

    def get_causal_children(
        self, device_id: str, min_weight: float = 0.3
    ) -> List[Tuple[str, float]]:
        """device_id が因果的に影響を与える子デバイスを取得する。"""
        children = []
        for (src, tgt), edge in self._causal_graph.items():
            if src == device_id and edge.weight >= min_weight:
                children.append((tgt, edge.weight))
        children.sort(key=lambda x: x[1], reverse=True)
        return children

    def get_graph_summary(self) -> Dict:
        """因果グラフの要約を返す。"""
        sig_edges = [
            e for e in self._causal_graph.values()
            if e.p_value < self.significance_level
        ]
        topo_consistent = sum(1 for e in sig_edges if e.topology_consistent)

        return {
            "total_edges": len(self._causal_graph),
            "significant_edges": len(sig_edges),
            "topology_consistent": topo_consistent,
            "avg_weight": (
                round(np.mean([e.weight for e in sig_edges]), 3)
                if sig_edges else 0.0
            ),
            "strongest_pairs": [
                {
                    "source": e.source,
                    "target": e.target,
                    "weight": e.weight,
                    "p_value": e.p_value,
                    "lag_hours": e.lag_hours,
                }
                for e in sorted(sig_edges, key=lambda x: x.weight, reverse=True)[:5]
            ],
        }

    def compute_causality_boost(
        self, device_id: str, direction: str = "incoming"
    ) -> float:
        """デバイスの因果的ブーストを計算する。

        direction="incoming": 他デバイスから device_id への因果的影響の合計
        direction="outgoing": device_id から他デバイスへの影響の合計

        Returns:
            0.0-0.20 のブースト値
        """
        if direction == "incoming":
            parents = self.get_causal_parents(device_id, min_weight=0.2)
            if not parents:
                return 0.0
            # 最大3つの因果親の重みの加重平均
            weights = [w for _, w in parents[:3]]
            avg_weight = np.mean(weights)
        else:
            children = self.get_causal_children(device_id, min_weight=0.2)
            if not children:
                return 0.0
            weights = [w for _, w in children[:3]]
            avg_weight = np.mean(weights)

        # 最大 0.10 (10%) のブースト
        return round(min(0.10, avg_weight * 0.12), 3)
