# digital_twin_pkg/gnn_trainer.py
# GNN合成事前学習: EscalationRuleから学習データを生成してGNNを事前学習
#
# 目的:
#   - デプロイ直後からGNNが意味のある推論を行えるようにする
#   - 「ランダム重み問題」を解決（現状: GNN未学習 → 30%がノイズ）
#   - 学習済みモデルを .pt ファイルとして保存 → 起動時に自動ロード
#
# 方式:
#   1. 各EscalationRuleから合成アラーム埋め込みを生成
#   2. 正常/異常のペアデータを構築
#   3. BCELoss + MSELoss で学習
#   4. モデルを保存

from __future__ import annotations
import logging
import os
import math
import random
import time
from typing import Any, Dict, List, Optional, Tuple
import numpy as np

logger = logging.getLogger(__name__)

try:
    import torch
    HAS_TORCH = True
except ImportError:
    HAS_TORCH = False

from .rules import DEFAULT_RULES, EscalationRule
from .gnn import GNNPredictionEngine, HAS_PYTORCH_GEOMETRIC
from .alarm_stream import DEGRADATION_SEQUENCES


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 合成学習データ生成
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _generate_synthetic_embedding(
    rule: EscalationRule,
    level: int,
    dim: int = 768,
    rng: Optional[random.Random] = None,
) -> np.ndarray:
    """
    ルールの意味的特徴を反映した合成埋め込みベクトルを生成。

    レベルに応じて異常信号の強度を変化させる:
      Level 0 (正常): ランダムノイズのみ
      Level 1-5: ルール固有のシードベクトル + レベル比例の信号強度
    """
    if rng is None:
        rng = random.Random()

    # ルール固有のシードベクトル（semantic_phrasesのハッシュで決定論的に生成）
    rule_seed = hash(tuple(rule.semantic_phrases))
    rule_rng = random.Random(rule_seed)
    seed_vector = np.array([rule_rng.gauss(0, 1) for _ in range(dim)], dtype=np.float32)
    seed_vector /= np.linalg.norm(seed_vector) + 1e-8

    if level == 0:
        # 正常: ランダムノイズのみ
        noise = np.array([rng.gauss(0, 0.3) for _ in range(dim)], dtype=np.float32)
        return noise

    # 異常: シードベクトル * 信号強度 + ノイズ
    signal_strength = 0.2 + (level / 5.0) * 0.8  # Level 1: 0.36, Level 5: 1.0
    noise_scale = 0.3 * (1.0 - level / 7.0)       # レベルが上がるほどノイズ減

    signal = seed_vector * signal_strength
    noise = np.array([rng.gauss(0, noise_scale) for _ in range(dim)], dtype=np.float32)

    embedding = signal + noise
    # L2正規化
    norm = np.linalg.norm(embedding)
    if norm > 0:
        embedding = embedding / norm

    return embedding


