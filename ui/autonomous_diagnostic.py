# ui/autonomous_diagnostic.py — AIエージェント自律診断オーケストレータ
#
# 設計方針:
#   「推論 → 実行（コマンド） → 再推論 → 最終報告」のループを回す。
#   RCA分析結果を受け取り、診断コマンドを自動生成・実行し、
#   その結果をもとに診断を深める。
#
# 構造:
#   [RCA結果] → plan_diagnostic_steps() → コマンドキュー生成
#   → execute_diagnostic_queue() → コマンド実行（シミュレーション）
#   → analyze_diagnostic_results() → 追加診断の判定 → 思考ログ蓄積
#
# 思考ログ:
#   各ステップの「なぜそのコマンドを選んだか」「結果から何が分かったか」を
#   時系列で蓄積し、UIに開示する。

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import streamlit as st

logger = logging.getLogger(__name__)

# 最大診断ラウンド数（無限ループ防止）
MAX_DIAGNOSTIC_ROUNDS = 3
# 1ラウンドあたりの最大コマンド数
MAX_COMMANDS_PER_ROUND = 5


# =====================================================
# データモデル
# =====================================================

@dataclass
class DiagnosticStep:
    """診断ステップ（思考ログの1エントリ）"""
    round_num: int                  # ラウンド番号（1〜）
    step_type: str                  # "plan" | "execute" | "analyze" | "conclude"
    timestamp: float                # タイムスタンプ
    description: str                # 何をしたか / 何を考えたか
    commands: List[str] = field(default_factory=list)      # 実行したコマンド
    results: List[Dict] = field(default_factory=list)      # コマンド実行結果
    insights: List[str] = field(default_factory=list)      # 得られた洞察
    next_action: str = ""           # 次のアクション（空なら終了）


@dataclass
class DiagnosticSession:
    """自律診断セッション"""
    device_id: str
    alarm_label: str
    scenario: str
    started_at: float = field(default_factory=time.time)
    steps: List[DiagnosticStep] = field(default_factory=list)
    is_complete: bool = False
    conclusion: str = ""
    current_round: int = 0


# =====================================================
# コマンド計画: アラーム内容に応じた診断コマンドの動的生成
# =====================================================

