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

### 原則1: 「システム状態」と「ビュー状態」の厳格な分離
- **システム操作**（現実の変更: 劣化進行度スライダー、予兆発報等）は**左サイドバーに集約**。本番誤操作防止のためシミュレーションモード内に隔離
- **ビュー操作**（視点の変更: What-Ifフェーズセレクター等）は**メイン画面に配置可能**。システム状態を書き換えないプレビュー機能として定義
- 2つの状態は**完全に独立した session_state キー**で管理。互いに書き合わない
  - システム状態: `pred_level`（サイドバースライダー）
  - ビュー状態: `whatif_phase`（What-Ifセレクター）
- `st.session_state` への毎rerunの無条件代入は State Lock を引き起こすため禁止

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
