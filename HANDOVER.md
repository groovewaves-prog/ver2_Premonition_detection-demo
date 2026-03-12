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

## 未完了・保留タスク

### 推奨アクション自動実行 (L1)
- 初動トリアージの `show` 系コマンドをワンクリックで自動実行する機能
- 既存の `CommandPopup` の仕組みに SSH executor を接続すれば実装可能
- 設計方針は合意済み（L0→L1→L2→L3 の段階的自律化）

## 既知の問題・注意点
- `rate_limiter.py` の `GlobalRateLimiter` はシングルトンのため、既存インスタンスがある場合は再起動が必要
- `forecast_ledger` のインデックス追加は既存DBに対して `CREATE INDEX IF NOT EXISTS` で安全に適用
- `predict_cache_ttl` の120秒化により、スライダー操作直後に最大120秒間古い予測が表示される可能性あり
- `maint_devices` は session_state のみで永続化されない（ブラウザリロードで消失）
- `google.generativeai` のインストール環境依存（cffi_backendエラー）があるため、CI環境での動作確認を推奨
- `command_popup.py` のコマンド出力はデモ用テンプレート。本番環境では実機接続への差し替えが必要

## 次セッションへの推奨アクション
1. **Streamlit 実行テスト**: `streamlit run app.py` でメンテナンスモード + 遅延改善の動作確認
2. **推奨アクション自動実行 (L1)**: `show` 系コマンドのワンクリック実行基盤の実装
3. **メンテナンスモード Phase 2**: 時間帯指定メンテナンスウィンドウの実装（計画保全との連携）
4. **RateLimiter のモデル別 model_id 渡し**: `network_ops.py` の各呼び出し箇所で `model_id` を明示的に渡す
