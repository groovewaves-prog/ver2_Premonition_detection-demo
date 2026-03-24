# ui/components/traffic_monitor.py — トラフィックモニタリングパネル
#
# PHM: トラフィック予測ティアでゲートされる。
# トポロジ JSON の interfaces を使い、
# 劣化シナリオ別のトラフィック影響予測を可視化する。
import random as _rng
import streamlit as st
import datetime as _dt
from typing import Optional, List, Dict

from digital_twin_pkg.common import (
    get_node_attr, get_metadata,
    get_downstream_devices,
    build_children_map,
)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 劣化シナリオ別トラフィック影響プロファイル
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# util_map: 劣化レベル(0-5) → 帯域利用率(%)
# secondary: 副次メトリクスの定義
#   name: メトリクス名, unit: 単位,
#   values: レベル(0-5)→値, color: グラフ色
TRAFFIC_IMPACT_PROFILES: Dict[str, dict] = {
    "optical": {
        "label": "光信号劣化",
        "description": "リンク帯域低下 → 利用率上昇",
        "util_map": {0: 35.0, 1: 55.0, 2: 70.0, 3: 85.0, 4: 93.0, 5: 99.0},
        "secondary": {
            "name": "Rx Power",
            "unit": "dBm",
            "values": {0: -8.0, 1: -18.5, 2: -20.2, 3: -22.0, 4: -23.5, 5: -25.0},
            "color": "#7B1FA2",
            "domain": [-30.0, 0.0],
        },
    },
    "microburst": {
        "label": "マイクロバースト",
        "description": "バッファ溢れ → 利用率は横ばい、ドロップ急増",
        "util_map": {0: 35.0, 1: 45.0, 2: 50.0, 3: 52.0, 4: 55.0, 5: 55.0},
        "secondary": {
            "name": "Queue Drops",
            "unit": "drops/s",
            "values": {0: 0.0, 1: 200.0, 2: 600.0, 3: 1500.0, 4: 3000.0, 5: 5000.0},
            "color": "#E65100",
            "domain": [0.0, 6000.0],
        },
    },
    "memory_leak": {
        "label": "メモリリーク",
        "description": "転送テーブル破損 → スループット不規則低下",
        "util_map": {0: 35.0, 1: 38.0, 2: 30.0, 3: 22.0, 4: 15.0, 5: 5.0},
        "secondary": {
            "name": "Memory Usage",
            "unit": "%",
            "values": {0: 45.0, 1: 72.0, 2: 80.0, 3: 88.0, 4: 94.0, 5: 98.0},
            "color": "#1565C0",
            "domain": [0.0, 100.0],
        },
    },
    "crc_fcs_error": {
        "label": "CRC/FCSエラー",
        "description": "フレーム破損 → 再送増加 → 実効帯域低下",
        "util_map": {0: 35.0, 1: 42.0, 2: 50.0, 3: 58.0, 4: 48.0, 5: 30.0},
        "secondary": {
            "name": "CRC Error Rate",
            "unit": "%",
            "values": {0: 0.0, 1: 0.3, 2: 1.0, 3: 2.5, 4: 5.0, 5: 8.0},
            "color": "#6A1B9A",
            "domain": [0.0, 10.0],
        },
    },
    "latency_jitter": {
        "label": "遅延/ジッター",
        "description": "RTT増大 → プロトコルタイムアウト → セッション断",
        "util_map": {0: 35.0, 1: 36.0, 2: 37.0, 3: 35.0, 4: 30.0, 5: 20.0},
        "secondary": {
            "name": "RTT",
            "unit": "ms",
            "values": {0: 2.0, 1: 15.0, 2: 50.0, 3: 150.0, 4: 300.0, 5: 500.0},
            "color": "#00695C",
            "domain": [0.0, 600.0],
        },
    },
}

# フォールバック（旧来の単調増加モデル）
_DEFAULT_UTIL_MAP = {0: 35.0, 1: 55.0, 2: 70.0, 3: 85.0, 4: 93.0, 5: 99.0}


