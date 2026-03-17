"""Device Type Registry — configs/device_types.json の共有ローダー。

4ファイルのハードコードを統一する中央レジストリ:
  - ui/graph.py (visual)
  - ui/autonomous_diagnostic.py (diagnostics)
  - ui/components/traffic_monitor.py (label)
  - digital_twin_pkg/engine.py (id_patterns)
"""

import json
from pathlib import Path
from functools import lru_cache
from typing import Dict, List, Tuple

_REGISTRY_PATH = Path(__file__).parent / "device_types.json"


@lru_cache(maxsize=1)
def _load_registry() -> dict:
    with open(_REGISTRY_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def get_visual(device_type: str) -> dict:
    """vis.js 形状・色定義を返す。未登録タイプは _unknown にフォールバック。"""
    reg = _load_registry()
    entry = reg.get(device_type, reg.get("_unknown", {}))
    return entry.get("visual", reg["_unknown"]["visual"])


def get_label(device_type: str) -> str:
    """トラフィックモニター用の短縮ラベルを返す。"""
    reg = _load_registry()
    entry = reg.get(device_type, reg.get("_unknown", {}))
    return entry.get("label", device_type)


def get_diagnostics(device_type: str) -> List[Tuple[str, str]]:
    """診断コマンドリストを返す。"""
    reg = _load_registry()
    entry = reg.get(device_type, reg.get("_unknown", {}))
    return [tuple(d) for d in entry.get("diagnostics", [])]


def get_all_visuals() -> Dict[str, dict]:
    """全デバイスタイプの visual 定義を返す（graph.py 互換の辞書）。"""
    reg = _load_registry()
    result = {}
    for dtype, entry in reg.items():
        if "visual" in entry:
            result[dtype] = entry["visual"]
    return result


def get_all_labels() -> Dict[str, str]:
    """全デバイスタイプの label 定義を返す。"""
    reg = _load_registry()
    return {dtype: entry.get("label", dtype)
            for dtype, entry in reg.items()
            if not dtype.startswith("_")}


def get_all_diagnostics() -> Dict[str, List[Tuple[str, str]]]:
    """全デバイスタイプの diagnostics を返す。"""
    reg = _load_registry()
    return {dtype: [tuple(d) for d in entry.get("diagnostics", [])]
            for dtype, entry in reg.items()
            if not dtype.startswith("_") and entry.get("diagnostics")}


def detect_device_type(device_id: str) -> str:
    """デバイスIDからデバイスタイプを推定する。
    id_patterns を上から順に照合し、最初にマッチしたタイプを返す。
    CLOUD_GATEWAY の patterns (AWS_DX, AWS_TGW) は CLOUD_RESOURCE の
    patterns (AWS_) より先にチェックされるよう、具体度の高い順に評価する。
    """
    reg = _load_registry()
    did_upper = device_id.upper()

    # 具体度が高い(パターン文字列が長い)順にソートして評価
    candidates = []
    for dtype, entry in reg.items():
        if dtype.startswith("_"):
            continue
        for pattern in entry.get("id_patterns", []):
            if pattern in did_upper:
                candidates.append((len(pattern), dtype, entry))

    if candidates:
        # 最長マッチ優先
        candidates.sort(key=lambda x: -x[0])
        dtype = candidates[0][1]
        return reg[dtype].get("label", dtype)

    return "Network Device"
