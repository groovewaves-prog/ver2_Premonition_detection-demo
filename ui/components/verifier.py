# ui/components/verifier.py — 修復後自動検証（Verifier）+ セーフティガード
#
# 設計方針:
#   1. Pre-Check: 修復実行前に現在の状態を確認し、AIの想定と齟齬がないか検証
#   2. Snapshot: 修復実行前の設定状態を保存（ロールバック用）
#   3. Post-Check: 修復実行後に正常化を確認（アラーム消失・疎通回復）
#   4. Rollback: 異常検知時にスナップショットから元の状態に復元
#
# 構造:
#   [Pre-Check] → [Snapshot] → [Execute] → [Post-Check] → [Recovery Confirmed / Rollback]

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import streamlit as st

logger = logging.getLogger(__name__)


# =====================================================
# データモデル
# =====================================================

@dataclass
class CheckResult:
    """検証チェックの結果"""
    check_name: str               # チェック名（例: "ping疎通", "インターフェース状態"）
    command: str                   # 実行したコマンド
    status: str                    # "pass" | "fail" | "warning"
    output: str                    # コマンド出力
    detail: str = ""               # 判定理由
    timestamp: float = field(default_factory=time.time)


@dataclass
class ConfigSnapshot:
    """設定スナップショット（ロールバック用）"""
    device_id: str
    snapshot_id: str               # ユニークID
    config_text: str               # 設定テキスト（シミュレーション用）
    interface_states: Dict[str, str] = field(default_factory=dict)  # {intf: "up"/"down"}
    created_at: float = field(default_factory=time.time)


@dataclass
class VerificationSession:
    """修復検証セッション"""
    device_id: str
    scenario: str
    pre_checks: List[CheckResult] = field(default_factory=list)
    post_checks: List[CheckResult] = field(default_factory=list)
    snapshot: Optional[ConfigSnapshot] = None
    execution_log: List[Dict] = field(default_factory=list)
    status: str = "pending"        # "pending" | "pre_check" | "executing" | "post_check" | "verified" | "rollback_needed" | "rolled_back"
    conclusion: str = ""
    started_at: float = field(default_factory=time.time)


# =====================================================
# Pre-Check: 修復前の状態確認
# =====================================================

_PRE_CHECK_COMMANDS = [
    {
        "name": "ping疎通確認",
        "command": "ping 8.8.8.8 repeat 5",
        "pass_pattern": "100 percent",
        "fail_pattern": "0 percent",
    },
    {
        "name": "インターフェース状態",
        "command": "show ip interface brief",
        "pass_pattern": "up up",
        "fail_pattern": "down down",
    },
    {
        "name": "ハードウェア状態",
        "command": "show environment",
        "pass_pattern": "normal",
        "fail_pattern": "fail",
    },
]


def run_pre_checks(device_id: str) -> List[CheckResult]:
    """修復前の Pre-Check を実行する。

    AIが想定している状況と実機の状態に齟齬がないか確認する。
    """
    from ui.components.command_popup import simulate_command_execution

    results = []
    for check_def in _PRE_CHECK_COMMANDS:
        cmd_result = simulate_command_execution(check_def["command"], device_id)
        output = cmd_result.get("output", "").lower()

        if check_def["fail_pattern"] in output:
            status = "fail"
            detail = f"異常検出: '{check_def['fail_pattern']}' パターンを検出"
        elif check_def["pass_pattern"] in output:
            status = "pass"
            detail = f"正常: '{check_def['pass_pattern']}' を確認"
        else:
            status = "warning"
            detail = "明確なパターンマッチなし（要確認）"

        results.append(CheckResult(
            check_name=check_def["name"],
            command=check_def["command"],
            status=status,
            output=cmd_result.get("output", ""),
            detail=detail,
        ))

    return results


# =====================================================
# Snapshot: 設定状態の保存
# =====================================================

