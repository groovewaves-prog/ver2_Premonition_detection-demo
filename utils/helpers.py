# utils/helpers.py  ―  ユーティリティ関数

import os
from typing import List
from utils.const import ImpactLevel, SCENARIO_IMPACT_MAP

# Alarm型: alarm_generator から直接 import
try:
    from alarm_generator import Alarm
except ImportError:
    # Fallback: alarm_generator がまだロードされていない場合
    class Alarm:
        def __init__(self, device_id, message, severity, is_root_cause=False):
            self.device_id = device_id
            self.message = message
            self.severity = severity
            self.is_root_cause = is_root_cause


def get_scenario_impact_level(scenario: str) -> int:
    """シナリオの影響度を取得"""
    for key, value in SCENARIO_IMPACT_MAP.items():
        if key in scenario:
            return value
    return ImpactLevel.DEGRADED_MID


def get_status_from_alarms(scenario: str, alarms: List) -> str:
    """アラームからステータスを判定"""
    if not alarms:
        return "正常"
    
    impact = get_scenario_impact_level(scenario)
    
    if impact >= ImpactLevel.COMPLETE_OUTAGE:
        return "停止"
    elif impact >= ImpactLevel.DEGRADED_HIGH:
        return "要対応"
    elif impact >= ImpactLevel.DEGRADED_MID:
        if any(a.severity == "CRITICAL" for a in alarms):
            return "要対応"
        return "注意"
    elif impact >= ImpactLevel.DOWNSTREAM:
        return "注意"
    else:
        return "正常"


def get_status_icon(status: str) -> str:
    """ステータスに対応するアイコンを取得"""
    return {
        "停止": "🔴",
        "要対応": "🟠",
        "注意": "🟡",
        "正常": "🟢"
    }.get(status, "⚪")


def load_config_by_id(device_id: str) -> str:
    """configsフォルダから設定ファイルを読み込む"""
    possible_paths = [f"configs/{device_id}.txt", f"{device_id}.txt"]
    for path in possible_paths:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception:
                pass
    return "Config file not found."