# アラームキーワード → 診断コマンドのマッピング
_DIAGNOSTIC_COMMAND_MAP = {
    # 接続性障害
    "link down": [
        ("show interfaces {intf}", "リンクダウンしたインターフェースの物理層状態を確認"),
        ("show ip interface brief", "全インターフェースのup/down状態を一覧確認"),
        ("show logging | include Interface", "インターフェース関連のログを時系列で確認"),
    ],
    "bgp": [
        ("show ip bgp summary", "BGPネイバーの状態と受信プレフィクス数を確認"),
        ("show ip bgp neighbors", "BGPネイバーの詳細状態とエラーカウンタを確認"),
        ("show ip route summary", "ルーティングテーブルの全体概要を確認"),
    ],
    "ospf": [
        ("show ip ospf neighbor", "OSPFネイバーの状態を確認"),
        ("show ip ospf interface", "OSPFが有効なインターフェースを確認"),
        ("show ip route ospf", "OSPF学習ルートを確認"),
    ],
    # ハードウェア障害
    "power": [
        ("show environment", "電源・ファン・温度の状態を確認"),
        ("show inventory", "搭載モジュールの一覧とステータスを確認"),
        ("show power detail", "電源モジュールの詳細出力を確認"),
    ],
    "temperature": [
        ("show environment temperature", "各コンポーネントの温度を確認"),
        ("show environment", "電源・ファン含むハードウェア全体状態を確認"),
        ("show processes cpu", "CPU使用率を確認（高温時の負荷相関）"),
    ],
    "fan": [
        ("show environment", "ファン・温度・電源の状態を確認"),
        ("show inventory", "搭載ファンモジュールの一覧を確認"),
    ],
    "memory": [
        ("show processes memory sorted", "プロセスごとのメモリ使用量を確認"),
        ("show memory statistics", "メモリプールの使用率を確認"),
        ("show processes cpu", "CPU使用率との相関を確認"),
    ],
    # 光学系
    "optical": [
        ("show interfaces transceiver detail", "光トランシーバーのRx/Txパワーを確認"),
        ("show interfaces counters errors", "インターフェースのエラーカウンタを確認"),
        ("show inventory", "トランシーバーモジュールの型番を確認"),
    ],
    "transceiver": [
        ("show interfaces transceiver detail", "光トランシーバーの受信パワーと閾値を確認"),
        ("show interfaces counters errors", "CRCエラー・入力エラーを確認"),
    ],
    # パフォーマンス
    "cpu": [
        ("show processes cpu sorted", "CPU使用率の高いプロセスを特定"),
        ("show processes cpu history", "CPU使用率の時系列推移を確認"),
        ("show memory statistics", "メモリ使用率との相関を確認"),
    ],
    "buffer": [
        ("show buffers", "バッファプールの使用状況を確認"),
        ("show interfaces counters errors", "パケットドロップの有無を確認"),
        ("show processes cpu", "CPU使用率との相関を確認"),
    ],
    "packet loss": [
        ("show interfaces counters errors", "全インターフェースのエラーカウンタを確認"),
        ("show ip interface brief", "インターフェースの状態を確認"),
        ("ping 8.8.8.8 repeat 10", "外部への疎通をパケットロス率で確認"),
    ],
    # トラフィック輻輳・帯域
    "utilization": [
        ("show interfaces", "全インターフェースのトラフィック統計・利用率を確認"),
        ("show interfaces counters rates", "インターフェースごとの入出力レートを確認"),
        ("show policy-map interface", "QoSポリシーマップの適用状況とドロップ数を確認"),
        ("show buffers", "バッファプールの使用状況・枯渇リスクを確認"),
    ],
    "congestion": [
        ("show interfaces", "インターフェースの利用率とエラーカウンタを確認"),
        ("show policy-map interface", "QoSポリシーのドロップ・キュー深度を確認"),
        ("show processes cpu", "パケット処理負荷によるCPU使用率を確認"),
        ("show buffers", "バッファ使用率と枯渇状況を確認"),
    ],
    "bandwidth": [
        ("show interfaces counters rates", "インターフェースごとの帯域使用率を確認"),
        ("show interfaces", "全インターフェースのトラフィック統計を確認"),
        ("show ip traffic", "プロトコル別のトラフィック統計を確認"),
    ],
    "traffic": [
        ("show interfaces counters rates", "トラフィックレートの確認"),
        ("show interfaces", "インターフェース統計の詳細確認"),
        ("show policy-map interface", "QoSポリシーとキュー統計を確認"),
    ],
    "qos": [
        ("show policy-map interface", "QoSポリシーマップの適用状況を確認"),
        ("show class-map", "トラフィック分類設定を確認"),
        ("show interfaces counters errors", "パケットドロップの有無を確認"),
    ],
    "output drops": [
        ("show interfaces", "出力ドロップが発生しているインターフェースを特定"),
        ("show policy-map interface", "QoSキューのドロップ統計を確認"),
        ("show buffers", "出力バッファの枯渇状態を確認"),
        ("show processes cpu", "パケット処理負荷を確認"),
    ],
    # サイレント障害（L2レベル）
    "silent": [
        ("show mac address-table", "MACアドレステーブルの異常（エントリ消失・フラッピング）を確認"),
        ("show spanning-tree", "STPトポロジー変更やブロッキングポートの有無を確認"),
        ("show interfaces counters errors", "CRC/入力エラーなどエラーカウンタの蓄積を確認"),
        ("show vlan brief", "VLAN設定の不整合や欠落を確認"),
    ],
    # フォールバック（汎用 — デバイスタイプ別フォールバックにも一致しない場合の最終手段）
    "_default": [
        ("show ip interface brief", "全インターフェースの状態を一覧確認"),
        ("show environment", "ハードウェア（電源・ファン・温度）の状態を確認"),
        ("show logging", "直近のシステムログを確認"),
        ("show processes cpu", "CPU使用率を確認"),
    ],
}

# デバイスタイプ → デフォルト診断コマンド（configs/device_types.json から取得）
# Google SRE "Four Golden Signals" に基づき、デバイス種別ごとに
# Latency/Traffic/Errors/Saturation の観点で標準コマンドを定義
from configs.device_registry import get_all_diagnostics as _get_all_diagnostics, get_diagnostics as _get_diagnostics

_DEVICE_TYPE_DIAGNOSTIC_MAP = _get_all_diagnostics()


