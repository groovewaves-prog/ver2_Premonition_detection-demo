# utils/sanitizer.py  ―  LLM送信前の共通サニタイズ処理
"""
全LLM呼出箇所で統一的に使用するサニタイズ関数群。
目的:
  1. IPアドレス・MACアドレス等の機密情報マスキング
  2. パスワード・秘密鍵・SNMP community 等の除去
  3. プロンプトインジェクション防御
  4. 入力長制限（トークン消費抑制）
"""

import re

# ── コンパイル済み正規表現（パフォーマンス最適化）──

_RE_IPV4 = re.compile(r'\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b')
_RE_IPV6 = re.compile(r'\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}\b')
_RE_MAC = re.compile(r'\b(?:[0-9a-fA-F]{2}[:\-]){5}[0-9a-fA-F]{2}\b')
_RE_HOSTNAME = re.compile(
    r'\b(?:prod|dev|test|stage|staging)-[\w\-]+', re.IGNORECASE
)
_RE_ASN = re.compile(r'\bAS\d+\b')
_RE_VLAN = re.compile(r'\bVLAN\s*\d+\b', re.IGNORECASE)
_RE_PASSWORD = re.compile(
    r'(password|secret|community|key|token|credential)\s*[=:]\s*\S+',
    re.IGNORECASE,
)
_RE_ENCRYPTED = re.compile(
    r'(encrypted-password\s+)"[^"]+"', re.IGNORECASE
)
_RE_SNMP = re.compile(
    r'(snmp-server community)\s+\S+', re.IGNORECASE
)
_RE_USERNAME_SECRET = re.compile(
    r'(username\s+\S+\s+secret)\s+\d\s+\S+', re.IGNORECASE
)
# プロンプトインジェクション検出パターン
_RE_INJECTION = re.compile(
    r'(ignore\s+(all\s+)?(previous|above)\s+instructions'
    r'|system\s*:\s*you\s+are'
    r'|<\s*/?\s*system\s*>'
    r'|\bDAN\s+mode\b'
    r'|jailbreak)',
    re.IGNORECASE,
)


def sanitize_for_llm(text: str, max_length: int = 2000) -> str:
    """LLM送信前の標準サニタイズ処理。

    以下を実行:
      1. IPアドレス（v4/v6）のマスキング
      2. MACアドレスのマスキング
      3. ホスト名の一般化
      4. ASN / VLAN IDのマスキング
      5. パスワード・秘密鍵・SNMP community の除去
      6. プロンプトインジェクション文字列の除去
      7. 入力長制限

    Args:
        text: サニタイズ対象テキスト
        max_length: 最大文字数（デフォルト2000）

    Returns:
        サニタイズ済みテキスト
    """
    if not text:
        return ""

    s = text

    # 1. ネットワークアドレスのマスキング
    s = _RE_IPV4.sub('[IP_MASKED]', s)
    s = _RE_IPV6.sub('[IPV6_MASKED]', s)
    s = _RE_MAC.sub('[MAC_MASKED]', s)

    # 2. ホスト名の一般化
    s = _RE_HOSTNAME.sub('[HOST_MASKED]', s)

    # 3. ネットワーク識別子のマスキング
    s = _RE_ASN.sub('[AS_MASKED]', s)
    s = _RE_VLAN.sub('[VLAN_MASKED]', s)

    # 4. 認証情報の除去
    s = _RE_PASSWORD.sub(r'\1=********', s)
    s = _RE_ENCRYPTED.sub(r'\1"********"', s)
    s = _RE_SNMP.sub(r'\1 ********', s)
    s = _RE_USERNAME_SECRET.sub(r'\1 5 ********', s)

    # 5. プロンプトインジェクション防御
    s = _RE_INJECTION.sub('[FILTERED]', s)

    # 6. 入力長制限
    if len(s) > max_length:
        s = s[:max_length] + f"\n... (以降省略: 全{len(text)}文字)"

    return s


def sanitize_device_id(device_id: str) -> str:
    """デバイスIDのサニタイズ（プロンプトインジェクション防御）。

    デバイスIDは英数字・ハイフン・アンダースコア・ドットのみ許可。
    """
    if not device_id:
        return ""
    # 許可文字以外を除去
    return re.sub(r'[^a-zA-Z0-9_\-./]', '', device_id)[:100]


def sanitize_user_input(text: str, max_length: int = 1000) -> str:
    """ユーザー入力のサニタイズ（チャット等）。

    sanitize_for_llm に加えて:
      - HTMLタグの除去
      - 制御文字の除去
    """
    if not text:
        return ""

    # HTMLタグ除去
    s = re.sub(r'<[^>]+>', '', text)
    # 制御文字除去（改行・タブは許可）
    s = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', s)

    return sanitize_for_llm(s, max_length=max_length)
