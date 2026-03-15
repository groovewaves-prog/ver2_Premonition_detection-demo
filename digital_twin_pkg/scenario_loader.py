# digital_twin_pkg/scenario_loader.py
# シナリオ定義のYAMLローダー
#
# YAML外部ファイルから劣化シナリオを読み込み、
# DegradationSequence / DegradationStage オブジェクトに変換する。
# YAMLが見つからない場合はハードコードされたフォールバック定義を使用。

from __future__ import annotations

import os
import logging
from typing import Dict, List, Optional

from digital_twin_pkg.alarm_stream import (
    DegradationSequence,
    DegradationStage,
    DEGRADATION_SEQUENCES as _HARDCODED_SEQUENCES,
)

logger = logging.getLogger(__name__)

_YAML_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config",
    "degradation_scenarios.yaml",
)


def _load_yaml_scenarios(path: str) -> Optional[Dict]:
    """YAMLファイルからシナリオ定義を読み込む"""
    try:
        import yaml
    except ImportError:
        logger.debug("PyYAML not installed; falling back to hardcoded definitions")
        return None

    if not os.path.exists(path):
        logger.debug("YAML file not found: %s", path)
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data.get("scenarios", {})
    except Exception as e:
        logger.warning("Failed to load YAML scenarios: %s", e)
        return None


def _yaml_to_sequence(key: str, cfg: dict) -> Optional[DegradationSequence]:
    """YAML辞書 → DegradationSequence"""
    if not cfg.get("enabled", True):
        return None

    stages_raw = cfg.get("stages", [])
    if not stages_raw:
        return None

    stages: List[DegradationStage] = []
    for s in stages_raw:
        stages.append(DegradationStage(
            level=s["level"],
            label=s["label"],
            duration_sec=s["duration_sec"],
            metric_value=s["metric_value"],
            alarm_templates=s.get("alarm_templates", []),
            severity=s.get("severity", "WARNING"),
            color=s.get("color", "#FFC107"),
        ))

    return DegradationSequence(
        pattern=key,
        category=cfg.get("category", ""),
        metric_name=cfg.get("metric_name", ""),
        metric_unit=cfg.get("metric_unit", ""),
        normal_value=cfg.get("normal_value", 0.0),
        failure_value=cfg.get("failure_value", 100.0),
        stages=stages,
    )


def load_all_scenarios(yaml_path: str = _YAML_PATH) -> Dict[str, DegradationSequence]:
    """
    有効なシナリオを全てロードして返す。

    優先順位:
      1. YAML ファイル（enabled: true のもの）
      2. ハードコード定義（YAML なし / パース失敗時のフォールバック）
    """
    yaml_data = _load_yaml_scenarios(yaml_path)

    if yaml_data is None:
        logger.info("Using hardcoded scenario definitions (fallback)")
        return dict(_HARDCODED_SEQUENCES)

    result: Dict[str, DegradationSequence] = {}
    for key, cfg in yaml_data.items():
        seq = _yaml_to_sequence(key, cfg)
        if seq is not None:
            result[key] = seq

    if not result:
        logger.warning("No enabled scenarios found in YAML; using hardcoded fallback")
        return dict(_HARDCODED_SEQUENCES)

    logger.info("Loaded %d scenarios from YAML: %s", len(result), list(result.keys()))
    return result


def get_scenario_display_names(yaml_path: str = _YAML_PATH) -> Dict[str, str]:
    """シナリオキー → 表示名のマッピングを返す（UI用）"""
    yaml_data = _load_yaml_scenarios(yaml_path)

    if yaml_data is None:
        from digital_twin_pkg.alarm_stream import get_available_scenarios
        return get_available_scenarios()

    result: Dict[str, str] = {}
    for key, cfg in yaml_data.items():
        if cfg.get("enabled", True) and cfg.get("stages"):
            result[key] = cfg.get("display_name", key)
    return result


def get_scenario_short_names(yaml_path: str = _YAML_PATH) -> Dict[str, str]:
    """シナリオキー → 短縮表示名（予兆シミュレーション用）"""
    yaml_data = _load_yaml_scenarios(yaml_path)

    if yaml_data is None:
        return {
            "optical": "Optical Decay (光減衰)",
            "microburst": "Microburst (パケット破棄)",
            "memory_leak": "Memory Leak (メモリリーク)",
            "crc_fcs_error": "CRC/FCS Error (物理回線劣化)",
            "latency_jitter": "Latency/Jitter (遅延・ジッター)",
        }

    result: Dict[str, str] = {}
    for key, cfg in yaml_data.items():
        if cfg.get("enabled", True) and cfg.get("stages"):
            result[key] = cfg.get("display_name_short", cfg.get("display_name", key))
    return result


def get_default_interfaces_for(device_id: str, scenario_key: str,
                                yaml_path: str = _YAML_PATH) -> List[str]:
    """デバイスとシナリオに応じたデフォルトインターフェース"""
    yaml_data = _load_yaml_scenarios(yaml_path)

    if yaml_data and scenario_key in yaml_data:
        return yaml_data[scenario_key].get("default_interfaces", ["Gi0/0/1"])

    # フォールバック
    from digital_twin_pkg.alarm_stream import get_default_interfaces
    return get_default_interfaces(device_id, scenario_key)


def get_all_scenario_metadata(yaml_path: str = _YAML_PATH) -> Dict[str, dict]:
    """全シナリオのメタデータを返す（Phase情報含む、GNN学習計画用）"""
    yaml_data = _load_yaml_scenarios(yaml_path)
    if yaml_data is None:
        return {
            k: {"phase": 1, "enabled": True, "category": v.category}
            for k, v in _HARDCODED_SEQUENCES.items()
        }

    result: Dict[str, dict] = {}
    for key, cfg in yaml_data.items():
        result[key] = {
            "phase": cfg.get("phase", 1),
            "enabled": cfg.get("enabled", True),
            "category": cfg.get("category", ""),
            "display_name": cfg.get("display_name", key),
            "has_stages": bool(cfg.get("stages")),
        }
    return result
