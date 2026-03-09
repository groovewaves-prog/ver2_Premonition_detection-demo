# ui/shared_sim_config.py
# 予兆シミュレーション ⇔ 連続劣化ストリーム 共通設定管理
#
# 「対象デバイス」と「劣化シナリオ」を一元管理し、
# 両機能間の整合性を保証する。

import streamlit as st
from typing import List, Tuple, Optional
from registry import list_sites, get_display_name, load_topology, get_paths
from digital_twin_pkg.scenario_loader import (
    get_scenario_short_names,
    get_scenario_display_names,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# セッションステートキー（一元管理）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SIM_DEVICE_KEY = "sim_shared_device"
SIM_SCENARIO_KEY = "sim_shared_scenario"

# シナリオキー ↔ 表示名 マッピング（キャッシュ）
_SCENARIO_KEY_MAP: Optional[dict] = None
_SHORT_NAME_MAP: Optional[dict] = None


def _get_scenario_key_map() -> dict:
    """表示名 → シナリオキーのマッピング"""
    global _SCENARIO_KEY_MAP
    if _SCENARIO_KEY_MAP is None:
        short_names = get_scenario_short_names()
        _SCENARIO_KEY_MAP = {v: k for k, v in short_names.items()}
    return _SCENARIO_KEY_MAP


def _get_short_name_map() -> dict:
    """シナリオキー → 短縮表示名のマッピング"""
    global _SHORT_NAME_MAP
    if _SHORT_NAME_MAP is None:
        _SHORT_NAME_MAP = get_scenario_short_names()
    return _SHORT_NAME_MAP


def scenario_key_to_display(key: str) -> str:
    """シナリオキー → 表示名"""
    return _get_short_name_map().get(key, key)


def scenario_display_to_key(display: str) -> str:
    """表示名 → シナリオキー"""
    return _get_scenario_key_map().get(display, display)


def build_device_options(site_id: Optional[str] = None) -> List[Tuple[str, str]]:
    """トポロジーからデバイス選択肢を構築"""
    if site_id is None:
        active = st.session_state.get("active_site")
        site_id = active if active else (list_sites()[0] if list_sites() else None)

    device_options = []
    if not site_id:
        return [("WAN_ROUTER_01", "WAN_ROUTER_01")]

    try:
        paths = get_paths(site_id)
        topo = load_topology(paths.topology_path)
        if topo:
            child_count = {}
            for dev_id, info in topo.items():
                pid = info.get('parent_id') if isinstance(info, dict) else getattr(info, 'parent_id', None)
                if pid:
                    child_count[pid] = child_count.get(pid, 0) + 1

            for dev_id, info in topo.items():
                if child_count.get(dev_id, 0) > 0:
                    if isinstance(info, dict):
                        dtype = info.get('type', '')
                        layer = info.get('layer', 0)
                        rg = info.get('redundancy_group')
                    else:
                        dtype = getattr(info, 'type', '')
                        layer = getattr(info, 'layer', 0)
                        rg = getattr(info, 'redundancy_group', None)

                    n_children = child_count.get(dev_id, 0)
                    tag = "SPOF" if not rg else "HA"
                    device_options.append(
                        (dev_id, f"L{layer} {dev_id} ({dtype}) [{tag}, 配下{n_children}台]")
                    )

            device_options.sort(key=lambda x: x[1])
    except Exception:
        pass

    if not device_options:
        device_options = [("WAN_ROUTER_01", "WAN_ROUTER_01")]

    return device_options


def render_shared_config() -> Tuple[str, str]:
    """
    共通設定UI を描画し、(target_device, scenario_key) を返す。

    この関数が返す値は予兆シミュレーション・連続劣化ストリーム
    の両方で使用される。
    """
    st.markdown("#### 共通シミュレーション設定")
    st.caption("予兆シミュレーションと連続劣化ストリームで共通の設定です。")

    device_options = build_device_options()

    target_device = st.selectbox(
        "対象デバイス",
        [d[0] for d in device_options],
        format_func=lambda x: next(
            (d[1] for d in device_options if d[0] == x), x
        ),
        key=SIM_DEVICE_KEY,
    )

    short_names = _get_short_name_map()
    scenario_display_list = list(short_names.values())

    scenario_display = st.selectbox(
        "劣化シナリオ",
        scenario_display_list,
        key=SIM_SCENARIO_KEY,
    )

    # 表示名 → 内部キーに変換
    scenario_key = scenario_display_to_key(scenario_display)

    return target_device, scenario_key
