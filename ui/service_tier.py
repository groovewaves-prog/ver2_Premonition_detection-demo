# ui/service_tier.py — サービスティア管理
#
# ティアモデル:
#   ベースティア（ラジオボタン）:
#     BASIC  — トポロジーマップ + アラート分析 + ノイズ削減 + ベイズ推論
#     PHM    — Basic + 選択した PHM 機能（チェックボックスで組み合わせ）
#     FULL   — 全機能
#
#   PHM 機能フラグ（チェックボックス、PHM 選択時のみ有効）:
#     phm_premonition  — 予兆検知 / Future Radar / シミュレーション
#     phm_rul          — RUL予測 / AI自律診断 / 自動復旧
#     phm_traffic      — トラフィック分析 / 帯域監視 / 輻輳予測
#
# 環境変数 SERVICE_TIER で切り替え可能。
# ティア不足時は折りたたみ表示 + ロックアイコンで段階的解放を演出。
import os
from contextlib import contextmanager
import streamlit as st

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ベースティア定義
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TIER_BASIC = "basic"
TIER_PHM   = "phm"
TIER_FULL  = "full"

BASE_TIERS = [TIER_BASIC, TIER_PHM, TIER_FULL]

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PHM 機能フラグ定義
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TIER_PHM_PREMONITION = "phm_premonition"
TIER_PHM_RUL         = "phm_rul"
TIER_PHM_TRAFFIC     = "phm_traffic"

PHM_FEATURE_LIST = [TIER_PHM_PREMONITION, TIER_PHM_RUL, TIER_PHM_TRAFFIC]

