# ui/components/command_popup.py — コマンド実行結果ポップアップ
#
# 拡張A/B/C共通: トリアージコマンド・修復コマンドの実行結果を
# st.dialog ポップアップで表示するユーティリティ。
import time
import logging
import streamlit as st

logger = logging.getLogger(__name__)

# デモ環境用のコマンド実行シミュレーション結果テンプレート
_DEMO_COMMAND_OUTPUTS = {
    "show": {
        "show interfaces": (
            "GigabitEthernet0/0 is up, line protocol is up\n"
            "  Hardware is iGbE, address is 0050.56ff.0001\n"
            "  Internet address is 192.168.1.1/24\n"
            "  MTU 1500 bytes, BW 1000000 Kbit/sec\n"
            "  5 minute input rate 2000 bits/sec, 10 packets/sec\n"
            "  5 minute output rate 3000 bits/sec, 12 packets/sec"
        ),
        "show processes cpu": (
            "CPU utilization for five seconds: 12%/8%; one minute: 15%; five minutes: 14%\n"
            " PID Runtime(ms)   Invoked  uSecs    5Sec   1Min   5Min TTY Process\n"
            "   1       12000     50000    240   0.50%  0.45%  0.42%   0 Chunk Manager\n"
            "   2        8000     30000    266   0.30%  0.25%  0.20%   0 Load Meter\n"
            "   3       45000    100000    450   2.00%  1.80%  1.50%   0 IP Input"
        ),
        "show memory": (
            "Processor Pool Total:  2147483648  Used:  1288490188  Free:   858993460\n"
            "      I/O Pool Total:   268435456  Used:    53687091  Free:   214748365\n"
            "  Reserve Pool Total:    16777216  Used:           0  Free:    16777216"
        ),
        "show logging": (
            "Syslog logging: enabled (0 messages dropped, 0 messages rate-limited)\n"
            "    Console logging: level debugging, 1234 messages logged\n"
            "    Monitor logging: level debugging, 0 messages logged\n"
            "    Buffer logging: level debugging, 5678 messages logged\n"
            "Log Buffer (65536 bytes):\n"
            "*Mar 11 10:00:00: %SYS-5-CONFIG_I: Configured from console\n"
            "*Mar 11 09:55:00: %LINEPROTO-5-UPDOWN: Line protocol on Interface GigabitEthernet0/1, changed state to up"
        ),
        "show environment": (
            "Power Supply:\n"
            "  Power Supply 1: Normal\n"
            "  Power Supply 2: Normal\n"
            "Temperature:\n"
            "  Inlet: 25C (Normal)\n"
            "  Outlet: 32C (Normal)\n"
            "  CPU: 45C (Normal)\n"
            "Fan Status:\n"
            "  Fan 1: OK\n"
            "  Fan 2: OK"
        ),
        "show version": (
            "Cisco IOS Software, Version 15.7(3)M\n"
            "ROM: System Bootstrap, Version 15.0(1r)M\n"
            "System uptime is 45 days, 12 hours, 33 minutes\n"
            "System returned to ROM by power-on\n"
            "System image file is \"flash:c7200-adventerprisek9-mz.157-3.M.bin\"\n"
            "cisco 7206VXR (NPE400) processor with 524288K bytes of memory"
        ),
        "show ip route": (
            "Gateway of last resort is 10.0.0.1 to network 0.0.0.0\n"
            "C    192.168.1.0/24 is directly connected, GigabitEthernet0/0\n"
            "S    10.0.0.0/8 [1/0] via 10.0.0.1\n"
            "O    172.16.0.0/16 [110/20] via 10.0.0.2, 01:23:45, GigabitEthernet0/1\n"
            "B    203.0.113.0/24 [20/0] via 10.0.0.3, 02:34:56"
        ),
        "show bgp summary": (
            "BGP router identifier 10.0.0.1, local AS number 65000\n"
            "BGP table version is 1234, main routing table version 1234\n"
            "100 network entries using 14400 bytes of memory\n"
            "Neighbor        V    AS MsgRcvd MsgSent   TblVer  InQ OutQ Up/Down  State/PfxRcd\n"
            "10.0.0.2        4 65001    5000    4800     1234    0    0 30d12h         50\n"
            "10.0.0.3        4 65002    3000    2900     1234    0    0 15d06h         30"
        ),
    },
    "request": {
        "default": (
            "request completed successfully\n"
            "Status: OK"
        ),
    },
    "ping": {
        "default": (
            "Type escape sequence to abort.\n"
            "Sending 5, 100-byte ICMP Echos, timeout is 2 seconds:\n"
            "!!!!!\n"
            "Success rate is 100 percent (5/5), round-trip min/avg/max = 1/2/4 ms"
        ),
    },
}


