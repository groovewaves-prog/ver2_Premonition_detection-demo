# digital_twin_pkg/gnn.py - Graph Neural Network モジュール
# ChiGAD ウェーブレットフィルタ統合 Heterogeneous GNN
#
# 参考論文:
#   Li et al., "ChiGAD", KDD 2025
#   — カイ二乗ウェーブレットフィルタによるグラフ異常検知
#   — 高周波成分（異常信号）を保持し、標準GNNのローパス問題を解決

from __future__ import annotations
import logging
import math
from typing import Dict, List, Optional, Tuple, Any
import numpy as np

logger = logging.getLogger(__name__)

try:
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
    from torch_geometric.nn import GATConv, SAGEConv, HeteroConv, Linear
    from torch_geometric.data import HeteroData
    from torch_geometric.utils import to_dense_adj, add_self_loops, degree
    HAS_PYTORCH_GEOMETRIC = True
except ImportError:
    HAS_PYTORCH_GEOMETRIC = False
    logger.warning("PyTorch Geometric not available. GNN features disabled.")
    nn = None
    F = None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ChiSquare Wavelet Filter (ChiGAD KDD 2025)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
if HAS_PYTORCH_GEOMETRIC:

    def _compute_normalized_laplacian(edge_index: torch.Tensor, num_nodes: int) -> torch.Tensor:
        """正規化ラプラシアン L_sym = I - D^{-1/2} A D^{-1/2} を計算"""
        edge_index_sl, _ = add_self_loops(edge_index, num_nodes=num_nodes)
        row, col = edge_index_sl[0], edge_index_sl[1]
        deg = degree(row, num_nodes=num_nodes, dtype=torch.float)
        deg_inv_sqrt = deg.pow(-0.5)
        deg_inv_sqrt[deg_inv_sqrt == float('inf')] = 0.0

        # D^{-1/2} A D^{-1/2}
        adj = to_dense_adj(edge_index_sl, max_num_nodes=num_nodes)[0]
        d_inv_sqrt = torch.diag(deg_inv_sqrt)
        norm_adj = d_inv_sqrt @ adj @ d_inv_sqrt

        laplacian = torch.eye(num_nodes, device=edge_index.device) - norm_adj
        return laplacian

    def _chi_square_wavelet_kernel(eigenvalues: torch.Tensor, scale: float) -> torch.Tensor:
        """
        カイ二乗ウェーブレットカーネル (ChiGAD, Eq. 3)

        ψ(s, λ) = λ * exp(-s * λ / 2)

        - s: スケールパラメータ (小さい→高周波, 大きい→低周波)
        - λ: ラプラシアン固有値 (0=DC, 2=最高周波数)

        このカーネルは:
        - λ=0 でゼロ（DCを除去）
        - 小さい s でピークが高周波側にシフト
        - 大きい s でピークが低周波側にシフト
        """
        return eigenvalues * torch.exp(-scale * eigenvalues / 2.0)

    def _chi_square_scaling_kernel(eigenvalues: torch.Tensor, scale: float) -> torch.Tensor:
        """
        スケーリング関数（低域フィルタ）

        φ(s, λ) = exp(-s * λ / 2)

        ウェーブレット分解の残差（平均的なトレンド成分）を捕捉
        """
        return torch.exp(-scale * eigenvalues / 2.0)


    class ChiSquareWaveletFilterBank(nn.Module):
        """
        カイ二乗ウェーブレットフィルタバンク

        マルチスケールのウェーブレットカーネルで信号を周波数帯域に分解:
        - スケーリング（低域）: 正常な相関パターン（ゆるやかな傾向）
        - ウェーブレット（高域）: 異常信号（急激な変化・逸脱）

        標準GNNのローパス問題を解決:
        標準GNN (GAT/GCN) は信号を平滑化するため高周波異常を抑圧するが、
        本フィルタは高周波成分を明示的に保持・増幅する。
        """
        def __init__(
            self,
            in_channels: int,
            out_channels: int,
            num_scales: int = 3,
            max_scale: float = 4.0,
        ):
            super().__init__()
            self.num_scales = num_scales
            # スケールパラメータ: 学習可能（初期値は対数等間隔）
            init_scales = torch.linspace(
                math.log(0.5), math.log(max_scale), num_scales
            )
            self.log_scales = nn.Parameter(init_scales)

            # 各スケールのウェーブレット出力 + 1つのスケーリング出力を結合
            total_bands = num_scales + 1  # wavelet bands + 1 scaling band
            self.combine = nn.Linear(in_channels * total_bands, out_channels)

            # 帯域ごとの重要度（学習可能）— 説明可能性に使用
            self.band_importance = nn.Parameter(torch.ones(total_bands) / total_bands)

            # キャッシュ（トポロジ不変の間は再計算不要）
            self._cached_decomposition = None
            self._cached_edge_hash = None

        def _spectral_decompose(self, edge_index: torch.Tensor, num_nodes: int):
            """ラプラシアン固有値分解（キャッシュ付き）"""
            edge_hash = hash(edge_index.data_ptr()) if edge_index.numel() > 0 else 0
            if self._cached_decomposition is not None and self._cached_edge_hash == edge_hash:
                return self._cached_decomposition

            L = _compute_normalized_laplacian(edge_index, num_nodes)
            # 固有値分解: L = U Λ U^T
            eigenvalues, eigenvectors = torch.linalg.eigh(L)
            # 数値安定性: 固有値を [0, 2] にクランプ
            eigenvalues = eigenvalues.clamp(0.0, 2.0)

            self._cached_decomposition = (eigenvalues, eigenvectors)
            self._cached_edge_hash = edge_hash
            return eigenvalues, eigenvectors

        def forward(
            self, x: torch.Tensor, edge_index: torch.Tensor, num_nodes: int
        ) -> Tuple[torch.Tensor, Dict[str, torch.Tensor]]:
            """
            Args:
                x: ノード特徴量 [N, in_channels]
                edge_index: エッジインデックス [2, E]
                num_nodes: ノード数

            Returns:
                filtered: フィルタ済み特徴量 [N, out_channels]
                band_scores: 周波数帯域別のエネルギー（説明可能性用）
                    {
                        "low_freq_energy": [N],    # 低周波（正常パターン）
                        "high_freq_energy": [N],   # 高周波（異常信号）
                        "band_weights": [num_scales+1],  # 帯域重要度
                        "anomaly_spectral_score": [N],  # スペクトル異常スコア
                    }
            """
            eigenvalues, U = self._spectral_decompose(edge_index, num_nodes)
            # スペクトル領域への変換: x_hat = U^T x
            x_hat = U.t() @ x  # [N, in_channels]

            scales = torch.exp(self.log_scales)  # 正の値に変換
            band_outputs = []
            band_energies = []

            # スケーリング関数（低域フィルタ）
            h_scaling = _chi_square_scaling_kernel(eigenvalues, scales[-1])
            # フィルタ適用: U * diag(h) * U^T * x
            filtered_scaling = U @ (h_scaling.unsqueeze(1) * x_hat)
            band_outputs.append(filtered_scaling)
            band_energies.append(filtered_scaling.norm(dim=1))

            # ウェーブレットフィルタ（各スケール）
            for i in range(self.num_scales):
                h_wavelet = _chi_square_wavelet_kernel(eigenvalues, scales[i])
                filtered_wavelet = U @ (h_wavelet.unsqueeze(1) * x_hat)
                band_outputs.append(filtered_wavelet)
                band_energies.append(filtered_wavelet.norm(dim=1))

            # 帯域結合（学習可能な重み付き）
            importance = F.softmax(self.band_importance, dim=0)
            weighted_bands = []
            for i, band in enumerate(band_outputs):
                weighted_bands.append(band * importance[i])

            concatenated = torch.cat(weighted_bands, dim=1)  # [N, in_channels * (num_scales+1)]
            filtered = self.combine(concatenated)

            # ─── 説明可能性: 周波数帯域別エネルギー ───
            energy_stack = torch.stack(band_energies, dim=1)  # [N, num_scales+1]
            low_freq_energy = energy_stack[:, 0]  # スケーリング = 低域
            high_freq_energy = energy_stack[:, 1:].sum(dim=1)  # ウェーブレット = 高域

            # スペクトル異常スコア: 高周波/全エネルギー比
            total_energy = (low_freq_energy + high_freq_energy).clamp(min=1e-8)
            anomaly_spectral_score = high_freq_energy / total_energy

            band_scores = {
                "low_freq_energy": low_freq_energy.detach(),
                "high_freq_energy": high_freq_energy.detach(),
                "band_weights": importance.detach(),
                "anomaly_spectral_score": anomaly_spectral_score.detach(),
            }

            return filtered, band_scores


    class HeteroNetworkGNN(nn.Module):
        """
        ChiGAD ウェーブレットフィルタ統合 Heterogeneous GNN

        アーキテクチャ:
        1. ノード特徴量プロジェクション (768 → hidden_dim)
        2. ヘテロGNN畳み込み (GAT: 親子関係, SAGE: 冗長関係)
           — 標準的なメッセージパッシング（低域フィルタとして機能）
        3. ChiSquare ウェーブレットフィルタバンク
           — 高周波異常信号を保持・増幅
        4. 低域(GNN出力) + 高域(ウェーブレット出力) のゲート融合
        5. 予測ヘッド (confidence, time_to_failure)

        これにより、ChiGAD論文(KDD 2025)が指摘した
        「標準GNNは高周波異常信号を平滑化してしまう」問題を解決する。
        """
        def __init__(
            self,
            input_dim: int = 768,
            hidden_dim: int = 128,
            output_dim: int = 64,
            num_layers: int = 3,
            num_wavelet_scales: int = 3,
            dropout: float = 0.2
        ):
            super().__init__()
            self.dropout = dropout
            self.hidden_dim = hidden_dim

            # ノード特徴量の次元削減（プロジェクション）
            self.node_proj = Linear(input_dim, hidden_dim)

            # ━━━ 低域パス: ヘテロGNN畳み込み ━━━
            self.convs = nn.ModuleList()
            for i in range(num_layers):
                conv = HeteroConv({
                    # 親子関係: カスケード障害の波及をAttention(GAT)で学習
                    ('device', 'depends_on', 'device'): GATConv(
                        -1, hidden_dim, add_self_loops=False
                    ),
                    # 冗長関係: 互いの状態を平均化(SAGE)して補完関係を学習
                    ('device', 'redundant_with', 'device'): SAGEConv(-1, hidden_dim)
                }, aggr='sum')
                self.convs.append(conv)

            # ━━━ 高域パス: ChiGAD ウェーブレットフィルタバンク ━━━
            self.wavelet_filter = ChiSquareWaveletFilterBank(
                in_channels=hidden_dim,
                out_channels=hidden_dim,
                num_scales=num_wavelet_scales,
            )

            # ━━━ ゲート融合: 低域(GNN) + 高域(ウェーブレット) ━━━
            # 学習可能なゲートで低域・高域の混合比を動的に調整
            self.gate = nn.Sequential(
                nn.Linear(hidden_dim * 2, hidden_dim),
                nn.Sigmoid()
            )

            # ━━━ 予測ヘッド ━━━
            self.fc_confidence = nn.Linear(hidden_dim, 1)
            self.fc_time_to_failure = nn.Linear(hidden_dim, 1)

        def forward(
            self, x_dict, edge_index_dict
        ) -> Tuple[torch.Tensor, torch.Tensor, Optional[Dict[str, torch.Tensor]]]:
            """
            Returns:
                confidence: [N, 1] — 各ノードの異常信頼度
                time_to_failure: [N, 1] — 推定残存時間
                spectral_info: 周波数帯域別の異常スコア（説明可能性用）
            """
            # ─── 初期プロジェクション ───
            x = self.node_proj(x_dict['device'])
            x = F.relu(x)
            num_nodes = x.size(0)
            x_dict_current = {'device': x}

            # ─── 低域パス: ヘテロGNN畳み込み (Residual) ───
            for conv in self.convs:
                x_in = x_dict_current['device']
                x_dict_current = conv(x_dict_current, edge_index_dict)
                x_out = x_dict_current['device']
                x_out = F.relu(x_out)
                x_out = F.dropout(x_out, p=self.dropout, training=self.training)
                if x_in.shape == x_out.shape:
                    x_out = x_out + x_in  # Residual
                x_dict_current['device'] = x_out

            x_low = x_dict_current['device']  # 低域出力

            # ─── 高域パス: ウェーブレットフィルタ ───
            # depends_on エッジ上でウェーブレットフィルタを適用
            # （カスケード障害の高周波信号を捕捉）
            depends_edge = edge_index_dict.get(
                ('device', 'depends_on', 'device')
            )
            spectral_info = None

            if depends_edge is not None and depends_edge.numel() > 0:
                x_high, spectral_info = self.wavelet_filter(
                    x, depends_edge, num_nodes
                )
            else:
                x_high = x  # エッジがない場合はプロジェクション出力をそのまま使用

            # ─── ゲート融合 ───
            # g = σ(W[x_low; x_high])
            # output = g * x_low + (1 - g) * x_high
            gate_input = torch.cat([x_low, x_high], dim=1)
            g = self.gate(gate_input)
            out = g * x_low + (1.0 - g) * x_high

            # ─── 予測 ───
            confidence = torch.sigmoid(self.fc_confidence(out))
            time_to_failure = F.relu(self.fc_time_to_failure(out))

            return confidence, time_to_failure, spectral_info

