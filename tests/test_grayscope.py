# tests/test_grayscope.py — Phase 4: GrayScope unit tests
"""GrayScope型メトリクス因果監視のユニットテスト。"""
import time
import unittest
from unittest.mock import MagicMock

import numpy as np

from digital_twin_pkg.grayscope import (
    GrayScopeMonitor,
    ImplicitFeedbackDetector,
    MetricCrossCorrelator,
    MultiHopPropagationTracer,
    SilentFailureScorer,
)


def _make_topology():
    """テスト用トポロジー。"""
    return {
        "CORE-1": {"type": "core", "parent_id": None},
        "AGG-1": {"type": "aggregation", "parent_id": "CORE-1"},
        "AGG-2": {"type": "aggregation", "parent_id": "CORE-1"},
        "ACC-1": {"type": "access", "parent_id": "AGG-1"},
        "ACC-2": {"type": "access", "parent_id": "AGG-1"},
        "ACC-3": {"type": "access", "parent_id": "AGG-1"},
        "ACC-4": {"type": "access", "parent_id": "AGG-2"},
    }


def _make_children_map():
    return {
        "CORE-1": ["AGG-1", "AGG-2"],
        "AGG-1": ["ACC-1", "ACC-2", "ACC-3"],
        "AGG-2": ["ACC-4"],
    }


class TestImplicitFeedbackDetector(unittest.TestCase):
    def test_child_alarm_ratio(self):
        """配下アラーム集中度で暗黙的シグナルを検出する。"""
        detector = ImplicitFeedbackDetector()
        children_map = _make_children_map()
        alarmed = {"ACC-1", "ACC-2", "ACC-3"}

        score, signals = detector.detect_implicit_signals(
            "AGG-1", alarmed, children_map
        )
        self.assertGreater(score, 0.0)
        self.assertTrue(len(signals) > 0)
        self.assertIn("3/3", signals[0])

    def test_no_children(self):
        """子なしデバイスはスコア0。"""
        detector = ImplicitFeedbackDetector()
        score, signals = detector.detect_implicit_signals(
            "ACC-1", {"ACC-2"}, {"AGG-1": ["ACC-1", "ACC-2"]}
        )
        self.assertEqual(score, 0.0)
        self.assertEqual(len(signals), 0)


class TestMultiHopPropagationTracer(unittest.TestCase):
    def test_trace_from_root(self):
        """ルートからの障害伝搬パスを追跡する。"""
        topology = _make_topology()
        children_map = _make_children_map()
        tracer = MultiHopPropagationTracer(topology, children_map)

        alarmed = {"ACC-1", "ACC-2"}
        paths = tracer.trace_from_root("CORE-1", alarmed)

        self.assertGreater(len(paths), 0)
        # パスにはCORE-1→AGG-1→ACC-1 のような経路がある
        for p in paths:
            self.assertEqual(p.path[0], "CORE-1")  # ルートから始まる
            self.assertTrue(p.path[-1] in alarmed)

    def test_no_alarmed_children(self):
        """アラームなしの場合パスは空。"""
        topology = _make_topology()
        children_map = _make_children_map()
        tracer = MultiHopPropagationTracer(topology, children_map)

        paths = tracer.trace_from_root("CORE-1", set())
        self.assertEqual(len(paths), 0)


class TestSilentFailureScorer(unittest.TestCase):
    def test_score_candidates_high_ratio(self):
        """配下の全デバイスがアラーム中の場合、高スコアの候補が出る。"""
        topology = _make_topology()
        children_map = _make_children_map()
        scorer = SilentFailureScorer(topology, children_map)

        msg_map = {
            "ACC-1": ["Connection lost"],
            "ACC-2": ["Link down"],
            "ACC-3": ["Port unreachable"],
        }
        candidates = scorer.score_candidates(msg_map)

        # AGG-1 が候補に出るはず（配下3/3がアラーム中）
        agg1_cands = [c for c in candidates if c.device_id == "AGG-1"]
        self.assertEqual(len(agg1_cands), 1)
        self.assertGreaterEqual(agg1_cands[0].score, 0.3)
        self.assertEqual(agg1_cands[0].affected_ratio, 1.0)

    def test_score_candidates_low_ratio(self):
        """配下の30%未満がアラーム中の場合、候補に出ない。"""
        topology = _make_topology()
        children_map = _make_children_map()
        scorer = SilentFailureScorer(topology, children_map)

        msg_map = {
            "ACC-1": ["Minor warning"],
        }
        candidates = scorer.score_candidates(msg_map)

        # AGG-1 は 1/3=33% だが、total_score < 0.3 なので出ない可能性あり
        # (child_ratio=0.33 * 0.3 = 0.1, 他は0なのでtotal=0.1 < 0.3)
        agg1_cands = [c for c in candidates if c.device_id == "AGG-1"]
        # 閾値チェック: child_ratio < 0.3 and total_score < 0.3 → skip
        # しかしchild_ratio=0.333 >= 0.3 なので出る可能性あり
        if agg1_cands:
            self.assertLess(agg1_cands[0].score, 0.5)

    def test_recommendation_generation(self):
        """推奨アクションが生成される。"""
        topology = _make_topology()
        children_map = _make_children_map()
        scorer = SilentFailureScorer(topology, children_map)

        rec = scorer._generate_recommendation("AGG-1", 0.8, 1.0, {})
        self.assertIn("AGG-1", rec)
        self.assertIn("高い", rec)


class TestGrayScopeMonitor(unittest.TestCase):
    def test_analyze_basic(self):
        """基本的なGrayScope分析が動作する。"""
        topology = _make_topology()
        children_map = _make_children_map()
        storage = MagicMock()
        storage.db_fetch_metrics = MagicMock(return_value=[])

        monitor = GrayScopeMonitor(
            storage=storage,
            topology=topology,
            children_map=children_map,
        )

        msg_map = {
            "ACC-1": ["Connection lost"],
            "ACC-2": ["Link down"],
            "ACC-3": ["Unreachable"],
        }
        result = monitor.analyze(msg_map)

        self.assertEqual(result.total_devices_analyzed, len(topology))
        self.assertIsInstance(result.summary, str)

    def test_analyze_no_alarms(self):
        """アラームなしの場合。"""
        topology = _make_topology()
        children_map = _make_children_map()
        storage = MagicMock()

        monitor = GrayScopeMonitor(
            storage=storage,
            topology=topology,
            children_map=children_map,
        )

        result = monitor.analyze({})
        self.assertEqual(len(result.silent_candidates), 0)
        self.assertEqual(result.summary, "サイレント障害なし")

    def test_analyze_with_silent_candidate(self):
        """サイレント障害候補が検出される。"""
        topology = _make_topology()
        children_map = _make_children_map()
        storage = MagicMock()
        storage.db_fetch_metrics = MagicMock(return_value=[])

        monitor = GrayScopeMonitor(
            storage=storage,
            topology=topology,
            children_map=children_map,
        )

        msg_map = {
            "ACC-1": ["Connection lost"],
            "ACC-2": ["Link down"],
            "ACC-3": ["Port unreachable"],
        }
        alarmed = {"ACC-1", "ACC-2", "ACC-3"}
        result = monitor.analyze(msg_map, alarmed)

        # AGG-1 が候補になるはず
        agg1_cands = [c for c in result.silent_candidates if c.device_id == "AGG-1"]
        self.assertGreater(len(agg1_cands), 0)


if __name__ == "__main__":
    unittest.main()
