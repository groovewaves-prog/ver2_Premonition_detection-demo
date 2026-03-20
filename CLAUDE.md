# Project: AIOps Incident Cockpit — Multi-Site Edition

## Quick Summary
ネットワーク障害の根本原因分析 + Digital Twin による予兆検知ツール。
WARNING アラームから将来の CRITICAL インシデントを予測し、ダッシュボードに表示する。

## Tech Stack
- Python / Streamlit
- NetworkX (BFS影響伝搬シミュレーション)
- Embedding ベースのエスカレーションルール照合

## Key Entry Points
- `app.py` — Streamlit アプリのエントリポイント
- `dashboard.py` — ダッシュボード UI
- `digital_twin.py` — Digital Twin Engine（予兆検知コア）
- `inference_engine.py` — 推論エンジン
- `alarm_generator.py` — アラーム生成
- `network_ops.py` — ネットワーク操作

## Running
```bash
pip install -r requirements.txt
streamlit run app.py
```

## UI/UX 5原則（リファクタリング・マスターガイド準拠）

以下の原則はプロジェクト全体で厳守すること。

### 原則1: 「システム状態」と「ビュー状態」の厳格な分離
- **システム操作**（現実の変更: 劣化進行度スライダー、予兆発報等）は**左サイドバーに集約**。本番誤操作防止のためシミュレーションモード内に隔離
- **ビュー操作**（視点の変更: What-Ifフェーズセレクター等）は**メイン画面に配置可能**。システム状態を書き換えないプレビュー機能として定義
- 2つの状態は**完全に独立した session_state キー**で管理。互いに書き合わない
  - システム状態: `pred_level`（サイドバースライダー）
  - ビュー状態: `whatif_phase`（What-Ifセレクター）
- `st.session_state` への毎rerunの無条件代入は State Lock を引き起こすため禁止
- **【追記】What-Ifプレビューの適用範囲（ダッシュボード全域の同期）:**
  ビュー状態（`whatif_phase` 等）を変更した場合、その影響範囲は局所的なテキスト領域にとどまらない。
  右側メイン画面を構成する**すべての視覚コンポーネント**が、選択されたプレビュー状態へ完全に同期して切り替わること:
  - ステップメーター（タイムライン進行バー）
  - スピードメーター（ゲージ）
  - 4つのKPIカード（severity, elapsed, remaining, stage label）
  - 時系列グラフの実線/点線境界
  「裏側で保持している現実のシステム状態（`pred_level` スライダーの値）」は絶対に上書きしない。
  「表側のUIコンポーネント群の表示」はすべて `whatif_phase` に従ってダイナミックに描画を更新すること。

### 原則2: 「物理法則」に基づく視覚同期
- フェーズの閾値・ラベルはUIにハードコードしない。バックエンドの `DegradationSequence.stages` から動的取得
- グラフの実線は選択フェーズの閾値クロス位置でスナップ。境界を越えて描画しない
- ゲージ値もステージ代表値にスナップ（ジッター排除）

### 原則3: 視覚的階層のトップダウン化
- KPIパルスアニメーション: CRITICAL severity / level>=4 / RUL<=6h のみで発火
- 根本原因テーブル描画前に BFS で下流ノードを「派生アラート(Symptom)」へ降格
- 派生・無関係アラートは `st.expander(expanded=False)` で初期非表示

### 原則4: エージェントUIの自律性
- ユーザー向けフィードバックボタン（「役に立ちましたか？」等）をUI上に配置しない
- LLM判定後、`_ai_severity_store.record_feedback(is_positive=True)` で自律的にナレッジベース登録

### 原則5: UIゾーニング（領域定義）の原則 — 設定と運用の分離
画面の左側（サイドバー）と右側（メインダッシュボード）の役割を、運用者のワークフローに基づいて厳密に定義し、コンポーネントの配置を決定する。
- **左サイドバー（コンテキスト設定・シミュレーション領域）**: 「これから何を見るか・どういう環境を作るか」という前提条件をセットする操作のみを配置
  - 例: 対象拠点の選択、サービスティアの切替、シミュレーション・モードでの疑似アラート発報
