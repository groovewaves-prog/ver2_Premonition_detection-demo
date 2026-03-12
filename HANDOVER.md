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
- `MODEL_RATE_CONFIGS` で gemma-3-12b-it (30RPM), gemma-3-4b-it (30RPM), gemini-2.0-flash (10RPM) を個別設定
- 後方互換: model_id 未指定時は `_default` バケットを使用（既存コード変更不要）

#### 対策3: forecast_ledger に source インデックス追加
- `digital_twin_pkg/engine.py` の `_init_forecast_ledger()` に `idx_fl_source` インデックスを追加
- simulation DELETE クエリが 100-500ms → <10ms に高速化

#### 対策4: predict_cache TTL を 30s→120s に延長
- `digital_twin_pkg/engine.py` の `_predict_cache_ttl` を 30.0 → 120.0 に変更

#### 対策5: トリアージキャッシュキーを msg 非依存に変更
- `future_radar.py` の `_generate_prediction_triage_lazy()` で実装済み

### 2. Phase 1: 機器単位メンテナンスモード
（内容は前回セッションから継続）

### 3. gemini-2.0-flash-exp → gemma-3-12b-it 全置換 + レートリミッター全面適用
- `digital_twin_pkg/engine.py`: gemini-2.0-flash-exp → gemma-3-12b-it に変更
- `rate_limiter.py`: 実際のGoogle AI Studio無料枠レート制限に修正
  - gemini-2.0-flash-exp: RPM=10, RPD=1500（使用停止推奨）
  - gemma-3シリーズ: RPM=30, RPD=14400
- `utils/llm_helper.py`: スタブRateLimiter → 実装版に置換
- `digital_twin_pkg/llm_client.py`: _call_llm() にレートリミッター追加
- `ui/components/future_radar.py`, `root_cause_table.py`, `diagnostic.py`: レートリミッター追加

### 4. APIリクエストのバッチ化 + 全LLM呼出のサニタイズ徹底

#### サニタイズ基盤
- `utils/sanitizer.py` (新規): 共通サニタイズモジュール
  - `sanitize_for_llm()`: IP/MAC/ホスト名/ASN/VLAN/認証情報マスキング + プロンプトインジェクション防御 + 入力長制限
  - `sanitize_device_id()`: デバイスIDのホワイトリスト検証
  - `sanitize_user_input()`: HTMLタグ・制御文字除去 + sanitize_for_llm

#### サニタイズ適用箇所（全LLM呼出サイト）
- `ui/components/future_radar.py`: トリアージプロンプトのメッセージ・デバイスID
- `ui/components/root_cause_table.py`: 障害トリアージプロンプトのメッセージ・デバイスID
- `ui/components/diagnostic.py`: 診断プロンプトのデバイスID・状態記述
- `ui/components/chat_panel.py`: ユーザー入力・CIコンテキスト
- `digital_twin_pkg/engine.py`: アクション生成プロンプトのメッセージ・デバイスID（メッセージ数50→20、文字数300に制限）
- `network_ops.py`: 全6関数のdevice_id/vendor/scenario（generate_fake_log_by_ai, predict_initial_symptoms, generate_analyst_report, generate_analyst_report_streaming, generate_remediation_commands, generate_remediation_commands_streaming）

#### APIバッチ化
- `digital_twin_pkg/engine.py`: rule_patternレベルのキャッシュ導入
  - 同一パターンの複数デバイスが同じLLM応答を共有（APIコール数を大幅削減）
  - `_gemini_actions_cache` + 5分TTL

### 5. トリアージキャッシュキー不一致修正 + インライン結果キー安定化
- `future_radar.py`: pc.get('predicted_state') のフォールバック不一致を修正
- card_idx を `enumerate()` ベース → `f"pred_{device_id}"` ベースに変更（rerun間で安定）
- `root_cause_table.py`: card_idx を `100` → `f"incident_{device_id}"` に変更

### 6. 全コマンド一括実行の結果表示
- `command_popup.py`: 実行結果サマリーブロック追加（緑カードで各コマンドの出力プレビュー・経過時間を表示）

## 過去セッションの完了タスク（参考）
- stream_dashboard.py リファクタリング
- サービスティアの実運用組み込み
- cockpit.py リファクタリング
- 画面表示の高速化（2段階）
- 将来拡張 A/B/C の実装
- 障害発生時の初動トリアージ対応
- トリアージ結果 → AI復旧計画への自動連携
- 障害シナリオ切替時の描画高速化
- トポロジーマップ/Legend間隔修正
- バグ修正: 劣化進行度0で予測が残留する問題
- 推奨アクション自動実行 L1
- メンテナンスモード Phase 2: 時間帯指定
- RateLimiter model_id 明示指定
- cockpit.py DT予兆パイプライン分離
- 描画遅延の根本修正: LLM呼出をレンダーパスから完全排除
- L1トリアージ: AI自動実行

## 未完了・保留タスク

### 推奨アクション L2: 実機接続
- `simulate_command_execution()` を SSH executor に差し替えるだけで L2 移行可能

### メンテナンスモード Phase 3: 永続化
- 現状 session_state のみ（リロードで消失）→ DB or ファイル保存に拡張可能

## 既知の問題・注意点
- `rate_limiter.py` の `GlobalRateLimiter` はシングルトンのため、既存インスタンスがある場合は再起動が必要
- `predict_cache_ttl` の120秒化により、スライダー操作直後に最大120秒間古い予測が表示される可能性あり
- `maint_devices` / `maint_windows` は session_state のみで永続化されない
- `google.generativeai` のインストール環境依存あり。CI環境での動作確認を推奨
- `command_popup.py` のコマンド出力はデモ用テンプレート。本番環境では SSH executor に差し替え必要
- 予兆ステータス履歴のインシデント名はデフォルト名を使用

## 次セッションへの推奨アクション
1. **Streamlit 実行テスト**: `streamlit run app.py` で全機能の動作確認
2. **サニタイズ動作確認**: IP/MAC/認証情報がマスクされていることを確認
3. **レート制限動作確認**: 30RPM超過時にwait_for_slotが正しく待機することを確認
4. **推奨アクション L2**: SSH executor の接続設計
5. **メンテナンスモード永続化**: DB保存の設計・実装