def _interpolate_level(level_map: dict, effective_level: float) -> float:
    """レベル間を線形補間する共通ヘルパー。"""
    level_low = int(effective_level)
    level_high = min(level_low + 1, 5)
    frac = effective_level - level_low
    val_low = level_map.get(level_low, level_map.get(0, 0.0))
    val_high = level_map.get(level_high, val_low)
    return val_low + (val_high - val_low) * frac


def _classify_interface_direction(
    iface: dict, device_id: str, topology: Optional[dict],
) -> str:
    """インターフェースの方向（Uplink/Downlink）をトポロジーから推定する。"""
    if not topology:
        return "unknown"
    connected_to = iface.get('connected_to', '')
    if not connected_to or connected_to == 'WAN_UPLINK':
        return "uplink"

    # 自デバイスの parent_id を取得
    node = topology.get(device_id)
    if not node:
        return "unknown"
    parent_id = (node.get('parent_id') if isinstance(node, dict)
                 else getattr(node, 'parent_id', None))

    # 接続先がHA peer（同一冗長グループ）かチェック
    rg = (node.get('redundancy_group') if isinstance(node, dict)
          else getattr(node, 'redundancy_group', None))
    if rg:
        peer_node = topology.get(connected_to)
        if peer_node:
            peer_rg = (peer_node.get('redundancy_group') if isinstance(peer_node, dict)
                       else getattr(peer_node, 'redundancy_group', None))
            if peer_rg == rg:
                return "ha_peer"

    if connected_to == parent_id:
        return "uplink"

    # 接続先の parent_id がこのデバイスなら downlink
    target = topology.get(connected_to)
    if target:
        target_parent = (target.get('parent_id') if isinstance(target, dict)
                         else getattr(target, 'parent_id', None))
        if target_parent == device_id:
            return "downlink"

    return "uplink"  # デフォルトはuplink扱い