- **右メイン画面（監視・運用タスク領域）**: 「今起きている事象の分析」と「それに対する運用者のアクション」を配置。実運用におけるタスクフローはすべて右側メイン画面内で完結させる
  - 例: インシデントのプレビュー（What-If操作）、予兆ステータス履歴（Inbox）の処理（「対応」「静観」ボタン等）、AIエージェントとのチャット、自動復旧（Remediation）の実行ボタン

## 予兆検知アルゴリズム — 論文・研究基盤リファレンス

本プロジェクトの予兆検知パイプラインで採用している学術論文・アルゴリズムの一覧。
ドキュメント作成・サポート・新規開発者のオンボーディング時に参照すること。

---

### レイヤー1: グラフ異常検知（スペクトル領域）

#### ChiGAD — カイ二乗ウェーブレットフィルタ
- **論文**: Li et al., "ChiGAD", **KDD 2025**
- **実装**: `digital_twin_pkg/gnn.py`
- **解決する課題**: 標準 GNN のローパスフィルタリングが高周波の異常信号を抑制してしまう問題
- **核心技術**:
  - ウェーブレットカーネル: `ψ(s, λ) = λ · exp(-s·λ / 2)`（帯域通過）
  - スケーリングカーネル: `φ(s, λ) = exp(-s·λ / 2)`（低域通過）
  - `ChiSquareWaveletFilterBank`: 多スケールウェーブレット分解（num_scales=3, max_scale=4.0）
  - ゲーテッド融合: 低周波（GNN出力）+ 高周波（ウェーブレット出力）を学習可能な重みで結合
- **出力**: confidence（sigmoid）, time_to_failure（ReLU）, spectral_info

#### Heterogeneous GNN（異種グラフニューラルネットワーク）
- **基盤**: GAT (Graph Attention), GraphSAGE (Neighborhood Aggregation)
- **実装**: `digital_twin_pkg/gnn.py` — `HeteroConv`, `GATConv`, `SAGEConv` (PyTorch Geometric)
- **特徴**: デバイスタイプ・関係種別に応じた異種メッセージパッシング
- **グラフ構造**: `('device', 'depends_on', 'device')` エッジタイプ
- **スペクトル分析**: 正規化ラプラシアン `L_sym = I - D^{-1/2} A D^{-1/2}` の固有値分解

---

### レイヤー2: グラフ偏差検知（ベースライン比較）

#### GDN — Graph Deviation Network
- **論文**: Deng & Hooi, **AAAI 2021**
- **実装**: `digital_twin_pkg/gdn.py`
- **解決する課題**: 正常状態からの多変量偏差を構造的に検出
- **核心技術**:
  - `DeviceBaselineTracker`: Welford のオンラインアルゴリズムによるデバイス毎の正常状態統計量（平均・標準偏差）の逐次更新
  - `GraphDeviationScorer`: 多メトリクス偏差スコアリング
  - Z スコア → シグモイド変換: `sigmoid(max_z - threshold)` で [0,1] に正規化
- **理論**: エスカレーションルールのメトリクス + アラーム特徴量を「センサー」として扱い、学習済みパターンからの偏差を検出

---

### レイヤー3: メトリクス因果推論（暗黙的障害信号）

#### GrayScope — メトリクスベース因果モニタリング
- **論文**: **NSDI 2023**
- **実装**: `digital_twin_pkg/grayscope.py`
- **解決する課題**: 直接アラームが発生しないサイレント障害の検出
- **核心技術**:
  - `MetricCrossCorrelator`: ピアソン相関 + ラグ分析（max_lag=6, 有意閾値 |r|≥0.5, n≥8）
  - `ImplicitFeedbackDetector`: 直接アラームなしの障害パターン検出
  - `MultiHopPropagationTracer`: 多段障害伝搬パスの追跡
  - `SilentFailureScorer`: 確率的サイレント障害スコアリング（ヒューリスティック50%閾値を置換）