def plan_diagnostic_commands(
    device_id: str,
    alarm_label: str,
    analysis_result: dict,
    round_num: int = 1,
    previous_insights: List[str] = None,
    device_type: str = "",
) -> List[Dict[str, str]]:
    """アラーム内容とRCA結果に基づき、次に実行すべき診断コマンドを計画する。

    Args:
        device_id: 対象デバイスID
        alarm_label: アラームラベル（テキスト）
        analysis_result: RCA分析結果の辞書
        round_num: 現在のラウンド番号
        previous_insights: 前ラウンドまでの洞察リスト
        device_type: デバイスタイプ（ROUTER/SWITCH/FIREWALL/ACCESS_POINT等）

    Returns:
        [{"command": str, "reason": str}, ...]
    """
    alarm_lower = alarm_label.lower()
    commands = []
    matched_categories = set()

    # アラームテキストからキーワードマッチで診断コマンドを選択
    for keyword, cmd_list in _DIAGNOSTIC_COMMAND_MAP.items():
        if keyword == "_default":
            continue
        if keyword in alarm_lower:
            matched_categories.add(keyword)
            for cmd_template, reason in cmd_list:
                cmd = cmd_template.replace("{intf}", "GigabitEthernet0/0/0")
                commands.append({"command": cmd, "reason": reason})

    # RCA結果のtypeからも補完
    cand_type = analysis_result.get("type", "").lower()
    for keyword in ["power", "temperature", "optical", "memory", "cpu"]:
        if keyword in cand_type and keyword not in matched_categories:
            for cmd_template, reason in _DIAGNOSTIC_COMMAND_MAP.get(keyword, []):
                cmd = cmd_template.replace("{intf}", "GigabitEthernet0/0/0")
                commands.append({"command": cmd, "reason": reason})

    # キーワード不一致 → デバイスタイプ別フォールバック → 最終デフォルト
    if not commands:
        _dt_upper = device_type.upper()
        _dt_cmds = _DEVICE_TYPE_DIAGNOSTIC_MAP.get(_dt_upper)
        if _dt_cmds:
            logger.info(
                f"キーワード不一致 → デバイスタイプ '{_dt_upper}' のデフォルトコマンドを使用: {device_id}"
            )
            for cmd_template, reason in _dt_cmds:
                cmd = cmd_template.replace("{intf}", "GigabitEthernet0/0/0")
                commands.append({"command": cmd, "reason": reason})
        else:
            for cmd_template, reason in _DIAGNOSTIC_COMMAND_MAP["_default"]:
                cmd = cmd_template.replace("{intf}", "GigabitEthernet0/0/0")
                commands.append({"command": cmd, "reason": reason})

    # ラウンド2以降: 前ラウンドの洞察に基づく追加コマンド
    if round_num >= 2 and previous_insights:
        insights_text = " ".join(previous_insights).lower()
        if "error" in insights_text or "エラー" in insights_text:
            commands.append({
                "command": "show logging | include Error",
                "reason": "前回の調査でエラー兆候を検知 → エラーログの詳細を確認",
            })
        if "down" in insights_text or "ダウン" in insights_text:
            commands.append({
                "command": "show interfaces status",
                "reason": "前回の調査でダウン状態を検知 → 全ポートの状態一覧を確認",
            })

    # 重複排除 & 上限
    seen = set()
    unique_commands = []
    for c in commands:
        if c["command"] not in seen:
            seen.add(c["command"])
            unique_commands.append(c)
    return unique_commands[:MAX_COMMANDS_PER_ROUND]


# =====================================================
# コマンド実行 & 結果分析
# =====================================================

def execute_diagnostic_commands(
    commands: List[Dict[str, str]],
    device_id: str,
) -> List[Dict]:
    """診断コマンドを実行し、結果を返す。

    デモ環境では simulate_command_execution を使用。
    本番環境では SSH executor に差し替え可能。
    """
    from ui.components.command_popup import simulate_command_execution

    results = []
    for cmd_info in commands:
        cmd = cmd_info["command"]
        result = simulate_command_execution(cmd, device_id)
        result["reason"] = cmd_info["reason"]
        results.append(result)

    return results


