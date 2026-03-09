# digital_twin_pkg/alarm_stream.py
# アラームストリームシミュレーター: 連続的な劣化シーケンスを時間軸で生成
#
# 目的:
#   - ワンショットアラームではなく、時間経過に伴う劣化進行をシミュレート
#   - RULトレンド予測 (linear regression) が実データで機能するようにする
#   - GNN学習データの自然蓄積基盤を提供
#
# 設計:
#   各 EscalationRule に対応する「劣化シーケンス定義」を保持し、
#   タイムステップごとにリアルな段階的アラームを生成する

from __future__ import annotations
import time
import math
import random
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 劣化ステージ定義
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class DegradationStage:
    """劣化の1ステージ（level 1-5 に対応）"""
    level: int
    label: str                        # 日本語ラベル
    duration_sec: float               # このステージの持続時間（秒）
    metric_value: float               # 代表メトリクス値
    alarm_templates: List[str]        # アラームメッセージテンプレート
    severity: str = "WARNING"         # WARNING or CRITICAL
    color: str = "#FFC107"            # 表示色


@dataclass
class DegradationSequence:
    """1つの劣化シナリオの全ステージ"""
    pattern: str                      # EscalationRule.pattern に対応
    category: str
    metric_name: str                  # 表示用メトリクス名
    metric_unit: str                  # dBm, %, drops/s 等
    normal_value: float               # 正常値
    failure_value: float              # 障害値
    stages: List[DegradationStage] = field(default_factory=list)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 定義済み劣化シーケンス
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# シナリオごとの base TTF (Time To Failure) — rules.py の early_warning_hours と対応
SCENARIO_BASE_TTF_HOURS: Dict[str, int] = {
    "optical": 336,      # 14日
    "microburst": 24,    # 1日
    "memory_leak": 336,  # 14日
}

# Level → RUL 減衰係数 (engine.py の _DETERMINISTIC_DECAY と同一)
_DETERMINISTIC_DECAY: Dict[int, float] = {
    1: 1.00,
    2: 0.50,
    3: 0.21,
    4: 0.07,
    5: 0.015,
}

