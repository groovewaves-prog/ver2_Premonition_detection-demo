# ui/prediction_pipeline.py — DT予兆パイプライン（cockpit.py から分離）
#
# cockpit.py のオーケストレータから予兆検知のデータ処理ロジックを分離し、
# テスト容易性と可読性を向上させる。
import time
import logging
import streamlit as st
from typing import List, Optional, Dict, Any

logger = logging.getLogger(__name__)

try:
    import google.generativeai as genai
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False


def run_prediction_pipeline(
    dt_engine,
    alarms: list,
    analysis_results: list,
    site_id: str,
    api_key: Optional[str],
    topology: dict,
    scenario: str,
) -> List[dict]:
    """DT予兆パイプラインを実行し、予測候補を analysis_results にマージする。

    cockpit.py の render_incident_cockpit() から呼ばれる。
    パイプラインの全ステップ（競合検出 → メッセージ集約 → predict_api →
    自動 outcome → シミュレーション変更検知）を一括実行する。

    Returns:
        dt_predictions: 生成された予測候補リスト（analysis_results にもマージ済み）
    """
    _injected = st.session_state.get("injected_weak_signal")
    _scenario_active = (scenario != "正常稼働")
    _sim_active = bool(_injected and _injected.get("device_id") in topology)
    _conflict = _scenario_active and _sim_active

    # ── Step 1: 競合検出 & 警告表示 ──
    _show_conflict_warnings(_injected, _conflict, _sim_active, alarms, scenario)

    # ── Step 2: メッセージ集約 ──
    _grouped, _signal_count, _sim_level = _collect_message_sources(
        _injected, _sim_active, _conflict, alarms, topology, site_id,
    )

    # ── Step 3: predict_api ループ ──
    dt_predictions = _run_predict_loop(
        dt_engine, _grouped, _sim_level, _signal_count,
        _injected, site_id, api_key,
    )

    # ── Step 4: 自動 outcome 登録 ──
    _auto_resolve_outcomes(dt_engine, alarms, site_id, _conflict)

    # ── Step 5: analysis_results にマージ ──
    # ★ BugFix: 古い予測を analysis_results から完全除去してから新しい予測を追加する。
    #   以前は既存予測を残したまま append していたため、analysis_results が
    #   session_state のキャッシュ参照である場合にキャッシュを汚染し、
    #   level=0 でも古い予測が残留 / level 変更時に内容が更新されない問題があった。
    _old_pred_count = len(analysis_results)
    analysis_results[:] = [r for r in analysis_results if not r.get("is_prediction")]
    if _old_pred_count != len(analysis_results):
        logger.debug(f"Stripped {_old_pred_count - len(analysis_results)} stale predictions from analysis_results")
    for _dp in dt_predictions:
        analysis_results.append(_dp)

    # ── Step 6: シミュレーション変更検知 → レポートリセット ──
    _reset_on_sim_change(_injected, site_id)

    return dt_predictions


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 内部関数
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _show_conflict_warnings(
    _injected: Optional[dict],
    _conflict: bool,
    _sim_active: bool,
    alarms: list,
    scenario: str,
):
    """予兆シミュレーションと障害シナリオの競合を検出し警告バナーを表示する。"""
    if not _conflict:
        return

    _sim_device = _injected.get("device_id", "")
    _critical_set = {a.device_id for a in alarms if a.severity == "CRITICAL"}
    _warning_set = {a.device_id for a in alarms if a.severity == "WARNING"}
    _conflict_level = (
        "CRITICAL" if _sim_device in _critical_set
        else "WARNING" if _sim_device in _warning_set
        else "OTHER"
    )

    if _conflict_level in ("CRITICAL", "WARNING"):
        st.warning(
            "⚠️ **予兆シミュレーション競合検出**\n\n"
            f"現在の障害シナリオ「**{scenario}**」により `{_sim_device}` は既に "
            f"**{_conflict_level}** 状態です。\n"
            "予兆シミュレーションは **無効化** されています。\n\n"
            "💡 予兆→障害の流れをデモするには:\n"
            "1. シナリオを「正常稼働」に戻す\n"
            "2. 予兆シミュレーションを実行（アンバー色でハイライト）\n"
            "3. 障害シナリオに切り替えて「予兆が的中した」を確認"
        )
    else:
        st.info(
            f"ℹ️ 障害シナリオ「**{scenario}**」実行中です。\n"
            f"`{_sim_device}` への予兆シミュレーションは継続しますが、"
            "forecast_ledger の自動 CONFIRMED 登録は **抑制** されます。"
        )


