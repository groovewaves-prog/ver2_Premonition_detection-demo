# -*- coding: utf-8 -*-
"""
digital_twin.py (Universal Edition - Production Ready v3.1)
===========================================================
AIOps Digital Twin Engine

[修正履歴]
 - Fix: predictメソッドでの「障害済み機器」の除外条件を厳格化。
        prob >= 0.85 または status == 'RED'/'CRITICAL' の機器は予兆検知の対象外とする。
"""

import logging
import numpy as np
from typing import List, Dict, Any, Optional, Set, Tuple
from dataclasses import dataclass

try:
    import networkx as nx
    HAS_NX = True
except ImportError:
    HAS_NX = False

try:
    from sentence_transformers import SentenceTransformer
    HAS_BERT = True
except ImportError:
    HAS_BERT = False

logger = logging.getLogger(__name__)

# ... (EscalationRuleクラスと定数は変更なし、そのまま維持) ...
# ※スペース節約のため省略しますが、以前のv3.0と同じ定義を含めてください

@dataclass
class EscalationRule:
    pattern: str
    semantic_phrases: List[str]
    escalated_state: str
    time_to_critical_min: int
    early_warning_hours: int
    base_confidence: float
    category: str = "Generic"

ESCALATION_RULES: List[EscalationRule] = [
    EscalationRule("stp_loop", ["stp loop", "tcn received", "blocking port"], "L2ループによるブロードキャストストーム", 5, 24, 0.95, "Network/L2"),
    EscalationRule("mac_flap", ["mac flapping", "host moving"], "MACテーブル不安定化によるフレーム消失", 10, 24, 0.90, "Network/L2"),
    EscalationRule("arp_storm", ["arp storm", "duplicate ip"], "ARPテーブル汚染による通信断", 10, 12, 0.85, "Network/L2"),
    EscalationRule("bgp_flap", ["bgp flapping", "neighbor down", "route oscillation"], "BGPセッション不安定化による経路消失", 15, 48, 0.90, "Network/Routing"),
    EscalationRule("ospf_adj", ["ospf adjacency down", "dead timer"], "OSPFネイバー喪失によるブラックホール", 15, 24, 0.85, "Network/Routing"),
    EscalationRule("ha_split", ["ha state degraded", "heartbeat lost"], "HA同期不全によるスプリットブレイン", 30, 48, 0.85, "Network/HA"),
    EscalationRule("bandwidth", ["bandwidth exceeded", "output drops"], "帯域飽和によるサービス品質劣化", 20, 72, 0.80, "Network/QoS"),
    EscalationRule("drop_error", ["input errors", "crc error", "fcs error", "crc_threshold", "err_counter"], "物理回線品質劣化によるスループット低下", 30, 168, 0.85, "Hardware/Physical"),
    EscalationRule("latency_jitter", ["latency", "jitter", "round-trip time", "rtt", "sla_threshold", "keepalive delay"], "遅延・ジッター増大によるサービス品質劣化", 20, 72, 0.85, "Network/QoS"),
    EscalationRule("ntp_drift", ["ntp unsynchronized", "time drift"], "時刻不整合による認証エラー", 120, 168, 0.70, "Network/Service"),
    EscalationRule("dhcp_dns", ["dhcp pool exhausted", "dns timeout"], "新規クライアント接続不可", 30, 48, 0.80, "Network/Service"),
    EscalationRule("optical", ["rx power", "optical signal", "transceiver", "light level", "dbm"], "光信号劣化による突然のリンクダウン", 60, 336, 0.95, "Hardware/Optical"),
    EscalationRule("temperature", ["temperature high", "overheat"], "熱暴走による緊急シャットダウン", 30, 48, 0.85, "Hardware/Thermal"),
    EscalationRule("fan_fail", ["fan failure", "fan malfunction"], "冷却能力喪失による温度上昇", 45, 72, 0.80, "Hardware/Thermal"),
    EscalationRule("power_quality", ["ups on battery", "power supply failed"], "電源供給不安定による再起動", 15, 24, 0.85, "Hardware/Power"),
    EscalationRule("storage", ["flash error", "nvram corruption"], "ストレージ障害による起動不能", 180, 720, 0.75, "Hardware/Storage"),
    EscalationRule("memory_leak", ["memory usage high", "malloc fail"], "メモリ枯渇によるシステムクラッシュ", 180, 336, 0.85, "Software/Resource"),
    EscalationRule("cpu_load", ["cpu usage high", "load average high"], "CPU枯渇によるプロトコルダウン", 20, 48, 0.85, "Software/Resource"),
    EscalationRule("process_crash", ["process terminated", "core dump"], "重要プロセス停止", 10, 24, 0.90, "Software/Process"),
    EscalationRule("auth_failure", ["authentication failed", "radius timeout"], "認証基盤障害", 15, 12, 0.80, "Security/Auth"),
    EscalationRule("crypto_vpn", ["ike sa deleted", "vpn tunnel down"], "VPNトンネル切断", 60, 720, 0.80, "Security/Crypto"),
    EscalationRule("generic_error", ["error", "fail", "critical", "warning"], "未分類のサービス劣化", 30, 24, 0.50, "Generic"),
]

