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

### FAD-2: トポロジーマップのゾーン表示 — エリア別グルーピング

**背景:** C拠点（18ノード）のようにサーバ・クラウドを含む大規模トポロジーでは、
ノードがどの物理エリア（データセンター、ラック列、クラウドリージョン等）に
属するかを視覚的に把握する必要がある。

**方針:** トポロジーJSONに `_zones` メタデータを定義し、vis.js の
`beforeDrawing` イベントで半透明の背景ボックスを描画する。

**準備済み（実装途中）:**
- `topologies/topology_c.json` に `_zones` 定義を追加済み
- `registry.py` で `_` 始まりキーをスキップするガード追加済み
- `ui/graph.py` に `_load_zones_for_site()` ヘルパー追加済み

**残作業:**
- vis.js の `beforeDrawing` コールバックでゾーンボックスを描画
- エッジを直線化（`smooth: false`）し、視認性向上
- ゾーンラベルをボックス上部に表示

**`_zones` フォーマット（topology JSON 内）:**
```json
{
  "_zones": {
    "dc_core": {
      "label": "DC Core (Network Infrastructure)",
      "color": "rgba(200,230,201,0.18)",
      "border": "#a5d6a7",
      "nodes": ["DC_ROUTER_C01", "FW_C01_PRIMARY", ...]
    },
    "aws_cloud": {
      "label": "AWS Cloud (ap-northeast-1)",
      "color": "rgba(237,231,246,0.22)",
      "border": "#b39ddb",
      "nodes": ["AWS_DX_C01", "AWS_TGW_C01", ...]
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
