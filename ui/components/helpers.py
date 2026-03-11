# ui/components/helpers.py — cockpit 共通ヘルパー関数
import hashlib
import re
import streamlit as st
import streamlit.components.v1 as components


def st_html(html: str, height: int = 0) -> None:
    """SVG/HTMLをStreamlitで描画する。

    height > 0: st.components.v1.html() で明示的高さ指定（SVG用）。
    height == 0: st.markdown(unsafe_allow_html=True)（通常HTML用）。
    """
    if height > 0:
        components.html(html, height=height, scrolling=False)
    else:
        st.markdown(html, unsafe_allow_html=True)


def hash_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def pick_first(mapping: dict, keys: list, default: str = "") -> str:
    for k in keys:
        try:
            v = mapping.get(k)
            if v:
                return str(v)
        except Exception:
            pass
    return default


def build_ci_context_for_chat(topology: dict, target_node_id: str) -> dict:
    """チャット用CIコンテキストを構築。"""
    from utils.helpers import load_config_by_id

    node = topology.get(target_node_id)
    if node and hasattr(node, 'metadata'):
        md = node.metadata or {}
    elif isinstance(node, dict):
        md = node.get('metadata', {})
    else:
        md = {}

    def _get(obj, attr, default=None):
        if isinstance(obj, dict):
            return obj.get(attr, default)
        return getattr(obj, attr, default)

    ci = {
        "device_id": target_node_id or "",
        "hostname":  pick_first(md, ["hostname", "host", "name"],            default=(target_node_id or "")),
        "vendor":    pick_first(md, ["vendor", "manufacturer", "maker", "brand"], default=""),
        "os":        pick_first(md, ["os", "platform", "os_name"],           default=""),
        "model":     pick_first(md, ["model", "hw_model", "product"],        default=""),
        "role":      pick_first(md, ["role", "type", "device_role"],         default=""),
        "layer":     pick_first(md, ["layer", "level", "network_layer"],     default=""),
        "site":      pick_first(md, ["site", "dc", "location"],              default=""),
    }

    if node and topology:
        parent_id       = _get(node, 'parent_id')
        redundancy_group = _get(node, 'redundancy_group')
        node_type       = _get(node, 'type', '')
        node_layer      = _get(node, 'layer', '')

        ci["node_type"]        = node_type
        ci["network_layer"]    = node_layer
        ci["redundancy_group"] = redundancy_group or "なし（SPOF）"

        if parent_id and parent_id in topology:
            p_node = topology[parent_id]
            p_md = _get(p_node, 'metadata') or {}
            ci["parent_device"] = {
                "id":     parent_id,
                "type":   _get(p_node, 'type', ''),
                "vendor": pick_first(p_md, ["vendor", "manufacturer"], default=""),
                "os":     pick_first(p_md, ["os", "platform"], default=""),
            }
        else:
            ci["parent_device"] = None

        children = []
        peers = []
        same_layer = []
        for nid, n in topology.items():
            if nid == target_node_id:
                continue
            if _get(n, 'parent_id') == target_node_id:
                n_md = _get(n, 'metadata') or {}
                children.append({
                    "id":     nid,
                    "type":   _get(n, 'type', ''),
                    "vendor": pick_first(n_md, ["vendor", "manufacturer"], default=""),
                    "os":     pick_first(n_md, ["os", "platform"], default=""),
                })
            if redundancy_group and _get(n, 'redundancy_group') == redundancy_group:
                n_md = _get(n, 'metadata') or {}
                peers.append({
                    "id":     nid,
                    "type":   _get(n, 'type', ''),
                    "vendor": pick_first(n_md, ["vendor", "manufacturer"], default=""),
                    "os":     pick_first(n_md, ["os", "platform"], default=""),
                })
            if _get(n, 'layer') == node_layer:
                same_layer.append(nid)
        ci["children_devices"] = children
        ci["children_count"]   = len(children)
        ci["redundancy_peers"] = peers
        ci["same_layer_devices"] = same_layer

    try:
        conf = load_config_by_id(target_node_id) if target_node_id else ""
        if conf:
            ci["config_excerpt"] = conf[:1500]
    except Exception:
        pass

    return ci


def sanitize_prediction_context(text: str, max_len: int = 800) -> str:
    """LLMプロンプト用サニタイズ"""
    text = re.sub(r'[\x00-\x1f]', '', text or "")
    text = re.sub(r'(?i)(password|passwd|secret|token|api.?key)\s*[=:]\s*\S+', r'=***', text)
    text = re.sub(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.)\d{1,3}', r'***', text)
    return text[:max_len]