def _collect_message_sources(
    _injected: Optional[dict],
    _sim_active: bool,
    _conflict: bool,
    alarms: list,
    topology: dict,
    site_id: str,
) -> tuple:
    """アラーム + シミュレーションからメッセージソースを集約する。

    Returns:
        (_grouped, _signal_count, _sim_level)
    """
    _msg_sources = []

    # A) 予兆シミュレーション注入シグナル
    if _sim_active:
        _sim_dev = _injected.get("device_id", "")
        _alarm_devices = {a.device_id for a in alarms
                          if a.severity in ("CRITICAL", "WARNING")}
        _disabled = (_conflict and _sim_dev in _alarm_devices)
        if not _disabled:
            _msgs = _injected.get("messages", [_injected.get("message", "")])
            for _m in _msgs:
                if _m:
                    _msg_sources.append((_sim_dev, _m, "simulation"))

    _sim_level = int((_injected or {}).get("level", 1)) if _sim_active else 1

    # デバイス変更時のレポートキャッシュクリア
    _prev_sim_dev_key = f"dt_prev_sim_device_{site_id}"
    _cur_sim_dev = (_injected or {}).get("device_id", "")
    if _cur_sim_dev != st.session_state.get(_prev_sim_dev_key, ""):
        for _k in [k for k in list(st.session_state.report_cache.keys())
                   if "analyst" in k and site_id in k]:
            del st.session_state.report_cache[_k]
        st.session_state.generated_report = None
        st.session_state.remediation_plan = None
        st.session_state.verification_log = None
        st.session_state[_prev_sim_dev_key] = _cur_sim_dev

    # B) 実アラームの WARNING/INFO
    for _a in alarms:
        if _a.severity in ("WARNING", "INFO") and not _a.is_root_cause:
            _msg_sources.append((_a.device_id, _a.message, "real"))

    _signal_count = len(_msg_sources)

    # メッセージ集約（デバイスごと）
    _grouped: Dict[str, tuple] = {}
    for _dev_id, _msg, _src in _msg_sources:
        if _dev_id not in _grouped:
            _grouped[_dev_id] = ([], _src)
        _grouped[_dev_id][0].append(_msg)

    return _grouped, _signal_count, _sim_level