def generate_training_data(
    topology: Dict[str, Any],
    children_map: Dict[str, List[str]],
    rules: Optional[List[EscalationRule]] = None,
    samples_per_rule: int = 50,
    seed: int = 42,
) -> List[Dict]:
    """
    EscalationRuleから合成学習データを生成。

    各ルールに対して:
      - 正常サンプル: level=0, actual_failure=False
      - 異常サンプル: level=1-5, actual_failure=True (level 4-5), False (level 1-2)
      - 境界サンプル: level=3, actual_failure=混在（学習の境界をカバー）

    Returns:
        List[Dict]: 各要素は {
            'alarm_embeddings': {device_id: ndarray[768]},
            'device_id': str,
            'actual_failure': bool,
            'time_to_failure': float (hours),
        }
    """
    if rules is None:
        rules = DEFAULT_RULES

    rng = random.Random(seed)
    device_ids = list(topology.keys())
    if not device_ids:
        logger.warning("Empty topology, cannot generate training data")
        return []

    training_data = []

    for rule in rules:
        if rule.pattern in ("generic_error", "analysis_signal"):
            continue

        for sample_idx in range(samples_per_rule):
            # ターゲットデバイスをランダム選択
            target_device = rng.choice(device_ids)

            # レベル分布: 正常40%, 軽度20%, 中度15%, 重度15%, 緊急10%
            level = rng.choices([0, 1, 2, 3, 4, 5], weights=[40, 12, 12, 15, 12, 9])[0]

            # ターゲットデバイスの埋め込み
            target_embedding = _generate_synthetic_embedding(rule, level, rng=rng)

            # 周辺デバイスの埋め込み（カスケード効果をシミュレート）
            alarm_embeddings = {}
            alarm_embeddings[target_device] = target_embedding

            # 配下デバイスにも影響を伝搬（レベルに応じて）
            if level >= 3 and target_device in children_map:
                children = children_map[target_device]
                affected_count = min(len(children), max(1, level - 1))
                for child in rng.sample(children, min(affected_count, len(children))):
                    # 子デバイスは親より弱い信号
                    child_level = max(0, level - rng.randint(1, 2))
                    alarm_embeddings[child] = _generate_synthetic_embedding(
                        rule, child_level, rng=rng
                    )

            # ラベル設定
            if level == 0:
                actual_failure = False
                ttf = float(rule.early_warning_hours)
            elif level <= 2:
                actual_failure = rng.random() < 0.15  # 15%で故障に至る
                ttf = rule.early_warning_hours * (1.0 - level * 0.2) + rng.gauss(0, 5)
            elif level == 3:
                actual_failure = rng.random() < 0.5   # 50%で故障に至る
                ttf = rule.time_to_critical_min / 60.0 * 3 + rng.gauss(0, 2)
            elif level == 4:
                actual_failure = rng.random() < 0.85  # 85%で故障に至る
                ttf = rule.time_to_critical_min / 60.0 * 1.5 + rng.gauss(0, 1)
            else:
                actual_failure = True
                ttf = rule.time_to_critical_min / 60.0 * 0.5 + rng.gauss(0, 0.5)

            ttf = max(0.1, ttf)

            training_data.append({
                'alarm_embeddings': alarm_embeddings,
                'device_id': target_device,
                'actual_failure': actual_failure,
                'time_to_failure': ttf,
                'rule_pattern': rule.pattern,
                'level': level,
            })

    rng.shuffle(training_data)
    logger.info(
        f"Generated {len(training_data)} synthetic training samples "
        f"from {len([r for r in rules if r.pattern not in ('generic_error', 'analysis_signal')])} rules"
    )
    return training_data


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# GNN 事前学習
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

DEFAULT_MODEL_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "models"
)
DEFAULT_MODEL_PATH = os.path.join(DEFAULT_MODEL_DIR, "gnn_pretrained.pt")


