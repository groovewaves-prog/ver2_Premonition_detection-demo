# digital_twin_pkg/stream_data_exporter.py
# GNN学習データエクスポーター
#
# 連続劣化ストリームの実行結果を、GNN学習に適した形式で
# エクスポートする。モック・ライブ両方のデータソースに対応。
#
# 出力スキーマ:
#   - ノード特徴量: [device_type, layer, metric_value, alarm_count, level]
#   - エッジ: トポロジー隣接関係
#   - ラベル: 劣化レベル (1-5)
#   - タイムスタンプ: 時系列順序

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

from digital_twin_pkg.alarm_stream import StreamEvent

logger = logging.getLogger(__name__)


@dataclass
class GNNNodeSnapshot:
    """GNN学習用: 1時刻におけるノード（デバイス）の状態"""
    device_id: str
    device_type: str
    layer: int
    metric_value: float
    alarm_count: int
    degradation_level: int        # ラベル (1-5)
    severity: str
    timestamp_sec: float          # セッション開始からの経過秒


@dataclass
class GNNEdge:
    """GNN学習用: トポロジーエッジ"""
    source: str
    target: str
    edge_type: str = "physical"   # physical, logical, redundancy


@dataclass
class GNNTrainingSession:
    """
    1回の劣化ストリーム実行から生成される学習データセッション。

    これが1つの学習サンプル（グラフスナップショットの時系列）になる。
    """
    session_id: str
    scenario_key: str
    target_device: str
    start_level: int
    source_type: str              # "mock" or "live"
    created_at: str
    snapshots: List[GNNNodeSnapshot] = field(default_factory=list)
    edges: List[GNNEdge] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


class StreamDataExporter:
    """
    ストリームデータをGNN学習形式にエクスポートする。

    使い方:
        exporter = StreamDataExporter(output_dir="data/gnn_training")

        # ストリーム完了後にエクスポート
        session = exporter.create_session(
            scenario_key="optical",
            target_device="WAN_ROUTER_01",
            start_level=1,
            source_type="mock",
            events=stream_events,
            topology=topo_dict,
        )
        exporter.save_session(session)
    """

    def __init__(self, output_dir: str = "data/gnn_training"):
        self._output_dir = output_dir

    def create_session(
        self,
        scenario_key: str,
        target_device: str,
        start_level: int,
        source_type: str,
        events: List[StreamEvent],
        topology: Optional[Dict[str, Any]] = None,
        device_type: str = "",
        device_layer: int = 0,
    ) -> GNNTrainingSession:
        """ストリームイベント群から学習セッションを生成"""
        session_id = f"{scenario_key}_{target_device}_{int(time.time())}"

        session = GNNTrainingSession(
            session_id=session_id,
            scenario_key=scenario_key,
            target_device=target_device,
            start_level=start_level,
            source_type=source_type,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        )

        # イベント → ノードスナップショット変換
        for ev in events:
            snapshot = GNNNodeSnapshot(
                device_id=target_device,
                device_type=device_type,
                layer=device_layer,
                metric_value=ev.metric_value,
                alarm_count=len(ev.messages),
                degradation_level=ev.level,
                severity=ev.severity,
                timestamp_sec=ev.elapsed_sec,
            )
            session.snapshots.append(snapshot)

        # トポロジーからエッジ構築
        if topology:
            session.edges = self._extract_edges(topology)

        session.metadata = {
            "total_events": len(events),
            "duration_sec": events[-1].elapsed_sec if events else 0,
            "max_level": max((e.level for e in events), default=0),
        }

        return session

    def _extract_edges(self, topology: Dict[str, Any]) -> List[GNNEdge]:
        """トポロジー辞書からGNNエッジを抽出"""
        edges = []
        for dev_id, info in topology.items():
            if isinstance(info, dict):
                parent_id = info.get("parent_id")
            else:
                parent_id = getattr(info, "parent_id", None)

            if parent_id:
                edges.append(GNNEdge(
                    source=parent_id,
                    target=dev_id,
                    edge_type="physical",
                ))
        return edges

    def save_session(self, session: GNNTrainingSession) -> str:
        """セッションをJSONファイルに保存"""
        os.makedirs(self._output_dir, exist_ok=True)
        filename = f"{session.session_id}.json"
        filepath = os.path.join(self._output_dir, filename)

        data = {
            "session_id": session.session_id,
            "scenario_key": session.scenario_key,
            "target_device": session.target_device,
            "start_level": session.start_level,
            "source_type": session.source_type,
            "created_at": session.created_at,
            "metadata": session.metadata,
            "snapshots": [asdict(s) for s in session.snapshots],
            "edges": [asdict(e) for e in session.edges],
        }

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        logger.info("GNN training session saved: %s (%d snapshots)",
                     filepath, len(session.snapshots))
        return filepath

    def load_session(self, filepath: str) -> GNNTrainingSession:
        """JSONファイルからセッションを読み込み"""
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)

        session = GNNTrainingSession(
            session_id=data["session_id"],
            scenario_key=data["scenario_key"],
            target_device=data["target_device"],
            start_level=data.get("start_level", 1),
            source_type=data.get("source_type", "mock"),
            created_at=data.get("created_at", ""),
            metadata=data.get("metadata", {}),
        )

        for s in data.get("snapshots", []):
            session.snapshots.append(GNNNodeSnapshot(**s))

        for e in data.get("edges", []):
            session.edges.append(GNNEdge(**e))

        return session

    def list_sessions(self) -> List[str]:
        """保存済みセッション一覧を返す"""
        if not os.path.exists(self._output_dir):
            return []
        return [
            f for f in os.listdir(self._output_dir)
            if f.endswith(".json")
        ]