# ネットワーク機器の実行可能CLIコマンドのプレフィックス
_CLI_COMMAND_PREFIXES = (
    "show ", "ping ", "traceroute ", "request ", "display ",
    "monitor ", "debug ", "test ", "clear ",
    "show\t",  # タブ補完対応
)


def extract_cli_commands(steps_text: str) -> list:
    """手順テキストからCLI実行可能なコマンドのみを抽出する。

    「PSU出力電圧の計測」「交換後にRx Powerの回復を確認」などの
    人手作業はフィルタし、show/ping/request等のCLIコマンドのみ返す。

    Args:
        steps_text: 改行 or \\n 区切りの手順テキスト
    Returns:
        CLIコマンド文字列のリスト
    """
    lines = steps_text.replace("\\n", "\n").split("\n")
    cmds = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        # 先頭の番号付き("1. ", "2. " 等)を除去
        import re
        cleaned = re.sub(r"^\d+[\.\)]\s*", "", stripped)
        cleaned_lower = cleaned.lower()
        # CLIコマンドプレフィックスで始まるもののみ抽出
        if any(cleaned_lower.startswith(p) for p in _CLI_COMMAND_PREFIXES):
            # "で" や "を" の後の日本語説明部分を除去
            # 例: "show environment power で電源状態を確認" → "show environment power"
            for sep in [" で", " を", " の", " に", "　で", "　を"]:
                if sep in cleaned:
                    cleaned = cleaned[:cleaned.index(sep)]
                    break
            # パイプ(|)やフィルタは残す
            cmds.append(cleaned.strip())
    return cmds


def simulate_command_execution(command: str, device_id: str) -> dict:
    """デモ環境でコマンド実行をシミュレートする。

    Returns:
        dict with keys: status, output, device_id, command, elapsed_sec
    """
    start = time.time()

    # コマンドからキーワードを抽出してテンプレートマッチング
    cmd_lower = command.strip().lower()
    output = None

    if cmd_lower.startswith("show"):
        show_templates = _DEMO_COMMAND_OUTPUTS["show"]
        # 最も長くマッチするテンプレートを探す
        best_match = None
        best_len = 0
        for key in show_templates:
            if key in cmd_lower and len(key) > best_len:
                best_match = key
                best_len = len(key)
        if best_match:
            output = show_templates[best_match]
        else:
            # 汎用 show コマンド出力
            output = f"{device_id}# {command}\n(output available — no issues detected)"
    elif cmd_lower.startswith("request"):
        output = _DEMO_COMMAND_OUTPUTS["request"]["default"]
    elif cmd_lower.startswith("ping"):
        output = _DEMO_COMMAND_OUTPUTS["ping"]["default"]
    else:
        output = f"{device_id}# {command}\nCommand executed successfully."

    # デモ用: 実行時間シミュレーション (0.3〜1.5秒)
    elapsed = time.time() - start
    if elapsed < 0.3:
        time.sleep(0.3 - elapsed)
    elapsed = time.time() - start

    return {
        "status": "success",
        "output": f"{device_id}# {command}\n{output}",
        "device_id": device_id,
        "command": command,
        "elapsed_sec": round(elapsed, 2),
    }


def render_command_result_popup(title: str, results: list):
    """コマンド実行結果をポップアップ（st.dialog）で表示する。

    Args:
        title: ポップアップのタイトル
        results: simulate_command_execution() の返値リスト
    """
    # session_state にポップアップデータを保存
    st.session_state["_cmd_popup_data"] = {
        "title": title,
        "results": results,
        "timestamp": time.time(),
    }


