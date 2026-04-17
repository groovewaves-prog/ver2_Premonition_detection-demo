# Session Handover

## 日付・ブランチ
- **日付**: 2026-03-24
- **ブランチ**: `claude/continue-from-handover-YO7m3`
- **前回セッション継続**: 白いベール根治 + バグ修正ラウンド

---

## 完了したタスク（今回セッション）

### 1. 白いベール根治（Phase 2: ポーリングループ撤廃）

前セッションで Custom Component 化は完了していたが、**実環境で依然白フラッシュが発生**していたため、追加調査と修正を実施。

#### 根本原因
- `app.py` 内で `time.sleep(0.5) + st.rerun()` のポーリングループが稼働
- ストリーム実行中、0.5秒間隔で**ページ全体を再描画**していた
- Custom Component の iframe は維持されるが、ページ全体の rerun は依然発生

#### 修正内容
| ファイル | 変更 |
|---|---|
| `app.py` | ポーリングループ（`time.sleep` + `st.rerun`）を完全撤廃、`import time` も削除 |
| `ui/components/future_radar.py` | `@st.fragment(run_every=timedelta(seconds=1))` デコレータ付き `_render_stream_fragment()` を新設。ストリームダッシュボードのみフラグメント単位で自動リフレッシュ |
| `ui/stream_dashboard.py` | `_stream_needs_refresh` フラグ書き込みを撤廃 |
| `ui/sidebar.py` | シナリオ切替時の `_stream_needs_refresh` / `_stream_rerun_count` クリア処理を撤廃（フラグ自体が不要に） |

#### 効果
- ストリーム中もページ全体の rerun が発生しない → 白フラッシュ根治
- フラグメント更新は局所的なため、他コンポーネント（トポロジー、KPI等）に影響しない

---

### 2. vis.js CDN 到達不可問題の解消

#### 症状
「Network Topology」ヘッダーは表示されるが、グラフ描画エリアが空白

#### 根本原因
- `components/topology_graph/frontend/index.html` で vis.js を `unpkg.com` CDN から読み込み
- ネットワーク到達不可環境（エアギャップ / 社内環境等）では CDN アクセス失敗
- `vis` が undefined のまま `onRender()` 実行 → JSエラー → 白画面

#### 修正内容
- pyvis バンドル（`/usr/local/lib/python3.11/dist-packages/pyvis/templates/lib/vis-9.1.2/`）から
  `vis-network.min.js` (468KB) をプロジェクトにローカルコピー
- `<script src="vis-network.min.js">` に変更（相対パス参照）
- CDN 依存を完全排除

| ファイル | サイズ |
|---|---|
| `components/topology_graph/frontend/vis-network.min.js` | 468 KB (新規追加) |
| `components/topology_graph/frontend/index.html` | 1行修正 |

---

### 3. 診断コマンド自動実行の撤廃

#### 症状
「▶ 全コマンド一括実行」ボタン未押下なのに、コマンド一覧に ✅ チェックマークが表示される

#### 根本原因
2箇所で**自動実行関数**が呼ばれていた:
- `ui/components/future_radar.py:267` → `_auto_execute_triage_commands()`
- `ui/components/root_cause_table.py:215` → `_auto_execute_incident_triage()`

トリアージ生成と同時に全 show コマンドを自動実行し、結果を `st.session_state[_inline_key]` に格納。`render_triage_cards()` がこのキーを見て ✅ を表示していた。

#### 修正内容
- 両自動実行関数の**呼出し削除**
- 両関数の**定義自体も削除**（未使用コード除去）
- `ui/sidebar.py`: シナリオ切替時に `_triage_inline_*`, `_triage_pred_*`, `_triage_incident_*` セッションキーを一括クリア（前シナリオの結果残存を防止）

---

### 4. L2SWサイレント障害の「影響レベル: なし」表示修正

#### 症状
A拠点で「L2SWサイレント障害」を選択しても、トラフィックモニターの影響レベルが **"なし"**（緑）と表示される

