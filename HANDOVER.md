# Session Handover

## 日付・ブランチ
- **日付**: 2026-03-11
- **ブランチ**: `claude/improve-project-discovery-e022I`

## 完了したタスク（今回セッション）

### 1. cockpit.py リファクタリング（2346行 → 536行 + コンポーネント群）
- **Before**: cockpit.py = 2346行の巨大モノリス
- **After**: cockpit.py = 536行（オーケストレータ）+ `ui/components/` 配下に12ファイル
- 分割構成:
  - `ui/components/helpers.py` — 共通ヘルパー（CI構築、サニタイズ等）
  - `ui/components/report_builders.py` — LLMプロンプト構築（診断ワークブック、予防措置プラン）
  - `ui/components/kpi_banner.py` — KPIメトリクス + ステータスバナー
  - `ui/components/future_radar.py` — 予兆専用表示エリア（AIOps Future Radar）
  - `ui/components/root_cause_table.py` — 根本原因候補テーブル + 派生/ノイズ一覧
  - `ui/components/topology_panel.py` — 左カラム（トポロジー + 影響伝搬 + AI学習ルール + Auto-Diagnostics）
  - `ui/components/analyst_report.py` — AI Analyst Report
  - `ui/components/remediation.py` — Remediation & Execute（予兆ステータス履歴含む）
  - `ui/components/chat_panel.py` — Chat with AI Agent
  - `ui/components/diagnostic.py` — Auto-Diagnostics 実行関数
- 後方互換性を維持: `run_diagnostic` は cockpit.py から再エクスポート
- `utils/helpers.py` の Alarm import を `alarm_generator` から直接に修正（循環import回避）

### 2. サービスティア基盤（段階的導入対応）
- `ui/service_tier.py` を新規作成
- 3ティア定義: `BASIC` (Phase 1-2) / `PHM` (Phase 3) / `FULL` (Phase 4+)
- 環境変数 `SERVICE_TIER` で切り替え可能（デフォルト: full）
- `render_tier_gated()` コンテキストマネージャでグレーアウト表示
- バックエンド停止なし（UIのみのゲーティング = デモに影響しない設計）

### 3. トポロジーマップとLegendの間隔修正
- `ui/graph.py`: vis.js コンテナ高さを600px→640px、iframe高さを680→650に調整
- Legend の `margin-top` を4px→-8pxに変更し、マップ直下に密着配置

### 4. 画面表示の高速化（2段階）
#### 第1段階（初回最適化）
- アラームデバイスIDセットの事前計算（O(n*m) → O(n+m)）
- GenAI モデルの session_state キャッシュ（毎回の再初期化を回避）
- ストリーム自動リフレッシュ間隔を2s→1sに短縮
- Remediation完了後のsleepを1.0s→0.5s、履歴クローズのsleepを0.8s→0.3sに短縮

#### 第2段階（深層最適化）
- **inference_engine.py**: `analyze()` 内にアラームハッシュベースのキャッシュ追加
  - 同一アラームセットなら GrayScope 分析をスキップ（`_analyze_cache_grayscope`）
  - 同一アラームセットなら Granger ペアワイズテストをスキップ（`_analyze_cache_granger_applied`）
  - `sev_order` 辞書をループ外で1回だけ定義
- **ui/cockpit.py**: `compute_topo_hash()` の結果を session_state にキャッシュ（毎描画の再計算回避）
  - AI動的トリアージ生成結果を session_state にキャッシュ（同一デバイス+メッセージならLLM呼び出しスキップ）
- **ui/graph.py**: vis.js バージョン固定（CDN再フェッチ抑制）、`network.fit()` のアニメーション無効化
  - トポロジーグラフHTML全体をキャッシュ（アラーム/分析結果が同一なら再構築スキップ）
- **ui/stream_dashboard.py**: 劣化曲線SVGキャッシュチェックを先行実行
  - ヒット時は `get_metric_history()` / `get_realtime_metric_history()` 計算自体をスキップ

## 過去セッションの完了タスク（参考）
- Phase 1: トレンド検出（メトリクス時系列分析）
- Phase 2: Granger因果テスト
- Phase 3: GDN ベースライン偏差検出
- Phase 4: GrayScope型メトリクス因果監視
- バグ修正、ノードマップ視認性改善、KPIカード文字欠け修正

### 5. 将来拡張 A/B/C の実装（コマンド実行ポップアップ + トリアージボタン化）

#### A. Execute結果のポップアップ（`ui/components/remediation.py`）
- `_execute_remediation()` の実行結果を `st.dialog` ポップアップで表示
- 修復3ステップ（Backup/Apply/Verify）+ 検証コマンド（show interfaces, show logging, ping）の結果を一覧表示
- 各コマンドの実行時間・出力を個別展開可能

