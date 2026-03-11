# ui/service_tier.py — サービスティア管理
#
# 段階的導入に対応するサービスティア定義:
#   BASIC      (Phase 1-2): トポロジーマップ + アラート分析 + ノイズ削減 + ベイズ推論
#   PHM        (Phase 3):   + RUL予測 + 消耗品寿命可視化 + 予兆検知フル機能
#   FULL       (Phase 4+):  + 全機能（将来拡張含む）
#
# 現在のデモ環境では全機能がON（FULL）。
# 環境変数 SERVICE_TIER で切り替え可能。
# UIはグレーアウト表示のみ（バックエンド停止なし）。
import os
import streamlit as st

# サービスティア定義
TIER_BASIC = "basic"
TIER_PHM   = "phm"
TIER_FULL  = "full"

# ティアの階層順（低→高）
_TIER_ORDER = {TIER_BASIC: 1, TIER_PHM: 2, TIER_FULL: 3}


def get_service_tier() -> str:
    """現在のサービスティアを取得。環境変数 → session_state → デフォルト(full)"""
    # session_state が優先（UI からの動的変更用）
    if "service_tier" in st.session_state:
        return st.session_state["service_tier"]
    # 環境変数
    tier = os.environ.get("SERVICE_TIER", "full").lower().strip()
    if tier not in _TIER_ORDER:
        tier = TIER_FULL
    return tier


def tier_has_access(required_tier: str) -> bool:
    """現在のティアが required_tier 以上の機能にアクセス可能か"""
    current = get_service_tier()
    return _TIER_ORDER.get(current, 0) >= _TIER_ORDER.get(required_tier, 0)


def render_tier_gated(required_tier: str, label: str = ""):
    """
    ティアが不足している場合にグレーアウト表示を行うコンテキストマネージャ。

    使い方:
        with render_tier_gated("phm", "RUL予測"):
            # この中のUIは、PHM以上のティアでのみアクティブ表示
            st.metric("RUL", "68h")

    ティア不足時はグレーアウトオーバーレイ + アップグレードメッセージを表示。
    ただしコンテンツ自体は描画される（デモ用に見せるため）。
    """
    return _TierGateContext(required_tier, label)


class _TierGateContext:
    """ティアゲートのコンテキストマネージャ"""

    _TIER_LABELS = {
        TIER_BASIC: "Basic",
        TIER_PHM:   "PHM (Predictive Health Management)",
        TIER_FULL:  "Full",
    }

    def __init__(self, required_tier: str, label: str):
        self.required_tier = required_tier
        self.label = label
        self.has_access = tier_has_access(required_tier)
        self._container = None

    def __enter__(self):
        if not self.has_access:
            # グレーアウトコンテナ
            self._container = st.container()
            with self._container:
                tier_label = self._TIER_LABELS.get(self.required_tier, self.required_tier)
                feature_label = f" ({self.label})" if self.label else ""
                st.markdown(
                    f'<div style="position:relative;">'
                    f'<div style="position:absolute;top:0;left:0;right:0;bottom:0;'
                    f'background:rgba(255,255,255,0.7);z-index:10;'
                    f'display:flex;align-items:center;justify-content:center;'
                    f'border-radius:8px;border:2px dashed #ccc;">'
                    f'<span style="background:#f5f5f5;padding:8px 16px;border-radius:20px;'
                    f'font-size:13px;color:#888;font-weight:600;">'
                    f'🔒 {tier_label} プランで利用可能{feature_label}'
                    f'</span></div></div>',
                    unsafe_allow_html=True,
                )
            return self._container
        return st

    def __exit__(self, *args):
        pass