def _render_trend_chart(
    device_id: str,
    interfaces: list,
    base_util_map: dict,
    current_level: int,
    secondary: Optional[dict] = None,
    topology: Optional[dict] = None,
):
    """過去24時間の帯域利用率トレンドを Uplink/Downlink 方向別に描画する。

    方向ごとに独立したチャートを生成し、利用率のスケールを分けることで
    どのリンクがボトルネックかを方向別に把握可能にする。
    """
    import altair as alt
    import pandas as pd

    now = _dt.datetime.now()
    n_points = 48
    interval_min = 30
    timestamps = [now - _dt.timedelta(minutes=interval_min * (n_points - 1 - i))
                  for i in range(n_points)]

    # ── インターフェースを方向別に分類 ──
    _dir_groups: Dict[str, list] = {"uplink": [], "downlink": [], "ha_peer": []}
    for iface in interfaces:
        if not isinstance(iface, dict):
            continue
        direction = _classify_interface_direction(iface, device_id, topology)
        _dir_groups.get(direction, _dir_groups["uplink"]).append(iface)

    _dir_configs = {
        "uplink": {
            "title": "⬆ Uplink（上流方向）",
            "subtitle": "WAN・上位機器への接続 — 障害時に最初に飽和する傾向",
            "util_scale": 1.0,
            "colors": ["#1565C0", "#0277BD", "#00838F", "#2E7D32"],
        },
        "downlink": {
            "title": "⬇ Downlink（下流方向）",
            "subtitle": "配下機器への接続 — 上流障害の波及度を示す",
            "util_scale": 0.72,
            "colors": ["#C62828", "#D84315", "#E65100", "#BF360C"],
        },
        "ha_peer": {
            "title": "⇔ HA Peer（冗長リンク）",
            "subtitle": "冗長ペア間のハートビート・同期リンク",
            "util_scale": 0.25,
            "colors": ["#6A1B9A", "#4A148C"],
        },
    }

    # 閾値定義（全チャート共通）
    thresholds = pd.DataFrame([
        {"利用率 (%)": 60, "label": "混雑 (60%)"},
        {"利用率 (%)": 80, "label": "輻輳 (80%)"},
        {"利用率 (%)": 90, "label": "飽和 (90%)"},
    ])

    # ── 方向別サマリ（ヘッダー）──
    _dir_peaks: Dict[str, float] = {}
    _any_rendered = False
    sec_rows: list = []

    for dir_key in ["uplink", "downlink", "ha_peer"]:
        iface_list = _dir_groups[dir_key]
        if not iface_list:
            continue
        cfg = _dir_configs[dir_key]
        _any_rendered = True

        # ── 時系列データ生成 ──
        rows: list = []
        for idx, iface in enumerate(iface_list):
            iface_name = iface.get('name', '?')
            connected_to = iface.get('connected_to', '')
            bw_mbps = iface.get('bandwidth_mbps', 100)
            label = f"{iface_name} → {connected_to}"

            # インターフェースごとの固有オフセット（線の重なり防止）
            _iface_hash = hash(f"offset_{device_id}_{iface_name}")
            _offset = ((_iface_hash % 17) - 8) * 1.2  # ±~10%

            for ti, ts in enumerate(timestamps):
                progress = ti / max(n_points - 1, 1)
                effective_level = current_level * progress
                base_val = _interpolate_level(base_util_map, effective_level)

                # 方向スケール + インターフェース固有オフセット
                scaled_val = base_val * cfg["util_scale"] + _offset

                _ts_seed = hash(f"trend_{device_id}_{iface_name}_{ti}")
                _rng_t = _rng.Random(_ts_seed)
                jitter = _rng_t.uniform(-5.0, 5.0)
                util = max(1.0, min(99.9, scaled_val + jitter))

                rows.append({
                    "時刻": ts,
                    "利用率 (%)": round(util, 1),
                    "インターフェース": label,
                    "帯域": f"{bw_mbps} Mbps",
                })

                # 副次メトリクス（Uplink 先頭インターフェースのみ）
                if secondary and dir_key == "uplink" and idx == 0:
                    sec_val = _interpolate_level(secondary["values"], effective_level)
                    sec_jitter = (_rng_t.uniform(-0.03, 0.03) * abs(sec_val)
                                  if sec_val != 0 else 0)
                    sec_rows.append({
                        "時刻": ts,
                        "value": round(sec_val + sec_jitter, 2),
                    })

        if not rows:
            continue

        df = pd.DataFrame(rows)

        # ピーク値を記録（サマリ用）
        last_ts = df["時刻"].max()
        last_df = df[df["時刻"] == last_ts]
        peak_util = last_df["利用率 (%)"].max()
        _dir_peaks[dir_key] = peak_util

        # ── 色スケール ──
        iface_names = df["インターフェース"].unique().tolist()
        colors = [cfg["colors"][i % len(cfg["colors"])]
                  for i in range(len(iface_names))]

        # ── チャート構築 ──
        line = alt.Chart(df).mark_line(strokeWidth=2.5).encode(
            x=alt.X("時刻:T", title="時刻", axis=alt.Axis(format="%H:%M")),
            y=alt.Y("利用率 (%):Q",
                     scale=alt.Scale(domain=[0, 100]),
                     title="利用率 (%)"),
            color=alt.Color(
                "インターフェース:N",
                scale=alt.Scale(domain=iface_names, range=colors),
                title="インターフェース",
            ),
            tooltip=["時刻:T", "インターフェース:N", "利用率 (%):Q", "帯域:N"],
        )

        # 右端に現在値ラベル
        end_labels = alt.Chart(last_df).mark_text(
            align="left", dx=5, fontSize=11, fontWeight="bold",
        ).encode(
            x="時刻:T",
            y="利用率 (%):Q",
            text=alt.Text("利用率 (%):Q", format=".0f"),
            color=alt.Color(
                "インターフェース:N",
                scale=alt.Scale(domain=iface_names, range=colors),
                legend=None,
            ),
        )

        rules = alt.Chart(thresholds).mark_rule(
            strokeDash=[4, 4], opacity=0.4,
        ).encode(
            y="利用率 (%):Q",
            color=alt.value("#FF9800"),
        )

        rule_labels = alt.Chart(thresholds).mark_text(
            align="right", dx=-4, dy=-8, fontSize=10, color="#999",
        ).encode(
            y="利用率 (%):Q",
            text="label:N",
        )

        chart_height = 200 if dir_key != "ha_peer" else 150
        chart = (line + end_labels + rules + rule_labels).properties(
            height=chart_height,
            title=alt.Title(
                text=cfg["title"],
                subtitle=cfg["subtitle"],
                fontSize=14,
                subtitleFontSize=11,
                subtitleColor="#888",
            ),
        ).configure_legend(orient="bottom")

        st.altair_chart(chart, use_container_width=True)

        # ボトルネック警告（飽和時）
        if peak_util >= 80:
            worst_row = last_df.loc[last_df["利用率 (%)"].idxmax()]
            _status = "飽和" if peak_util >= 90 else "輻輳"
            _warn_color = "#D32F2F" if peak_util >= 90 else "#FF5722"
            st.markdown(
                f'<div style="font-size:12px;color:{_warn_color};font-weight:600;'
                f'margin:-8px 0 8px 4px;">'
                f'⚠ ボトルネック: {worst_row["インターフェース"]}'
                f' — {peak_util:.0f}% ({_status})</div>',
                unsafe_allow_html=True,
            )

    # ── フォールバック: 方向分類不可の場合 ──
    if not _any_rendered:
        _render_trend_chart_simple(
            device_id, interfaces, base_util_map, current_level,
            timestamps, n_points, thresholds, secondary,
        )
        return

    # ── 副次メトリクス（独立グラフ）──
    if secondary and sec_rows and current_level > 0:
        sec_df = pd.DataFrame(sec_rows)
        sec_name = secondary["name"]
        sec_unit = secondary["unit"]
        sec_color = secondary["color"]
        sec_domain = secondary.get("domain",
                                    [sec_df["value"].min(), sec_df["value"].max()])

        sec_line = alt.Chart(sec_df).mark_area(
            line={"color": sec_color, "strokeWidth": 2},
            color=alt.Gradient(
                gradient="linear",
                stops=[
                    alt.GradientStop(color=sec_color, offset=1),
                    alt.GradientStop(color=f"{sec_color}20", offset=0),
                ],
                x1=1, x2=1, y1=1, y2=0,
            ),
        ).encode(
            x=alt.X("時刻:T", title="時刻", axis=alt.Axis(format="%H:%M")),
            y=alt.Y("value:Q",
                     scale=alt.Scale(domain=sec_domain),
                     title=f"{sec_name} ({sec_unit})"),
            tooltip=[
                alt.Tooltip("時刻:T"),
                alt.Tooltip("value:Q", title=sec_name, format=".1f"),
            ],
        )

        sec_chart = sec_line.properties(
            height=140,
            title=alt.Title(
                text=f"{sec_name} ({sec_unit}) — 劣化シナリオ連動",
                fontSize=13,
                color="#555",
            ),
        )

        st.altair_chart(sec_chart, use_container_width=True)