- **統合**: Phase 1-3 信号（メトリクストレンド、Granger因果、GDN偏差）を統合した包括的障害検出

#### Granger 因果性検定
- **理論**: 古典的統計手法
- **実装**: `digital_twin_pkg/granger.py` — `granger_f_test()`
- **F検定**: `F = ((RSS_r - RSS_u) / p) / (RSS_u / (n - 2p - 1))`
- **帰無仮説**: ソースデバイスの過去値はターゲットの予測に寄与しない
- **p値近似**: 誤差関数（erfc）による近似（scipy 非依存）
- **用途**: 静的トポロジーを補完する動的因果関係の発見

---

### レイヤー4: トレンド分析・残存寿命推定

#### 線形回帰トレンド分析（STL 簡易版）
- **実装**: `digital_twin_pkg/trend.py` — `analyze_trend()`
- **手法**: `np.polyfit()` による1次多項式フィット
- **出力**: 傾き（劣化方向 [0.0-1.0]）, R²（適合度）, TTF推定値
- **TTF計算**: `TTF = (failure_value - current_value) / slope`
- **用途**: WARNING アラームのメトリクスから CRITICAL 到達時刻を予測

---

### レイヤー5: 確率推論・信頼度統合

#### ベイズ推論エンジン
- **実装**: `digital_twin_pkg/bayesian.py` — `BayesianInferenceEngine`
- **ベイズの定理**: `P(failure|signal) = P(signal|failure) × P(failure) / P(signal)`
- **特徴**:
  - 過去168時間（7日間）の履歴データからの事後確率更新
  - 時系列パターン強化（`_calculate_temporal_boost()`）
  - 事前確率キャッシュ（TTL 3600秒）

#### 信頼度ブースト（多信号集約）
- **実装**: `digital_twin.py`
- **計算式**:
  ```
  confidence = base_confidence
             × (0.8 + 0.2 × match_quality)
             × (1.0 - REDUNDANCY_DISCOUNT)   # HA構成時: 0.15
             × SPOF_BOOST                     # SPOF時: 1.10
  ```
- **閾値**: `EMBEDDING_THRESHOLD = 0.40`, `MIN_PREDICTION_CONFIDENCE = 0.40`

---

### レイヤー6: セマンティック照合

#### Sentence Transformers（all-MiniLM-L6-v2）
- **論文**: Reimers & Gurevych, "Sentence-BERT", **EMNLP 2019**
- **実装**: `digital_twin.py` — `SentenceTransformer('all-MiniLM-L6-v2')`
- **用途**: アラームメッセージとエスカレーションルールのコサイン類似度照合
- **類似度計算**: `cos(θ) = (u·v) / (||u|| × ||v||)`
- **フォールバック**: N-gram ハッシュ埋め込み（`vector_store.py` — オフライン環境用）

#### Vector Store（ChromaDB）
- **実装**: `digital_twin_pkg/vector_store.py`
- **用途**: 類似インシデントのセマンティック検索・過去事例参照

---

### レイヤー7: グラフ伝搬・影響分析

#### BFS（幅優先探索）による障害伝搬
- **実装**: `digital_twin_pkg/common.py`, `digital_twin.py`
- **用途**: ネットワークトポロジー上の下流影響範囲の算出
- **深度制限**: `MAX_PROPAGATION_HOPS = 3`
- **関連構造**: `children_map`（親子隣接リスト）, `redundancy_groups`（HA ペア検出）
- **NetworkX**: `nx.DiGraph`, `nx.bfs_tree()`, `nx.shortest_path_length()`

---

### レイヤー8: クロス検証（多エージェント合意）

#### 2エージェント交差検証
- **実装**: `cross_verification.py`
- **Agent 1（トポロジーベース）**: BFS伝搬 + 冗長性分析 + SPOF検出
- **Agent 2（埋め込みベース）**: エスカレーションルール照合 + セマンティック類似度
- **合意ロジック**: 合意 → 信頼度ボーナス / 不一致 → エスカレーションフラグ
- **目的**: コンセンサスベースの検証による偽陽性削減

---