def analyze_command_results(
    results: List[Dict],
    alarm_label: str,
    round_num: int,
) -> List[str]:
    """コマンド実行結果を解析し、洞察を抽出する。

    パターンマッチによる軽量な結果解析（LLM不要）。
    """
    insights = []

    for r in results:
        output = r.get("output", "").lower()
        cmd = r.get("command", "")

        # インターフェース状態
        if "down down" in output:
            insights.append(f"⚠ {cmd}: インターフェースがダウン状態を検出")
        elif "up up" in output and "interface" in cmd.lower():
            insights.append(f"✅ {cmd}: インターフェースは正常稼働中")

        # ハードウェア
        if "fail" in output or "critical" in output:
            insights.append(f"🔴 {cmd}: ハードウェア異常（FAIL/CRITICAL）を検出")
        elif "warning" in output:
            insights.append(f"🟡 {cmd}: 警告レベルの異常を検出")
        elif "normal" in output and "environment" in cmd.lower():
            insights.append(f"✅ {cmd}: ハードウェア状態は正常")

        # パケットロス
        if "success rate is 100 percent" in output:
            insights.append(f"✅ {cmd}: パケットロスなし（100%到達）")
        elif "success rate is 0 percent" in output:
            insights.append(f"🔴 {cmd}: 完全疎通不可（0%到達）")
        elif "success rate is" in output:
            insights.append(f"🟡 {cmd}: 部分的パケットロスを検出")

        # 光トランシーバー
        if "rx power" in output and ("warning" in output or "low" in output):
            insights.append(f"🟡 {cmd}: 光受信パワーの劣化を検出")

        # CPU/メモリ
        if "cpu" in cmd.lower() and any(w in output for w in ["high", "99%", "98%", "97%"]):
            insights.append(f"🔴 {cmd}: CPU高負荷を検出")

        # サイレント障害（L2レベル）
        if "mac address-table" in cmd.lower() and "flapping" in output:
            insights.append(f"🔴 {cmd}: MACアドレスフラッピングを検出 — L2ループまたはポート不安定の疑い")
        if "spanning-tree" in cmd.lower():
            if "topology change" in output.lower():
                insights.append(f"🔴 {cmd}: STPトポロジー変更を検出 — ネットワーク不安定の疑い")
            if " blk " in output.lower():
                insights.append(f"🟡 {cmd}: ブロッキングポートを検出 — 通信経路の遮断の可能性")
        if "counters errors" in cmd.lower():
            if any(w in output for w in ["crc", "input errors", "output errors"]):
                insights.append(f"🟡 {cmd}: インターフェースエラーカウンタの蓄積を検出")
        if "vlan" in cmd.lower() and "no active uplink" in output.lower():
            insights.append(f"🔴 {cmd}: アクティブなアップリンクが存在しないVLANを検出 — 通信断の原因")

    if not insights:
        insights.append(f"調査{round_num}回目: 明確な異常パターンは検出されませんでした")

    return insights


def should_continue_diagnosis(
    insights: List[str],
    round_num: int,
) -> bool:
    """追加診断が必要かどうかを判定する。"""
    if round_num >= MAX_DIAGNOSTIC_ROUNDS:
        return False

    # 警告レベル以上の異常が見つかった場合は深掘り
    has_warning = any("🟡" in i or "⚠" in i for i in insights)
    has_critical = any("🔴" in i for i in insights)

    # CRITICAL は追加調査の余地あり、WARNING は1回だけ深掘り
    if has_critical and round_num < 2:
        return True
    if has_warning and round_num < 2:
        return True

    return False


# =====================================================
# 自律診断セッション管理
# =====================================================

_SESSION_KEY = "_autonomous_diag_sessions"


def _get_sessions() -> Dict[str, DiagnosticSession]:
    """セッションストアを取得"""
    if _SESSION_KEY not in st.session_state:
        st.session_state[_SESSION_KEY] = {}
    return st.session_state[_SESSION_KEY]


def get_diagnostic_session(device_id: str) -> Optional[DiagnosticSession]:
    """デバイスの診断セッションを取得"""
    return _get_sessions().get(device_id)