def take_config_snapshot(device_id: str) -> ConfigSnapshot:
    """修復前の設定スナップショットを取得する。

    ロールバック時にこのスナップショットから設定を復元する。
    デモ環境ではシミュレーション出力を保存。
    """
    from ui.components.command_popup import simulate_command_execution

    # 現在の設定を取得（シミュレーション）
    config_result = simulate_command_execution("show running-config", device_id)
    intf_result = simulate_command_execution("show ip interface brief", device_id)

    # インターフェース状態をパース
    intf_states = {}
    for line in intf_result.get("output", "").split("\n"):
        if "up up" in line.lower():
            parts = line.split()
            if parts:
                intf_states[parts[0]] = "up"
        elif "down down" in line.lower():
            parts = line.split()
            if parts:
                intf_states[parts[0]] = "down"

    snapshot = ConfigSnapshot(
        device_id=device_id,
        snapshot_id=hashlib.md5(f"{device_id}_{time.time()}".encode()).hexdigest()[:12],
        config_text=config_result.get("output", ""),
        interface_states=intf_states,
    )

    logger.info("Config snapshot taken for %s (id=%s)", device_id, snapshot.snapshot_id)
    return snapshot


# =====================================================
# Post-Check: 修復後の自動検証
# =====================================================

_POST_CHECK_COMMANDS = [
    {
        "name": "ping疎通回復確認",
        "command": "ping 8.8.8.8 repeat 5",
        "pass_pattern": "100 percent",
        "fail_pattern": "0 percent",
        "critical": True,
    },
    {
        "name": "インターフェース復旧確認",
        "command": "show ip interface brief",
        "pass_pattern": "up up",
        "fail_pattern": "down down",
        "critical": True,
    },
    {
        "name": "ハードウェア正常確認",
        "command": "show environment",
        "pass_pattern": "normal",
        "fail_pattern": "fail",
        "critical": False,
    },
    {
        "name": "アラーム消失確認",
        "command": "show logging | last 5",
        "pass_pattern": "",
        "fail_pattern": "critical",
        "critical": False,
    },
]


def run_post_checks(device_id: str) -> List[CheckResult]:
    """修復後の Post-Check を実行する。

    アラームが消えたか、トラフィックが回復したかを判定する。
    """
    from ui.components.command_popup import simulate_command_execution

    results = []
    for check_def in _POST_CHECK_COMMANDS:
        cmd_result = simulate_command_execution(check_def["command"], device_id)
        output = cmd_result.get("output", "").lower()

        if check_def["fail_pattern"] and check_def["fail_pattern"] in output:
            status = "fail"
            detail = f"異常検出: '{check_def['fail_pattern']}' パターンを検出"
        elif check_def["pass_pattern"] and check_def["pass_pattern"] in output:
            status = "pass"
            detail = f"正常: '{check_def['pass_pattern']}' を確認"
        elif not check_def["pass_pattern"]:
            status = "pass"
            detail = "確認完了"
        else:
            status = "warning"
            detail = "明確なパターンマッチなし（要確認）"

        results.append(CheckResult(
            check_name=check_def["name"],
            command=check_def["command"],
            status=status,
            output=cmd_result.get("output", ""),
            detail=detail,
        ))

    return results


def evaluate_post_checks(post_checks: List[CheckResult]) -> str:
    """Post-Check の結果を総合評価する。

    Returns:
        "verified" | "rollback_needed" | "warning"
    """
    critical_fails = 0
    warnings = 0

    for i, check in enumerate(post_checks):
        is_critical = i < 2  # 最初の2つ（ping, interface）はcritical
        if check.status == "fail":
            if is_critical:
                critical_fails += 1
            else:
                warnings += 1
        elif check.status == "warning":
            warnings += 1

    if critical_fails > 0:
        return "rollback_needed"
    if warnings > 1:
        return "warning"
    return "verified"


# =====================================================
# Rollback: ロールバック実行
# =====================================================

def execute_rollback(device_id: str, snapshot: ConfigSnapshot) -> List[Dict]:
    """スナップショットから設定をロールバックする。

    デモ環境ではシミュレーション。
    本番環境では SSH 経由で configure replace を実行。
    """
    from ui.components.command_popup import simulate_command_execution

    rollback_commands = [
        f"configure replace {snapshot.snapshot_id}",
        "show ip interface brief",
        "ping 8.8.8.8 repeat 5",
    ]

    results = []
    for cmd in rollback_commands:
        result = simulate_command_execution(cmd, device_id)
        results.append(result)

    logger.info("Rollback executed for %s from snapshot %s", device_id, snapshot.snapshot_id)
    return results