### レイヤー9: 可視化アルゴリズム

#### GML Grid-based Zone Layout
- **論文**: **PLOS ONE 2019**
- **実装**: `ui/graph.py` — 詳細は FAD-2 セクション参照
- **用途**: トポロジーマップの非重複ゾーン表示

#### vis.js Hierarchical Layout + Post-render Resize
- **実装**: `ui/graph.py` — afterDrawing コールバック
- **参照**: vis.js #1832, Streamlit #4659, Chart.js responsive pattern

---

### パイプライン全体像

```
WARNING アラーム受信
    │
    ├─→ [L6] Sentence Transformer: エスカレーションルール照合
    ├─→ [L4] 線形回帰: メトリクストレンド → TTF推定
    ├─→ [L7] BFS: 下流影響範囲の算出
    │
    ├─→ [L1] ChiGAD + HeteroGNN: スペクトル異常検知
    ├─→ [L2] GDN: ベースライン偏差検知
    ├─→ [L3] GrayScope + Granger: メトリクス因果推論
    │
    ├─→ [L5] ベイズ推論: 事後確率 → 信頼度統合
    ├─→ [L8] 2エージェント交差検証: 偽陽性フィルタ
    │
    └─→ 予兆ステータス発報 (confidence ≥ 0.40)
         └─→ [L9] ダッシュボード可視化
```

---

## 将来の設計方針（Future Architecture Decisions）

以下はまだ実装されていない将来のリファクタリング方針。
要望時に別途設計ドキュメント（例: `docs/design-*.md`）へ展開可能。

---

### FAD-1: デバイスタイプレジストリ — 未知機器への対応

**背景:** 現在、デバイスタイプ（ROUTER, SERVER, CLOUD_GATEWAY 等）の表示定義は
4ファイルにハードコードされており、新タイプ追加に毎回コード修正が必要。
フィジカルAI（エッジ推論機器、自律ロボット等）のように CLI を持たず
REST API / gRPC / MQTT で監視する機器は、現在の前提と根本的に合わない。

**方針:** `configs/device_types.json` に全タイプ定義を集約し、コード修正ゼロで
新デバイスタイプを追加可能にする。

**移行対象（現在のハードコード箇所）:**

| 現在のファイル | ハードコード内容 | 移行先キー |
|---|---|---|
| `ui/graph.py` → `_DEVICE_TYPE_VISUALS` | vis.js 形状・色 | `visual` |
| `ui/autonomous_diagnostic.py` → `_DEVICE_TYPE_DIAGNOSTIC_MAP` | 診断コマンド | `diagnostics` |
| `ui/components/traffic_monitor.py` → `_type_label_map` | ラベル短縮名 | `label` |
| `digital_twin_pkg/engine.py` → if/elif chain | デバイスID推定 | `id_patterns` |

**レジストリフォーマット案:**
```json
{
  "EDGE_AI": {
    "label": "Edge AI",
    "shape": "star",
    "bg": "#fff3e0",
    "border": "#E65100",
    "icon": "🤖",
    "protocol": "rest_api",
    "diagnostics": [
      ["GET /health", "ヘルスチェック"],
      ["GET /metrics/gpu", "GPU温度・利用率"]
    ],
    "id_patterns": ["EDGE_AI", "JETSON", "CORAL"]
  },
  "_unknown": {
    "label": "?",
    "shape": "box",
    "bg": "#fafafa",
    "diagnostics": []
  }
}
```

**フィジカルAI特有の考慮事項:**

| 観点 | 従来のネットワーク機器 | フィジカルAI機器 |
|---|---|---|
| 診断プロトコル | CLI (`show` commands) | REST API / gRPC / MQTT |
| 主要メトリクス | 帯域利用率 | GPU温度、推論レイテンシ、モデル精度 |
| 障害モード | リンクダウン、パケットロス | モデルドリフト、GPU OOM、推論タイムアウト |
| トポロジー | 親子階層 | メッシュ / エッジクラスタの可能性 |