else:
    class HeteroNetworkGNN:
        """Dummy class when PyTorch Geometric is not installed"""
        def __init__(self, *args, **kwargs): pass


class GNNPredictionEngine:
    def __init__(
        self,
        topology: Dict[str, Any],
        children_map: Dict[str, List[str]],
        model_path: Optional[str] = None
    ):
        self.topology = topology
        self.children_map = children_map
        self.model = None
        self._cached_topology_structure = None

        if not HAS_PYTORCH_GEOMETRIC:
            logger.warning("PyTorch Geometric not available. GNN features disabled.")
            return

        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.model = HeteroNetworkGNN().to(self.device)

        if model_path:
            try:
                self.model.load_state_dict(torch.load(model_path, map_location=self.device))
                self.model.eval()
                logger.info(f"Loaded pretrained GNN model from {model_path}")
            except Exception as e:
                logger.warning(f"Failed to load model from {model_path}: {e}")

    def topology_to_graph(
        self,
        alarm_embeddings: Dict[str, np.ndarray],
        device_states: Optional[Dict[str, Dict]] = None
    ) -> Optional[HeteroData]:
        if not HAS_PYTORCH_GEOMETRIC:
            return None

        current_topo_state = str(list(self.topology.keys())) + str(self.children_map)

        if self._cached_topology_structure is None or getattr(self, '_last_topo_state', '') != current_topo_state:

            logger.info("Topology change detected or cache empty. Rebuilding GNN graph structure...")

            device_ids = list(self.topology.keys())
            device_to_idx = {dev_id: idx for idx, dev_id in enumerate(device_ids)}

            # 親子関係エッジ (depends_on)
            depends_edges = []
            for parent_id, children in self.children_map.items():
                if parent_id not in device_to_idx: continue
                p_idx = device_to_idx[parent_id]
                for child_id in children:
                    if child_id not in device_to_idx: continue
                    c_idx = device_to_idx[child_id]
                    depends_edges.append([p_idx, c_idx])
                    depends_edges.append([c_idx, p_idx])

            # 冗長関係エッジ (redundant_with)
            redundant_groups = {}
            for dev_id, attrs in self.topology.items():
                rg = attrs.get('redundancy_group') if isinstance(attrs, dict) else getattr(attrs, 'redundancy_group', None)
                if rg:
                    redundant_groups.setdefault(rg, []).append(device_to_idx[dev_id])

            redundant_edges = []
            for members in redundant_groups.values():
                for i in range(len(members)):
                    for j in range(i + 1, len(members)):
                        redundant_edges.append([members[i], members[j]])
                        redundant_edges.append([members[j], members[i]])

            if not depends_edges: depends_edges = [[0, 0]]
            if not redundant_edges: redundant_edges = [[0, 0]]

            self._cached_topology_structure = {
                'device_ids': device_ids,
                'device_to_idx': device_to_idx,
                'depends_on': torch.tensor(depends_edges, dtype=torch.long).t().contiguous(),
                'redundant_with': torch.tensor(redundant_edges, dtype=torch.long).t().contiguous()
            }
            self._last_topo_state = current_topo_state

        cache = self._cached_topology_structure
        device_ids = cache['device_ids']

        # ノード特徴量（アラーム情報）の構築のみ毎回実行
        node_features = []
        for dev_id in device_ids:
            if dev_id in alarm_embeddings:
                feature = alarm_embeddings[dev_id]
            else:
                feature = np.zeros(768)
            node_features.append(feature)

        x = torch.tensor(np.array(node_features), dtype=torch.float)

        data = HeteroData()
        data['device'].x = x
        data['device', 'depends_on', 'device'].edge_index = cache['depends_on']
        data['device', 'redundant_with', 'device'].edge_index = cache['redundant_with']

        data.device_ids = device_ids
        data.device_to_idx = cache['device_to_idx']

        return data

    def predict_with_gnn(
        self,
        alarm_embeddings: Dict[str, np.ndarray],
        target_device_id: str
    ) -> Tuple[float, float, Optional[Dict[str, float]]]:
        """
        GNNによる予測（ChiGADウェーブレットフィルタ統合）

        Returns:
            confidence: 異常信頼度 [0, 1]
            time_to_failure: 推定残存時間（時間）
            spectral_scores: 周波数帯域別の異常スコア（説明可能性用）
                {
                    "anomaly_spectral_score": float,  # 高周波比率（高い=異常的）
                    "low_freq_energy": float,          # 低周波エネルギー（正常パターン）
                    "high_freq_energy": float,         # 高周波エネルギー（異常信号）
                }
        """
        if not HAS_PYTORCH_GEOMETRIC or self.model is None:
            return 0.5, 336.0, None

        data = self.topology_to_graph(alarm_embeddings)
        if data is None or target_device_id not in data.device_to_idx:
            return 0.5, 336.0, None

        target_idx = data.device_to_idx[target_device_id]
        data = data.to(self.device)

        self.model.eval()
        with torch.no_grad():
            confidence, time_to_failure, spectral_info = self.model(
                data.x_dict, data.edge_index_dict
            )

        target_confidence = float(confidence[target_idx].cpu().numpy())
        target_ttf = float(time_to_failure[target_idx].cpu().numpy())

        # 周波数帯域別スコアの抽出
        spectral_scores = None
        if spectral_info is not None:
            spectral_scores = {
                "anomaly_spectral_score": float(
                    spectral_info["anomaly_spectral_score"][target_idx].cpu().numpy()
                ),
                "low_freq_energy": float(
                    spectral_info["low_freq_energy"][target_idx].cpu().numpy()
                ),
                "high_freq_energy": float(
                    spectral_info["high_freq_energy"][target_idx].cpu().numpy()
                ),
            }

        return target_confidence, target_ttf, spectral_scores

    def train_on_historical_data(
        self,
        training_data: List[Dict],
        epochs: int = 100,
        lr: float = 0.001
    ):
        if not HAS_PYTORCH_GEOMETRIC or self.model is None:
            logger.warning("GNN training not available")
            return

        optimizer = torch.optim.Adam(self.model.parameters(), lr=lr)
        criterion_confidence = nn.BCELoss()
        criterion_ttf = nn.MSELoss()

        self.model.train()
        for epoch in range(epochs):
            total_loss = 0.0
            for sample in training_data:
                optimizer.zero_grad()

                data = self.topology_to_graph(sample['alarm_embeddings'])
                if data is None: continue

                data = data.to(self.device)

                pred_conf, pred_ttf, _ = self.model(data.x_dict, data.edge_index_dict)

                target_idx = data.device_to_idx.get(sample['device_id'])
                if target_idx is None: continue

                target_conf = torch.tensor([1.0 if sample['actual_failure'] else 0.0],
                                          dtype=torch.float, device=self.device)
                target_ttf_val = torch.tensor([sample['time_to_failure']],
                                            dtype=torch.float, device=self.device)

                loss_conf = criterion_confidence(pred_conf[target_idx], target_conf)
                loss_ttf = criterion_ttf(pred_ttf[target_idx], target_ttf_val)
                loss = loss_conf + 0.1 * loss_ttf

                loss.backward()
                optimizer.step()
                total_loss += loss.item()

            if (epoch + 1) % 10 == 0:
                avg_loss = total_loss / max(len(training_data), 1)
                logger.info(f"Epoch {epoch+1}/{epochs}, Loss: {avg_loss:.4f}")


def create_gnn_engine(topology: dict, children_map: dict) -> Optional[GNNPredictionEngine]:
    if not HAS_PYTORCH_GEOMETRIC:
        return None
    try:
        return GNNPredictionEngine(topology, children_map)
    except Exception as e:
        logger.error(f"Failed to create ChiGAD Hetero GNN engine: {e}")
        return None