def _render_trend_chart_simple(
    device_id: str,
    interfaces: list,
    base_util_map: dict,
    current_level: int,
    timestamps: list,
    n_points: int,
    thresholds,
    secondary: Optional[dict] = None,
):
    """方向分類不可時のフォールバック（単一チャート）。"""
    import altair as alt
    import pandas as pd

    rows = []
    for iface in interfaces:
        if not isinstance(iface, dict):
            continue
        iface_name = iface.get('name', '?')
        connected_to = iface.get('connected_to', '')
        label = f"{iface_name} → {connected_to}"
        _iface_hash = hash(f"offset_{device_id}_{iface_name}")
        _offset = ((_iface_hash % 17) - 8) * 1.2

        for ti, ts in enumerate(timestamps):
            progress = ti / max(n_points - 1, 1)
            effective_level = current_level * progress
            base_val = _interpolate_level(base_util_map, effective_level)
            _ts_seed = hash(f"trend_{device_id}_{iface_name}_{ti}")
            _rng_t = _rng.Random(_ts_seed)
            jitter = _rng_t.uniform(-5.0, 5.0)
            util = max(1.0, min(99.9, base_val + _offset + jitter))
            rows.append({
                "時刻": ts, "利用率 (%)": round(util, 1),
                "インターフェース": label,
            })

    if not rows:
        return
    df = pd.DataFrame(rows)

    line = alt.Chart(df).mark_line(strokeWidth=2).encode(
        x=alt.X("時刻:T", title="時刻", axis=alt.Axis(format="%H:%M")),
        y=alt.Y("利用率 (%):Q", scale=alt.Scale(domain=[0, 100])),
        color="インターフェース:N",
        tooltip=["時刻:T", "インターフェース:N", "利用率 (%):Q"],
    )
    rules = alt.Chart(thresholds).mark_rule(
        strokeDash=[4, 4], opacity=0.4,
    ).encode(y="利用率 (%):Q", color=alt.value("#FF9800"))

    chart = (line + rules).properties(height=280).configure_legend(orient="bottom")
    st.altair_chart(chart, use_container_width=True)


