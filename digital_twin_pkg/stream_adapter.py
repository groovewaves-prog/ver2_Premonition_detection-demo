# digital_twin_pkg/stream_adapter.py
# ストリームアダプター: モック / 実運用の切り替え基盤
#
# 設計意図:
#   - モックモード: AlarmStreamSimulator による合成データ（デモ・GNN学習用）
#   - ライブモード: SNMP Trap / Syslog / API からの実データ（実運用）
#   - 同一インターフェースで切り替え可能にし、上位（UI・GNN学習）は
#     データソースを意識せずに利用可能

from __future__ import annotations

import abc
import time
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Any

from digital_twin_pkg.alarm_stream import (
    AlarmStreamSimulator,
    StreamEvent,
    DegradationSequence,
    DEGRADATION_SEQUENCES,
    get_default_interfaces,
)

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 抽象基底クラス
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class BaseStreamAdapter(abc.ABC):
    """
    ストリームデータソースの抽象基底クラス。

    モック（シミュレーション）と実運用（ライブ）の両方を
    同一インターフェースで扱えるようにする。
    """

    @abc.abstractmethod
    def start(self) -> None:
        """ストリームを開始する"""

    @abc.abstractmethod
    def stop(self) -> None:
        """ストリームを停止する"""

    @property
    @abc.abstractmethod
    def is_started(self) -> bool:
        """ストリームが開始されているか"""

    @property
    @abc.abstractmethod
    def is_complete(self) -> bool:
        """ストリームが完了しているか（ライブでは常にFalse）"""

    @abc.abstractmethod
    def get_current_level(self) -> int:
        """現在のレベル (0-5)"""

    @abc.abstractmethod
    def get_events_until_now(self) -> List[StreamEvent]:
        """現在までのイベント一覧"""

    @abc.abstractmethod
    def get_latest_messages(self) -> List[str]:
        """最新のアラームメッセージ"""

    @abc.abstractmethod
    def get_metric_history(self) -> List[tuple]:
        """(elapsed_sec, metric_value) のリスト"""

    @property
    @abc.abstractmethod
    def device_id(self) -> str:
        """対象デバイスID"""

    @property
    @abc.abstractmethod
    def scenario_key(self) -> str:
        """シナリオキー"""

    @property
    @abc.abstractmethod
    def source_type(self) -> str:
        """データソースの種別 ("mock" or "live")"""

    @abc.abstractmethod
    def to_state_dict(self) -> Dict[str, Any]:
        """シリアライズ（セッション保存用）"""

    @classmethod
    @abc.abstractmethod
    def from_state_dict(cls, state: Dict[str, Any]) -> "BaseStreamAdapter":
        """デシリアライズ"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# モックアダプター（既存 AlarmStreamSimulator のラッパー）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class MockStreamAdapter(BaseStreamAdapter):
    """
    AlarmStreamSimulator をラップするモックアダプター。

    デモ、テスト、GNN学習データ生成で使用。
    start_level パラメータにより、任意のレベルから開始可能。
    """

    def __init__(
        self,
        scenario_key: str,
        device_id: str,
        interfaces: Optional[List[str]] = None,
        speed_multiplier: float = 1.0,
        start_level: int = 1,
    ):
        self._scenario_key = scenario_key
        self._device_id = device_id
        self._interfaces = interfaces or get_default_interfaces(device_id, scenario_key)
        self._speed_multiplier = speed_multiplier
        self._start_level = max(1, min(5, start_level))

        # start_level に対応したシミュレーターを構築
        self._sim = AlarmStreamSimulator(
            scenario_key=scenario_key,
            device_id=device_id,
            interfaces=self._interfaces,
            speed_multiplier=speed_multiplier,
            start_level=self._start_level,
        )

    def start(self) -> None:
        self._sim.start()

    def stop(self) -> None:
        self._sim._start_time = None

    @property
    def is_started(self) -> bool:
        return self._sim.is_started

    @property
    def is_complete(self) -> bool:
        return self._sim.is_complete

    def get_current_level(self) -> int:
        return self._sim.get_current_level()

    def get_events_until_now(self) -> List[StreamEvent]:
        return self._sim.get_all_events_until_now()

    def get_latest_messages(self) -> List[str]:
        return self._sim.get_latest_messages()

    def get_metric_history(self) -> List[tuple]:
        return self._sim.get_metric_history()

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def scenario_key(self) -> str:
        return self._scenario_key

    @property
    def source_type(self) -> str:
        return "mock"

    @property
    def start_level(self) -> int:
        return self._start_level

    @property
    def sequence(self) -> DegradationSequence:
        return self._sim.sequence

    @property
    def total_duration_sec(self) -> float:
        return self._sim.total_duration_sec

    @property
    def current_elapsed_sec(self) -> float:
        return self._sim.current_elapsed_sec

    @property
    def current_progress_pct(self) -> float:
        return self._sim.current_progress_pct

    def get_all_events(self) -> List[StreamEvent]:
        return self._sim.get_all_events()

    def to_state_dict(self) -> Dict[str, Any]:
        state = self._sim.to_state_dict()
        state["adapter_type"] = "mock"
        state["start_level"] = self._start_level
        return state

    @classmethod
    def from_state_dict(cls, state: Dict[str, Any]) -> "MockStreamAdapter":
        adapter = cls(
            scenario_key=state["scenario_key"],
            device_id=state["device_id"],
            interfaces=state.get("interfaces", ["Gi0/0/1"]),
            speed_multiplier=state.get("speed_multiplier", 1.0),
            start_level=state.get("start_level", 1),
        )
        adapter._sim._start_time = state.get("start_time")
        adapter._sim._last_emitted_idx = state.get("last_emitted_idx", -1)
        return adapter


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ライブアダプター（実運用用スタブ）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class LiveStreamAdapter(BaseStreamAdapter):
    """
    実運用向けストリームアダプター。

    SNMP Trap、Syslog、またはモニタリングAPI からリアルタイムに
    アラームデータを受信する。

    NOTE: 現在はインターフェース定義のみ。
    実運用時に以下を実装する:
      - SNMP Trap レシーバー連携
      - Syslog パーサー連携
      - Prometheus / Zabbix API 連携
    """

    def __init__(
        self,
        device_id: str,
        scenario_key: str = "unknown",
        endpoint: str = "",
        auth: Optional[Dict[str, str]] = None,
    ):
        self._device_id = device_id
        self._scenario_key = scenario_key
        self._endpoint = endpoint
        self._auth = auth or {}
        self._started = False
        self._events: List[StreamEvent] = []
        self._start_time: Optional[float] = None

    def start(self) -> None:
        self._started = True
        self._start_time = time.time()
        logger.info("LiveStreamAdapter started for %s (endpoint: %s)",
                     self._device_id, self._endpoint)

    def stop(self) -> None:
        self._started = False
        logger.info("LiveStreamAdapter stopped for %s", self._device_id)

    @property
    def is_started(self) -> bool:
        return self._started

    @property
    def is_complete(self) -> bool:
        return False  # ライブは完了しない

    def get_current_level(self) -> int:
        if not self._events:
            return 0
        return self._events[-1].level

    def get_events_until_now(self) -> List[StreamEvent]:
        return list(self._events)

    def get_latest_messages(self) -> List[str]:
        if not self._events:
            return []
        return self._events[-1].messages

    def get_metric_history(self) -> List[tuple]:
        return [(ev.elapsed_sec, ev.metric_value) for ev in self._events]

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def scenario_key(self) -> str:
        return self._scenario_key

    @property
    def source_type(self) -> str:
        return "live"

    def ingest_event(self, event: StreamEvent) -> None:
        """外部ソースからイベントを取り込む（実運用時に使用）"""
        self._events.append(event)

    def to_state_dict(self) -> Dict[str, Any]:
        return {
            "adapter_type": "live",
            "device_id": self._device_id,
            "scenario_key": self._scenario_key,
            "endpoint": self._endpoint,
            "started": self._started,
            "start_time": self._start_time,
        }

    @classmethod
    def from_state_dict(cls, state: Dict[str, Any]) -> "LiveStreamAdapter":
        adapter = cls(
            device_id=state["device_id"],
            scenario_key=state.get("scenario_key", "unknown"),
            endpoint=state.get("endpoint", ""),
        )
        adapter._started = state.get("started", False)
        adapter._start_time = state.get("start_time")
        return adapter


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ファクトリ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def create_adapter_from_state(state: Dict[str, Any]) -> BaseStreamAdapter:
    """state_dict から適切なアダプターを復元するファクトリ"""
    adapter_type = state.get("adapter_type", "mock")
    if adapter_type == "live":
        return LiveStreamAdapter.from_state_dict(state)
    return MockStreamAdapter.from_state_dict(state)
