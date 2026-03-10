# inference_engine.py
import json
import hashlib
import logging
import os
import re
import threading
import time
from enum import Enum
from typing import List, Dict, Any, Optional, Set
from digital_twin_pkg.common import inject_downstream_symptoms, classify_device as _common_classify

_logger = logging.getLogger(__name__)


# ==========================================================
# AI判定結果の永続キャッシュ + 自動ルール昇格
# ==========================================================

class _AISeverityStore:
    """
    AI (LLM) が判定したアラーム重要度を永続保存するストア。

    機能:
      1. 永続キャッシュ: 同一アラームパターンに対するAI判定結果を
         JSONファイルに保存し、再起動後もLLM不要で即座に返す
      2. 自動ルール昇格: 同一パターンが N回以上同じステータスで
         判定されたら「ルール候補」として昇格フラグを立てる

    保存先: {data_dir}/ai_severity_cache.json
    """

    PROMOTION_THRESHOLD = 3   # 同一判定N回でルール候補に昇格

    def __init__(self, data_dir: str = "./config"):
        self._data_dir = data_dir
        self._file_path = os.path.join(data_dir, "ai_severity_cache.json")
        self._lock = threading.Lock()
        self._store: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _pattern_key(self, alert_text: str) -> str:
        """アラームテキストを正規化してキーを生成。
        デバイスID等の固有名詞を除いた汎用パターンとして保存。"""
        normalized = re.sub(r'[A-Z0-9_]+-[A-Z0-9_]+', '<DEVICE>', alert_text)
        normalized = re.sub(r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}', '<IP>', normalized)
        normalized = re.sub(r'\d+', '<N>', normalized)
        normalized = normalized.strip().lower()
        return hashlib.md5(normalized.encode()).hexdigest()

    def _load(self):
        """JSONファイルからストアを読み込む。"""
        if os.path.exists(self._file_path):
            try:
                with open(self._file_path, 'r', encoding='utf-8') as f:
                    self._store = json.load(f)
                _logger.info(
                    f"AI severity cache loaded: {len(self._store)} patterns "
                    f"from {self._file_path}"
                )
            except Exception as e:
                _logger.warning(f"AI severity cache load failed: {e}")
                self._store = {}
        else:
            self._store = {}

    def _save(self):
        """ストアをJSONファイルに永続保存する。"""
        try:
            os.makedirs(self._data_dir, exist_ok=True)
            with open(self._file_path, 'w', encoding='utf-8') as f:
                json.dump(self._store, f, ensure_ascii=False, indent=2)
        except Exception as e:
            _logger.warning(f"AI severity cache save failed: {e}")

    def lookup(self, alert_text: str) -> Optional[Dict[str, Any]]:
        """キャッシュ済みの判定結果を検索。なければ None。"""
        key = self._pattern_key(alert_text)
        with self._lock:
            entry = self._store.get(key)
        if entry is None:
            return None
        return {
            "status": entry["status"],
            "avg_score": entry["avg_score"],
            "narrative": entry.get("narrative", ""),
            "hit_count": entry.get("hit_count", 0),
            "is_promoted": entry.get("is_promoted", False),
        }

    def record(
        self,
        alert_text: str,
        status: str,
        avg_score: float,
        narrative: str = "",
    ):
        """AI判定結果を記録。同一パターンの累積カウントを更新し、
        閾値超えでルール候補に昇格する。"""
        key = self._pattern_key(alert_text)
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                entry = {
                    "pattern_sample": alert_text[:200],
                    "status": status,
                    "avg_score": avg_score,
                    "narrative": narrative,
                    "hit_count": 1,
                    "is_promoted": False,
                    "first_seen": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "last_seen": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "status_history": {status: 1},
                }
            else:
                entry["hit_count"] = entry.get("hit_count", 0) + 1
                entry["last_seen"] = time.strftime("%Y-%m-%d %H:%M:%S")
                # ステータス履歴を更新
                sh = entry.get("status_history", {})
                sh[status] = sh.get(status, 0) + 1
                entry["status_history"] = sh
                # 最頻ステータスで上書き
                dominant_status = max(sh, key=sh.get)
                dominant_count = sh[dominant_status]
                entry["status"] = dominant_status
                entry["avg_score"] = avg_score
                entry["narrative"] = narrative
                # 昇格判定: 同一ステータスが N回以上
                if dominant_count >= self.PROMOTION_THRESHOLD:
                    entry["is_promoted"] = True

            self._store[key] = entry
            self._save()

    def get_promoted_rules(self) -> List[Dict[str, Any]]:
        """ルール昇格候補の一覧を返す。"""
        with self._lock:
            return [
                {
                    "pattern_sample": v.get("pattern_sample", ""),
                    "status": v["status"],
                    "avg_score": v.get("avg_score", 0),
                    "hit_count": v.get("hit_count", 0),
                    "first_seen": v.get("first_seen", ""),
                    "last_seen": v.get("last_seen", ""),
                    "narrative": v.get("narrative", ""),
                }
                for v in self._store.values()
                if v.get("is_promoted", False)
            ]

    def get_all_entries(self) -> List[Dict[str, Any]]:
        """全キャッシュエントリを返す（管理画面用）。"""
        with self._lock:
            return [
                {
                    "pattern_sample": v.get("pattern_sample", ""),
                    "status": v["status"],
                    "avg_score": v.get("avg_score", 0),
                    "hit_count": v.get("hit_count", 0),
                    "is_promoted": v.get("is_promoted", False),
                    "first_seen": v.get("first_seen", ""),
                    "last_seen": v.get("last_seen", ""),
                }
                for v in self._store.values()
            ]

