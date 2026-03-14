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

## UI/UX 4原則（リファクタリング・マスターガイド準拠）

以下の原則はプロジェクト全体で厳守すること。

### 原則1: 状態管理の一元化（Single Source of Truth）
- **操作コンポーネント**（スライダー・セレクトボックス等）はすべて**左サイドバーに集約**
- **右側メインダッシュボード**には操作可能な入力コンポーネントを配置しない
- 右側に状態を表示する場合は「表示専用インジケーター」（read-only HTML/SVG）として実装
- `st.session_state` への書き込みは `on_change` コールバック経由のみ。毎rerunの無条件代入は State Lock を引き起こすため禁止

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

## Session Handover Rule
セッション終了時（ユーザーが作業完了を伝えた時、またはpush完了時）に、
HANDOVER.md を以下のフォーマットで作成・更新し、コミットすること。

### HANDOVER.md フォーマット
- 日付・ブランチ名
- 完了したタスク一覧
- 未完了・保留タスク
- 既知の問題・注意点
- 次セッションへの推奨アクション