# ... (DigitalTwinEngineクラスのinit等は v3.0 と同じため省略、predictメソッドのみ修正) ...

class DigitalTwinEngine:
    # (クラス変数と__init__などは前回のv3.0と同じ)
    _model: Optional[Any] = None
    _rule_embeddings: Optional[Dict[str, Any]] = None
    _model_loaded: bool = False
    MIN_PREDICTION_CONFIDENCE = 0.40
    MAX_PROPAGATION_HOPS = 3
    REDUNDANCY_DISCOUNT = 0.15
    SPOF_BOOST = 1.10
    EMBEDDING_THRESHOLD = 0.40
    MULTI_SIGNAL_BOOST = 0.05

    def __init__(self, topology: Dict[str, Any], children_map: Optional[Dict[str, List[str]]] = None):
        self.topology = topology
        self.children_map = children_map or {}
        self.graph = None
        if HAS_NX:
            self.graph = nx.DiGraph()
            for node_id, attrs in topology.items():
                node_attrs = attrs if isinstance(attrs, dict) else vars(attrs)
                self.graph.add_node(node_id, **node_attrs)
                parent_id = node_attrs.get("parent_id")
                if parent_id and parent_id in topology:
                    self.graph.add_edge(parent_id, node_id, relation="downstream")
                    self.graph.add_edge(node_id, parent_id, relation="upstream")
                if node_id in self.children_map:
                    for child in self.children_map[node_id]:
                        if child in topology:
                            if not self.graph.has_edge(node_id, child):
                                self.graph.add_edge(node_id, child, relation="downstream")
                            if not self.graph.has_edge(child, node_id):
                                self.graph.add_edge(child, node_id, relation="upstream")
        self._redundancy_groups = self._build_redundancy_map()
        self._ensure_model_loaded()

    def _build_redundancy_map(self) -> Dict[str, List[str]]:
        rg_map = {}
        for dev_id, info in self.topology.items():
            attrs = info if isinstance(info, dict) else vars(info)
            rg = attrs.get('redundancy_group')
            if rg:
                rg_map.setdefault(rg, []).append(dev_id)
        return rg_map

    @classmethod
    def _ensure_model_loaded(cls):
        if cls._model_loaded: return
        if not HAS_BERT:
            cls._model_loaded = True
            return
        try:
            cls._model = SentenceTransformer('all-MiniLM-L6-v2')
            all_phrases = []
            phrase_to_rule_idx = []
            for idx, rule in enumerate(ESCALATION_RULES):
                for phrase in rule.semantic_phrases:
                    all_phrases.append(phrase)
                    phrase_to_rule_idx.append(idx)
            if all_phrases:
                embeddings = cls._model.encode(all_phrases, convert_to_numpy=True)
                cls._rule_embeddings = {"vectors": embeddings, "phrase_to_rule_idx": phrase_to_rule_idx, "phrases": all_phrases}
            cls._model_loaded = True
        except:
            cls._model = None
            cls._model_loaded = True

    def _match_rule(self, alarm_text: str) -> Tuple[Optional[EscalationRule], float]:
        text_lower = alarm_text.lower()
        for rule in ESCALATION_RULES:
            if rule.pattern in text_lower:
                return rule, 1.0
        if self._model and self._rule_embeddings:
            try:
                query_vec = self._model.encode([alarm_text], convert_to_numpy=True)
                rule_vecs = self._rule_embeddings["vectors"]
                similarities = np.dot(rule_vecs, query_vec.T).flatten()
                norms = np.linalg.norm(rule_vecs, axis=1) * np.linalg.norm(query_vec)
                cosine_sim = similarities / np.where(norms==0, 1e-10, norms)
                best_idx = np.argmax(cosine_sim)
                best_score = float(cosine_sim[best_idx])
                if best_score >= self.EMBEDDING_THRESHOLD:
                    return ESCALATION_RULES[self._rule_embeddings["phrase_to_rule_idx"][best_idx]], best_score
            except: pass
        return None, 0.0

    def _get_downstream_impact(self, root_id: str) -> List[Tuple[str, int]]:
        impacts = []
        if not self.graph or root_id not in self.graph: return impacts
        try:
            def downstream_filter(u, v):
                return self.graph[u][v].get("relation") == "downstream"
            subgraph = nx.subgraph_view(self.graph, filter_edge=downstream_filter)
            tree = nx.bfs_tree(subgraph, root_id, depth_limit=self.MAX_PROPAGATION_HOPS)
            for node in tree:
                if node == root_id: continue
                dist = nx.shortest_path_length(subgraph, root_id, node)
                impacts.append((node, dist))
        except: pass
        return impacts

    def _calculate_confidence(self, rule: EscalationRule, device_id: str, match_quality: float) -> float:
        attrs = self.topology.get(device_id, {})
        if not isinstance(attrs, dict): attrs = vars(attrs)
        rg = attrs.get('redundancy_group')
        has_redundancy = bool(rg and len(self._redundancy_groups.get(rg, [])) > 1)
        is_spof = bool(self.children_map.get(device_id) and not has_redundancy)
        confidence = rule.base_confidence
        confidence *= (0.8 + 0.2 * match_quality)
        if has_redundancy: confidence *= (1.0 - self.REDUNDANCY_DISCOUNT)
        if is_spof: confidence *= self.SPOF_BOOST
        return min(0.99, max(0.1, confidence))

    def _build_prediction(self, dev_id, rule, quality, matched_signals, confidence, extra_signal_count, boost_factor):
        downstream = self._get_downstream_impact(dev_id)
        impact_count = len(downstream)
        affected_names = [d[0] for d in downstream[:3]]
        if impact_count > 3: affected_names.append(f"他{impact_count-3}台")
        affected_str = ", ".join(affected_names) if affected_names else "配下なし"
        
        if rule.early_warning_hours >= 24:
            early_str = f"最大 {rule.early_warning_hours // 24}日前"
        else:
            early_str = f"最大 {rule.early_warning_hours}時間前"
        
        multi_signal_note = ""
        if extra_signal_count > 0:
            boost_val = min(extra_signal_count * boost_factor, 0.20)
            multi_signal_note = f"\n・相関分析: 他 {extra_signal_count} 件の関連シグナルを検知 (確信度 +{boost_val:.0%})"

        return {
            "id": dev_id,
            "label": f"🔮 [予兆] {rule.escalated_state}",
            "severity": "CRITICAL",
            "status": "CRITICAL",
            "prob": round(confidence, 2),
            "type": f"Predictive/{rule.category}",
            "tier": 1,
            "reason": (
                f"【Digital Twin未来予測】\n"
                f"・早期予兆: {early_str} から検知可能なパターン\n"
                f"・急性期: 発症後 {rule.time_to_critical_min}分 で深刻化する恐れ\n"
                f"・影響範囲: {affected_str} ({impact_count}台) が通信断になるリスク\n"
                f"・推奨: 次回メンテナンスウィンドウでの予防交換/対応\n"
                f"--------------------------------\n"
                f"・検出根拠: {matched_signals[0][2]} (Match: {quality:.2f}){multi_signal_note}"
            ),
            "is_prediction": True,
            "prediction_timeline": f"{rule.time_to_critical_min}分後",
            "prediction_early_warning_hours": rule.early_warning_hours,
            "prediction_affected_count": impact_count,
            "prediction_escalated_state": rule.escalated_state,
            "prediction_signal_count": len(matched_signals),
            "prediction_confidence_factors": {"base": rule.base_confidence}
        }

    def predict(self, analysis_results: List[Dict[str, Any]], msg_map: Dict[str, List[str]], alarms: Optional[List] = None) -> List[Dict[str, Any]]:
        predictions = []
        MULTI_SIGNAL_BOOST = 0.08

        # ★ 修正: 既に障害(CRITICAL/RED)判定されている機器は、予兆検知の対象から確実に除外する
        # prob >= 0.85 は障害とみなす
        critical_ids = {
            r["id"] for r in analysis_results 
            if r.get("status") in ["RED", "CRITICAL"] or r.get("severity") == "CRITICAL" or r.get("prob", 0) >= 0.85
        }

        # 候補選定: Warning機器 + ログがある全機器 - 障害済み機器
        warning_ids = {
            r["id"] for r in analysis_results
            if 0.45 <= float(r.get("prob", 0)) <= 0.85
        }
        active_ids = set(msg_map.keys())
        candidates = (warning_ids.union(active_ids)) - critical_ids
        
        processed_devices = set()

        for dev_id in candidates:
            if dev_id in processed_devices: continue
            messages = msg_map.get(dev_id, [])
            if not messages: continue

            matched_signals = []
            for msg in messages:
                rule, quality = self._match_rule(msg)
                if rule and quality >= 0.30 and rule.pattern != "generic_error":
                    matched_signals.append((rule, quality, msg))

            if not matched_signals:
                rule, quality = self._match_rule(messages[0])
                if not rule: continue
                matched_signals = [(rule, quality, messages[0])]

            matched_signals.sort(key=lambda x: x[1], reverse=True)
            primary_rule, primary_quality, primary_msg = matched_signals[0]

            confidence = self._calculate_confidence(primary_rule, dev_id, primary_quality)
            extra_signals = len(matched_signals) - 1
            if extra_signals > 0:
                boost = min(extra_signals * MULTI_SIGNAL_BOOST, 0.20)
                confidence = min(0.99, confidence + boost)

            if confidence < self.MIN_PREDICTION_CONFIDENCE: continue

            pred = self._build_prediction(
                dev_id, primary_rule, primary_quality, matched_signals, 
                confidence, extra_signals, MULTI_SIGNAL_BOOST
            )
            predictions.append(pred)
            processed_devices.add(dev_id)
            
        return predictions