#### 根本原因
- トラフィックモニターは**帯域利用率のみ**で影響レベル判定
- L2SWサイレント障害は `latency_jitter` プロファイル: Level 2 で帯域 37%（正常域）
- 60%未満 → 「影響レベル: なし」に落ちる

#### 修正内容
`ui/components/traffic_monitor.py` に `latency_jitter` 専用の影響判定を追加:
- Level 2 以上かつ帯域 < 60% → 副指標（RTT）で判定
- RTT ≥ 150ms → 「重大」
- RTT ≥ 30ms → 「軽微」（Level 2 の RTT=50ms はここに該当）

---

### 5. インターフェース方向分類バグの修正（Bug #1, #2, #3）

#### Bug #2（**実害あり・重要**）: Spine-Leaf方向誤分類
- C拠点の `SPINE_SW_C02 → LEAF_SW_C01/02/03` が全て `uplink` に誤分類
- 原因: LEAF の `parent_id` が `SPINE_SW_C01` のみで `SPINE_SW_C02` は未記載
- 修正: 接続先の parent が**自分の冗長ピア**なら downlink と判定するロジック追加

#### Bug #1: WAN_UPLINK ハードコード比較
- `connected_to == 'WAN_UPLINK'` の文字列マッチに依存
- 修正: `connected_to not in topology` で汎用判定（外部回線は名前不問）

#### Bug #3: interfaces未定義デバイスの不可視性
- `interfaces` を持たないデバイスは無言で除外されていた
- 修正: 除外されたデバイス数をユーザーに表示
- メタデータキー（`_zones` 等）もここで除外

#### 検証結果
| デバイス | インターフェース | 修正前 | 修正後 |
|---|---|:---:|:---:|
| SPINE_SW_C02 | Ethernet1/2 → LEAF_SW_C01 | uplink ❌ | **downlink** ✅ |
| SPINE_SW_C02 | Ethernet1/3 → LEAF_SW_C02 | uplink ❌ | **downlink** ✅ |
| SPINE_SW_C02 | Ethernet1/4 → LEAF_SW_C03 | uplink ❌ | **downlink** ✅ |
| WAN_ROUTER_01 | Gi0/0/0 → WAN_UPLINK | uplink ✅ | uplink ✅ |
| LEAF_SW_C01 | Ethernet1/2 → SPINE_SW_C02 | uplink ✅ | uplink ✅ |

---

### 6. セキュリティ調査

**litellm PyPI サプライチェーン攻撃（2026/3/24 発覚）への影響確認**
- 汚染バージョン: v1.82.7, v1.82.8（PyPI 直接アップロード、GitHub tag なし）
- 攻撃手法: `.pth` ファイルによる Python 起動時自動実行
- **本プロジェクトへの影響: なし**
  - `requirements.txt` に litellm 未記載
  - `import litellm` コード参照なし
  - `litellm_init.pth` ファイル非存在
  - LLM 連携は `google-generativeai` / `google-genai`（Gemini API）のみ使用

---

## プロジェクトアーキテクチャ参考情報

### ベクトルDB: ChromaDB
- **実装**: `digital_twin_pkg/vector_store.py`
- **用途**: 過去インシデントのセマンティック類似検索のみ（**学習には未使用**）
- **埋め込み**: `all-MiniLM-L6-v2`（384次元、遅延ロード、32-40MB）
- **保持期間**: 90日（自動クリーンアップ）
- **保存場所**: `.dt_storage/{tenant_id}/chromadb/`

### 学習アーキテクチャ（3ストリーム並列）
1. **GNN学習**: `data/gnn_training/*.json`（ストリーム生データ）→ `models/gnn_pretrained.pt`（**無期限**）
2. **ルール閾値昇格**: `forecast_ledger`（SQLite, 90日）→ `shadow_eval_state.json`（**無期限**）
3. **GDN ベースライン**: Welford 統計 → SQLite `state` テーブル（**無期限**）

### 90日後に失われるもの vs 残るもの
| 削除 | 保存 |
|---|---|
| ChromaDB の個別インシデント | GNN モデル重み |
| forecast_ledger の個別予測 | 昇格済みルール閾値 |
| GNN学習セッション JSON | GDN ベースライン統計 |
| event_log.jsonl | 昇格の状態機械 |