def _run_predict_loop(
    dt_engine,
    _grouped: dict,
    _sim_level: int,
    _signal_count: int,
    _injected: Optional[dict],
    site_id: str,
    api_key: Optional[str],
) -> List[dict]:
    """デバイスごとに predict_api を呼び出し、予測結果を返す。"""
    dt_predictions: List[dict] = []

    # 予測キャッシュ
    _ck_pred_cache = "dt_prediction_cache"
    if _ck_pred_cache not in st.session_state:
        st.session_state[_ck_pred_cache] = {}

    # GenAI モデル初期化（session_state にキャッシュ）
    if api_key and GENAI_AVAILABLE:
        _genai_cache_key = f"_genai_model_{api_key[:8]}"
        if _genai_cache_key not in st.session_state:
            try:
                genai.configure(api_key=api_key)
                _genai_model = genai.GenerativeModel('gemma-3-4b-it')
                st.session_state[_genai_cache_key] = _genai_model
            except Exception:
                pass

    for _dev_id, (_msgs_list, _src) in _grouped.items():
        try:
            _combined_msg = "\n".join(_msgs_list)
            _cache_key = f"v4_{_dev_id}|{_sim_level}|{hash(_combined_msg[:200])}"

            _cached = st.session_state[_ck_pred_cache].get(_cache_key)
            if _cached is not None:
                for _p in _cached:
                    _p["is_prediction"] = True
                    if not any(d.get("id") == _dev_id for d in dt_predictions):
                        dt_predictions.append(_p)
                continue

            _resp = dt_engine.predict_api({
                "tenant_id":       site_id,
                "device_id":       _dev_id,
                "msg":             _combined_msg,
                "timestamp":       time.time(),
                "record_forecast": True,
                "attrs":           {
                    "source":            _src,
                    "degradation_level": _sim_level if _src == "simulation" else 1,
                    "signal_count":      len(_msgs_list),
                }
            })

            _preds_returned = _resp.get("predictions", []) if _resp.get("ok") else []

            if not _preds_returned and _src == "simulation":
                _sim_scenario = _injected.get("scenario", "異常")
                _preds_returned = [{
                    "label": f"🔮 [予兆] {_sim_scenario} の初期兆候",
                    "predicted_state": _sim_scenario,
                    "prob": min(0.65 + (_sim_level * 0.05), 0.99),
                    "confidence": min(0.65 + (_sim_level * 0.05), 0.99),
                    "prediction_timeline": "1〜3日",
                    "prediction_affected_count": 2,
                    "prediction_time_to_critical_min": 60,
                    "prediction_time_to_failure_hours": max(72 - (_sim_level * 12), 2),
                    "rule_pattern": f"{_sim_scenario}_Auto",
                    "reasons": _msgs_list,
                    "recommended_actions": []
                }]

            # トリアージ生成は future_radar.py で遅延実行
            _preds_to_cache = []
            for _p in _preds_returned:
                _p["id"]     = _dev_id
                _p["source"] = _src
                _p["prediction_signal_count"] = _signal_count
                _p["is_prediction"] = True
                _preds_to_cache.append(_p)

                if not any(d.get("id") == _dev_id for d in dt_predictions):
                    dt_predictions.append(_p)

            st.session_state[_ck_pred_cache][_cache_key] = _preds_to_cache
            if len(st.session_state[_ck_pred_cache]) > 20:
                _keys = list(st.session_state[_ck_pred_cache].keys())
                for _old_k in _keys[:10]:
                    st.session_state[_ck_pred_cache].pop(_old_k, None)

        except Exception as _pred_err:
            logger.warning(f"predict_api failed for {_dev_id}: {_pred_err}")

    return dt_predictions


def _auto_resolve_outcomes(
    dt_engine,
    alarms: list,
    site_id: str,
    _conflict: bool,
):
    """Execute 成功や CRITICAL アラームによる自動 outcome 登録。"""
    # 自動 outcome 登録（Execute 成功）
    for _rid, _recovered in list(st.session_state.get("recovered_devices", {}).items()):
        if _recovered:
            _auto_key = f"dt_auto_mitigated_{site_id}_{_rid}"
            if not st.session_state.get(_auto_key):
                dt_engine.forecast_auto_resolve(
                    _rid, "mitigated", note="Execute 成功による自動解消")
                st.session_state[_auto_key] = True

    # CRITICAL アラームによる自動確定
    if not _conflict:
        _critical_devices = {a.device_id for a in alarms if a.severity == "CRITICAL"}
        for _cd in _critical_devices:
            _auto_key = f"dt_auto_confirmed_{site_id}_{_cd}"
            if not st.session_state.get(_auto_key):
                dt_engine.forecast_auto_resolve(
                    _cd, "confirmed_incident",
                    note="CRITICAL アラームによる自動確定")
                st.session_state[_auto_key] = True


def _reset_on_sim_change(_injected: Optional[dict], site_id: str):
    """シミュレーション状態変更時にレポートキャッシュをリセットする。"""
    _sim_state_now = (
        f"{_injected.get('device_id')}_{_injected.get('scenario')}_{_injected.get('level')}"
        if _injected else None
    )
    _sim_state_key = f"dt_last_sim_state_{site_id}"
    _sim_state_prev = st.session_state.get(_sim_state_key)

    if _sim_state_now != _sim_state_prev:
        st.session_state.generated_report    = None
        st.session_state.remediation_plan    = None
        st.session_state.verification_log    = None
        st.session_state.live_result         = None
        st.session_state.verification_result = None
        _keys_to_del = [k for k in st.session_state.get("report_cache", {})
                        if "analyst" in k or "remediation" in k]
        for _k in _keys_to_del:
            st.session_state.report_cache.pop(_k, None)
        st.session_state[_sim_state_key] = _sim_state_now