# 新SDK優先、旧SDKにフォールバック
try:
    from google import genai as _new_genai
    _USE_NEW_SDK = True
    genai = _new_genai  # 統一参照
except ImportError:
    _new_genai = None
    _USE_NEW_SDK = False
    try:
        import google.generativeai as genai  # 旧SDK互換フォールバック
    except ImportError:
        genai = None  # どちらも未インストールの場合

# --- Digital Twin Integration (V45 Package) ---
try:
    from digital_twin_pkg import DigitalTwinEngine
    DIGITAL_TWIN_AVAILABLE = True
except ImportError:
    DIGITAL_TWIN_AVAILABLE = False

# ==========================================================
# AIOps health status
# ==========================================================
class HealthStatus(Enum):
    NORMAL = "GREEN"
    WARNING = "YELLOW"
    CRITICAL = "RED"


class LogicalRCA:
    """
    LogicalRCA (v5.1 - V45 Integrated):
      - Uses digital_twin_pkg.DigitalTwinEngine
    """

    SILENT_MIN_CHILDREN = 2
    SILENT_RATIO = 0.5

    def __init__(self, topology, config_dir: str = "./configs"):
        if isinstance(topology, str):
            self.topology = self._load_topology(topology)
        elif isinstance(topology, dict):
            self.topology = topology
        else:
            raise ValueError("topology must be either a file path (str) or a dictionary")

        self.config_dir = config_dir
        self.model = None
        self._api_configured = False

        self.children_map: Dict[str, List[str]] = {}
        for dev_id, info in self.topology.items():
            p = None
            if isinstance(info, dict):
                p = info.get("parent_id")
            else:
                if hasattr(info, "parent_id"):
                    p = getattr(info, "parent_id")
                elif hasattr(info, "paren"):
                    p = getattr(info, "paren", None)
            if p:
                self.children_map.setdefault(p, []).append(dev_id)

        self.digital_twin = None
        if DIGITAL_TWIN_AVAILABLE:
            try:
                # V45 Engine Initialization
                # tenant_id defaults to "default", data is stored in ./data/default
                self.digital_twin = DigitalTwinEngine(
                    topology=self.topology,
                    children_map=self.children_map,
                    tenant_id="default"
                )
            except Exception as e:
                print(f"[!] Digital Twin initialization failed: {e}")

        # AI判定結果の永続ストア
        self._ai_severity_store = _AISeverityStore(data_dir=config_dir)

    # ----------------------------
    # Topology helpers
    # ----------------------------
    def _get_device_info(self, device_id: str) -> Any:
        return self.topology.get(device_id, {})

    def _get_parent_id(self, device_id: str) -> Optional[str]:
        info = self._get_device_info(device_id)
        if isinstance(info, dict):
            return info.get("parent_id")
        if hasattr(info, "parent_id"):
            return getattr(info, "parent_id")
        return None

    def _get_metadata(self, device_id: str) -> Dict[str, Any]:
        info = self._get_device_info(device_id)
        if isinstance(info, dict):
            md = info.get("metadata", {})
            return md if isinstance(md, dict) else {}
        if hasattr(info, "metadata"):
            md = getattr(info, "metadata")
            return md if isinstance(md, dict) else {}
        return {}

    def _get_psu_count(self, device_id: str, default: int = 1) -> int:
        md = self._get_metadata(device_id)
        if isinstance(md, dict):
            hw = md.get("hw_inventory", {})
            if isinstance(hw, dict) and "psu_count" in hw:
                try:
                    return int(hw.get("psu_count"))
                except Exception:
                    pass
            if str(md.get("redundancy_type", "")).upper() == "PSU":
                return 2
        return default

    # ----------------------------
    # LLM init
    # ----------------------------
    def _ensure_api_configured(self) -> bool:
        if self._api_configured:
            return True
        api_key = os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            return False
        try:
            if _USE_NEW_SDK and _new_genai:
                # 新SDK: Client ベース
                self._genai_client = _new_genai.Client(api_key=api_key)
                self.model = None  # 新SDKではmodelオブジェクト不要
            else:
                # 旧SDK互換フォールバック
                genai.configure(api_key=api_key)
                self.model = genai.GenerativeModel("gemma-3-12b-it")
            self._api_configured = True
            return True
        except Exception as e:
            print(f"[!] API Configuration Error: {e}")
            return False

    # ----------------------------
    # IO
    # ----------------------------
    def _load_topology(self, path: str) -> Dict:
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _read_config(self, device_id: str) -> str:
        config_path = os.path.join(self.config_dir, f"{device_id}.txt")
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception as e:
                return f"Error reading config: {str(e)}"
        return "Config file not found."

    def _sanitize_text(self, text: str) -> str:
        text = re.sub(r'(encrypted-password\s+)"[^"]+"', r'\1"********"', text)
        text = re.sub(r"(password|secret)\s+(\d)\s+\S+", r"\1 \2 ********", text)
        text = re.sub(r"(username\s+\S+\s+secret)\s+\d\s+\S+", r"\1 5 ********", text)
        text = re.sub(r"(snmp-server community)\s+\S+", r"\1 ********", text)
        return text

    # ==========================================================
    # Silent failure inference
    # ==========================================================
    def _is_connection_loss(self, msg: str) -> bool:
        msg_l = msg.lower()
        return (
            "connection lost" in msg_l
            or "link down" in msg_l
            or "port down" in msg_l
            or "unreachable" in msg_l
        )

    def _detect_silent_failures(self, msg_map: Dict[str, List[str]]) -> Dict[str, Dict[str, Any]]:
        suspects: Dict[str, Dict[str, Any]] = {}
        for parent_id, children in self.children_map.items():
            if not children:
                continue
            if parent_id in msg_map:
                continue
            affected = []
            for c in children:
                msgs = msg_map.get(c, [])
                if any(self._is_connection_loss(m) for m in msgs):
                    affected.append(c)
            if not affected:
                continue
            total = len(children)
            ratio = len(affected) / max(total, 1)
            if len(affected) >= self.SILENT_MIN_CHILDREN and ratio >= self.SILENT_RATIO:
                report = (
                    f"[Silent Failure Heuristic]\n"
                    f"- Suspected upstream device: {parent_id}\n"
                    f"- Affected children: {len(affected)}/{total} (ratio={ratio:.2f})\n"
                    f"- Evidence: children raised Connection Lost/Unreachable simultaneously\n"
                )
                suspects[parent_id] = {
                    "children": affected,
                    "evidence_count": len(affected),
                    "total_children": total,
                    "ratio": ratio,
                    "report": report,
                }
        return suspects

    # ==========================================================
    # Public API
    # ==========================================================
    def analyze(self, alarms: List) -> List[Dict[str, Any]]:
        if not alarms:
            return [{
                "id": "SYSTEM",
                "label": "No alerts detected",
                "prob": 0.0,
                "type": "Normal",
                "tier": 0,
                "reason": "No active alerts detected.",
                "status": "GREEN",
                "classification": "unrelated"
            }]

        msg_map: Dict[str, List[str]] = {}
        for a in alarms:
            msg_map.setdefault(a.device_id, []).append(a.message)

        # アラームの is_root_cause フラグをデバイス単位で集約
        root_cause_device_ids: set = set()
        alarm_severity_map: Dict[str, str] = {}
        for a in alarms:
            if getattr(a, 'is_root_cause', False):
                root_cause_device_ids.add(a.device_id)
            # 最大 severity を保持
            sev_order = {'CRITICAL': 3, 'WARNING': 2, 'INFO': 1}
            cur = alarm_severity_map.get(a.device_id, 'INFO')
            if sev_order.get(a.severity, 0) > sev_order.get(cur, 0):
                alarm_severity_map[a.device_id] = a.severity

        silent_suspects = self._detect_silent_failures(msg_map)
        for parent_id, info in silent_suspects.items():
            msg_map.setdefault(parent_id, []).append("Silent Failure Suspected")

        results: List[Dict[str, Any]] = []

        for device_id, messages in msg_map.items():
            # --- 3分類ロジック ---
            # root_cause: is_root_cause フラグ、サイレント疑い、または親が障害でないのに自身が障害
            # symptom:    is_root_cause=False かつ親デバイスが root_cause
            # unrelated:  上記のいずれにも該当しない（ノイズアラート）
            classification = self._classify_device(
                device_id, root_cause_device_ids, silent_suspects, alarm_severity_map
            )

            # 親がサイレント疑い
            if device_id in silent_suspects:
                info = silent_suspects[device_id]
                # サイレント疑いは根本原因候補 → 確信度に応じてステータス決定
                _silent_prob = 0.8
                _silent_status = "RED" if _silent_prob >= 0.6 else "YELLOW"
                results.append({
                    "id": device_id,
                    "label": " / ".join(messages),
                    "prob": _silent_prob,
                    "type": "Network/SilentFailure",
                    "tier": 1,
                    "reason": f"Silent failure suspected.",
                    "status": _silent_status,
                    "is_prediction": False,
                    "classification": "root_cause"
                })
                continue

            # 通常分析
            analysis = self.analyze_redundancy_depth(device_id, messages)

            status_val = analysis["status"].value

            if analysis.get("impact_type") == "UNKNOWN" and "API key not configured" in analysis.get("reason", ""):
                prob = 0.5
                tier = 3
            else:
                if analysis["status"] == HealthStatus.CRITICAL:
                    prob = 0.95
                    tier = 1
                elif analysis["status"] == HealthStatus.WARNING:
                    prob = 0.7
                    tier = 2
                else:
                    prob = 0.3
                    tier = 3

            results.append({
                "id": device_id,
                "label": " / ".join(messages),
                "prob": prob,
                "type": analysis.get("impact_type", "UNKNOWN"),
                "tier": tier,
                "reason": analysis.get("reason", "AI provided no reason"),
                "status": status_val,
                "is_prediction": False,
                "classification": classification
            })

        # ==========================================================
        # ★ 派生アラート自動生成: 真因デバイスの配下に symptom を付与
        #   → digital_twin_pkg.common.inject_downstream_symptoms() に委譲
        # ==========================================================
        inject_downstream_symptoms(self.topology, results)

        # ==========================================================
        # ★ Digital Twin: 予兆検知
        # ==========================================================
        if self.digital_twin is not None:
            try:
                predictions = self.digital_twin.predict(
                    analysis_results=results,
                    msg_map=msg_map,
                    alarms=alarms,
                )

                if predictions:
                    existing_ids = {r["id"] for r in results}
                    for pred in predictions:
                        if pred["id"] not in existing_ids:
                            results.append(pred)
                        else:
                            # 既存がCRITICAL未満なら予兆を優先
                            existing = next((r for r in results if r["id"] == pred["id"]), None)
                            if existing and existing.get("prob", 0) < 0.8:
                                results.remove(existing)
                                results.append(pred)

            except Exception as e:
                print(f"[!] Digital Twin prediction error: {e}")

        # 優先順位ソート
        results.sort(key=lambda x: (
            0 if (x.get("prob", 0) >= 0.9 and not x.get("is_prediction")) else # Real Incident Priority
            1 if x.get("is_prediction") else                                   # Prediction Priority
            2,                                                                 # Others
            -x.get("prob", 0)                                                  # Prob Descending
        ))

        return results

    def _classify_device(
        self,
        device_id: str,
        root_cause_device_ids: set,
        silent_suspects: Dict[str, Any],
        alarm_severity_map: Dict[str, str],
    ) -> str:
        """
        デバイスを3分類する → digital_twin_pkg.common.classify_device に委譲
        """
        return _common_classify(
            device_id,
            root_cause_ids=root_cause_device_ids,
            silent_suspect_ids=set(silent_suspects.keys()),
            topology=self.topology,
        )

    # ── LLMスコアからHealthStatusへの変換閾値 ──
    _LLM_CRITICAL_THRESHOLD = 0.7   # semantic+trend 平均がこれ以上 → RED
    _LLM_WARNING_THRESHOLD  = 0.4   # これ以上 → YELLOW、未満 → GREEN

    def analyze_redundancy_depth(self, device_id: str, alerts: List[str]) -> Dict[str, Any]:
        if not alerts:
            return {"status": HealthStatus.NORMAL, "reason": "No active alerts.", "impact_type": "NONE"}

        safe_alerts = [self._sanitize_text(a) for a in alerts]
        joined = " ".join(safe_alerts)
        joined_lower = joined.lower()

        # ── 既知ルール: ハードウェア障害 ──
        if ("Power Supply: Dual Loss" in joined) or ("Dual Loss" in joined) or ("Device Down" in joined) or ("Thermal Shutdown" in joined):
            return {"status": HealthStatus.CRITICAL, "reason": "Device down / dual PSU loss detected.", "impact_type": "Hardware/Physical"}

        psu_count = self._get_psu_count(device_id, default=1)
        psu_single_fail = ("power supply" in joined_lower and "failed" in joined_lower and "dual" not in joined_lower)
        if psu_single_fail:
            if psu_count >= 2:
                return {"status": HealthStatus.WARNING, "reason": "Single PSU failure (Redundant).", "impact_type": "Hardware/Redundancy"}
            return {"status": HealthStatus.CRITICAL, "reason": "Single PSU failure (Non-Redundant).", "impact_type": "Hardware/Physical"}

        if "critical" in joined_lower:
             return {"status": HealthStatus.CRITICAL, "reason": "Critical alert detected.", "impact_type": "Generic/Critical"}

        # ── 未知パターン: LLM による動的ステータス判定 ──
        return self._llm_assess_severity(device_id, joined)

    def _score_to_result(
        self, status_str: str, avg_score: float, narrative: str, source: str,
    ) -> Dict[str, Any]:
        """スコアと判定ステータスから統一結果辞書を生成する。"""
        status_map = {"RED": HealthStatus.CRITICAL, "YELLOW": HealthStatus.WARNING, "GREEN": HealthStatus.NORMAL}
        hs = status_map.get(status_str, HealthStatus.WARNING)

        if hs == HealthStatus.CRITICAL:
            reason = f"AI判定: 重大な障害の可能性 (スコア: {avg_score:.2f}). {narrative}"
            impact = "AI/Critical"
        elif hs == HealthStatus.WARNING:
            reason = f"AI判定: 注意が必要 (スコア: {avg_score:.2f}). {narrative}"
            impact = "AI/Warning"
        else:
            reason = f"AI判定: 低リスク (スコア: {avg_score:.2f}). {narrative}"
            impact = "AI/Normal"

        if source == "cache":
            reason = f"[学習済] {reason}"

        return {"status": hs, "reason": reason, "impact_type": impact}

    def _llm_assess_severity(self, device_id: str, alert_text: str) -> Dict[str, Any]:
        """
        既知ルールにマッチしないアラームに対する AI ステータス判定。

        フロー:
          1. 永続キャッシュを参照 → ヒットすればLLM不要で即返却
          2. キャッシュミス → LLM呼び出し → 結果を永続保存
          3. 同一パターンが N回蓄積 → 自動ルール候補に昇格
          4. LLM未接続時は従来通り YELLOW フォールバック
        """
        _fallback = {
            "status": HealthStatus.WARNING,
            "reason": "Alert detected.",
            "impact_type": "Generic/Warning",
        }

        # ── 1. 永続キャッシュ参照 ──
        cached = self._ai_severity_store.lookup(alert_text)
        if cached is not None:
            _logger.debug(
                f"AI severity cache hit for {device_id}: "
                f"{cached['status']} (count={cached['hit_count']})"
            )
            # キャッシュヒットでもカウント加算（昇格判定の更新）
            self._ai_severity_store.record(
                alert_text=alert_text,
                status=cached["status"],
                avg_score=cached["avg_score"],
                narrative=cached.get("narrative", ""),
            )
            return self._score_to_result(
                cached["status"], cached["avg_score"],
                cached.get("narrative", ""), source="cache",
            )

        # ── 2. LLM 呼び出し ──
        llm = getattr(self.digital_twin, "llm", None) if self.digital_twin else None
        if llm is None or not llm.available:
            return _fallback

        try:
            info = self._get_device_info(device_id)
            device_type = "network"
            if isinstance(info, dict):
                device_type = info.get("type", info.get("device_type", "network"))
            elif hasattr(info, "type"):
                device_type = getattr(info, "type", "network")

            result = llm.score_alarm(
                alarm_text=alert_text,
                device_id=device_id,
                device_type=str(device_type),
            )

            avg_score = (result.scores.semantic + result.scores.trend) / 2.0
            narrative = result.scores.narrative or ""

            if avg_score >= self._LLM_CRITICAL_THRESHOLD:
                status_str = "RED"
            elif avg_score >= self._LLM_WARNING_THRESHOLD:
                status_str = "YELLOW"
            else:
                status_str = "GREEN"

            # ── 3. 永続保存（自動昇格判定を含む）──
            self._ai_severity_store.record(
                alert_text=alert_text,
                status=status_str,
                avg_score=avg_score,
                narrative=narrative,
            )

            return self._score_to_result(status_str, avg_score, narrative, source="llm")

        except Exception as e:
            _logger.warning(f"LLM severity assessment failed for {device_id}: {e}")
            return _fallback

    def get_ai_rule_candidates(self) -> List[Dict[str, Any]]:
        """自動ルール昇格候補の一覧を返す（UI向け公開API）。"""
        return self._ai_severity_store.get_promoted_rules()

    def get_ai_severity_cache_stats(self) -> Dict[str, Any]:
        """AI判定キャッシュの統計情報を返す。"""
        entries = self._ai_severity_store.get_all_entries()
        promoted = [e for e in entries if e.get("is_promoted")]
        return {
            "total_patterns": len(entries),
            "promoted_rules": len(promoted),
            "entries": entries,
        }
