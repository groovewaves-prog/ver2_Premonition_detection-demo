# ui/components/diagnostic.py — Auto-Diagnostics 実行関数
import time
import logging
import streamlit as st

try:
    import google.generativeai as genai
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False


def run_diagnostic(scenario: str, target_node_obj, use_llm: bool = True) -> dict:
    """ハイブリッド診断実行関数（LLM + テンプレートフォールバック）"""
    device_id = getattr(target_node_obj, "id", None) if target_node_obj else None
    if not device_id or device_id == "SYSTEM":
        inj = st.session_state.get("injected_weak_signal")
        if inj and inj.get("device_id"):
            device_id = inj.get("device_id")
        else:
            device_id = "L1_WAN_ROUTER_01"

    ts = time.strftime("%Y-%m-%d %H:%M:%S")

    injected = st.session_state.get("injected_weak_signal")
    try:
        level = int(injected.get("level", 0)) if injected else 0
    except Exception:
        level = 0
    pred_scenario = injected.get("scenario", "") if injected else ""

    # ==========================================
    # LLMによる動的シミュレーション
    # ==========================================
    if use_llm and GENAI_AVAILABLE:
        _diag_cache_key = f"diag_{device_id}_{scenario}_{pred_scenario}_{level}"
        if "diag_cache" not in st.session_state:
            st.session_state.diag_cache = {}

        if _diag_cache_key in st.session_state.diag_cache:
            cached_res = st.session_state.diag_cache[_diag_cache_key].copy()
            cached_res["sanitized_log"] = cached_res["sanitized_log"].replace(
                cached_res.get("_cached_ts", ""), ts
            )
            return cached_res

        try:
            if scenario != "正常稼働":
                state_desc = f"現在、ネットワーク全体で「{scenario}」という障害が発生しています。"
            elif level == 0:
                state_desc = "ネットワークは完全に正常稼働（アラームなし、ロス0%、ハードウェア異常なし）しています。"
            elif level == 5:
                state_desc = f"「{pred_scenario}」の深刻な劣化（レベル5/5）が起きています。"
            else:
                state_desc = f"「{pred_scenario}」の微細な劣化兆候（レベル{level}/5）が検知されています。"

            prompt = f"""
            あなたはCisco/Juniper等の実在するネットワーク機器（ホスト名: {device_id}）です。
            現在、{state_desc}
            管理者がターミナルでトラブルシューティングを行っています。実際の機器が出力するような、極めてリアルで生々しいターミナル出力（生ログ）を生成してください。

            【必須で含めるコマンドと出力要件（※ログ監視パーサーが読み取るため絶対遵守）】
            1. `{device_id}# ping 8.8.8.8 repeat 5`
               - 正常時はパケットロス0% (!!!!!) と `100 percent` を出力。異常時はロスを表現 (..!!!等)。
            2. `{device_id}# show ip interface brief`
               - 正常時（レベル0含む）は必ず `up up` という連続した文字列（間にスペース1つ）を含めること（例: `GigabitEthernet0/0/0  10.1.1.1  YES NVRAM  up up`）。
               - 異常時（ダウン時）は `down down` という連続した文字列を含めること。
            3. `{device_id}# show environment` (または show chassis hardware)
               - 正常時（レベル0含む）は必ず `NORMAL` または `OK` というキーワードのみを含めること（例: `Fan 1: NORMAL`）。
               - 異常がある場合は `WARNING` や `FAIL` を含めること。
            4. 劣化原因（{pred_scenario}）または障害に直結する詳細確認コマンド
               - 例: 光減衰なら `show interfaces transceiver detail` 等。レベルに応じた数値をリアルに出力する。
               - レベル0（正常時）の場合は、すべての数値が完全に正常であることを示し、WARNING等の警告文は一切出さないこと。

            【出力ルール】
            ・コードフェンス(```)は絶対に使わず、ターミナルに表示されるテキストそのままを出力すること。
            ・MACアドレスやuptime、ダミーのIPなどを適度に散りばめて生々しくする（機密情報は `***` でサニタイズされた体裁）。
            """

            cfg = st.session_state.get("llm_config", {})
            api_key = cfg.get("google_key")

            if api_key:
                client = genai.Client(api_key=api_key)
                response = client.models.generate_content(
                    model='gemma-3-4b-it',
                    contents=prompt
                )

                llm_log = (f"[SYSTEM AUTO-DIAGNOSTICS]\nTarget Device: {device_id}\n"
                           f"Timestamp: {ts} UTC\n"
                           f"==================================================\n"
                           + response.text.strip())
                result = {
                    "status": "SUCCESS",
                    "sanitized_log": llm_log,
                    "device_id": device_id,
                    "_cached_ts": ts
                }

                st.session_state.diag_cache[_diag_cache_key] = result
                return result

        except Exception as e:
            logging.warning(f"LLM diagnostic generation failed: {e}. Falling back to template.")

    # ==========================================
    # テンプレートフォールバック
    # ==========================================
    _p = f"{device_id}#"

    lines = [
        f"[SYSTEM AUTO-DIAGNOSTICS] (Template Fallback)",
        f"Target Device: {device_id}",
        f"Timestamp: {ts} UTC",
        "=================================================="
    ]

    recovered_devices = st.session_state.get("recovered_devices") or {}
    recovered_map = st.session_state.get("recovered_scenario_map") or {}

    if recovered_devices.get(device_id) and recovered_map.get(device_id) == scenario:
        lines += [
            f"{_p} show system alarms", "No active alarms",
            f"{_p} ping 8.8.8.8 repeat 5", "Success rate is 100 percent (5/5)",
            f"{_p} show ip interface brief", "GigabitEthernet0/0/0 10.1.1.254 YES NVRAM up up",
            f"{_p} show environment", "Fan: NORMAL, Temp: NORMAL, Power: NORMAL"
        ]
        return {"status": "SUCCESS", "sanitized_log": "\n".join(lines), "device_id": device_id}

    if scenario != "正常稼働":
        if "WAN" in scenario:
            lines += [
                f"{_p} show ip interface brief", "GigabitEthernet0/0/0 10.1.1.254 YES NVRAM down down",
                f"{_p} show ip bgp summary", "Neighbor 203.0.113.2 Idle",
                f"{_p} ping 203.0.113.2 repeat 5", "Success rate is 0 percent (0/5)",
                f"{_p} show environment", "Fan: NORMAL, Temp: NORMAL, Power: NORMAL"
            ]
        elif "FW" in scenario:
            lines += [
                f"{_p} ping 8.8.8.8 repeat 5", "Success rate is 100 percent (5/5)",
                f"{_p} show ip interface brief", "GigabitEthernet0/0/0 10.1.1.254 YES NVRAM up up",
                f"{_p} show chassis cluster status", "Redundancy group 0: degraded", "control link: down", "fabric link: up",
                f"{_p} show environment", "Fan: NORMAL, Temp: NORMAL, Power: WARNING"
            ]
        else:
            lines += [
                f"{_p} ping 8.8.8.8 repeat 5", "Success rate is 100 percent (5/5)",
                f"{_p} show ip interface brief", "GigabitEthernet0/0/0 10.1.1.254 YES NVRAM down down",
                f"{_p} show environment", "Fan: FAIL, Temperature: HIGH, Power: NORMAL"
            ]
    else:
        lines += [
            f"{_p} ping 8.8.8.8 repeat 5",
            "Type escape sequence to abort.",
            "Sending 5, 100-byte ICMP Echos to 8.8.8.8, timeout is 2 seconds:",
            "!!!!!",
            "Success rate is 100 percent (5/5), round-trip min/avg/max = 1/2/4 ms",
            f"{_p} show ip interface brief",
            "Interface              IP-Address      OK? Method Status    Protocol",
            "GigabitEthernet0/0/0   10.1.1.254      YES NVRAM  up up             ",
            f"{_p} show environment",
            "Fan 1: NORMAL, Fan 2: NORMAL",
            "Temp: 35C (NORMAL)",
            "Power Supply 1: NORMAL"
        ]
        if level > 0:
            lines.append(f"{_p} -- Extended Diagnostics ({pred_scenario} Lv.{level}) --")
            if "Optical" in pred_scenario:
                lines += [f"{_p} show interfaces transceiver detail", f"  Te0/0/1 Rx Power: {-23.0 - (level * 0.4):.1f} dBm (WARNING)"]
            elif "Microburst" in pred_scenario:
                lines += [f"{_p} show hardware internal buffer", f"  Queue Drops: {level * 200} drops/sec (WARNING)"]
            elif "Memory" in pred_scenario:
                free_mem = max(10, 800 - (level * 150))
                lines += [f"{_p} show processes memory", f"  Processor Pool Total: 8192M  Free: {free_mem}M (WARNING)"]

    return {"status": "SUCCESS", "sanitized_log": "\n".join(lines), "device_id": device_id}