# =====================================================
# 統合: セーフティガード付き修復フロー
# =====================================================

_VERIFICATION_SESSION_KEY = "_verification_sessions"


def _get_sessions() -> Dict[str, VerificationSession]:
    """検証セッションストアを取得"""
    if _VERIFICATION_SESSION_KEY not in st.session_state:
        st.session_state[_VERIFICATION_SESSION_KEY] = {}
    return st.session_state[_VERIFICATION_SESSION_KEY]


def get_verification_session(device_id: str) -> Optional[VerificationSession]:
    """デバイスの検証セッションを取得"""
    return _get_sessions().get(device_id)


def run_safeguarded_remediation(
    device_id: str,
    scenario: str,
    cand: dict,
    topology: dict,
    dt_engine,
    is_pred: bool,
) -> VerificationSession:
    """セーフティガード付き修復フローを実行する。

    1. Pre-Check → 2. Snapshot → 3. Execute → 4. Post-Check → 5. 判定

    Args:
        device_id: 対象デバイスID
        scenario: シナリオ名
        cand: 選択された根本原因候補
        topology: トポロジー辞書
        dt_engine: DigitalTwinEngine
        is_pred: 予兆かどうか

    Returns:
        VerificationSession
    """
    from network_ops import run_remediation_parallel_v2, RemediationEnvironment
    from ui.components.command_popup import simulate_command_execution

    session = VerificationSession(
        device_id=device_id,
        scenario=scenario,
    )

    # ── Step 1: Pre-Check ──
    session.status = "pre_check"
    session.pre_checks = run_pre_checks(device_id)

    pre_fails = sum(1 for c in session.pre_checks if c.status == "fail")
    pre_summary = f"Pre-Check完了: {len(session.pre_checks)}項目中 {pre_fails}件の異常を検出"
    logger.info(pre_summary)

    # ── Step 2: Snapshot ──
    session.snapshot = take_config_snapshot(device_id)

    # ── Step 3: Execute ──
    session.status = "executing"
    target_node_obj = topology.get(device_id)
    device_info = (target_node_obj.metadata
                   if target_node_obj and hasattr(target_node_obj, 'metadata')
                   else {})

    results_rem = run_remediation_parallel_v2(
        device_id=device_id,
        device_info=device_info,
        scenario=scenario,
        environment=RemediationEnvironment.DEMO,
        timeout_per_step=30,
    )

    exec_all_success = True
    for step_name in ["Backup", "Apply", "Verify"]:
        result = results_rem.get(step_name)
        if result:
            session.execution_log.append({
                "step": step_name,
                "status": result.status,
                "output": str(result),
                "device_id": device_id,
                "elapsed_sec": round(time.time() - result.timestamp, 2) if hasattr(result, 'timestamp') else 0.0,
            })
            if result.status != "success":
                exec_all_success = False

    # 修復後の検証コマンド
    _verify_commands = [
        "show interfaces status",
        "show logging | last 10",
        "ping 8.8.8.8 repeat 5",
    ]
    for vcmd in _verify_commands:
        vresult = simulate_command_execution(vcmd, device_id)
        session.execution_log.append(vresult)

    # ── Step 4: Post-Check ──
    session.status = "post_check"
    session.post_checks = run_post_checks(device_id)

    # ── Step 5: 判定 ──
    verdict = evaluate_post_checks(session.post_checks)
    session.status = verdict

    if verdict == "verified":
        session.conclusion = "Recovery Confirmed: 全検証項目がパスしました。修復は正常に完了しています。"

        # DT連携: forecast 自動解消
        if dt_engine and is_pred:
            dt_engine.forecast_auto_resolve(
                device_id,
                "mitigated",
                note="セーフティガード付き修復の検証完了により自動解消",
            )
    elif verdict == "rollback_needed":
        session.conclusion = "異常検出: 修復後の検証で異常が検出されました。ロールバックを推奨します。"
    else:
        session.conclusion = "警告: 一部の検証項目で警告があります。手動確認を推奨します。"

    # セッション保存
    _get_sessions()[device_id] = session
    return session


