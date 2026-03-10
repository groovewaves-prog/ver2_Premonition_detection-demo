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

## Session Handover Rule
セッション終了時（ユーザーが作業完了を伝えた時、またはpush完了時）に、
HANDOVER.md を以下のフォーマットで作成・更新し、コミットすること。

### HANDOVER.md フォーマット
- 日付・ブランチ名
- 完了したタスク一覧
- 未完了・保留タスク
- 既知の問題・注意点
- 次セッションへの推奨アクション