def run_autonomous_diagnostic(
    device_id: str,
    alarm_label: str,
    scenario: str,
    analysis_result: dict,
    device_type: str = "",
) -> DiagnosticSession:
    """自律診断を実行する。

    RCA結果を受け取り、「コマンド計画 → 実行 → 分析」のループを回す。
    各ステップは思考ログとして蓄積される。

    Args:
        device_id: 対象デバイスID
        alarm_label: アラームラベル
        scenario: シナリオ名
        analysis_result: RCA分析結果
        device_type: デバイスタイプ（ROUTER/SWITCH/FIREWALL/ACCESS_POINT等）

    Returns:
        DiagnosticSession: 完了した診断セッション
    """
    sessions = _get_sessions()
    session = DiagnosticSession(
        device_id=device_id,
        alarm_label=alarm_label,
        scenario=scenario,
    )

    all_insights = []

    for round_num in range(1, MAX_DIAGNOSTIC_ROUNDS + 1):
        session.current_round = round_num

        # ── Step 1: コマンド計画 ──
        commands = plan_diagnostic_commands(
            device_id=device_id,
            alarm_label=alarm_label,
            analysis_result=analysis_result,
            round_num=round_num,
            previous_insights=all_insights,
            device_type=device_type,
        )

        plan_step = DiagnosticStep(
            round_num=round_num,
            step_type="plan",
            timestamp=time.time(),
            description=f"調査{round_num}回目: アラーム「{alarm_label[:60]}」に基づき{len(commands)}個の診断コマンドを計画",
            commands=[c["command"] for c in commands],
        )
        session.steps.append(plan_step)

        if not commands:
            break

        # ── Step 2: コマンド実行 ──
        results = execute_diagnostic_commands(commands, device_id)

        exec_step = DiagnosticStep(
            round_num=round_num,
            step_type="execute",
            timestamp=time.time(),
            description=f"調査{round_num}回目: {len(results)}個のコマンドを実行完了",
            commands=[c["command"] for c in commands],
            results=results,
        )
        session.steps.append(exec_step)

        # ── Step 3: 結果分析 ──
        insights = analyze_command_results(results, alarm_label, round_num)
        all_insights.extend(insights)

        analysis_step = DiagnosticStep(
            round_num=round_num,
            step_type="analyze",
            timestamp=time.time(),
            description=f"調査{round_num}回目: コマンド結果を分析し{len(insights)}個の所見を取得",
            insights=insights,
        )

        # ── Step 4: 継続判定 ──
        if should_continue_diagnosis(insights, round_num):
            analysis_step.next_action = "追加診断が必要 → 次の調査へ"
        else:
            analysis_step.next_action = "診断完了"

        session.steps.append(analysis_step)

        if not should_continue_diagnosis(insights, round_num):
            break

    # ── 最終結論 ──
    conclusion_parts = []
    has_critical = any("🔴" in i for i in all_insights)
    has_warning = any("🟡" in i or "⚠" in i for i in all_insights)
    has_normal = any("✅" in i for i in all_insights)
    # サイレント障害判定: アラームラベルに "silent" を含む場合、
    # ハードウェア正常だけで「正常」と結論してはならない
    _is_silent = "silent" in alarm_label.lower()

    if has_critical:
        if _is_silent:
            conclusion_parts.append(
                "サイレント障害の兆候を検出しました。L2レベル（MAC/STP/VLAN）に異常があり、"
                "配下デバイスへの通信影響が発生している可能性があります。即座の対応を推奨します。"
            )
        else:
            conclusion_parts.append("重大な異常を検出しました。即座の対応が必要です。")
    elif has_warning:
        if _is_silent:
            conclusion_parts.append(
                "サイレント障害の疑いがあります。ハードウェアは正常ですが、"
                "L2レベルで警告兆候を検出しました。詳細調査を推奨します。"
            )
        else:
            conclusion_parts.append("警告レベルの異常を検出しました。経過観察または予防的対処を推奨します。")
    elif _is_silent:
        # サイレント障害アラームが出ているのに全て正常 → 潜在的リスクを警告
        conclusion_parts.append(
            "サイレント障害の疑いが報告されています。CLIレベルでは明確な異常を検出できませんでしたが、"
            "配下デバイスとの疎通確認およびトラフィック監視を推奨します。"
        )
    elif has_normal:
        conclusion_parts.append("主要指標は正常範囲内です。引き続き監視を継続してください。")
    else:
        conclusion_parts.append("診断を完了しました。明確な異常は検出されませんでした。")

    session.conclusion = " ".join(conclusion_parts)
    session.is_complete = True

    conclude_step = DiagnosticStep(
        round_num=session.current_round,
        step_type="conclude",
        timestamp=time.time(),
        description=session.conclusion,
        insights=all_insights,
    )
    session.steps.append(conclude_step)

    # セッション保存
    sessions[device_id] = session
    return session


# =====================================================
# UI描画: 自律診断パネル
# =====================================================