**設計思想**: 生データは揮発、学習された知識は恒久保存（ML の一般的パラダイム）

### メモリ消費概算
| 状態 | メモリ |
|---|---|
| 起動直後（遅延ロード前） | ~50 MB |
| 1拠点アクティブ（SentenceTransformer ロード後） | ~100-150 MB |
| 全拠点アクティブ + GNN有効 | ~300-500 MB |

---

## 今セッションのコミット履歴

```
d12ed00 fix: トラフィックモニタの方向分類バグ3件を修正
dbf1a00 fix: 白いベール根治 + 自動実行残存修正 + サイレント障害の影響レベル修正
bd689d0 fix: vis.js CDN→ローカルバンドル + 診断コマンド自動実行を削除
da89ceb docs: element ID 安定性の証拠を HANDOVER.md に追記
fb983f3 docs: プロトコル互換性検証結果を HANDOVER.md に記載
2f629ca refactor: 未使用 import と変数を削除（graph.py クリーンアップ）
ab0221c feat: 白いベール根治 — Streamlit Custom Component 移行
```

---

## 全サイト共通動作の検証

3つの修正（白いベール・自動実行・影響レベル）が **全サイト（A/B/C）+ 将来の新拠点** で動作することを検証済み:

- `SCENARIO_MAP` はグローバル辞書（サイト別分岐なし）
- `sites = list_sites()` で全サイトを動的列挙
- `_triage_inline_*` キーは `startswith` マッチで全サイト分をクリア
- `latency_jitter` プロファイルはグローバル定義
- トラフィックモニターの方向分類は `parent_id` / `redundancy_group` ベースで完全動的

**サイト固有ハードコードは一切なし**。新拠点追加時もコード修正不要（トポロジー JSON が同スキーマに従う限り）。

---

## 既知の問題・注意点

### Custom Component 関連
- `streamlit-component-lib` CDN ではなく `postMessage` インラインプロトコルを実装
  - Streamlit 内部 API 変更時に互換性破壊の可能性（v1.55.0 では動作確認済）
- vis.js は**ローカルバンドル** (`vis-network.min.js` v9.1.2) 使用
- `components/` ディレクトリはプロジェクトルート（`ui/components/` とは別物）

### トポロジー設計の前提（**バグではなく設計制約**）
- ツリー型トポロジーを前提（`parent_id` / `children_map` ベースの BFS）
- メッシュ型トポロジーは要アーキテクチャ拡張
- `interfaces` 配列はトラフィックモニター対象デバイスで必須

### 既存の未解決課題（前セッションから継続）
- FAD-1: デバイスタイプレジストリ未実装（CLAUDE.md 記載の方針のみ）
- `simulate_command_execution()` の SSH executor 差し替え（L2移行）
- LLM 駆動の診断コマンド計画（現在はルールベース）
- メンテナンスモード永続化（session_state のみ）
- テストファイルのパス不整合（`tests/test_digital_twin_v2.py` 等）

---

## 次セッションへの推奨アクション

### 優先度: 高
1. **修正結果のブラウザ実機検証**
   - 白いベール: ストリーム実行中にページ全体が白フラッシュしないか
   - 自動実行: 「▶ 全コマンド一括実行」を押す前は ✅ が出ないか
   - C拠点 Spine-Leaf: SPINE_SW_C02 選択時に Downlink として LEAF が表示されるか
   - L2SWサイレント障害: 影響レベルが「軽微」以上になるか

### 優先度: 中
2. **FAD-1 実装検討**: デバイスタイプレジストリ（`configs/device_types.json`）
3. **L2SWサイレント障害の影響レベル閾値調整**: 現在 RTT≥30ms で「軽微」だが、運用実態に応じて調整
4. **メンテナンスモード永続化**: SQLite の `state` テーブルへ移行

### 優先度: 低
5. **テストの復旧**: `tests/test_digital_twin_v2.py`, `test_integration_v2.py` のパス修正
6. **トポロジースキーマバリデーション**: `interfaces` 必須化、JSON Schema 導入検討