#### B. 初動トリアージのボタン化（`ui/components/future_radar.py`）
- 従来のHTML静的表示 → Streamlitボタンに変換
- プライオリティ別アイコン: 🔴最優先(primary) / 🟠推奨(secondary) / 🔵その他
- 個別実行ボタン: 各トリアージコマンドを個別に実行 → 結果ポップアップ
- 一括実行ボタン: 全コマンドをまとめて実行 → 結果一覧ポップアップ
- `steps` フィールドの改行区切り複数コマンドにも対応

#### C. 予兆シミュレーションでのExecute対応（`ui/components/remediation.py`）
- 予兆（is_prediction=True）時の予防措置Executeも同じポップアップ機構を使用
- ポップアップタイトルを「🔮 予防措置 実行結果」に自動切替
- エラー時もポップアップで詳細表示

#### 共通基盤: `ui/components/command_popup.py`（新規）
- `simulate_command_execution()`: デモ環境用コマンド実行シミュレーション
  - show系コマンド（interfaces, processes cpu, memory, logging, environment, version, ip route, bgp summary）のリアルな出力テンプレート
  - request, ping コマンドにも対応
- `render_command_result_popup()`: ポップアップデータをsession_stateに保存
- `show_command_popup_if_pending()`: `@st.dialog` でポップアップを描画（成功/エラーの色分け、展開UI）
- `render_triage_cards()`: 初動トリアージカード表示（予兆・障害共通コンポーネント）
- `extract_cli_commands()`: 手順テキストからCLIコマンドのみ自動抽出（人手作業はフィルタ）

### 6. 障害発生時の初動トリアージ対応
- **cockpit.py**: 障害シナリオ時（scenario != "正常稼働"）、root_cause デバイスにも LLM でトリアージ自動生成
  - 結果は `recommended_actions` として分析結果に付与、session_state キャッシュで再生成回避
- **root_cause_table.py**: 選択された root_cause 候補にトリアージがあれば、根本原因テーブル直下に expander で表示
  - 予兆候補は Future Radar 側で表示するため重複回避
- **トリアージカード共有化**: `render_triage_cards()` を `command_popup.py` に集約
  - `future_radar.py` / `root_cause_table.py` 両方から同一コンポーネントを使用

### 7. ノードマップの間隔調整
- `ui/graph.py`: `levelSeparation` 120→160px、`nodeSpacing` 180→220px、`treeSpacing` 220→250px
- コンテナ高さ 640→700px、iframe 650→720px

### 8. トリアージ結果 → AI復旧計画への自動連携
- **設計思想**: 初動トリアージ（Phase 1: showコマンドで状況把握）→ AI復旧計画（Phase 2: config変更で復旧）の2段階モデル
- トリアージのコマンド実行結果を session_state に永続蓄積し、AI復旧計画生成時に自動注入
- **command_popup.py**:
  - `_store_triage_results()`: トリアージ実行結果をデバイスID別にsession_stateに蓄積
  - `get_triage_results()`: 蓄積結果の取得API
  - `format_triage_results_for_llm()`: LLMプロンプト用にフォーマット（出力10行制限でトークン肥大化防止）
- **report_builders.py**:
  - `build_prediction_report_scenario()` (Step②): トリアージ実行結果があればプロンプトに注入→実機出力を踏まえたOK/NG判定を生成
  - `build_prevention_plan_scenario()` (Step③): トリアージ実行結果があればプロンプトに注入→実機状態に基づく具体的な予防措置コマンドを生成
- **remediation.py**:
  - 障害時復旧プラン生成でも `analysis_result` にトリアージ結果を連結→ `network_ops.py` の `generate_remediation_commands_streaming()` にコンテキスト伝達
  - キャッシュキーにトリアージ結果を含めることで、トリアージ実行後は新しい復旧計画が生成される
  - ステータス表示: 「✅ 初動トリアージの実行結果を検出しました。復旧計画に自動反映されます。」

## 未完了・保留タスク

（現時点で未完了の将来拡張はありません）

## 既知の問題・注意点
- cockpit.py のDT予兆パイプライン（predict_api呼び出しループ）は引き続きオーケストレータ内に残存。将来的にはこれも別モジュールに切り出すことを推奨
- stream_dashboard.py (45KB) も肥大化傾向あり。次のリファクタリング候補
- `google.generativeai` のインストール環境依存（cffi_backendエラー）があるため、CI環境での動作確認を推奨
- PyTorch Geometric 未インストール環境では GNN 機能が無効化される（既存動作、変更なし）
- `command_popup.py` のコマンド出力はデモ用テンプレート。本番環境では実機接続（Netmiko/NAPALM等）への差し替えが必要

## 次セッションへの推奨アクション
1. **Streamlit 実行テスト**: `streamlit run app.py` で全画面遷移を確認（拡張A/B/C含む動作検証）
2. **stream_dashboard.py のリファクタリング検討**: cockpit.py と同様の分割アプローチ
4. **サービスティアの実運用組み込み**: `render_tier_gated()` を各コンポーネントの適切な箇所に適用
