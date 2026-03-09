# docs/changelog.py
# PDF更新インフラストラクチャ: aiops_executive_summary.pdf の更新準備
#
# 目的:
#   - 機能追加・改善のたびに変更ログを自動蓄積
#   - PDF更新時に差分サマリーを生成
#   - 「PDFとの差分」セクションの自動更新に必要な情報を提供
#
# 使い方:
#   from docs.changelog import add_entry, generate_update_summary
#   add_entry("GNN事前学習", "EscalationRuleから合成データでGNNを事前学習", "resolved")
#   summary = generate_update_summary()

from __future__ import annotations
import json
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional
from datetime import datetime

CHANGELOG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "changelog.json"
)


@dataclass
class ChangelogEntry:
    """変更ログの1エントリ"""
    timestamp: str
    title: str
    description: str
    category: str  # "feature", "improvement", "fix", "resolved_gap"
    status: str    # "resolved", "partial", "deferred"
    pdf_section: str = ""  # 対応するPDFセクション
    files_changed: List[str] = field(default_factory=list)
    related_gap: str = ""  # PDFとの差分項目名


def _load_changelog() -> List[Dict]:
    if os.path.exists(CHANGELOG_PATH):
        with open(CHANGELOG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []


def _save_changelog(entries: List[Dict]):
    os.makedirs(os.path.dirname(CHANGELOG_PATH), exist_ok=True)
    with open(CHANGELOG_PATH, 'w', encoding='utf-8') as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)


def add_entry(
    title: str,
    description: str,
    category: str = "feature",
    status: str = "resolved",
    pdf_section: str = "",
    files_changed: Optional[List[str]] = None,
    related_gap: str = "",
):
    """変更ログにエントリを追加"""
    entries = _load_changelog()
    entry = ChangelogEntry(
        timestamp=datetime.now().isoformat(),
        title=title,
        description=description,
        category=category,
        status=status,
        pdf_section=pdf_section,
        files_changed=files_changed or [],
        related_gap=related_gap,
    )
    entries.append(asdict(entry))
    _save_changelog(entries)


def generate_update_summary() -> Dict[str, Any]:
    """
    PDF更新用のサマリーを生成。

    Returns:
        {
            "total_changes": int,
            "resolved_gaps": [...],   # 解決済みPDF差分
            "new_features": [...],     # 新機能
            "improvements": [...],     # 改善
            "remaining_gaps": [...],   # 未解決差分
            "update_sections": {...},  # PDFセクション別の更新内容
            "markdown_summary": str,   # Markdownフォーマットのサマリー
        }
    """
    entries = _load_changelog()

    resolved_gaps = [e for e in entries if e.get("category") == "resolved_gap"]
    features = [e for e in entries if e.get("category") == "feature"]
    improvements = [e for e in entries if e.get("category") == "improvement"]
    deferred = [e for e in entries if e.get("status") == "deferred"]

    # セクション別に整理
    sections = {}
    for e in entries:
        sec = e.get("pdf_section", "General")
        sections.setdefault(sec, []).append(e)

    # Markdownサマリー生成
    lines = ["# aiops_executive_summary.pdf 更新サマリー\n"]
    lines.append(f"生成日時: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")

    if resolved_gaps:
        lines.append("## 解決済み差分（PDF反映必要）\n")
        for e in resolved_gaps:
            lines.append(f"- **{e['title']}**: {e['description']}")
            if e.get('related_gap'):
                lines.append(f"  - 関連差分: {e['related_gap']}")
        lines.append("")

    if features:
        lines.append("## 新機能\n")
        for e in features:
            lines.append(f"- **{e['title']}**: {e['description']}")
        lines.append("")

    if improvements:
        lines.append("## 改善\n")
        for e in improvements:
            lines.append(f"- **{e['title']}**: {e['description']}")
        lines.append("")

    if deferred:
        lines.append("## 将来課題（未解決）\n")
        for e in deferred:
            lines.append(f"- **{e['title']}**: {e['description']}")
        lines.append("")

    return {
        "total_changes": len(entries),
        "resolved_gaps": resolved_gaps,
        "new_features": features,
        "improvements": improvements,
        "remaining_gaps": deferred,
        "update_sections": sections,
        "markdown_summary": "\n".join(lines),
    }


def get_pdf_gap_status() -> Dict[str, str]:
    """PDFとの差分ステータスを返す"""
    entries = _load_changelog()

    # 既知の差分項目
    known_gaps = {
        "ChiGADカイ二乗ウェーブレットフィルタ": "resolved",
        "PHM: 1DCNN-BiLSTMモデル": "deferred",
        "連合学習 FedAvg": "deferred",
        "GNN事前学習": "pending",
        "連続アラームストリーム": "pending",
    }

    # changelog から resolved を反映
    for e in entries:
        gap = e.get("related_gap", "")
        if gap and e.get("status") == "resolved":
            known_gaps[gap] = "resolved"

    return known_gaps
