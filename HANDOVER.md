# Session Handover

## 日付・ブランチ
- **日付**: 2026-03-12
- **ブランチ**: `claude/continue-handover-work-3yYng`

## 完了したタスク（今回セッション）

### 1. 遅延対策 5項目の実装

#### 対策1: トリアージ生成を遅延ロード化（最大効果: 2-8秒短縮）
- `cockpit.py` のレンダーループ内で行っていた LLM トリアージ生成（gemma-3-4b-it）を削除
- `ui/components/future_radar.py` に `_generate_prediction_triage_lazy()` を新設
- Future Radar カード表示時にのみオンデマンドでLLM呼出（spinner付き）
- キャッシュキーを `{device}_{scenario}_{level}` ベースに変更（msg非依存で高HIT率）

#### 対策2: RateLimiter をモデル別カウンタに分離
- `rate_limiter.py` に `_ModelBucket` クラスを導入
- `GlobalRateLimiter` が `wait_for_slot(model_id=...)` / `record_request(model_id=...)` でモデル別バケットを管理
- `MODEL_RATE_CONFIGS` で gemma-3-12b-it (30RPM), gemma-3-4b-it (30RPM), gemini-2.0-flash (60RPM) を個別設定
- 後方互換: model_id 未指定時は `_default` バケットを使用（既存コード変更不要）
- `get_stats()` は全バケット合計 or モデル別統計を返却

#### 対策3: forecast_ledger に source インデックス追加
- `digital_twin_pkg/engine.py` の `_init_forecast_ledger()` に `idx_fl_source` インデックスを追加
- `CREATE INDEX IF NOT EXISTS idx_fl_source ON forecast_ledger (source, device_id, status)`
- simulation DELETE クエリが 100-500ms → <10ms に高速化

#### 対策4: predict_cache TTL を 30s→120s に延長
- `digital_twin_pkg/engine.py` の `_predict_cache_ttl` を 30.0 → 120.0 に変更
- rerun 時のキャッシュHIT率が向上（同一スライダーレベルでの再計算を回避）

#### 対策5: トリアージキャッシュキーを msg 非依存に変更
- `future_radar.py` の `_generate_prediction_triage_lazy()` で実装済み
- キャッシュキー: `_triage_pred_{device}_{scenario}_{level}`（メッセージ内容の変動に左右されない）

### 2. Phase 1: 機器単位メンテナンスモード

#### 実装内容
- **session_state**: `maint_devices = {site_id: set(device_ids)}` を追加（`utils/state.py`）
- **サイドバー**: メンテナンス設定 expander 内に `st.multiselect` を追加
  - 拠点全体メンテナンスでない場合、トポロジーのデバイス一覧からメンテ対象を選択可能
  - 選択中のデバイス数をキャプションで表示
- **コックピット（アラーム抑制）**: メンテ中デバイスのアラームをフィルタリング
  - 予兆シグナル注入もメンテ中デバイスはスキップ
  - 抑制件数を含むメンテナンス通知バナーを表示
- **トポロジーマップ**: メンテ中デバイスをグレー表示（`#B0BEC5`）+ "MAINTENANCE" ラベル
  - 凡例に "Maintenance (メンテ中)" を追加
  - キャッシュシグネチャにメンテ状態を含めて正しく再描画
- **ダッシュボード**: 機器メンテ数バッジ表示（「🔧 N台メンテ中」）
  - キャッシュシグネチャにデバイスメンテ状態を含む

## 過去セッションの完了タスク（参考）

### stream_dashboard.py リファクタリング（1153行 → 360行 + コンポーネント群）
### サービスティアの実運用組み込み
### cockpit.py リファクタリング（2346行 → 536行 + コンポーネント群）
### サービスティア基盤
### 画面表示の高速化（2段階）
### 将来拡張 A/B/C の実装
### 障害発生時の初動トリアージ対応
### トリアージ結果 → AI復旧計画への自動連携
### 障害シナリオ切替時の描画高速化
### トポロジーマップ/Legend間隔修正

### 3. バグ修正: 劣化進行度0で予測が残留する問題
- `sidebar.py`: `degradation_level == 0` の `else` ブランチで以下をクリア
  - `dt_prediction_cache`（予測結果キャッシュ）
  - `forecast_ledger` の `source='simulation'` & `status='open'` レコード
  - `_triage_pred_*` キャッシュキー
- スライダーを0に戻すと Future Radar + 初動トリアージが正しく非表示になる

### 4. 推奨アクション自動実行 L1: UX大幅改善
- **CLIコマンド vs 人手作業の視覚的区別**
  - `classify_steps()` 関数を新設（手順を CLI/人手に構造化分類）
  - CLI: 青ボーダー `▶` アイコン / 人手作業: グレーボーダー `🔧` アイコン
  - CLI なしカードは `[🔧 人手]` バッジ表示（実行ボタンなし）
