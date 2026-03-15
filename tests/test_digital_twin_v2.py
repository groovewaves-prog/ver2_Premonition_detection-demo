# -*- coding: utf-8 -*-
"""
test_digital_twin_v2.py - v2改修の検証テスト
"""
import sys
import os
sys.path.insert(0, '/home/claude')

# digital_twin_v2.py をテスト対象としてインポート
import importlib.util
spec = importlib.util.spec_from_file_location("digital_twin", "/home/claude/digital_twin_v2.py")
dt_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dt_mod)

EscalationRule = dt_mod.EscalationRule
ESCALATION_RULES = dt_mod.ESCALATION_RULES
DigitalTwinEngine = dt_mod.DigitalTwinEngine

passed = 0
failed = 0

def check(name, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  ✅ {name}")
        passed += 1
    else:
        print(f"  ❌ {name} {detail}")
        failed += 1

print("=" * 70)
print("TEST 1: EscalationRule dataclass に early_warning_hours が存在する")
print("=" * 70)
import dataclasses
fields = {f.name for f in dataclasses.fields(EscalationRule)}
check("early_warning_hours フィールド存在", "early_warning_hours" in fields)
check("time_to_critical_min フィールド存在", "time_to_critical_min" in fields)
check("pattern フィールド存在", "pattern" in fields)
check("semantic_phrases フィールド存在", "semantic_phrases" in fields)
check("category フィールド存在", "category" in fields)

print()
print("=" * 70)
print("TEST 2: 21ルール全数チェック")
print("=" * 70)
check(f"ルール数 = 22 (実際: {len(ESCALATION_RULES)})", len(ESCALATION_RULES) == 22)

expected_patterns = [
    "stp_loop", "mac_flap", "arp_storm",
    "bgp_flap", "ospf_adj",
    "ha_split",
    "bandwidth", "drop_error", "latency_jitter",
    "ntp_drift", "dhcp_dns",
    "optical", "temperature", "fan_fail", "power_quality", "storage",
    "memory_leak", "cpu_load", "process_crash",
    "auth_failure", "crypto_vpn",
    "generic_error"
]
actual_patterns = [r.pattern for r in ESCALATION_RULES]
for pat in expected_patterns:
    check(f"ルール '{pat}' 存在", pat in actual_patterns)

print()
print("=" * 70)
print("TEST 3: early_warning_hours 値チェック")
print("=" * 70)
rule_map = {r.pattern: r for r in ESCALATION_RULES}
ew_checks = {
    "stp_loop": 24, "bgp_flap": 48, "optical": 336,
    "memory_leak": 336, "storage": 720, "crypto_vpn": 720,
    "arp_storm": 12, "auth_failure": 12, "generic_error": 24
}
for pat, expected_hrs in ew_checks.items():
    r = rule_map[pat]
    check(f"{pat}.early_warning_hours = {expected_hrs}", r.early_warning_hours == expected_hrs,
          f"(実際: {r.early_warning_hours})")

print()
print("=" * 70)
print("TEST 4: app.py 注入メッセージのマッチング互換性")
print("=" * 70)

# DigitalTwinEngine をダミートポロジーで初期化
topology = {
    "FW_01_PRIMARY": {"parent_id": None, "redundancy_group": "fw_group"},
    "FW_01_SECONDARY": {"parent_id": None, "redundancy_group": "fw_group"},
    "CORE_SW_01": {"parent_id": "FW_01_PRIMARY", "redundancy_group": None},
}
children_map = {
    "FW_01_PRIMARY": ["CORE_SW_01"],
}
engine = DigitalTwinEngine(topology, children_map)

# Optical Decay シナリオ
opt_msgs = [
    "%TRANSCEIVER-4-THRESHOLD_VIOLATION: Rx Power -24.6 dBm (Threshold -25.0 dBm). Signal degrading.",
    "%LINK-3-ERROR: CRC errors increasing on Gi0/0/0 (Count: 450/min). Input queue drops detected.",
    "%OSPF-4-ADJCHANGE: Neighbor keepalive delayed (3 consecutive misses). Stability warning.",
]
for msg in opt_msgs:
    rule, quality = engine._match_rule(msg)
    check(f"Optical注入 → マッチ: '{msg[:60]}...'",
          rule is not None and rule.pattern != "generic_error",
          f"(rule={rule.pattern if rule else 'None'}, q={quality:.2f})")

# Microburst シナリオ
mb_msgs = [
    "%HARDWARE-3-ASIC_ERROR: Input queue drops detected (Count: 400). Burst traffic.",
    "%QOS-4-POLICER: Traffic exceeding CIR on interface ge-0/0/1. Buffer overflow risk.",
    "%TCP-5-RETRANSMIT: Retransmission rate 200/sec on monitored flows. Route updates increasing.",
]
for msg in mb_msgs:
    rule, quality = engine._match_rule(msg)
    check(f"Microburst注入 → マッチ: '{msg[:60]}...'",
          rule is not None and rule.pattern != "generic_error",
          f"(rule={rule.pattern if rule else 'None'}, q={quality:.2f})")

# Route Instability シナリオ
rt_msgs = [
    "BGP-5-ADJCHANGE: Route updates 2500/min. Stability warning.",
    "%BGP-4-MAXPFX: Prefix count approaching limit (92%). Route oscillation detected.",
    "%ROUTING-3-CONVERGENCE: RIB convergence delayed. Prefix withdrawal detected on multiple peers.",
]
for msg in rt_msgs:
    rule, quality = engine._match_rule(msg)
    check(f"Route注入 → マッチ: '{msg[:60]}...'",
          rule is not None and rule.pattern != "generic_error",
          f"(rule={rule.pattern if rule else 'None'}, q={quality:.2f})")

print()
print("=" * 70)
print("TEST 5: predict() 出力フィールドチェック")
print("=" * 70)

analysis_results = [
    {"id": "FW_01_PRIMARY", "prob": 0.65, "status": "WARNING"},
]
msg_map = {
    "FW_01_PRIMARY": [
        "%TRANSCEIVER-4-THRESHOLD_VIOLATION: Rx Power -24.6 dBm (Threshold -25.0 dBm). Signal degrading.",
        "%LINK-3-ERROR: CRC errors increasing on Gi0/0/0 (Count: 450/min). Input queue drops detected.",
    ],
}
predictions = engine.predict(analysis_results, msg_map)
check("predict() が予測を返す", len(predictions) > 0)

if predictions:
    pred = predictions[0]
    # 既存必須フィールド
    check("prediction_timeline 存在", "prediction_timeline" in pred)
    check("prediction_affected_count 存在", "prediction_affected_count" in pred)
    check("prediction_affected_devices 存在", "prediction_affected_devices" in pred)
    check("prediction_signal_count 存在", "prediction_signal_count" in pred)
    check("prediction_confidence_factors 存在", "prediction_confidence_factors" in pred)
    check("is_prediction = True", pred.get("is_prediction") is True)
    check("prob は float", isinstance(pred.get("prob"), float))

    # 新規フィールド
    check("prediction_early_warning_hours 存在", "prediction_early_warning_hours" in pred)
    check("prediction_time_to_critical_min 存在", "prediction_time_to_critical_min" in pred)
    check("prediction_early_warning_hours > 0", pred.get("prediction_early_warning_hours", 0) > 0)
    check("prediction_time_to_critical_min > 0", pred.get("prediction_time_to_critical_min", 0) > 0)

    # ナラティブ2軸チェック
    reason = pred.get("reason", "")
    check("ナラティブに 'Predictive Maintenance' 含む", "Predictive Maintenance" in reason)
    check("ナラティブに '早期予兆' 含む", "早期予兆" in reason)
    check("ナラティブに '急性期進行' 含む", "急性期進行" in reason)
    check("ナラティブに '推奨' 含む", "推奨" in reason)

    # 複数シグナル検出
    check("prediction_signal_count >= 2 (2メッセージ注入)", pred.get("prediction_signal_count", 0) >= 2)

    # confidence_factors の中身
    cf = pred.get("prediction_confidence_factors", {})
    check("confidence_factors.base 存在", "base" in cf)
    check("confidence_factors.match_quality 存在", "match_quality" in cf)
    check("confidence_factors.correlated_signals 存在", "correlated_signals" in cf)
    check("confidence_factors.correlation_boost 存在", "correlation_boost" in cf)

print()
print("=" * 70)
print("TEST 6: MIN_PREDICTION_CONFIDENCE = 0.40")
print("=" * 70)
check("MIN_PREDICTION_CONFIDENCE = 0.40", DigitalTwinEngine.MIN_PREDICTION_CONFIDENCE == 0.40)

print()
print("=" * 70)
print("TEST 7: Secondary Scan 動作確認")
print("=" * 70)
# dev_id が warning_seeds (prob 0.45-0.85) に含まれないケース
# → Secondary scan が拾う必要がある
analysis_results_2 = [
    {"id": "FW_01_PRIMARY", "prob": 0.20, "status": "INFO"},  # primary scan対象外
]
msg_map_2 = {
    "FW_01_PRIMARY": [
        "%TRANSCEIVER-4-THRESHOLD_VIOLATION: Rx Power -25.2 dBm (Threshold -25.0 dBm). Signal degrading.",
    ],
}
predictions_2 = engine.predict(analysis_results_2, msg_map_2)
check("Secondary Scan で低probデバイスも検出", len(predictions_2) > 0,
      f"(predictions: {len(predictions_2)})")

print()
print("=" * 70)
print("TEST 8: 早期予兆表示フォーマット")
print("=" * 70)
fmt = DigitalTwinEngine._format_early_warning
check("336h → '最大 14日前'", fmt(336) == "最大 14日前")
check("720h → '最大 30日前'", fmt(720) == "最大 30日前")
check("48h → '最大 2日前'", fmt(48) == "最大 2日前")
check("24h → '最大 1日前'", fmt(24) == "最大 1日前")
check("12h → '最大 12時間前'", fmt(12) == "最大 12時間前")

print()
print("=" * 70)
print("TEST 9: 多段シグナル相関ブースト")
print("=" * 70)
analysis_results_3 = [
    {"id": "FW_01_PRIMARY", "prob": 0.65, "status": "WARNING"},
]
# 1シグナル
msg_map_1sig = {
    "FW_01_PRIMARY": [
        "%TRANSCEIVER-4-THRESHOLD_VIOLATION: Rx Power -24.6 dBm. Signal degrading.",
    ],
}
pred_1 = engine.predict(analysis_results_3, msg_map_1sig)
conf_1 = pred_1[0]["prob"] if pred_1 else 0

# 2シグナル
msg_map_2sig = {
    "FW_01_PRIMARY": [
        "%TRANSCEIVER-4-THRESHOLD_VIOLATION: Rx Power -24.6 dBm. Signal degrading.",
        "%LINK-3-ERROR: CRC errors increasing. Input queue drops detected.",
    ],
}
pred_2 = engine.predict(analysis_results_3, msg_map_2sig)
conf_2 = pred_2[0]["prob"] if pred_2 else 0

# 3シグナル
msg_map_3sig = {
    "FW_01_PRIMARY": [
        "%TRANSCEIVER-4-THRESHOLD_VIOLATION: Rx Power -24.6 dBm. Signal degrading.",
        "%LINK-3-ERROR: CRC errors increasing. Input queue drops detected.",
        "%OSPF-4-ADJCHANGE: Neighbor keepalive delayed. Stability warning.",
    ],
}
pred_3 = engine.predict(analysis_results_3, msg_map_3sig)
conf_3 = pred_3[0]["prob"] if pred_3 else 0

check(f"2シグナル > 1シグナル (conf: {conf_2:.2f} > {conf_1:.2f})", conf_2 > conf_1)
check(f"3シグナル > 2シグナル (conf: {conf_3:.2f} > {conf_2:.2f})", conf_3 > conf_2)

print()
print("=" * 70)
summary = f"結果: {passed} passed, {failed} failed"
if failed == 0:
    print(f"🎉 ALL TESTS PASSED! {summary}")
else:
    print(f"⚠️  {summary}")
print("=" * 70)