# PHM 機能の表示情報
PHM_FEATURE_INFO = {
    TIER_PHM_PREMONITION: {
        "label": "予兆検知",
        "icon": "🔮",
        "description": "Future Radar / シミュレーション / トレンド分析",
    },
    TIER_PHM_RUL: {
        "label": "RUL予測",
        "icon": "⏱️",
        "description": "AI自律診断 / 自動復旧 (Remediation)",
    },
    TIER_PHM_TRAFFIC: {
        "label": "トラフィック予測",
        "icon": "📊",
        "description": "帯域利用率監視 / 輻輳予測 / 影響ユーザー推定",
    },
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 表示名・説明
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_TIER_LABELS = {
    TIER_BASIC:           "Basic",
    TIER_PHM:             "PHM",
    TIER_PHM_PREMONITION: "PHM: 予兆検知",
    TIER_PHM_RUL:         "PHM: RUL予測",
    TIER_PHM_TRAFFIC:     "PHM: トラフィック予測",
    TIER_FULL:            "Full",
}

TIER_DESCRIPTIONS = {
    TIER_BASIC: "トポロジー可視化 / アラート分析 / ノイズ削減 / ベイズ推論",
    TIER_PHM:   "Basic + 選択した PHM 機能",
    TIER_PHM_PREMONITION: "予兆検知 / Future Radar / シミュレーション",
    TIER_PHM_RUL:         "RUL予測 / AI自律診断 / 自動復旧",
    TIER_PHM_TRAFFIC:     "トラフィック分析 / 帯域監視 / 輻輳予測",
    TIER_FULL:  "全機能（Granger因果 / GDN偏差 / GrayScope / GNN 含む）",
}

# 後方互換: ALL_TIERS（render_tier_section のアップグレードパス用）
ALL_TIERS = [TIER_BASIC, TIER_PHM_PREMONITION, TIER_PHM_RUL, TIER_PHM_TRAFFIC, TIER_FULL]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ティア取得
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def get_service_tier() -> str:
    """現在のベースティアを取得。環境変数 → session_state → デフォルト(full)"""
    if "service_tier" in st.session_state:
        val = st.session_state["service_tier"]
        # 後方互換: 旧 phm_* 値を "phm" に変換
        if val in PHM_FEATURE_LIST:
            return TIER_PHM
        return val
    tier = os.environ.get("SERVICE_TIER", "full").lower().strip()
    if tier not in set(BASE_TIERS):
        tier = TIER_FULL
    return tier


def get_enabled_phm_features() -> set:
    """現在有効なPHM機能フラグのセットを返す。

    - Full: 全PHM機能が有効
    - PHM:  session_state["phm_features"] のチェックボックス値に従う
    - Basic: 空セット
    """
    tier = get_service_tier()
    if tier == TIER_FULL:
        return set(PHM_FEATURE_LIST)
    if tier == TIER_BASIC:
        return set()
    # PHM モード: セッションステートから読み取り
    return set(st.session_state.get("phm_features", set()))


def tier_has_access(required_tier: str) -> bool:
    """現在の設定で required_tier の機能にアクセス可能か。

    - required_tier が TIER_BASIC → 常に True
    - required_tier が TIER_FULL  → Full ティアのみ True
    - required_tier が PHM 機能   → その機能フラグが有効なら True
    - required_tier が TIER_PHM   → いずれかの PHM 機能が有効なら True
    """
    tier = get_service_tier()
    if tier == TIER_FULL:
        return True
    if required_tier == TIER_BASIC:
        return True
    if tier == TIER_BASIC:
        return False
    # Full ティア専用機能は PHM では使えない
    if required_tier == TIER_FULL:
        return False
    # PHM モード: 機能フラグをチェック
    enabled = get_enabled_phm_features()
    if required_tier == TIER_PHM:
        return len(enabled) > 0
    return required_tier in enabled


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ティアゲート UI コンポーネント
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def render_tier_gated(required_tier: str, label: str = ""):
    """ティアが不足している場合にグレーアウト表示を行うコンテキストマネージャ。"""
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
            self._container = st.container()
            with self._container:
                tier_label = _TIER_LABELS.get(self.required_tier, self.required_tier)
                feature_label = f" ({self.label})" if self.label else ""
                # PHM 機能の場合は「PHM プランで○○を有効化」と表示
                if self.required_tier in PHM_FEATURE_LIST:
                    unlock_msg = f"PHM プランで「{tier_label.replace('PHM: ', '')}」を有効化すると利用可能{feature_label}"
                else:
                    unlock_msg = f"{tier_label} プランで利用可能{feature_label}"
                st.markdown(
                    f'<div style="position:relative;">'
                    f'<div style="position:absolute;top:0;left:0;right:0;bottom:0;'
                    f'background:rgba(255,255,255,0.7);z-index:10;'
                    f'display:flex;align-items:center;justify-content:center;'
                    f'border-radius:8px;border:2px dashed #ccc;">'
                    f'<span style="background:#f5f5f5;padding:8px 16px;border-radius:20px;'
                    f'font-size:13px;color:#888;font-weight:600;">'
                    f'🔒 {unlock_msg}'
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
    """
    has_access = tier_has_access(required_tier)

    if has_access:
        yield True
    else:
        tier_label = _TIER_LABELS.get(required_tier, required_tier)

        # PHM 機能の場合はチェックボックス有効化を促す
        if required_tier in PHM_FEATURE_LIST:
            feature_name = PHM_FEATURE_INFO.get(required_tier, {}).get("label", tier_label)
            expander_title = (
                f"🔒 {icon} {label}  —  PHM「{feature_name}」で解放"
                if icon else
                f"🔒 {label}  —  PHM「{feature_name}」で解放"
            )
        else:
            expander_title = (
                f"🔒 {icon} {label}  —  {tier_label} プランで解放"
                if icon else
                f"🔒 {label}  —  {tier_label} プランで解放"
            )

        with st.expander(expander_title, expanded=False):
            if required_tier in PHM_FEATURE_LIST:
                feature_name = PHM_FEATURE_INFO.get(required_tier, {}).get("label", tier_label)
                _desc = description or f"この機能は PHM プランで「**{feature_name}**」を有効にすると利用できます。"
                st.info(f"""
**{icon} {label}**

{_desc}

サイドバーの「🔑 サービスティア」で **PHM** を選択し、「**{feature_name}**」にチェックを入れてください。
""")
            else:
                _desc = description or f"この機能は **{tier_label}** プラン以上でご利用いただけます。"
                st.info(f"""
**{icon} {label}**

{_desc}

サイドバーの「🔑 サービスティア」から **{tier_label}** 以上を選択すると、この機能が解放されます。
""")

            # PHM 機能一覧（どの機能を有効にすると何が使えるか）
            current_tier = get_service_tier()
            if current_tier == TIER_BASIC:
                st.caption("📋 PHM プランの機能:")
                for fkey in PHM_FEATURE_LIST:
                    finfo = PHM_FEATURE_INFO[fkey]
                    st.caption(f"- {finfo['icon']} **{finfo['label']}**: {finfo['description']}")

        yield False