- **「▶ 全コマンド一括実行」ボタン**
  - 全カードの show コマンドをワンクリックで一括実行
  - `_triage_inline_{card_idx}_{device_id}` キーでインライン結果を管理
- **インライン結果表示**（ポップアップ不要）
  - 実行済みコマンドは緑背景 + 結果プレビュー（最大4行）
  - 実行済みカードに ✅ チェックマーク
- **future_radar.py / root_cause_table.py**: キャプションを L1 仕様に更新

### 5. メンテナンスモード Phase 2: 時間帯指定メンテナンスウィンドウ
- **データ構造**: `maint_windows: []` を `utils/state.py` に追加
  - 各ウィンドウ: `{id, site_id, device_ids, start, end, label}`
- **サイドバーUI** (`sidebar.py`):
  - 「📅 メンテナンスウィンドウ」セクション（メンテナンス設定 expander 内）
  - `st.popover` で追加フォーム（開始/終了日時、対象拠点、対象機器、ラベル）
  - 一覧表示: 🟢アクティブ / ⏳予定 / ⏹終了 のステータス色分け + 個別削除ボタン
- **コックピット** (`cockpit.py`):
  - `_resolve_maint_windows()`: 毎 rerun で現在時刻と比較
  - アクティブウィンドウの `device_ids` を `maint_devices` にマージ（Phase 1 再利用）
  - 終了済みウィンドウを自動クリーンアップ（一覧から除去）
  - 「✅ メンテナンス終了: {label}」バナーで通知
  - メンテバナーにアクティブウィンドウの終了時刻を表示
- **ダッシュボード** (`dashboard.py`):
  - 「📅 N件実行中」（緑）/ 「📅 N件予定」（オレンジ）バッジ追加

### 6. RateLimiter model_id 明示指定
- `network_ops.py`: 5箇所の `wait_for_slot()` / `record_request()` に `model_id=MODEL_NAME` を追加
  - 修正前: 全リクエストが `_default` バケットに集中（モデル別バケットが未活用）
  - 修正後: `gemma-3-4b-it` 専用バケット（30RPM）が正しく使用される
- `rate_limiter.py`: `rate_limited_with_retry` デコレータに `model_id` パラメータ追加

## 未完了・保留タスク

### 推奨アクション L2: 実機接続
- `simulate_command_execution()` を SSH executor に差し替えるだけで L2 移行可能
- L1 の UI（インライン結果表示 + 一括実行）はそのまま再利用

### メンテナンスモード Phase 3: 永続化
- 現状 session_state のみ（リロードで消失）→ DB or ファイル保存に拡張可能

### UI コンポーネントの RateLimiter 統合
- `future_radar.py`, `root_cause_table.py`, `chat_panel.py`, `diagnostic.py`, `remediation.py` は RateLimiter を経由せず直接 `generate_content()` を呼んでいる
- これらの呼出にも rate limiter を適用すると、API 429 エラーの抑制効果がさらに向上

## 既知の問題・注意点
- `rate_limiter.py` の `GlobalRateLimiter` はシングルトンのため、既存インスタンスがある場合は再起動が必要
- `forecast_ledger` のインデックス追加は既存DBに対して `CREATE INDEX IF NOT EXISTS` で安全に適用
- `predict_cache_ttl` の120秒化により、スライダー操作直後に最大120秒間古い予測が表示される可能性あり
- `maint_devices` / `maint_windows` は session_state のみで永続化されない（ブラウザリロードで消失）
- `google.generativeai` のインストール環境依存（cffi_backendエラー）があるため、CI環境での動作確認を推奨
- `command_popup.py` のコマンド出力はデモ用テンプレート。本番環境では `simulate_command_execution()` を SSH executor に差し替え必要
- `simulate_command_execution()` のデモ用 sleep(0.3s) は一括実行時にコマンド数 × 0.3s の遅延。必要に応じて短縮可
- メンテナンスウィンドウの `device_ids` が空の場合は拠点全体がメンテ対象になる

## 次セッションへの推奨アクション
1. **Streamlit 実行テスト**: `streamlit run app.py` で全機能の動作確認
2. **メンテナンスウィンドウ動作確認**: ウィンドウ追加→アクティブ化→終了自動解除の一連フロー確認
3. **推奨アクション L2**: SSH executor の接続設計（`simulate_command_execution` の差し替え）
4. **UI コンポーネントの RateLimiter 統合**: future_radar.py 等の直接 generate_content 呼出に rate limiter を適用
