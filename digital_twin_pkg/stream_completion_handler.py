# digital_twin_pkg/stream_completion_handler.py
# ストリーム完了時のDB連携ハンドラ
#
# ストリーム劣化シミュレーション完了時に:
#   1. ChromaDB にインシデント類似検索データを蓄積
#   2. GNN学習データをエクスポート（JSON）
#
# これにより forecast_ledger (SQLite) + ChromaDB + GNN学習データ
# の3系統がすべてストリームデータで同期される。

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from digital_twin_pkg.alarm_stream import AlarmStreamSimulator, StreamEvent
from digital_twin_pkg.stream_data_exporter import StreamDataExporter

logger = logging.getLogger(__name__)

_SESSION_KEY = "stream_completion_exported"


def handle_stream_completion(
    sim: AlarmStreamSimulator,
    engine: Any,
    topology: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    ストリーム完了時にChromaDB蓄積 + GNN学習データエクスポートを実行。

    Args:
        sim: 完了したAlarmStreamSimulator
        engine: DigitalTwinEngine インスタンス（vector_store アクセス用）
        topology: ネットワークトポロジー辞書（GNNエッジ構築用）

    Returns:
        処理結果サマリー dict
    """
    result = {
        "chromadb_added": 0,
        "gnn_session_path": None,
        "errors": [],
    }

    events = sim.get_all_events()
    if not events:
        return result

    # --- 1. ChromaDB にインシデントデータを蓄積 ---
    result["chromadb_added"] = _register_events_to_chromadb(
        events=events,
        sim=sim,
        engine=engine,
        result=result,
    )

    # --- 2. GNN学習データをエクスポート ---
    result["gnn_session_path"] = _export_gnn_training_data(
        events=events,
        sim=sim,
        topology=topology,
        result=result,
    )

    logger.info(
        "Stream completion: ChromaDB +%d incidents, GNN session=%s",
        result["chromadb_added"],
        result["gnn_session_path"],
    )
    return result


def _register_events_to_chromadb(
    events: List[StreamEvent],
    sim: AlarmStreamSimulator,
    engine: Any,
    result: Dict[str, Any],
) -> int:
    """ストリームイベントをChromaDBに登録"""
    added = 0
    vs = getattr(engine, "vector_store", None)
    if vs is None or not getattr(vs, "is_ready", False):
        result["errors"].append("ChromaDB not available")
        return 0

    for ev in events:
        try:
            primary_msg = ev.messages[0] if ev.messages else ""
            if not primary_msg:
                continue

            incident_id = f"stream_{sim.device_id}_{ev.level}_{int(ev.elapsed_sec * 1000)}"
            vs.add_incident(
                alarm_text=primary_msg,
                device_id=sim.device_id,
                rule_pattern=sim.sequence.pattern,
                confidence=ev.level / 5.0,
                vendor_context=f"stream_level={ev.level},severity={ev.severity}",
                anomaly_type="trend",
                outcome="pending" if ev.level < 5 else "confirmed_incident",
                incident_id=incident_id,
                created_at=time.time() - (sim.total_duration_sec - ev.elapsed_sec),
            )
            added += 1
        except Exception as e:
            logger.debug("ChromaDB add_incident error: %s", e)

    return added


def _export_gnn_training_data(
    events: List[StreamEvent],
    sim: AlarmStreamSimulator,
    topology: Optional[Dict[str, Any]],
    result: Dict[str, Any],
) -> Optional[str]:
    """GNN学習データをJSONファイルにエクスポート"""
    try:
        exporter = StreamDataExporter()
        device_type = ""
        device_layer = 0

        if topology and sim.device_id in topology:
            dev_info = topology[sim.device_id]
            if isinstance(dev_info, dict):
                device_type = dev_info.get("type", "")
                device_layer = dev_info.get("layer", 0)
            else:
                device_type = getattr(dev_info, "type", "")
                device_layer = getattr(dev_info, "layer", 0)

        session = exporter.create_session(
            scenario_key=sim.sequence.pattern,
            target_device=sim.device_id,
            start_level=sim.start_level,
            source_type="mock",
            events=events,
            topology=topology,
            device_type=device_type,
            device_layer=device_layer,
        )
        filepath = exporter.save_session(session)
        return filepath

    except Exception as e:
        logger.warning("GNN training data export failed: %s", e)
        result["errors"].append(f"GNN export: {e}")
        return None
