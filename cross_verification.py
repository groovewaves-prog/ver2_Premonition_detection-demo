# cross_verification.py
"""
マルチエージェント相互検証モジュール (Multi-Agent Cross-Verification)

2つの独立した診断エージェントの結果を突合し、一致度を評価する:
  Agent 1 (物理シミュレーション): BFS影響伝搬 + トポロジー分類 + 冗長性分析
  Agent 2 (LLM/Embedding):       エスカレーションルール照合 (意味的類似度)

一致 → 確信度ボーナス
不一致 → 人間エスカレーションフラグ
"""

import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

# 閾値定義
DIVERGENCE_THRESHOLD = 0.30   # スコア差がこれ以上 → 「不一致」
ESCALATION_THRESHOLD = 0.40   # スコア差がこれ以上 → 「人間エスカレーション推奨」
AGREEMENT_BONUS = 0.03        # 一致時の確信度ボーナス
DIVERGENCE_PENALTY_FACTOR = 0.1  # 不一致時のペナルティ係数


def cross_verify(
    analysis_results: List[Dict[str, Any]],
    predictions: List[Dict[str, Any]],
    msg_map: Dict[str, List[str]],
    digital_twin_engine: Any = None,
) -> List[Dict[str, Any]]:
    """
    2つのエージェントの診断結果を相互検証し、各結果に verification メタデータを付与する。

    Args:
        analysis_results: Agent 1 (BFS/Topology) の診断結果リスト
        predictions: Agent 2 (Embedding) の予測結果リスト
        msg_map: デバイスID → アラームメッセージのマッピング
        digital_twin_engine: DigitalTwinEngine インスタンス (embedding再評価用)

    Returns:
        verification メタデータが付与された analysis_results
    """
    # Agent 2 の結果をデバイスIDでインデックス化
    pred_by_device: Dict[str, Dict] = {}
    for pred in predictions:
        pred_by_device[pred["id"]] = pred

    # Agent 1 の結果をデバイスIDでインデックス化 (予測以外)
    analysis_by_device: Dict[str, Dict] = {}
    for result in analysis_results:
        if not result.get("is_prediction"):
            analysis_by_device[result["id"]] = result

    for result in analysis_results:
        device_id = result.get("id", "")
        if device_id == "SYSTEM":
            continue

        if result.get("is_prediction"):
            # 予測結果 (Agent 2) → Agent 1 の情報で検証
            topo_result = analysis_by_device.get(device_id)
            if topo_result:
                topo_score = topo_result.get("prob", 0)
                embed_score = result.get("prob", 0)
                _apply_verification(result, topo_score, embed_score)
            else:
                result["verification"] = _single_source_meta(
                    topology_score=None,
                    embedding_score=result.get("prob", 0),
                    source="embedding_only",
                )
        else:
            # 通常の分析結果 (Agent 1) → Agent 2 の情報で検証
            pred_result = pred_by_device.get(device_id)
            topo_score = result.get("prob", 0)

            if pred_result:
                embed_score = pred_result.get("prob", 0)
                _apply_verification(result, topo_score, embed_score)
            else:
                # Agent 2 が予測を出さなかったデバイス → embedding で直接評価
                embed_score = _get_embedding_score(
                    digital_twin_engine, msg_map.get(device_id, [])
                )
                if embed_score is not None:
                    _apply_verification(result, topo_score, embed_score)
                else:
                    result["verification"] = _single_source_meta(
                        topology_score=topo_score,
                        embedding_score=None,
                        source="topology_only",
                    )

    return analysis_results


def _get_embedding_score(
    dt_engine: Any, messages: List[str]
) -> Optional[float]:
    """Agent 2 (Embedding) のマッチスコアをメッセージから直接計算する。"""
    if dt_engine is None or not hasattr(dt_engine, "_match_rule"):
        return None
    if not messages:
        return None

    best_score = 0.0
    for msg in messages:
        try:
            rule, quality = dt_engine._match_rule(msg)
            if rule and quality > best_score:
                best_score = quality
        except Exception:
            continue

    return best_score if best_score > 0 else None


def _single_source_meta(
    topology_score: Optional[float],
    embedding_score: Optional[float],
    source: str,
) -> Dict[str, Any]:
    """片方のエージェントのみが評価した場合のメタデータ。"""
    return {
        "topology_score": round(topology_score, 3) if topology_score is not None else None,
        "embedding_score": round(embedding_score, 3) if embedding_score is not None else None,
        "agreement": "single_source",
        "source": source,
        "confidence_gap": 0,
        "escalation_required": False,
    }


def _apply_verification(
    result: Dict[str, Any],
    topo_score: float,
    embed_score: float,
):
    """スコアベースの相互検証を適用し、結果に verification を付与する。"""
    gap = abs(topo_score - embed_score)

    if gap < DIVERGENCE_THRESHOLD:
        agreement = "consistent"
        escalation = False
    elif gap < ESCALATION_THRESHOLD:
        agreement = "divergent"
        escalation = False
    else:
        agreement = "divergent"
        escalation = True

    result["verification"] = {
        "topology_score": round(topo_score, 3),
        "embedding_score": round(embed_score, 3),
        "agreement": agreement,
        "confidence_gap": round(gap, 3),
        "escalation_required": escalation,
    }

    # 確信度の調整: 一致時はボーナス、不一致時は保守的に
    original_prob = result.get("prob", 0)
    if agreement == "consistent":
        adjusted = original_prob + AGREEMENT_BONUS
    else:
        adjusted = original_prob - gap * DIVERGENCE_PENALTY_FACTOR

    result["prob"] = round(max(0.0, min(0.99, adjusted)), 3)


def get_verification_summary(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """検証結果のサマリーを生成する（KPIバナー等で使用）。"""
    total = 0
    consistent = 0
    divergent = 0
    single_source = 0
    escalation_required = 0

    for r in results:
        v = r.get("verification")
        if not v:
            continue
        total += 1
        agreement = v.get("agreement", "")
        if agreement == "consistent":
            consistent += 1
        elif agreement == "divergent":
            divergent += 1
        elif agreement == "single_source":
            single_source += 1
        if v.get("escalation_required"):
            escalation_required += 1

    return {
        "total": total,
        "consistent": consistent,
        "divergent": divergent,
        "single_source": single_source,
        "escalation_required": escalation_required,
        "consistency_rate": round(consistent / max(total, 1), 2),
    }