def render_autonomous_diagnostic_panel(
    selected_candidate: Optional[dict],
    topology: dict,
    scenario: str,
):
    """自律診断パネルを描画する。

    cockpit.py から呼ばれ、選択された根本原因候補に対して
    自律診断の実行・思考ログの表示を行う。
    """
    if not selected_candidate:
        return

    device_id = selected_candidate.get("id", "")
    if device_id == "SYSTEM":
        return

    alarm_label = selected_candidate.get("label", "")

    # 既存セッションの確認
    session = get_diagnostic_session(device_id)
    session_cache_key = f"_autodiag_{device_id}_{hashlib.md5(alarm_label[:200].encode()).hexdigest()}"

    with st.expander("🤖 AI自律診断", expanded=session is not None):
        if session and session.is_complete:
            # ── 完了済みセッションの表示 ──
            _render_thought_log(session)

            col1, col2 = st.columns([1, 1])
            with col1:
                if st.button("🔄 再診断", key=f"rediag_{device_id}"):
                    _get_sessions().pop(device_id, None)
                    st.rerun()
        else:
            # ── 診断実行 ──
            st.caption(
                "AIエージェントがアラーム内容を分析し、診断コマンドを自動で計画・実行・解析します。"
                "各確認項目の思考プロセスが時系列で可視化されます。"
            )

            _diag_btn_key = f"start_autodiag_{device_id}"
            if st.button(
                "▶ 自律診断を開始",
                key=_diag_btn_key,
                type="primary",
            ):
                # トポロジーからデバイスタイプを取得（メモリ参照のみ、コスト0）
                _node = topology.get(device_id)
                _dev_type = ""
                if _node:
                    _dev_type = getattr(_node, "type", "") or (
                        _node.get("type", "") if isinstance(_node, dict) else ""
                    )
                with st.spinner("🤖 AI自律診断を実行中..."):
                    session = run_autonomous_diagnostic(
                        device_id=device_id,
                        alarm_label=alarm_label,
                        scenario=scenario,
                        analysis_result=selected_candidate,
                        device_type=_dev_type,
                    )

                st.rerun()


def _build_situation_briefing(
    session: DiagnosticSession,
    crit_insights: list,
    warn_insights: list,
    ok_insights: list,
) -> list:
    """検出結果から運用者向けの状況ブリーフィングを組み立てる。

    個別の検出項目を集約し、「今どういう状況か」「なぜ次のステップが必要か」を
    平易な日本語で伝える文章を返す。
    """
    lines = []

    # ── 検出パターンの集約 ──
    all_insights = crit_insights + warn_insights
    _patterns = {
        "hw_fail": False,       # ハードウェア異常
        "intf_down": False,     # インターフェースダウン
        "packet_loss": False,   # パケットロス
        "optical_degrade": False,  # 光レベル劣化
        "cpu_high": False,      # CPU高負荷
        "mac_flap": False,      # MACフラッピング
        "stp_change": False,    # STPトポロジー変更
        "stp_block": False,     # STPブロッキング
        "error_counter": False, # エラーカウンタ
        "vlan_no_uplink": False,  # VLANアップリンク断
        "full_loss": False,     # 完全疎通不可
    }

    for ins in all_insights:
        ins_lower = ins.lower()
        if "ハードウェア異常" in ins or "fail" in ins_lower or "critical" in ins_lower:
            _patterns["hw_fail"] = True
        if "ダウン状態" in ins:
            _patterns["intf_down"] = True
        if "部分的パケットロス" in ins:
            _patterns["packet_loss"] = True
        if "完全疎通不可" in ins or "0%到達" in ins:
            _patterns["full_loss"] = True
        if "光受信パワー" in ins:
            _patterns["optical_degrade"] = True
        if "cpu高負荷" in ins_lower:
            _patterns["cpu_high"] = True
        if "macアドレスフラッピング" in ins_lower:
            _patterns["mac_flap"] = True
        if "stpトポロジー変更" in ins_lower:
            _patterns["stp_change"] = True
        if "ブロッキングポート" in ins:
            _patterns["stp_block"] = True
        if "エラーカウンタ" in ins:
            _patterns["error_counter"] = True
        if "アップリンクが存在しない" in ins:
            _patterns["vlan_no_uplink"] = True

    # ── 状況の組み立て ──
    if _patterns["hw_fail"]:
        lines.append("• 対象機器で<b>ハードウェアの異常</b>が検出されています。電源・ファン・温度等に問題がある可能性があります。")

    if _patterns["intf_down"]:
        lines.append("• 一部の<b>インターフェースがダウン</b>しており、通信に影響が出ている可能性があります。")

    if _patterns["full_loss"]:
        lines.append("• 疎通確認で<b>完全に通信不可</b>の状態です。早急な復旧が必要です。")
    elif _patterns["packet_loss"]:
        lines.append("• <b>部分的なパケットロス</b>が発生しています。通信品質が低下しています。")

    if _patterns["optical_degrade"]:
        lines.append("• <b>光トランシーバーの受信パワーが低下</b>しています。ファイバーまたはモジュールの劣化が疑われます。")

    if _patterns["cpu_high"]:
        lines.append("• <b>CPUが高負荷</b>の状態です。処理遅延やパケットドロップの原因になっている可能性があります。")

    if _patterns["mac_flap"]:
        lines.append("• <b>MACアドレスフラッピング</b>を検出しました。L2ループが発生している可能性があります。")

    if _patterns["stp_change"]:
        lines.append("• <b>STPのトポロジー変更</b>が発生しています。ネットワーク全体で経路再計算が行われ、通信が不安定になっています。")

    if _patterns["stp_block"]:
        lines.append("• <b>STPブロッキングポート</b>を検出しました。通信経路が遮断されている可能性があります。")

    if _patterns["error_counter"]:
        lines.append("• <b>インターフェースのエラーカウンタが蓄積</b>しています。物理層（ケーブル・コネクタ）の問題が疑われます。")

    if _patterns["vlan_no_uplink"]:
        lines.append("• <b>アクティブなアップリンクが存在しないVLAN</b>があります。該当VLANのユーザーは通信できない状態です。")

    # ── 正常項目のサマリ ──
    if ok_insights and not lines:
        lines.append(f"• 主要な{len(ok_insights)}項目を確認しましたが、<b>現時点で明確な異常は検出されていません</b>。")
        lines.append("• 引き続き監視を継続し、状態変化があれば再診断を実施してください。")
        return lines

    if ok_insights:
        lines.append(f"• 一方、{len(ok_insights)}項目は正常を確認しています。")

    # ── 次ステップへの誘導 ──
    if crit_insights:
        lines.append(
            "→ <b>次のステップ「③ 確認手順書」</b>で、この状況に対する"
            "具体的な確認手順と対処方法を確認してください。"
        )
    elif warn_insights:
        lines.append(
            "→ <b>次のステップ「③ 確認手順書」</b>で、警告項目の詳細な確認方法と"
            "予防的な対処方法を確認することを推奨します。"
        )

    return lines