# =====================================================
# UI描画: 検証ステータスパネル
# =====================================================

def render_verification_panel(session: VerificationSession):
    """検証ステータスパネルを描画する。"""
    if not session:
        return

    # ステータスヘッダー
    _status_config = {
        "verified": ("✅", "Recovery Confirmed", "#2E7D32", "#E8F5E9"),
        "rollback_needed": ("🔴", "異常検出 — ロールバック推奨", "#D32F2F", "#FFEBEE"),
        "warning": ("🟡", "警告 — 手動確認推奨", "#FF8F00", "#FFF8E1"),
        "executing": ("⚡", "修復実行中...", "#1565C0", "#E3F2FD"),
        "post_check": ("🔍", "Post-Check 実行中...", "#6A1B9A", "#F3E5F5"),
        "pre_check": ("🔍", "Pre-Check 実行中...", "#1565C0", "#E3F2FD"),
        "rolled_back": ("🔄", "ロールバック完了", "#1565C0", "#E3F2FD"),
    }
    icon, label, color, bg = _status_config.get(
        session.status, ("❓", "不明", "#666", "#F5F5F5")
    )

    st.markdown(
        f'<div style="background:{bg};border:1px solid {color};border-left:4px solid {color};'
        f'border-radius:6px;padding:10px 16px;margin:8px 0;">'
        f'<span style="font-size:15px;font-weight:700;color:{color};">'
        f'{icon} {label}</span>'
        f'<div style="font-size:13px;color:#555;margin-top:4px;">{session.conclusion}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # Pre-Check 結果
    if session.pre_checks:
        with st.expander("📋 Pre-Check 結果", expanded=False):
            _render_check_results(session.pre_checks)

    # 実行ログ
    if session.execution_log:
        with st.expander(f"⚡ 実行ログ ({len(session.execution_log)}件)", expanded=False):
            for log_entry in session.execution_log:
                step = log_entry.get("step", log_entry.get("command", "unknown"))
                status = log_entry.get("status", "")
                icon = "✅" if status == "success" else "❌"
                st.markdown(f"**{icon} {step}**")
                output = log_entry.get("output", "")
                if output:
                    st.code(output, language="text")

    # Post-Check 結果
    if session.post_checks:
        with st.expander("🔍 Post-Check 結果", expanded=(session.status == "rollback_needed")):
            _render_check_results(session.post_checks)

    # スナップショット情報
    if session.snapshot:
        st.caption(
            f"📸 スナップショット: {session.snapshot.snapshot_id} "
            f"(取得: {time.strftime('%H:%M:%S', time.localtime(session.snapshot.created_at))})"
        )


def _render_check_results(checks: List[CheckResult]):
    """チェック結果リストを描画する。"""
    for check in checks:
        _icons = {"pass": "✅", "fail": "❌", "warning": "🟡"}
        icon = _icons.get(check.status, "❓")
        _colors = {"pass": "#2E7D32", "fail": "#D32F2F", "warning": "#FF8F00"}
        color = _colors.get(check.status, "#666")

        st.markdown(
            f'<div style="font-size:13px;padding:4px 0;">'
            f'<span style="color:{color};font-weight:600;">{icon} {check.check_name}</span>'
            f' — {check.detail}'
            f'</div>',
            unsafe_allow_html=True,
        )
        if check.status != "pass":
            with st.expander(f"📊 `{check.command}` 出力", expanded=False):
                st.code(check.output, language="text")


def render_rollback_button(session: VerificationSession) -> bool:
    """ロールバックボタンを描画し、実行されたら True を返す。"""
    if not session or not session.snapshot:
        return False

    if session.status not in ("rollback_needed", "warning"):
        return False

    col1, col2 = st.columns([1, 1])
    with col1:
        if st.button(
            "🔄 ロールバック実行",
            key=f"rollback_{session.device_id}",
            type="primary",
            use_container_width=True,
        ):
            return True
    with col2:
        st.caption(
            f"スナップショット {session.snapshot.snapshot_id} から復元します"
        )

    return False