def show_command_popup_if_pending():
    """保留中のコマンド実行結果ポップアップがあれば表示する。

    このメソッドは render 関数の最上位（dialog が定義できるスコープ）で呼ぶ。
    """
    popup_data = st.session_state.get("_cmd_popup_data")
    if not popup_data:
        return

    @st.dialog(popup_data["title"], width="large")
    def _show_results():
        results = popup_data["results"]
        all_success = all(r.get("status") == "success" for r in results)

        if all_success:
            st.success(f"全 {len(results)} コマンド正常完了")
        else:
            failed = sum(1 for r in results if r.get("status") != "success")
            st.error(f"{failed}/{len(results)} コマンドでエラーが発生")

        for i, r in enumerate(results):
            status_icon = "✅" if r.get("status") == "success" else "❌"
            with st.expander(
                f"{status_icon} `{r.get('command', 'unknown')}` ({r.get('elapsed_sec', 0):.1f}s)",
                expanded=(i == 0 or r.get("status") != "success"),
            ):
                st.code(r.get("output", "No output"), language="text")

        if st.button("閉じる", key="_cmd_popup_close", type="primary", use_container_width=True):
            st.session_state.pop("_cmd_popup_data", None)
            st.rerun()

    _show_results()
    # ポップアップ表示後にデータをクリア（次回 rerun でダイアログが消える）
    # 注: dialog 表示中は rerun でダイアログが維持されるため、
    # 「閉じる」ボタンで明示的にクリアする


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 初動トリアージカード表示（予兆・障害共通）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def render_triage_cards(rec_actions: list, device_id: str, card_idx: int):
    """初動トリアージを旧スタイルのカード表示で描画（予兆・障害共通）。

    各カードの「最優先」「推奨」「補助」バッジをボタンにし、
    押下時にカード内の手順からCLIコマンドのみを抽出して実行、
    結果をポップアップで表示する。

    Args:
        rec_actions: recommended_actions リスト
        device_id: 対象デバイスID
        card_idx: ボタンキーの一意化用インデックス
    """
    if not rec_actions:
        return

    # プライオリティ別ソート
    _priority_order = {"最優先": 0, "high": 0, "推奨": 1, "medium": 1, "補助": 2, "low": 2}
    sorted_actions = sorted(
        rec_actions,
        key=lambda x: _priority_order.get(str(x.get("priority", "")).lower(), 3),
    )

    for act_idx, ra in enumerate(sorted_actions):
        _title     = ra.get("title", "")
        _effect    = ra.get("effect", "")
        _rationale = ra.get("rationale", "")
        _priority  = ra.get("priority", "")
        _steps     = ra.get("steps", ra.get("command", ra.get("action", "")))

        # プライオリティ判定
        _pri_lower = str(_priority).lower()
        if _pri_lower in ("最優先", "high"):
            _pri_label = "最優先"
            _pri_bg = "#D32F2F"
        elif _pri_lower in ("推奨", "medium"):
            _pri_label = "推奨"
            _pri_bg = "#FF9800"
        else:
            _pri_label = "補助"
            _pri_bg = "#558B2F"

        # 手順テキストをフォーマット
        _steps_display = _steps.replace("\\n", "\n")
        _steps_lines = [line.strip() for line in _steps_display.split("\n") if line.strip()]
        _steps_numbered = "\n".join(
            f"{i+1}. {line}" if not line[0].isdigit() else line
            for i, line in enumerate(_steps_lines)
        )

        # CLIコマンド抽出（ボタン押下時に実行する対象）
        _cli_cmds = extract_cli_commands(_steps)

        # カード描画: ヘッダ行（番号 + タイトル + ボタン）
        _col_info, _col_btn = st.columns([4, 1])
        with _col_info:
            st.markdown(
                f"**🔴 {act_idx + 1}. ⚠ {_title}**" if _pri_label == "最優先"
                else f"**🟠 {act_idx + 1}. ⚠ {_title}**" if _pri_label == "推奨"
                else f"**🟢 {act_idx + 1}. {_title}**"
            )
        with _col_btn:
            _btn_key = f"triage_{card_idx}_{act_idx}_{device_id}"
            if _cli_cmds:
                if st.button(
                    _pri_label,
                    key=_btn_key,
                    type="primary" if _pri_label == "最優先" else "secondary",
                    use_container_width=True,
                ):
                    _results = [
                        simulate_command_execution(cmd, device_id)
                        for cmd in _cli_cmds
                    ]
                    render_command_result_popup(
                        f"🔧 {_title}: {device_id}",
                        _results,
                    )
                    st.rerun()
            else:
                # CLIコマンドがない場合はバッジのみ表示（ボタンにしない）
                st.markdown(
                    f'<span style="background:{_pri_bg};color:#fff;padding:4px 12px;'
                    f'border-radius:4px;font-size:13px;font-weight:700;">{_pri_label}</span>',
                    unsafe_allow_html=True,
                )

        # 効果・根拠
        if _effect:
            st.caption(f"💡 効果: {_effect}")
        if _rationale:
            st.caption(f"⭐ 根拠: {_rationale}")

        # 手順（コードブロック風）
        if _steps_lines:
            st.markdown(
                f'<div style="background:#f8f8f8;border:1px solid #e8e8e8;border-radius:4px;'
                f'padding:8px 12px;font-size:13px;font-family:monospace;margin:4px 0 12px 0;'
                f'white-space:pre-wrap;line-height:1.6;">📋 手順:\n{_steps_numbered}</div>',
                unsafe_allow_html=True,
            )