DEGRADATION_SEQUENCES: Dict[str, DegradationSequence] = {
    "optical": DegradationSequence(
        pattern="optical",
        category="Hardware/Optical",
        metric_name="Rx Power",
        metric_unit="dBm",
        normal_value=-8.0,
        failure_value=-25.0,
        stages=[
            DegradationStage(1, "初期劣化", 10.0, -18.5, [
                "%TRANSCEIVER-4-THRESHOLD_VIOLATION: Rx Power {value:.1f} dBm on {intf} (optical signal degrading). transceiver rx power below threshold.",
            ], "WARNING", "#FFC107"),
            DegradationStage(2, "劣化進行", 8.0, -20.2, [
                "%TRANSCEIVER-4-THRESHOLD_VIOLATION: Rx Power {value:.1f} dBm on {intf} (optical signal degrading). transceiver rx power below threshold.",
                "%OPTICAL-3-SIGNAL_WARN: optical signal level degrading on {intf}. light level {value:.1f} dBm. transceiver rx power loss detected.",
            ], "WARNING", "#FF9800"),
            DegradationStage(3, "警戒域", 6.0, -22.0, [
                "%TRANSCEIVER-3-THRESHOLD_VIOLATION: Rx Power {value:.1f} dBm on {intf} (CRITICAL). transceiver rx power below threshold.",
                "%OPTICAL-3-SIGNAL_WARN: optical signal level degrading on {intf}. light level {alt_value:.1f} dBm. transceiver rx power loss detected.",
                "%LINK-3-UPDOWN: Interface {intf}, changed state to down (intermittent)",
            ], "WARNING", "#FF5722"),
            DegradationStage(4, "危険域", 5.0, -23.5, [
                "%TRANSCEIVER-2-THRESHOLD_VIOLATION: Rx Power {value:.1f} dBm on {intf} (CRITICAL). transceiver rx power critical low.",
                "%OPTICAL-2-SIGNAL_FAIL: optical signal LOSS on {intf}. light level {value:.1f} dBm.",
                "%LINK-3-UPDOWN: Interface {intf}, changed state to down",
                "%LINEPROTO-5-UPDOWN: Line protocol on Interface {intf}, changed state to down",
            ], "CRITICAL", "#D32F2F"),
            DegradationStage(5, "障害直前", 3.0, -25.0, [
                "%TRANSCEIVER-1-THRESHOLD_VIOLATION: Rx Power {value:.1f} dBm on {intf} (EMERGENCY). transceiver FAILURE imminent.",
                "%OPTICAL-1-SIGNAL_FAIL: optical signal LOSS on {intf}. NO LIGHT DETECTED.",
                "%LINK-2-UPDOWN: Interface {intf}, changed state to down (PERMANENT)",
                "%LINEPROTO-2-UPDOWN: Line protocol on Interface {intf}, changed state to down",
                "%SYS-1-LINK_FAIL: Multiple interface failures detected. Cascade risk HIGH.",
            ], "CRITICAL", "#B71C1C"),
        ]
    ),

    "microburst": DegradationSequence(
        pattern="microburst",
        category="Network/QoS",
        metric_name="Queue Drops",
        metric_unit="drops/s",
        normal_value=0.0,
        failure_value=5000.0,
        stages=[
            DegradationStage(1, "初期兆候", 8.0, 200.0, [
                "%HARDWARE-3-ASIC_ERROR: asic_error queue drops detected on {intf} (Count: {value:.0f}). output drops on burst traffic.",
            ], "WARNING", "#FFC107"),
            DegradationStage(2, "バースト増加", 7.0, 600.0, [
                "%HARDWARE-3-ASIC_ERROR: asic_error queue drops detected on {intf} (Count: {value:.0f}). output drops on burst traffic.",
                "%QOS-4-BUFFER: buffer overflow risk on {intf}. queue drops {value:.0f}/sec. output drops increasing.",
            ], "WARNING", "#FF9800"),
            DegradationStage(3, "バッファ圧迫", 5.0, 1500.0, [
                "%HARDWARE-2-ASIC_ERROR: asic_error queue drops CRITICAL on {intf} (Count: {value:.0f}). buffer near exhaustion.",
                "%QOS-3-POLICER: Traffic policing activated on {intf}. drops {value:.0f}/sec.",
                "%PLATFORM-3-ELEMENT_WARNING: High CPU usage during burst processing.",
            ], "WARNING", "#FF5722"),
            DegradationStage(4, "バッファ枯渇", 4.0, 3000.0, [
                "%HARDWARE-1-ASIC_ERROR: asic_error CRITICAL on {intf}. Buffer EXHAUSTED (Count: {value:.0f}).",
                "%QOS-2-BUFFER_EXHAUSTED: ALL buffers consumed on {intf}. Packet loss imminent.",
                "%PLATFORM-2-ELEMENT_CRITICAL: System stability at risk. Memory pressure HIGH.",
                "%SYS-3-CPUHOG: CPU utilization exceeded threshold during burst.",
            ], "CRITICAL", "#D32F2F"),
            DegradationStage(5, "サービス断", 3.0, 5000.0, [
                "%HARDWARE-0-ASIC_FAIL: ASIC failure on {intf}. ALL packets dropped.",
                "%QOS-1-BUFFER_FAIL: Buffer allocation failure. Service DISRUPTED.",
                "%LINK-2-UPDOWN: Interface {intf}, changed state to down (buffer exhaustion)",
                "%SYS-1-SERVICE_DISRUPTED: Service disruption detected. Multiple interfaces affected.",
                "%PLATFORM-1-CRASH_IMMINENT: System crash risk. Emergency action required.",
            ], "CRITICAL", "#B71C1C"),
        ]
    ),

    "memory_leak": DegradationSequence(
        pattern="memory_leak",
        category="Software/Resource",
        metric_name="Memory Usage",
        metric_unit="%",
        normal_value=45.0,
        failure_value=98.0,
        stages=[
            DegradationStage(1, "使用率上昇", 12.0, 72.0, [
                "%SYS-4-MEMORY_WARN: High memory usage detected. Processor Pool Free: {free}M. Potential memory leak.",
            ], "WARNING", "#FFC107"),
            DegradationStage(2, "リーク加速", 10.0, 80.0, [
                "%SYS-4-MEMORY_WARN: High memory usage detected. Processor Pool Free: {free}M. Potential memory leak.",
                "%PLATFORM-3-ELEMENT_WARNING: Used Memory value {value:.0f}% exceeds warning threshold. System instability risk.",
            ], "WARNING", "#FF9800"),
            DegradationStage(3, "高使用率", 8.0, 88.0, [
                "%SYS-3-MEMORY_CRITICAL: Memory usage {value:.0f}%. Free: {free}M. Process instability likely.",
                "%PLATFORM-2-ELEMENT_CRITICAL: Used Memory value {value:.0f}% exceeds critical threshold.",
                "%SYS-3-CPUHOG: CPU utilization high due to memory pressure. GC cycles increasing.",
            ], "WARNING", "#FF5722"),
            DegradationStage(4, "枯渇域", 6.0, 94.0, [
                "%SYS-2-MALLOCFAIL: Memory allocation of 65536 bytes failed. Pool: Processor, Free: {free}M.",
                "%SYS-2-MEMORY_CRITICAL: Memory usage {value:.0f}%. System UNSTABLE.",
                "%PLATFORM-1-ELEMENT_CRITICAL: Used Memory value {value:.0f}%. CRASH imminent.",
                "%SYS-2-WATCHDOG: Process restart triggered by memory exhaustion.",
            ], "CRITICAL", "#D32F2F"),
            DegradationStage(5, "クラッシュ直前", 4.0, 98.0, [
                "%SYS-1-MALLOCFAIL: CRITICAL memory allocation failure. Pool: Processor EMPTY.",
                "%SYS-0-MEMORY_EXHAUSTED: ALL memory pools exhausted. System CRASH imminent.",
                "%PLATFORM-0-CRASH: System crash due to memory exhaustion. Reboot required.",
                "%SYS-1-SERVICE_DISRUPTED: Multiple services failed. Emergency reload needed.",
                "%SYS-0-FATAL: Unrecoverable memory state. Automatic reload in 60 seconds.",
            ], "CRITICAL", "#B71C1C"),
        ]
    ),
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ストリームシミュレーター
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class StreamEvent:
    """ストリーム上の1イベント"""
    timestamp: float
    elapsed_sec: float
    level: int
    stage_label: str
    severity: str
    metric_value: float
    messages: List[str]
    color: str
    progress_pct: float           # 0-100: 全体の進行度


class AlarmStreamSimulator:
    """
    連続的な劣化アラームシーケンスを生成するシミュレーター。

    使い方:
        sim = AlarmStreamSimulator("optical", "WAN_ROUTER_01", ["Gi0/0/1", "Te1/0/1"])
        sim.start()

        # タイマーまたはループで呼び出し
        events = sim.get_events_until_now()
        for ev in events:
            print(ev.level, ev.messages)
    """

    def __init__(
        self,
        scenario_key: str,
        device_id: str,
        interfaces: Optional[List[str]] = None,
        speed_multiplier: float = 1.0,
        start_level: int = 1,
    ):
        if scenario_key not in DEGRADATION_SEQUENCES:
            raise ValueError(f"Unknown scenario: {scenario_key}. Available: {list(DEGRADATION_SEQUENCES.keys())}")

        self.sequence = DEGRADATION_SEQUENCES[scenario_key]
        self.device_id = device_id
        self.interfaces = interfaces or ["Gi0/0/1"]
        self.speed_multiplier = speed_multiplier
        self.start_level = max(1, min(5, start_level))

        self._start_time: Optional[float] = None
        self._last_emitted_idx: int = -1
        self._all_events: List[StreamEvent] = []
        self._rng = random.Random(hash(f"{device_id}_{scenario_key}"))

        # 全イベントを事前計算
        self._precompute_events()

    def _precompute_events(self):
        """全ステージのイベントを事前計算（start_level 以降のみ）"""
        active_stages = [s for s in self.sequence.stages if s.level >= self.start_level]
        total_duration = sum(s.duration_sec / self.speed_multiplier for s in active_stages)
        cumulative_time = 0.0
        # 単調劣化を保証: ジッタを劣化方向にバイアス
        degrading = self.sequence.failure_value < self.sequence.normal_value
        prev_metric = None
        # 障害値を絶対限界としてクランプ
        fail_val = self.sequence.failure_value
        norm_val = self.sequence.normal_value

        for stage in active_stages:
            duration = stage.duration_sec / self.speed_multiplier
            # 各ステージ内で2-3回のアラーム発火タイミングを生成
            num_ticks = min(len(stage.alarm_templates), 3)
            tick_interval = duration / (num_ticks + 1)

            for tick in range(num_ticks):
                tick_time = cumulative_time + tick_interval * (tick + 1)
                progress = (tick_time / total_duration) * 100.0

                # メトリクス値にジッタを追加（劣化方向バイアス付き）
                # abs(jitter) で常に正値を取り、劣化方向に適用
                jitter_magnitude = abs(self._rng.gauss(0, abs(stage.metric_value) * 0.015))
                if degrading:
                    metric_val = stage.metric_value - jitter_magnitude
                else:
                    metric_val = stage.metric_value + jitter_magnitude
                # 前回値より劣化方向に進んでいることを保証
                if prev_metric is not None:
                    if degrading:
                        metric_val = min(metric_val, prev_metric - 0.05)
                    else:
                        metric_val = max(metric_val, prev_metric + 0.05)
                # failure_value を超えないようクランプ
                if degrading:
                    metric_val = max(metric_val, fail_val)
                else:
                    metric_val = min(metric_val, fail_val)
                prev_metric = metric_val

                # インターフェース選択
                intf = self.interfaces[tick % len(self.interfaces)]

                # メッセージ生成（テンプレート展開）
                num_msgs = min(tick + 1, len(stage.alarm_templates))
                messages = []
                for i in range(num_msgs):
                    tmpl = stage.alarm_templates[i]
                    free_mem = max(64, 8192 - int(metric_val * 80))
                    msg = tmpl.format(
                        value=metric_val,
                        alt_value=metric_val + 1.5,
                        intf=intf,
                        free=free_mem,
                    )
                    messages.append(msg)

                event = StreamEvent(
                    timestamp=tick_time,
                    elapsed_sec=tick_time,
                    level=stage.level,
                    stage_label=stage.label,
                    severity=stage.severity,
                    metric_value=metric_val,
                    messages=messages,
                    color=stage.color,
                    progress_pct=min(progress, 100.0),
                )
                self._all_events.append(event)

            cumulative_time += duration

        self._total_duration = total_duration

    @property
    def total_duration_sec(self) -> float:
        return self._total_duration

    @property
    def is_started(self) -> bool:
        return self._start_time is not None

    @property
    def is_complete(self) -> bool:
        if self._start_time is None:
            return False
        return (time.time() - self._start_time) >= self._total_duration

    @property
    def current_elapsed_sec(self) -> float:
        if self._start_time is None:
            return 0.0
        return time.time() - self._start_time

    @property
    def current_progress_pct(self) -> float:
        if self._start_time is None:
            return 0.0
        elapsed = time.time() - self._start_time
        return min((elapsed / self._total_duration) * 100.0, 100.0)

    def start(self):
        """シミュレーション開始"""
        self._start_time = time.time()
        self._last_emitted_idx = -1
        logger.info(
            f"AlarmStream started: {self.sequence.pattern} on {self.device_id} "
            f"(duration={self._total_duration:.1f}s, events={len(self._all_events)})"
        )

    def get_new_events(self) -> List[StreamEvent]:
        """前回呼び出し以降に発生した新規イベントを取得"""
        if self._start_time is None:
            return []

        elapsed = time.time() - self._start_time
        new_events = []

        for idx, event in enumerate(self._all_events):
            if idx <= self._last_emitted_idx:
                continue
            if event.timestamp <= elapsed:
                new_events.append(event)
                self._last_emitted_idx = idx
            else:
                break

        return new_events

    def get_all_events_until_now(self) -> List[StreamEvent]:
        """開始時刻から現在までの全イベントを取得"""
        if self._start_time is None:
            return []

        elapsed = time.time() - self._start_time
        return [e for e in self._all_events if e.timestamp <= elapsed]

    def get_all_events(self) -> List[StreamEvent]:
        """全イベント（未来分含む）を取得（プレビュー用）"""
        return list(self._all_events)

    def get_current_stage(self) -> Optional[DegradationStage]:
        """現在のステージを取得"""
        events = self.get_all_events_until_now()
        if not events:
            return None
        return self.sequence.stages[events[-1].level - 1]

    def get_current_level(self) -> int:
        """現在のレベル (0=未開始)"""
        events = self.get_all_events_until_now()
        if not events:
            return 0
        return events[-1].level

    def get_metric_history(self) -> List[Tuple[float, float]]:
        """(elapsed_sec, metric_value) のリストを返す（チャート用）"""
        events = self.get_all_events_until_now()
        # 開始レベルに応じた初期値を先頭に追加
        if self.start_level > 1:
            # 開始レベルの1つ前のステージのメトリクス値を初期値にする
            prev_stage = self.sequence.stages[self.start_level - 2]
            initial_value = prev_stage.metric_value
        else:
            initial_value = self.sequence.normal_value
        history = [(0.0, initial_value)]
        # failure_value をハードリミットとして適用
        fail_val = self.sequence.failure_value
        norm_val = self.sequence.normal_value
        degrading = fail_val < norm_val
        for ev in events:
            v = ev.metric_value
            if degrading:
                v = max(v, fail_val)
            else:
                v = min(v, fail_val)
            history.append((ev.elapsed_sec, v))
        return history

    def get_realtime_metric_history(self) -> Tuple[List[Tuple[float, float]], float, float]:
        """(real_hours, metric_value) のリストと (x_start_hours, x_end_hours) を返す。

        シミュレーション経過秒を、RUL 減衰モデルに基づく実時間（時間単位）に変換する。
        各ステージの実時間上の幅は _DETERMINISTIC_DECAY から算出:
          Level N の到達時刻 = base_ttf * (1 - decay[N])

        戻り値:
          (history, x_start_hours, x_end_hours)
          - history: [(real_hours, metric_value), ...]
          - x_start_hours: X軸表示開始 (start_level の到達時刻)
          - x_end_hours: X軸表示終了 (障害発生時刻 = base_ttf)
        """
        base_ttf = SCENARIO_BASE_TTF_HOURS.get(self.sequence.pattern, 336)

        # 各レベルが実時間上で到達される時刻 (時間)
        # level_start_hours[level] = base_ttf * (1 - decay[level])
        level_real_start: Dict[int, float] = {}
        for lvl in range(1, 6):
            decay = _DETERMINISTIC_DECAY.get(lvl, 0.50)
            level_real_start[lvl] = base_ttf * (1.0 - decay)
        # 障害発生 = base_ttf

        # アクティブステージの情報を構築
        active_stages = [s for s in self.sequence.stages if s.level >= self.start_level]

        # シミュレーション上での各ステージの累積開始秒
        sim_stage_starts: Dict[int, float] = {}
        cum = 0.0
        for s in active_stages:
            sim_stage_starts[s.level] = cum
            cum += s.duration_sec / self.speed_multiplier

        # 実時間上の表示範囲
        x_start_hours = level_real_start.get(self.start_level, 0.0)
        x_end_hours = base_ttf  # 障害発生時刻

        # シミュレーション秒 → 実時間(時間) への変換
        def sim_to_real(elapsed_sec: float) -> float:
            """シミュレーション秒を実時間(時間)に変換"""
            # どのステージに属するか特定
            target_stage = active_stages[0]
            for i, s in enumerate(active_stages):
                stage_start = sim_stage_starts[s.level]
                stage_end = stage_start + s.duration_sec / self.speed_multiplier
                if elapsed_sec <= stage_end + 0.001:
                    target_stage = s
                    break

            stage_sim_start = sim_stage_starts[target_stage.level]
            stage_sim_dur = target_stage.duration_sec / self.speed_multiplier

            # ステージ内での割合
            if stage_sim_dur > 0:
                frac = (elapsed_sec - stage_sim_start) / stage_sim_dur
            else:
                frac = 1.0
            frac = max(0.0, min(1.0, frac))

            # このステージの実時間範囲
            real_stage_start = level_real_start.get(target_stage.level, 0.0)
            if target_stage.level < 5:
                real_stage_end = level_real_start.get(target_stage.level + 1, base_ttf)
            else:
                real_stage_end = base_ttf
            return real_stage_start + frac * (real_stage_end - real_stage_start)

        # メトリクス履歴を実時間に変換
        sim_history = self.get_metric_history()
        real_history: List[Tuple[float, float]] = []

        # L5 のメトリクスを補間するための準備:
        # L5 の metric_value は failure_value と同一のため、対数チャート上で
        # 障害線 (base_ttf) より手前にメトリクスが障害値に達して見える。
        # → L5 区間内のメトリクスを、直前レベルの値から failure_value へ
        #   位置に応じて線形補間し、障害線位置でちょうど到達するようにする。
        last_stage = active_stages[-1] if active_stages else None
        l5_real_start = level_real_start.get(5, base_ttf)
        # L5 直前のメトリクス値（L4 の metric_value、または直前データポイントの値）
        pre_l5_metric = None
        if last_stage and last_stage.level == 5 and len(active_stages) >= 2:
            pre_l5_metric = active_stages[-2].metric_value
        elif last_stage and last_stage.level == 5 and self.start_level == 5:
            # L5 開始の場合、初期値を使用
            if self.start_level > 1:
                pre_l5_metric = self.sequence.stages[self.start_level - 2].metric_value
            else:
                pre_l5_metric = self.sequence.normal_value

        for elapsed_sec, metric_val in sim_history:
            real_h = sim_to_real(elapsed_sec)

            # L5 区間のメトリクス補間
            if (pre_l5_metric is not None
                    and last_stage is not None
                    and last_stage.level == 5
                    and real_h >= l5_real_start - 0.01):
                l5_span = base_ttf - l5_real_start
                if l5_span > 0:
                    frac_in_l5 = min(1.0, (real_h - l5_real_start) / l5_span)
                    metric_val = pre_l5_metric + frac_in_l5 * (
                        self.sequence.failure_value - pre_l5_metric
                    )

            real_history.append((real_h, metric_val))

        # シミュレーション完了時: 障害線位置に最終点を追加
        if self.is_complete:
            real_history.append((base_ttf, self.sequence.failure_value))

        return real_history, x_start_hours, x_end_hours

    def get_latest_messages(self) -> List[str]:
        """最新イベントのメッセージを返す"""
        events = self.get_all_events_until_now()
        if not events:
            return []
        return events[-1].messages

    def to_state_dict(self) -> Dict[str, Any]:
        """Streamlit session_state 保存用"""
        return {
            "scenario_key": self.sequence.pattern,
            "device_id": self.device_id,
            "interfaces": self.interfaces,
            "speed_multiplier": self.speed_multiplier,
            "start_level": self.start_level,
            "start_time": self._start_time,
            "last_emitted_idx": self._last_emitted_idx,
        }

    @classmethod
    def from_state_dict(cls, state: Dict[str, Any]) -> "AlarmStreamSimulator":
        """session_state から復元"""
        sim = cls(
            scenario_key=state["scenario_key"],
            device_id=state["device_id"],
            interfaces=state.get("interfaces", ["Gi0/0/1"]),
            speed_multiplier=state.get("speed_multiplier", 1.0),
            start_level=state.get("start_level", 1),
        )
        sim._start_time = state.get("start_time")
        sim._last_emitted_idx = state.get("last_emitted_idx", -1)
        return sim


def get_available_scenarios() -> Dict[str, str]:
    """利用可能なシナリオの {key: 表示名} を返す"""
    return {
        "optical": "Optical Decay (光減衰進行)",
        "microburst": "Microburst (パケット破棄増加)",
        "memory_leak": "Memory Leak (メモリ枯渇進行)",
    }


def get_default_interfaces(device_id: str, scenario_key: str) -> List[str]:
    """デバイスとシナリオに応じたデフォルトインターフェース"""
    if scenario_key == "optical":
        return ["Gi0/0/1", "Gi0/0/2", "Te1/0/1"]
    elif scenario_key == "microburst":
        return ["Gi0/1/0", "Gi0/1/1", "Gi0/1/2"]
    else:
        return ["System"]