def _render_thought_log(session: DiagnosticSession):
    """思考ログを運用者向けに表示する。

    従来の技術者向けログ（R1計画/R1実行/...）を、
    運用者が「今何が起きていて、何をすべきか」を直感的に理解できる
    プログレス形式に変換して表示する。
    """
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 1. 診断結論（最も重要な情報を最上部に）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    if session.conclusion:
        _is_silent = "サイレント障害" in session.conclusion
        has_critical = "重大" in session.conclusion or "サイレント障害の兆候" in session.conclusion
        has_warning = "警告" in session.conclusion or "サイレント障害の疑い" in session.conclusion

        if has_critical:
            st.error(f"🔴 **診断結論**: {session.conclusion}")
        elif has_warning:
            st.warning(f"🟡 **診断結論**: {session.conclusion}")
        elif _is_silent:
            st.warning(f"🟣 **診断結論**: {session.conclusion}")
        else:
            st.success(f"✅ **診断結論**: {session.conclusion}")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 2. 現在の状況ブリーフィング（検出結果を平易に要約）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    all_commands = []
    all_insights_ok = []
    all_insights_warn = []
    all_insights_crit = []
    for step in session.steps:
        if step.commands:
            all_commands.extend(step.commands)
        for ins in (step.insights or []):
            if "🔴" in ins:
                all_insights_crit.append(ins)
            elif "🟡" in ins or "⚠" in ins:
                all_insights_warn.append(ins)
            elif "✅" in ins:
                all_insights_ok.append(ins)

    _situation_lines = _build_situation_briefing(
        session, all_insights_crit, all_insights_warn, all_insights_ok
    )
    if _situation_lines:
        _situation_color = "#D32F2F" if all_insights_crit else "#FF8F00" if all_insights_warn else "#2E7D32"
        _situation_bg = "#FFF3F3" if all_insights_crit else "#FFF8E1" if all_insights_warn else "#F1F8E9"
        st.markdown(
            f'<div style="background:{_situation_bg};border-left:4px solid {_situation_color};'
            f'padding:10px 14px;border-radius:4px;margin:8px 0;font-size:13px;line-height:1.7;">'
            f'<b>📌 現在の状況:</b><br>'
            + "<br>".join(_situation_lines)
            + '</div>',
            unsafe_allow_html=True,
        )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 3. 診断サマリ（何を調べて何がわかったか）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    st.markdown(
        f'<div style="background:#f8f9fa;padding:10px 14px;border-radius:8px;'
        f'margin:8px 0;font-size:13px;">'
        f'<b>対象:</b> {session.device_id} &nbsp;|&nbsp; '
        f'<b>実行コマンド:</b> {len(all_commands)}件 &nbsp;|&nbsp; '
        f'<b>検出:</b> '
        f'<span style="color:#D32F2F;">異常 {len(all_insights_crit)}</span> / '
        f'<span style="color:#FF8F00;">警告 {len(all_insights_warn)}</span> / '
        f'<span style="color:#2E7D32;">正常 {len(all_insights_ok)}</span>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 4. 検出結果（異常・警告を先に、正常は折りたたみ）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    if all_insights_crit or all_insights_warn:
        st.markdown("**検出された問題:**")
        for ins in all_insights_crit:
            st.markdown(f"<div style='font-size:13px;padding:2px 0;'>{ins}</div>",
                        unsafe_allow_html=True)
        for ins in all_insights_warn:
            st.markdown(f"<div style='font-size:13px;padding:2px 0;'>{ins}</div>",
                        unsafe_allow_html=True)

    if all_insights_ok:
        with st.expander(f"✅ 正常確認済み ({len(all_insights_ok)}件)", expanded=False):
            for ins in all_insights_ok:
                st.markdown(f"<div style='font-size:13px;padding:2px 0;'>{ins}</div>",
                            unsafe_allow_html=True)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 5. 診断プロセス詳細（折りたたみ — 詳しく見たい人向け）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    with st.expander(
        f"🔬 診断プロセス詳細（{session.current_round}回目の調査 / 確認項目{len(session.steps)}件）",
        expanded=False,
    ):
        for step in session.steps:
            _render_step(step)


def _render_step(step: DiagnosticStep):
    """1ステップを描画する（詳細ビュー内）。"""
    # 運用者向けラベル（技術ラベルから変換）
    _type_config = {
        "plan":     ("🧠", "調査項目の選定", "#1565C0",
                     "アラーム内容から、確認すべきコマンドを選定しました"),
        "execute":  ("⚡", "コマンド実行", "#FF8F00",
                     "選定したコマンドを機器に実行しました"),
        "analyze":  ("🔍", "結果の読み取り", "#6A1B9A",
                     "実行結果から異常パターンを検出しました"),
        "conclude": ("📋", "判定", "#2E7D32",
                     "全ての結果を総合して判定しました"),
    }
    icon, label, color, guide = _type_config.get(
        step.step_type, ("❓", "不明", "#666", "")
    )

    # ヘッダー（運用者向けラベル）
    st.markdown(
        f'<div style="border-left:3px solid {color};padding:4px 12px;margin:6px 0;">'
        f'<span style="font-size:13px;font-weight:600;color:{color};">'
        f'{icon} {label}</span>'
        f'<span style="font-size:12px;color:#888;margin-left:8px;">'
        f'調査{step.round_num}回目: {step.description}</span>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # コマンドリスト（何のために実行するか reason 付き）
    if step.commands:
        cmd_text = "\n".join(f"  $ {c}" for c in step.commands)
        st.code(cmd_text, language="bash")

    # コマンド実行結果
    if step.results:
        with st.expander(f"📊 実行結果 ({len(step.results)}件)", expanded=False):
            for r in step.results:
                reason = r.get("reason", "")
                if reason:
                    st.caption(f"💡 **目的:** {reason}")
                st.code(r.get("output", ""), language="text")

    # 洞察
    if step.insights:
        for insight in step.insights:
            st.markdown(f"<div style='font-size:13px;padding:2px 0;'>{insight}</div>",
                        unsafe_allow_html=True)

    # 次アクション
    if step.next_action:
        st.caption(f"→ {step.next_action}")


def get_thought_log_for_llm(device_id: str) -> str:
    """思考ログをLLMプロンプト用テキストに整形する。

    analyst_report や chat_panel から呼ばれ、
    AIの診断プロセスをコンテキストとしてLLMに渡す。
    """
    session = get_diagnostic_session(device_id)
    if not session or not session.steps:
        return ""

    lines = [f"■ AI診断ログ ({session.device_id})"]
    for step in session.steps:
        _type_labels = {
            "plan": "調査項目の選定", "execute": "コマンド実行",
            "analyze": "結果の読み取り", "conclude": "判定",
        }
        label = _type_labels.get(step.step_type, step.step_type)
        lines.append(f"  [{label}] 調査{step.round_num}回目: {step.description}")

        if step.commands:
            for c in step.commands:
                lines.append(f"    $ {c}")

        if step.insights:
            for i in step.insights:
                lines.append(f"    {i}")

        if step.results:
            for r in step.results:
                output = r.get("output", "").strip()
                # 出力は最大5行に制限
                output_lines = output.split("\n")
                if len(output_lines) > 5:
                    output = "\n".join(output_lines[:5]) + "\n... (省略)"
                lines.append(f"    [{r.get('command', '')}]\n    {output}")

    if session.conclusion:
        lines.append(f"  [結論] {session.conclusion}")

    return "\n".join(lines)