**対応フロー（実装時）:**
1. `configs/device_types.json` を作成
2. 各ハードコード箇所をレジストリ参照に置換
3. 未登録タイプは `_unknown` にフォールバック（描画は崩れない）

---

### FAD-2: トポロジーマップのゾーン表示 — エリア別グルーピング【実装済み】

**背景:** C拠点（18ノード）のようにサーバ・クラウドを含む大規模トポロジーでは、
ノードがどの物理エリア（データセンター、ラック列、クラウドリージョン等）に
属するかを視覚的に把握する必要がある。

**実装済みアルゴリズム: GML Grid-based Zone Layout**

ゾーン矩形の非重複を**構造的に保証**するトップダウン方式。
学術的根拠: GML (Grid and Modularity-based Layout, PLOS ONE 2019)

| レイヤー | 処理内容 | ファイル |
|---|---|---|
| Python `_compute_fixed_positions()` Pass 1.5 | 各グリッド列の必要幅を動的計算 | `ui/graph.py` |
| Python Pass 4 | `_col_bounds` + `_row_bounds` メタデータを zones に注入 | `ui/graph.py` |
| JS `beforeDrawing` Pass 1 | `grid: [col, row, colspan, rowspan]` + `_col_bounds` + `_row_bounds` からゾーン矩形をトップダウン算出 | `ui/graph.py` |
| JS Pass 1b | 安全ネット: ノード BB がセル外にはみ出す場合のみ拡張 | `ui/graph.py` |
| JS Pass 2 | エンベロープ = 子ゾーンの和集合 + パディング | `ui/graph.py` |
| JS Pass 3 | 描画 (エンベロープ → ゾーン) | `ui/graph.py` |

**設計原則（必ず遵守）:**
1. **ゾーン矩形はグリッドセル境界から決定する（トップダウン）。** ノード BB からボトムアップに算出してはならない。ボトムアップ方式は vis.js の描画サイズ誤差で重なりが発生し、midpoint snapping + origBounds clamp の悪循環で解消不能になる。
2. **衝突解消ロジックを書かない。** グリッドセルは Python 側で非重複に配置済みであり、JS 側で collision resolution は不要。追加すると複雑化するだけで効果がない。
3. **グリッド情報がないゾーンのみフォールバック** として旧来のノード BB 方式を使用する。
4. **キャンバス高さは post-render resize で動的決定する。** Python 側ではコンテナ実幅が不明なため `_canvas_h` を正確に算出できない（vis.js #1832, Streamlit #4659）。`afterDrawing` で `clientWidth` を取得し、コンテンツのアスペクト比から必要高さを逆算 → iframe を動的リサイズ。`_canvas_h` はフォールバック初期値としてのみ機能する。
5. **固定座標レイアウトで afterDrawing リフローを実行しない。** `moveNode()` は `fixed: true` ノードも移動するため、グリッド配置を破壊する。リフローは hierarchical レイアウト（Site A/B）専用。

**`_zones` フォーマット（topology JSON 内）:**
```json
{
  "_zones": {
    "_grid": {"col_width": 380, "node_h_gap": 200, "font_size": 12, "edge_gap": 45, "zone_gap": 30},
    "_envelopes": {
      "site_c_dc": {
        "label": "Site-C データセンター",
        "children": ["dc_core", "web_tier", "app_tier", "db_tier"]
      }
    },
    "dc_core": {
      "label": "DC Core",
      "grid": [0, 0, 3, 1],
      "rows": [["DC_ROUTER_C01"], ["FW_C01_PRIMARY", "FW_C01_SECONDARY"]],
      "nodes": ["DC_ROUTER_C01", "FW_C01_PRIMARY", "FW_C01_SECONDARY"]
    }
  }
}
```

---

## Session Handover Rule
セッション終了時（ユーザーが作業完了を伝えた時、またはpush完了時）に、
HANDOVER.md を以下のフォーマットで作成・更新し、コミットすること。

### HANDOVER.md フォーマット
- 日付・ブランチ名
- 完了したタスク一覧
- 未完了・保留タスク
- 既知の問題・注意点
- 次セッションへの推奨アクション
