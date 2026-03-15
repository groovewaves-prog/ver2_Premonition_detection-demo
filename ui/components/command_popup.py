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
        # サイレント障害検出用コマンド出力（異常状態をシミュレート）
        "show mac address-table": (
            "Mac Address Table\n"
            "-------------------------------------------\n"
            "Vlan    Mac Address       Type        Ports\n"
            "----    -----------       --------    -----\n"
            " 10     0050.56ff.0001    DYNAMIC     Gi0/1\n"
            " 10     0050.56ff.0002    DYNAMIC     Gi0/1  ← flapping detected\n"
            " 20     0050.56ff.0010    DYNAMIC     Gi0/2\n"
            "Total Mac Addresses for this criterion: 3\n"
            "WARNING: MAC address flapping detected between Gi0/1 and Gi0/3"
        ),
        "show spanning-tree": (
            "VLAN0010\n"
            "  Spanning tree enabled protocol ieee\n"
            "  Root ID    Priority    32778\n"
            "             Address     0050.56ff.aa01\n"
            "             This bridge is the root\n"
            "  Bridge ID  Priority    32778\n"
            "             Address     0050.56ff.aa01\n"
            "Interface        Role Sts Cost      Prio.Nbr Type\n"
            "---------------- ---- --- --------- -------- ------\n"
            "Gi0/1            Desg BLK 4         128.1    P2p\n"
            "Gi0/2            Desg FWD 4         128.2    P2p\n"
            "Gi0/3            Desg BLK 4         128.3    P2p\n"
            "WARNING: Topology Change detected — 3 changes in last 60 seconds"
        ),
        "show vlan brief": (
            "VLAN Name                             Status    Ports\n"
            "---- -------------------------------- --------- ------\n"
            "1    default                          active    Gi0/4\n"
            "10   DATA                             active    Gi0/1, Gi0/2\n"
            "20   VOICE                            active    Gi0/3\n"
            "99   MGMT                             active\n"
            "NOTE: VLAN 10 has no active uplink port — traffic isolation possible"
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


def classify_steps(steps_text: str) -> list:
    """手順テキストを CLIコマンド / 人手作業 に分類して返す。

    Returns:
        list of dict: [{"text": str, "is_cli": bool, "cleaned": str}, ...]
    """
    import re
    lines = steps_text.replace("\\n", "\n").split("\n")
    result = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        cleaned = re.sub(r"^\d+[\.\)]\s*", "", stripped)
        cleaned_lower = cleaned.lower()
        is_cli = any(cleaned_lower.startswith(p) for p in _CLI_COMMAND_PREFIXES)
        cli_cmd = ""
        if is_cli:
            cli_cmd = cleaned
            for sep in [" で", " を", " の", " に", "　で", "　を"]:
                if sep in cli_cmd:
                    cli_cmd = cli_cmd[:cli_cmd.index(sep)]
                    break
            cli_cmd = cli_cmd.strip()
        result.append({
            "text": cleaned,
            "is_cli": is_cli,
            "cleaned": cli_cmd if is_cli else cleaned,
        })
    return result


def extract_cli_commands(steps_text: str) -> list:
    """手順テキストからCLI実行可能なコマンドのみを抽出する。

    「PSU出力電圧の計測」「交換後にRx Powerの回復を確認」などの
    人手作業はフィルタし、show/ping/request等のCLIコマンドのみ返す。

    Args:
        steps_text: 改行 or \\n 区切りの手順テキスト
    Returns:
        CLIコマンド文字列のリスト
    """
    return [s["cleaned"] for s in classify_steps(steps_text) if s["is_cli"]]


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

    # デモ用: 実行時間シミュレーション（最小遅延）
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


def render_triage_cards(rec_actions: list, device_id: str, card_idx):
    """初動トリアージカード表示。

    過去バージョン準拠の整理されたカードレイアウト:
    - 各カードが優先度別の色付き左ボーダー枠で囲まれる
    - 優先度バッジが右上に配置
    - 効果・根拠・手順がカード内に収まる
    - コマンド実行結果はカード内の手順セクションに折りたたみ表示
    - サマリーブロックは廃止（二重表示を解消）

    Args:
        rec_actions: recommended_actions リスト
        device_id: 対象デバイスID
        card_idx: ボタンキーの一意化用識別子（int or str）
    """
    if not rec_actions:
        return

    # プライオリティ別ソート
    _priority_order = {"最優先": 0, "high": 0, "推奨": 1, "medium": 1, "補助": 2, "low": 2}
    sorted_actions = sorted(
        rec_actions,
        key=lambda x: _priority_order.get(str(x.get("priority", "")).lower(), 3),
    )

    # ── 全カードの CLI コマンドを事前収集（一括実行用）──
    _all_cli_cmds = []
    for ra in sorted_actions:
        _steps = ra.get("steps", ra.get("command", ra.get("action", "")))
        _all_cli_cmds.extend(extract_cli_commands(_steps))

    # ── インライン結果ストアのキー ──
    _inline_key = f"_triage_inline_{card_idx}_{device_id}"

    # ── 一括実行ボタン（CLI コマンドが1つ以上ある場合のみ）──
    if _all_cli_cmds:
        _batch_col1, _batch_col2 = st.columns([3, 1])
        with _batch_col1:
            _executed = st.session_state.get(_inline_key)
            if _executed:
                st.caption(f"✅ {len(_executed)} コマンド実行済み")
            else:
                st.caption(f"📡 実行対象: {len(_all_cli_cmds)} コマンド")
        with _batch_col2:
            _batch_key = f"batch_exec_{card_idx}_{device_id}"
            if st.button(
                "▶ 全コマンド一括実行",
                key=_batch_key,
                type="primary",
                use_container_width=True,
            ):
                _results = {}
                for cmd in _all_cli_cmds:
                    _results[cmd] = simulate_command_execution(cmd, device_id)
                st.session_state[_inline_key] = _results
                _store_triage_results(
                    device_id, f"一括トリアージ ({len(_all_cli_cmds)}cmd)",
                    list(_results.values()),
                )
                st.rerun()

    # ── 実行済み結果の取得 ──
    _inline_results = st.session_state.get(_inline_key, {})

    # ── 各カード描画（過去バージョン準拠のカードレイアウト）──
    for act_idx, ra in enumerate(sorted_actions):
        _title     = ra.get("title", "")
        _effect    = ra.get("effect", "")
        _rationale = ra.get("rationale", "")
        _priority  = ra.get("priority", "")
        _steps     = ra.get("steps", ra.get("command", ra.get("action", "")))

        # プライオリティ判定 → カード色
        _pri_lower = str(_priority).lower()
        if _pri_lower in ("最優先", "high"):
            _pri_label = "最優先"
            _border_color = "#D32F2F"
            _bg_color = "#FFF5F5"
            _badge_bg = "#D32F2F"
        elif _pri_lower in ("推奨", "medium"):
            _pri_label = "推奨"
            _border_color = "#FF9800"
            _bg_color = "#FFF8E1"
            _badge_bg = "#FF9800"
        else:
            _pri_label = "補助"
            _border_color = "#558B2F"
            _bg_color = "#F1F8E9"
            _badge_bg = "#455A64"

        # 手順を CLI / 人手 に分類
        _classified = classify_steps(_steps)
        _cli_cmds = [s["cleaned"] for s in _classified if s["is_cli"]]

        # 手順HTML構築
        _steps_html = ""
        if _classified:
            _step_lines = []
            for i, step in enumerate(_classified):
                _num = i + 1
                if step["is_cli"]:
                    _cmd = step["cleaned"]
                    _result = _inline_results.get(_cmd)
                    if _result:
                        _step_lines.append(
                            f'<div style="font-size:15px;color:#2E7D32;padding:2px 0;">'
                            f'{_num}. <code style="font-size:15px;">{_cmd}</code> ✅</div>'
                        )
                    else:
                        _step_lines.append(
                            f'<div style="font-size:15px;color:#333;padding:2px 0;">'
                            f'{_num}. <code style="font-size:15px;">{_cmd}</code></div>'
                        )
                else:
                    _step_lines.append(
                        f'<div style="font-size:15px;color:#333;padding:2px 0;">'
                        f'{_num}. {step["text"]}</div>'
                    )
            _steps_html = (
                f'<div style="margin-top:8px;padding:6px 10px;background:#FAFAFA;'
                f'border-radius:4px;">'
                f'<div style="font-size:12px;font-weight:600;color:#555;margin-bottom:4px;">'
                f'📋 手順:</div>'
                f'{"".join(_step_lines)}'
                f'</div>'
            )

        # 効果・根拠HTML
        _meta_html = ""
        if _effect:
            _meta_html += (
                f'<div style="font-size:12px;color:#666;padding:2px 0;">'
                f'💡 効果: {_effect}</div>'
            )
        if _rationale:
            _meta_html += (
                f'<div style="font-size:12px;color:#666;padding:2px 0;">'
                f'⭐ 根拠: {_rationale}</div>'
            )

        # カード全体を1つのHTMLブロックで描画
        st.markdown(
            f'<div style="background:{_bg_color};border:1px solid {_border_color};'
            f'border-left:4px solid {_border_color};border-radius:6px;'
            f'padding:12px 16px;margin:8px 0;">'
            f'<div style="display:flex;justify-content:space-between;align-items:center;">'
            f'<div style="font-size:14px;font-weight:700;color:#333;">'
            f'🔴 {act_idx + 1}. ⚠ {_title}</div>'
            f'<span style="background:{_badge_bg};color:#fff;padding:3px 12px;'
            f'border-radius:4px;font-size:12px;font-weight:700;">{_pri_label}</span>'
            f'</div>'
            f'{_meta_html}'
            f'{_steps_html}'
            f'</div>',
            unsafe_allow_html=True,
        )

        # ── コマンド実行結果（カード直下に折りたたみ表示）──
        _card_results = {cmd: _inline_results[cmd] for cmd in _cli_cmds if cmd in _inline_results}
        if _card_results:
            with st.expander(f"📊 実行結果 ({len(_card_results)}件)", expanded=False):
                for _cmd, _res in _card_results.items():
                    _out = _res.get("output", "").strip()
                    st.code(_out, language="text")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# トリアージ実行結果の永続ストア（AI復旧計画との自動連携用）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_TRIAGE_RESULTS_KEY = "_triage_exec_results"


def _store_triage_results(device_id: str, action_title: str, results: list):
    """トリアージコマンド実行結果を session_state に蓄積する。

    device_id ごとにリストで保持し、同一デバイスの複数トリアージを蓄積可能。
    AI復旧計画生成時に get_triage_results() で取得される。
    """
    if _TRIAGE_RESULTS_KEY not in st.session_state:
        st.session_state[_TRIAGE_RESULTS_KEY] = {}
    store = st.session_state[_TRIAGE_RESULTS_KEY]
    if device_id not in store:
        store[device_id] = []
    store[device_id].append({
        "title": action_title,
        "results": results,
        "timestamp": time.time(),
    })


def get_triage_results(device_id: str) -> list:
    """指定デバイスのトリアージ実行結果を取得する。

    Returns:
        list of dict: 各要素は {"title": str, "results": [...], "timestamp": float}
    """
    store = st.session_state.get(_TRIAGE_RESULTS_KEY, {})
    return store.get(device_id, [])


def format_triage_results_for_llm(device_id: str) -> str:
    """トリアージ実行結果をLLMプロンプト用テキストに整形する。

    AI復旧計画（ステップ③）生成時にプロンプトへ注入するためのフォーマッタ。
    結果が無い場合は空文字を返す。

    Returns:
        str: LLMプロンプトに埋め込むテキスト（空文字=結果なし）
    """
    entries = get_triage_results(device_id)
    if not entries:
        return ""

    lines = []
    for entry in entries:
        title = entry.get("title", "")
        lines.append(f"■ {title}")
        for r in entry.get("results", []):
            cmd = r.get("command", "")
            status = r.get("status", "")
            output = r.get("output", "")
            # 出力は最大10行に制限（プロンプト肥大化防止）
            output_lines = output.strip().split("\n")
            if len(output_lines) > 10:
                output_trimmed = "\n".join(output_lines[:10]) + "\n... (以下省略)"
            else:
                output_trimmed = output
            status_mark = "✅" if status == "success" else "❌"
            lines.append(f"  {status_mark} {cmd}")
            lines.append(f"    {output_trimmed}")
        lines.append("")

    return "\n".join(lines)