def render_traffic_monitor(
    topology: dict,
    target_device_id: Optional[str] = None,
    degradation_level: int = 0,
    scenario_key: str = "optical",
):
    """トラフィックモニタリングパネルを描画する。

    Args:
        topology: トポロジー辞書
        target_device_id: 選択中のデバイスID（なければ全デバイス概要）
        degradation_level: 劣化進行度 (0-5)、利用率シミュレーションに反映
        scenario_key: 劣化シナリオキー ("optical", "microburst", "memory_leak")
    """
    if not topology:
        st.info("トポロジーが読み込まれていません。")
        return

    # ---- プロファイル取得 ----
    profile = TRAFFIC_IMPACT_PROFILES.get(scenario_key)
    if profile:
        base_util_map = profile["util_map"]
        secondary = profile["secondary"]
        scenario_label = profile["label"]
        scenario_desc = profile["description"]
    else:
        base_util_map = _DEFAULT_UTIL_MAP
        secondary = None
        scenario_label = scenario_key
        scenario_desc = "帯域利用率が単調に上昇"

    # ---- 折りたたみ可能パネル ----
    _traffic_label = "📊 トラフィックモニタ"
    if degradation_level > 0:
        _traffic_label += f" — {scenario_label}: Level {degradation_level}"

    with st.expander(_traffic_label, expanded=degradation_level > 0):
        # ---- デバイスセレクター ----
        devices_with_interfaces = [
            dev_id for dev_id, node in topology.items()
            if get_node_attr(node, 'interfaces')
        ]
        if not devices_with_interfaces:
            st.info("トポロジーにインターフェース情報がありません。")
            return

        if degradation_level > 0:
            st.caption(f"劣化シナリオ: **{scenario_label}** — {scenario_desc}")

        if target_device_id and target_device_id in devices_with_interfaces:
            selected_device = target_device_id
        else:
            selected_device = devices_with_interfaces[0]

        selected_device = st.selectbox(
            "対象デバイス",
            devices_with_interfaces,
            index=devices_with_interfaces.index(selected_device),
            key="_traffic_device_select",
        )

        node = topology.get(selected_device)
        if not node:
            return

        interfaces = get_node_attr(node, 'interfaces', [])
        md = get_metadata(node)
        vendor = md.get('vendor', 'Unknown')
        model_name = md.get('model', '')

        st.caption(f"**{selected_device}** ({vendor} {model_name})")

        # ---- 利用率シミュレーション ----
        base_util = base_util_map.get(degradation_level, 35.0)

        _seed = hash(f"traffic_{selected_device}_{degradation_level}")
        rng = _rng.Random(_seed)

        # ---- インターフェース帯域利用率 ----
        st.markdown("##### インターフェース帯域利用率")

        # 表示モード切替
        _chart_mode = st.radio(
            "表示モード",
            ["棒グラフ（現在値）", "折れ線グラフ（時系列トレンド）"],
            horizontal=True,
            key="_traffic_chart_mode",
            label_visibility="collapsed",
        )

        total_capacity = 0
        total_used = 0
        iface_data: List[Dict] = []

        for iface in interfaces:
            if not isinstance(iface, dict):
                continue
            name = iface.get('name', '?')
            bw_mbps = iface.get('bandwidth_mbps', 100)
            connected_to = iface.get('connected_to', '')
            link_type = iface.get('link_type', 'copper')
            direction = _classify_interface_direction(iface, selected_device, topology)

            jitter = rng.uniform(-15.0, 15.0)
            util_pct = max(1.0, min(99.9, base_util + jitter))
            used_mbps = bw_mbps * util_pct / 100.0

            total_capacity += bw_mbps
            total_used += used_mbps

            if util_pct < 60:
                color = "#4CAF50"
                status_label = "正常"
            elif util_pct < 80:
                color = "#FF9800"
                status_label = "混雑"
            elif util_pct < 90:
                color = "#FF5722"
                status_label = "輻輳"
            else:
                color = "#D32F2F"
                status_label = "飽和"

            iface_data.append({
                "name": name, "connected_to": connected_to,
                "link_type": link_type, "bw_mbps": bw_mbps,
                "util_pct": util_pct, "used_mbps": used_mbps,
                "color": color, "status": status_label,
                "direction": direction,
            })

        if _chart_mode == "棒グラフ（現在値）":
            # 方向別にグループ化して表示
            _dir_order = {"uplink": 0, "downlink": 1, "ha_peer": 2, "unknown": 3}
            _dir_labels = {"uplink": "⬆ Uplink", "downlink": "⬇ Downlink",
                           "ha_peer": "⇔ HA Peer", "unknown": ""}
            _sorted_iface = sorted(iface_data, key=lambda x: _dir_order.get(x["direction"], 3))
            _prev_dir = None
            for d in _sorted_iface:
                if d["direction"] != _prev_dir:
                    _dir_lbl = _dir_labels.get(d["direction"], "")
                    if _dir_lbl:
                        st.markdown(f"<div style='font-size:12px;color:#666;font-weight:600;"
                                    f"margin:8px 0 2px;'>{_dir_lbl}</div>",
                                    unsafe_allow_html=True)
                    _prev_dir = d["direction"]
                link_icon = "🔗" if d["link_type"] == "fiber" else "🔌"
                st.markdown(
                    f'<div style="margin:4px 0;padding:6px 10px;background:#f8f9fa;'
                    f'border-radius:6px;border-left:4px solid {d["color"]};">'
                    f'<div style="display:flex;justify-content:space-between;align-items:center;font-size:13px;">'
                    f'<span><b>{d["name"]}</b> {link_icon} → {d["connected_to"]}</span>'
                    f'<span style="color:{d["color"]};font-weight:700;">{d["util_pct"]:.1f}% ({d["status"]})</span>'
                    f'</div>'
                    f'<div style="background:#e0e0e0;border-radius:3px;height:8px;margin-top:4px;">'
                    f'<div style="background:{d["color"]};width:{min(d["util_pct"], 100):.1f}%;'
                    f'height:100%;border-radius:3px;transition:width 0.3s;"></div>'
                    f'</div>'
                    f'<div style="font-size:11px;color:#888;margin-top:2px;">'
                    f'{d["used_mbps"]:.1f} / {d["bw_mbps"]} Mbps'
                    f'</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

            # 棒グラフモードでも副次メトリクス現在値を表示
            if secondary and degradation_level > 0:
                sec_val = secondary["values"].get(degradation_level, 0)
                sec_color = secondary["color"]
                st.markdown(
                    f'<div style="margin:8px 0;padding:8px 12px;background:{sec_color}10;'
                    f'border-radius:6px;border-left:4px solid {sec_color};">'
                    f'<span style="font-size:13px;color:{sec_color};font-weight:700;">'
                    f'{secondary["name"]}: {sec_val:.1f} {secondary["unit"]}'
                    f'</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
        else:
            # ---- 折れ線グラフ（時系列トレンド）+ 副次メトリクス ----
            _render_trend_chart(
                selected_device, interfaces, base_util_map, degradation_level,
                secondary=secondary if degradation_level > 0 else None,
                topology=topology,
            )

        # ---- サマリKPI ----
        avg_util = (total_used / total_capacity * 100) if total_capacity > 0 else 0

        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("平均利用率", f"{avg_util:.1f}%")
        with col2:
            st.metric("合計帯域", f"{total_capacity:,} Mbps")
        with col3:
            st.metric("使用帯域", f"{total_used:,.0f} Mbps")

        # ---- 影響デバイス範囲（BFS下流）----
        st.markdown("##### 影響デバイス範囲（BFS下流）")

        children_map = build_children_map(topology)
        downstream = get_downstream_devices(
            topology, selected_device, children_map=children_map,
        )

        if not downstream:
            st.caption("下流デバイスはありません（末端ノード）。")
        else:
            # デバイス種別ごとに集計
            _type_counts: Dict[str, List[str]] = {}
            for dev_id in downstream:
                _dn_node = topology.get(dev_id)
                if not _dn_node:
                    continue
                dev_type = get_node_attr(_dn_node, 'type', 'UNKNOWN')
                _type_counts.setdefault(dev_type, []).append(dev_id)

            total_devices = len(downstream)

            # 影響レベル判定（帯域利用率ベース + 障害シナリオ考慮）
            # サイレント障害やレイテンシ系障害は帯域が低くても影響がある
            _is_silent_scenario = scenario_key == "latency_jitter" and degradation_level >= 2
            if _is_silent_scenario and avg_util < 60:
                # サイレント障害: 帯域は正常だが接続品質が劣化
                _secondary = profile.get("secondary", {}) if profile else {}
                _sec_values = _secondary.get("values", {})
                _sec_val = _interpolate_level(_sec_values, degradation_level) if _sec_values else 0
                _sec_name = _secondary.get("name", "")
                if _sec_val >= 150:
                    impact_level = "重大"
                    impact_color = "#FF5722"
                    impact_desc = f"{_sec_name}劣化により配下デバイスで接続品質が大幅に低下"
                elif _sec_val >= 30:
                    impact_level = "軽微"
                    impact_color = "#FF9800"
                    impact_desc = f"{_sec_name}増大 ({_sec_val:.0f}{_secondary.get('unit', '')}) — 接続品質に影響の可能性"
                else:
                    impact_level = "なし"
                    impact_color = "#4CAF50"
                    impact_desc = "通常のトラフィック状態"
            elif avg_util >= 90:
                impact_level = "深刻"
                impact_color = "#D32F2F"
                impact_desc = "帯域飽和により配下デバイス全体に影響"
            elif avg_util >= 80:
                impact_level = "重大"
                impact_color = "#FF5722"
                impact_desc = "帯域輻輳により遅延・パケットロスが頻発"
            elif avg_util >= 60:
                impact_level = "軽微"
                impact_color = "#FF9800"
                impact_desc = "一部の配下デバイスで速度低下の可能性"
            else:
                impact_level = "なし"
                impact_color = "#4CAF50"
                impact_desc = "通常のトラフィック状態"

            # 種別サマリ文字列を生成  例: "FW×2, SW×3, AP×4"
            from configs.device_registry import get_label as _get_dev_label
            _type_parts = []
            for dtype, devs in sorted(_type_counts.items()):
                _type_lbl = _get_dev_label(dtype)
                _type_parts.append(f"{_type_lbl}×{len(devs)}")
            _type_summary = ", ".join(_type_parts)

            st.markdown(
                f'<div style="padding:10px;border-radius:8px;'
                f'border:2px solid {impact_color};background:{impact_color}10;margin:6px 0;">'
                f'<div style="font-size:14px;font-weight:700;color:{impact_color};">'
                f'影響レベル: {impact_level}'
                f'</div>'
                f'<div style="font-size:13px;color:#555;margin-top:4px;">'
                f'{impact_desc}'
                f'</div>'
                f'<div style="font-size:20px;font-weight:700;margin-top:6px;">'
                f'影響範囲: {total_devices}台'
                f'</div>'
                f'<div style="font-size:12px;color:#888;margin-top:4px;">'
                f'{_type_summary}'
                f'</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

            # デバイス一覧（折りたたみ）
            with st.expander(f"📡 配下デバイス一覧 ({total_devices}台)", expanded=False):
                for dtype, devs in sorted(_type_counts.items()):
                    _type_lbl = _get_dev_label(dtype)
                    dev_list = ", ".join(devs)
                    st.caption(f"**{_type_lbl}** ({len(devs)}台): {dev_list}")