def pretrain_gnn(
    topology: Dict[str, Any],
    children_map: Dict[str, List[str]],
    epochs: int = 80,
    lr: float = 0.001,
    samples_per_rule: int = 50,
    model_save_path: Optional[str] = None,
    progress_callback=None,
) -> Optional[Dict[str, Any]]:
    """
    GNNを合成データで事前学習し、モデルを保存。

    Args:
        topology: ネットワークトポロジー
        children_map: 親子関係マップ
        epochs: 学習エポック数
        lr: 学習率
        samples_per_rule: ルールあたりのサンプル数
        model_save_path: モデル保存パス (None=デフォルト)
        progress_callback: 進捗コールバック fn(epoch, total, loss)

    Returns:
        学習結果の統計情報 or None (失敗時)
    """
    if not HAS_PYTORCH_GEOMETRIC:
        logger.error("PyTorch Geometric not available. Cannot pretrain GNN.")
        return None

    if model_save_path is None:
        model_save_path = DEFAULT_MODEL_PATH

    # ディレクトリ作成
    os.makedirs(os.path.dirname(model_save_path), exist_ok=True)

    logger.info(f"Starting GNN pretraining: epochs={epochs}, samples/rule={samples_per_rule}")
    start_time = time.time()

    # 1. 合成データ生成
    training_data = generate_training_data(
        topology, children_map,
        samples_per_rule=samples_per_rule,
    )
    if not training_data:
        logger.error("No training data generated")
        return None

    # 2. GNNエンジン作成
    engine = GNNPredictionEngine(topology, children_map)
    if engine.model is None:
        logger.error("Failed to create GNN model")
        return None

    # 3. 学習実行
    optimizer = torch.optim.Adam(engine.model.parameters(), lr=lr)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)
    criterion_conf = torch.nn.BCELoss()
    criterion_ttf = torch.nn.MSELoss()

    engine.model.train()
    loss_history = []
    best_loss = float('inf')
    best_state = None

    for epoch in range(epochs):
        epoch_loss = 0.0
        n_samples = 0
        random.shuffle(training_data)

        for sample in training_data:
            optimizer.zero_grad()

            data = engine.topology_to_graph(sample['alarm_embeddings'])
            if data is None:
                continue

            data = data.to(engine.device)
            pred_conf, pred_ttf, _ = engine.model(data.x_dict, data.edge_index_dict)

            target_idx = data.device_to_idx.get(sample['device_id'])
            if target_idx is None:
                continue

            target_conf = torch.tensor(
                [1.0 if sample['actual_failure'] else 0.0],
                dtype=torch.float, device=engine.device
            )
            target_ttf_val = torch.tensor(
                [sample['time_to_failure']],
                dtype=torch.float, device=engine.device
            )

            loss_conf = criterion_conf(pred_conf[target_idx], target_conf)
            loss_ttf = criterion_ttf(pred_ttf[target_idx], target_ttf_val)
            loss = loss_conf + 0.1 * loss_ttf

            loss.backward()
            # 勾配クリッピング
            torch.nn.utils.clip_grad_norm_(engine.model.parameters(), max_norm=1.0)
            optimizer.step()

            epoch_loss += loss.item()
            n_samples += 1

        scheduler.step()
        avg_loss = epoch_loss / max(n_samples, 1)
        loss_history.append(avg_loss)

        # ベストモデル保存
        if avg_loss < best_loss:
            best_loss = avg_loss
            best_state = {k: v.clone() for k, v in engine.model.state_dict().items()}

        if progress_callback:
            progress_callback(epoch + 1, epochs, avg_loss)

        if (epoch + 1) % 10 == 0:
            logger.info(f"Epoch {epoch+1}/{epochs}, Loss: {avg_loss:.4f}, LR: {scheduler.get_last_lr()[0]:.6f}")

    # 4. ベストモデルを保存
    if best_state is not None:
        torch.save(best_state, model_save_path)
        logger.info(f"Pretrained GNN model saved to {model_save_path}")

    elapsed = time.time() - start_time

    result = {
        "epochs": epochs,
        "total_samples": len(training_data),
        "final_loss": loss_history[-1] if loss_history else None,
        "best_loss": best_loss,
        "elapsed_sec": elapsed,
        "model_path": model_save_path,
        "loss_history": loss_history,
    }

    logger.info(
        f"GNN pretraining complete: {elapsed:.1f}s, "
        f"best_loss={best_loss:.4f}, samples={len(training_data)}"
    )
    return result


def get_pretrained_model_path() -> Optional[str]:
    """事前学習済みモデルが存在すればパスを返す"""
    if os.path.exists(DEFAULT_MODEL_PATH):
        return DEFAULT_MODEL_PATH
    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 蓄積データからの学習データ変換
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

GNN_TRAINING_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "gnn_training"
)


