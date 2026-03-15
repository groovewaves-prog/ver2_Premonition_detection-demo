# ui/service_tier.py — サービスティア管理
#
# 段階的導入に対応するサービスティア定義:
#   BASIC              (Phase 1-2): トポロジーマップ + アラート分析 + ノイズ削減 + ベイズ推論
#   PHM_PREMONITION    (Phase 3a):  + 予兆検知 / Future Radar / シミュレーション
#   PHM_RUL            (Phase 3b):  + RUL予測 / 消耗品寿命可視化 / AI自律診断 / 自動復旧
#   PHM_TRAFFIC        (Phase 3c):  + トラフィック分析 / 帯域利用率監視 / 輻輳予測
#   FULL               (Phase 4+):  + 全機能（Granger因果 / GDN偏差 / GrayScope / GNN）
#
# 現在のデモ環境では全機能がON（FULL）。
# 環境変数 SERVICE_TIER で切り替え可能。
# ティア不足時は折りたたみ表示 + ロックアイコンで段階的解放を演出。
#
# ★ 後方互換: TIER_PHM は TIER_PHM_RUL のエイリアス。
#   tier_has_access(TIER_PHM) は PHM_PREMONITION 以上で True を返す。
import os
from contextlib import contextmanager
import streamlit as st

# サービスティア定義
TIER_BASIC           = "basic"
TIER_PHM_PREMONITION = "phm_premonition"
TIER_PHM_RUL         = "phm_rul"
TIER_PHM_TRAFFIC     = "phm_traffic"
TIER_FULL            = "full"

# ★ 後方互換エイリアス: 既存コードで TIER_PHM を参照している箇所はそのまま動作
TIER_PHM = TIER_PHM_PREMONITION

# ティアの階層順（低→高）
_TIER_ORDER = {
    TIER_BASIC: 1,
    TIER_PHM_PREMONITION: 2,
    TIER_PHM_RUL: 3,
    TIER_PHM_TRAFFIC: 4,
    TIER_FULL: 5,
}

# ティアの表示名
_TIER_LABELS = {
    TIER_BASIC:           "Basic",
    TIER_PHM_PREMONITION: "PHM: 予兆検知",
    TIER_PHM_RUL:         "PHM: RUL予測",
    TIER_PHM_TRAFFIC:     "PHM: トラフィック",
    TIER_FULL:            "Full",
}

# ティア別の概要説明（サイドバー用）
TIER_DESCRIPTIONS = {
    TIER_BASIC:           "トポロジー可視化 / アラート分析 / ノイズ削減 / ベイズ推論",
    TIER_PHM_PREMONITION: "↑ Basic + 予兆検知 / Future Radar / シミュレーション",
    TIER_PHM_RUL:         "↑ 予兆検知 + RUL予測 / AI自律診断 / 自動復旧",
    TIER_PHM_TRAFFIC:     "↑ RUL予測 + トラフィック分析 / 帯域監視 / 輻輳予測",
    TIER_FULL:            "↑ トラフィック + Granger因果 / GDN偏差 / GrayScope / GNN",
}

# 全ティアの順序付きリスト（UI表示用）
ALL_TIERS = [TIER_BASIC, TIER_PHM_PREMONITION, TIER_PHM_RUL, TIER_PHM_TRAFFIC, TIER_FULL]


def get_service_tier() -> str:
    """現在のサービスティアを取得。環境変数 → session_state → デフォルト(full)"""
    # session_state が優先（UI からの動的変更用）
    if "service_tier" in st.session_state:
        return st.session_state["service_tier"]
    # 環境変数（後方互換: "phm" → TIER_PHM_PREMONITION）
    tier = os.environ.get("SERVICE_TIER", "full").lower().strip()
    if tier == "phm":
        tier = TIER_PHM_PREMONITION
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
                tier_label = _TIER_LABELS.get(self.required_tier, self.required_tier)
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


@contextmanager
def render_tier_section(
    required_tier: str,
    label: str,
    icon: str = "",
    description: str = "",
):
    """ティア不足時に折りたたみ表示する段階的解放UIコンポーネント。

    - アクセス可能   → 通常表示（コンテンツをそのまま描画）
    - アクセス不可   → 折りたたみ expander + ロックアイコン + アップグレード案内
                       中身は描画されない（パフォーマンス節約）

    使い方:
        with render_tier_section(TIER_PHM, "予兆検知", icon="🔮") as accessible:
            if accessible:
                render_future_radar(...)

    Args:
        required_tier: 必要なティア (TIER_BASIC / TIER_PHM / TIER_FULL)
        label: 機能名（表示用）
        icon: アイコン（省略可）
        description: 機能概要（ロック時に表示。省略時はデフォルト）
    """
    has_access = tier_has_access(required_tier)

    if has_access:
        # アクセスOK → そのまま描画
        yield True
    else:
        # アクセス不可 → 折りたたみ expander で「プレビュー」
        tier_label = _TIER_LABELS.get(required_tier, required_tier)
        expander_title = f"🔒 {icon} {label}  —  {tier_label} プランで解放" if icon else f"🔒 {label}  —  {tier_label} プランで解放"

        with st.expander(expander_title, expanded=False):
            # アップグレード案内
            _desc = description or f"この機能は **{tier_label}** プラン以上でご利用いただけます。"
            st.info(f"""
**{icon} {label}**

{_desc}

サイドバーの「🔑 サービスティア」から **{tier_label}** 以上を選択すると、この機能が解放されます。
""")

            # 各ティアの機能一覧をコンパクトに表示
            current = get_service_tier()
            current_order = _TIER_ORDER.get(current, 0)
            required_order = _TIER_ORDER.get(required_tier, 0)

            upgrade_path = []
            for t_key in ALL_TIERS:
                t_order = _TIER_ORDER[t_key]
                if current_order < t_order <= required_order:
                    t_desc = TIER_DESCRIPTIONS.get(t_key, "")
                    upgrade_path.append(f"**{_TIER_LABELS[t_key]}**: {t_desc}")

            if upgrade_path:
                st.caption("📋 アップグレードパス:\n" + "\n".join(f"- {p}" for p in upgrade_path))

        yield False
