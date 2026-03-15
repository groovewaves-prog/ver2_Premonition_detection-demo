# ui/components/traffic_monitor.py — トラフィックモニタリングパネル
#
# PHM: トラフィック予測ティアでゲートされる。
# トポロジ JSON の interfaces / estimated_users を使い、
# インターフェース帯域利用率と影響ユーザー推定を可視化する。
import random as _rng
import streamlit as st
from typing import Optional

from digital_twin_pkg.common import (
    get_node_attr, get_metadata,
    estimate_downstream_users,
    build_children_map,
)


def render_traffic_monitor(
    topology: dict,
    target_device_id: Optional[str] = None,
    degradation_level: int = 0,
):
    """トラフィックモニタリングパネルを描画する。

    Args:
        topology: トポロジー辞書
        target_device_id: 選択中のデバイスID（なければ全デバイス概要）
        degradation_level: 劣化進行度 (0-5)、利用率シミュレーションに反映
    """
    st.subheader("📊 トラフィックモニタ")

    if not topology:
        st.info("トポロジーが読み込まれていません。")
        return

    # ---- デバイスセレクター ----
    devices_with_interfaces = [
        dev_id for dev_id, node in topology.items()
        if get_node_attr(node, 'interfaces')
    ]
    if not devices_with_interfaces:
        st.info("トポロジーにインターフェース情報がありません。")
        return

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
    model = md.get('model', '')

    st.caption(f"**{selected_device}** ({vendor} {model})")

    # ---- 利用率シミュレーション ----
    # base_util: degradation_level に応じた基本利用率
    base_util_map = {0: 35.0, 1: 55.0, 2: 70.0, 3: 85.0, 4: 93.0, 5: 99.0}
    base_util = base_util_map.get(degradation_level, 35.0)

    _seed = hash(f"traffic_{selected_device}_{degradation_level}")
    rng = _rng.Random(_seed)

    # ---- インターフェース帯域利用率バー ----
    st.markdown("##### インターフェース帯域利用率")

    total_capacity = 0
    total_used = 0

    for iface in interfaces:
        if not isinstance(iface, dict):
            continue
        name = iface.get('name', '?')
        bw_mbps = iface.get('bandwidth_mbps', 100)
        connected_to = iface.get('connected_to', '')
        link_type = iface.get('link_type', 'copper')

        # 個別ジッター
        jitter = rng.uniform(-15.0, 15.0)
        util_pct = max(1.0, min(99.9, base_util + jitter))
        used_mbps = bw_mbps * util_pct / 100.0

        total_capacity += bw_mbps
        total_used += used_mbps

        # 色の決定
        if util_pct < 60:
            color = "#4CAF50"   # green
            status = "正常"
        elif util_pct < 80:
            color = "#FF9800"   # orange
            status = "混雑"
        elif util_pct < 90:
            color = "#FF5722"   # deep orange
            status = "輻輳"
        else:
            color = "#D32F2F"   # red
            status = "飽和"

        # リンクアイコン
        link_icon = "🔗" if link_type == "fiber" else "🔌"

        st.markdown(
            f'<div style="margin:4px 0;padding:6px 10px;background:#f8f9fa;'
            f'border-radius:6px;border-left:4px solid {color};">'
            f'<div style="display:flex;justify-content:space-between;align-items:center;font-size:13px;">'
            f'<span><b>{name}</b> {link_icon} → {connected_to}</span>'
            f'<span style="color:{color};font-weight:700;">{util_pct:.1f}% ({status})</span>'
            f'</div>'
            f'<div style="background:#e0e0e0;border-radius:3px;height:8px;margin-top:4px;">'
            f'<div style="background:{color};width:{min(util_pct, 100):.1f}%;'
            f'height:100%;border-radius:3px;transition:width 0.3s;"></div>'
            f'</div>'
            f'<div style="font-size:11px;color:#888;margin-top:2px;">'
            f'{used_mbps:.1f} / {bw_mbps} Mbps'
            f'</div>'
            f'</div>',
            unsafe_allow_html=True,
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

    # ---- 影響ユーザー推定 ----
    st.markdown("##### 影響ユーザー推定（BFS下流）")

    children_map = build_children_map(topology)
    user_info = estimate_downstream_users(topology, selected_device, children_map)

    if user_info["ap_count"] == 0:
        st.caption("下流にアクセスポイントがありません。")
    else:
        # 利用率が高い場合の影響度計算
        if avg_util >= 90:
            impact_level = "深刻"
            impact_color = "#D32F2F"
            impact_desc = "帯域飽和によりほぼ全ユーザーに影響"
            affected_ratio = 0.95
        elif avg_util >= 80:
            impact_level = "重大"
            impact_color = "#FF5722"
            impact_desc = "帯域輻輳により遅延・パケットロスが頻発"
            affected_ratio = 0.70
        elif avg_util >= 60:
            impact_level = "軽微"
            impact_color = "#FF9800"
            impact_desc = "一部ユーザーで速度低下の可能性"
            affected_ratio = 0.30
        else:
            impact_level = "なし"
            impact_color = "#4CAF50"
            impact_desc = "通常のトラフィック状態"
            affected_ratio = 0.0

        affected_users = int(user_info["total_users"] * affected_ratio)

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
            f'推定 {affected_users:,} / {user_info["total_users"]:,} ユーザーに影響'
            f'</div>'
            f'<div style="font-size:12px;color:#888;margin-top:4px;">'
            f'AP数: {user_info["ap_count"]}台'
            f'</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

        # AP 詳細テーブル
        with st.expander(f"📡 AP別ユーザー内訳 ({user_info['ap_count']}台)", expanded=False):
            for ap in user_info["ap_details"]:
                ap_affected = int(ap["users"] * affected_ratio)
                st.caption(
                    f"**{ap['id']}** ({ap['location']}) — "
                    f"{ap_affected}/{ap['users']} ユーザーに影響"
                )