def list_training_sessions() -> List[str]:
    """蓄積済みGNN学習データセッション一覧を返す"""
    if not os.path.exists(GNN_TRAINING_DIR):
        return []
    return sorted(
        [f for f in os.listdir(GNN_TRAINING_DIR) if f.endswith(".json")],
        reverse=True,
    )


def convert_sessions_to_training_data(
    session_paths: List[str],
    topology: Dict[str, Any],
    children_map: Dict[str, List[str]],
) -> List[Dict]:
    """蓄積データ (StreamDataExporter JSON) を GNN 学習データ形式に変換。

    合成データの generate_training_data() と同じ出力形式を返す。
    合成埋め込みベクトルを使うが、劣化レベルは実データから取得するため
    合成のみの学習よりリアルな分布になる。
    """
    import json

    training_data = []
    device_ids = list(topology.keys())
    if not device_ids:
        return []

    rng = random.Random(int(time.time()))
    rules_by_pattern = {r.pattern: r for r in DEFAULT_RULES}

    for spath in session_paths:
        full_path = os.path.join(GNN_TRAINING_DIR, spath) if not os.path.isabs(spath) else spath
        try:
            with open(full_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning("Failed to load session %s: %s", spath, e)
            continue

        scenario_key = data.get("scenario_key", "")
        target_device = data.get("target_device", "")
        rule = rules_by_pattern.get(scenario_key)
        if not rule:
            for r in DEFAULT_RULES:
                if r.pattern not in ("generic_error", "analysis_signal"):
                    rule = r
                    break
        if not rule:
            continue

        for snap in data.get("snapshots", []):
            level = snap.get("degradation_level", 0)
            dev = snap.get("device_id", target_device)
            if dev not in topology:
                dev = rng.choice(device_ids)

            target_embedding = _generate_synthetic_embedding(rule, level, rng=rng)
            alarm_embeddings = {dev: target_embedding}

            if level >= 3 and dev in children_map:
                children = children_map[dev]
                for child in children[:min(len(children), level - 1)]:
                    child_level = max(0, level - rng.randint(1, 2))
                    alarm_embeddings[child] = _generate_synthetic_embedding(
                        rule, child_level, rng=rng
                    )

            if level == 0:
                actual_failure = False
                ttf = float(rule.early_warning_hours)
            elif level <= 2:
                actual_failure = rng.random() < 0.15
                ttf = rule.early_warning_hours * (1.0 - level * 0.2)
            elif level == 3:
                actual_failure = rng.random() < 0.5
                ttf = rule.time_to_critical_min / 60.0 * 3
            elif level == 4:
                actual_failure = rng.random() < 0.85
                ttf = rule.time_to_critical_min / 60.0 * 1.5
            else:
                actual_failure = True
                ttf = rule.time_to_critical_min / 60.0 * 0.5

            training_data.append({
                'alarm_embeddings': alarm_embeddings,
                'device_id': dev,
                'actual_failure': actual_failure,
                'time_to_failure': max(0.1, ttf + rng.gauss(0, 1)),
                'rule_pattern': rule.pattern,
                'level': level,
            })

    rng.shuffle(training_data)
    logger.info("Converted %d snapshots from %d sessions to training data",
                len(training_data), len(session_paths))
    return training_data


def finetune_gnn(
    topology: Dict[str, Any],
    children_map: Dict[str, List[str]],
    session_files: List[str],
    epochs: int = 40,
    lr: float = 0.0005,
    model_save_path: Optional[str] = None,
    progress_callback=None,
) -> Optional[Dict[str, Any]]:
    """蓄積データでGNNをファインチューニング。

    既存の学習済みモデルがあればそこから開始し、蓄積データで追加学習する。
    なければ新規学習。
    """
    if not HAS_PYTORCH_GEOMETRIC:
        logger.error("PyTorch Geometric not available.")
        return None

    if model_save_path is None:
        model_save_path = DEFAULT_MODEL_PATH
    os.makedirs(os.path.dirname(model_save_path), exist_ok=True)

    training_data = convert_sessions_to_training_data(
        session_files, topology, children_map
    )
    if not training_data:
        logger.error("No training data from sessions")
        return None

    # 合成データも混合（過学習防止）
    synthetic_data = generate_training_data(
        topology, children_map, samples_per_rule=20, seed=int(time.time()),
    )
    combined_data = training_data + synthetic_data

    # モデル初期化（既存モデルがあればロード）
    existing_path = get_pretrained_model_path()
    engine = GNNPredictionEngine(
        topology, children_map,
        model_path=existing_path,
    )
    if engine.model is None:
        logger.error("Failed to create GNN model")
        return None

    logger.info(
        "Fine-tuning GNN: %d stream + %d synthetic = %d samples, epochs=%d",
        len(training_data), len(synthetic_data), len(combined_data), epochs,
    )
    start_time = time.time()

    optimizer = torch.optim.Adam(engine.model.parameters(), lr=lr)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)
    criterion_conf = torch.nn.BCELoss()
    criterion_ttf = torch.nn.MSELoss()

    engine.model.train()
    loss_history = []
    best_loss = float('inf')
    best_state = None

    for epoch in range(epochs):
        epoch_loss = 0.0
        n_samples = 0
        random.shuffle(combined_data)

        for sample in combined_data:
            optimizer.zero_grad()
            data = engine.topology_to_graph(sample['alarm_embeddings'])
            if data is None:
                continue
            data = data.to(engine.device)
            pred_conf, pred_ttf, _ = engine.model(data.x_dict, data.edge_index_dict)
            target_idx = data.device_to_idx.get(sample['device_id'])
            if target_idx is None:
                continue

            target_conf = torch.tensor(
                [1.0 if sample['actual_failure'] else 0.0],
                dtype=torch.float, device=engine.device
            )
            target_ttf_val = torch.tensor(
                [sample['time_to_failure']],
                dtype=torch.float, device=engine.device
            )
            loss_conf = criterion_conf(pred_conf[target_idx], target_conf)
            loss_ttf = criterion_ttf(pred_ttf[target_idx], target_ttf_val)
            loss = loss_conf + 0.1 * loss_ttf
            loss.backward()
            torch.nn.utils.clip_grad_norm_(engine.model.parameters(), max_norm=1.0)
            optimizer.step()
            epoch_loss += loss.item()
            n_samples += 1

        scheduler.step()
        avg_loss = epoch_loss / max(n_samples, 1)
        loss_history.append(avg_loss)

        if avg_loss < best_loss:
            best_loss = avg_loss
            best_state = {k: v.clone() for k, v in engine.model.state_dict().items()}

        if progress_callback:
            progress_callback(epoch + 1, epochs, avg_loss)

    if best_state is not None:
        torch.save(best_state, model_save_path)
        logger.info("Fine-tuned GNN model saved to %s", model_save_path)

    elapsed = time.time() - start_time
    return {
        "epochs": epochs,
        "total_samples": len(combined_data),
        "stream_samples": len(training_data),
        "synthetic_samples": len(synthetic_data),
        "final_loss": loss_history[-1] if loss_history else None,
        "best_loss": best_loss,
        "elapsed_sec": elapsed,
        "model_path": model_save_path,
        "loss_history": loss_history,
    }


def load_pretrained_gnn(
    topology: Dict[str, Any],
    children_map: Dict[str, List[str]],
) -> Optional[GNNPredictionEngine]:
    """
    事前学習済みモデルをロードしたGNNエンジンを返す。
    モデルが存在しなければ None。
    """
    model_path = get_pretrained_model_path()
    if model_path is None:
        return None

    try:
        engine = GNNPredictionEngine(topology, children_map, model_path=model_path)
        logger.info(f"Loaded pretrained GNN from {model_path}")
        return engine
    except Exception as e:
        logger.warning(f"Failed to load pretrained GNN: {e}")
        return None
