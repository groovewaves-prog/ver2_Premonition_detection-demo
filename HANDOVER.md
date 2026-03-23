# Session Handover

## 日付・ブランチ
- **日付**: 2026-03-23
- **ブランチ**: `claude/continue-from-handover-MeV8M`

## 完了したタスク（今回セッション）

### 白いベール問題の根本原因分析
- 5つの独立した発火経路を特定し、構造的な「壁」を定義
- 壁の本質: **Streamlit の rerun モデル + `st.components.v1.html()` の iframe 再生成**
- 個別パッチ（キャッシュ丸め、デルタパス固定等）では根治不可能であることを確認

### Site A/B ゾーン自動生成の撤回
- コミット `2ee0d7a`: Site A/B に不要なゾーン自動生成を適用した問題を修正
- Site A/B は hierarchical レイアウトに復元済み

## ★ 次セッションの最優先タスク: 白いベール根治 — Streamlit Custom Component 移行

### 背景と目的
現在の `ui/graph.py` は vis.js グラフを **HTML 文字列 → `st.components.v1.html()` → iframe** で描画している。
HTML が1文字でも変わると iframe が破棄・再生成され、白いベール（白フラッシュ）が発生する。
**Streamlit Custom Component に移行し、iframe を破棄せずデータ差分のみ更新する**ことで根治する。

### 実装計画（3フェーズ）

#### Phase 1: 概念実証（POC）
- Streamlit Custom Component の骨格を作成
- 空の vis.js ネットワークを iframe なしで描画できることを確認
- ファイル構成:
  ```
  components/
    topology_graph/
      __init__.py          # Python API (declare_component)
      frontend/
        index.html         # vis.js を読み込む HTML
        main.js            # Streamlit ↔ vis.js ブリッジ
  ```
- Streamlit Custom Component のライフサイクル:
  - `Streamlit.setComponentReady()` → 初回のみ iframe 生成
  - `Streamlit.onRender(args)` → データ変更時に呼ばれる（iframe は維持）
  - `nodes.update()` / `edges.update()` で vis.js DataSet を差分更新

#### Phase 2: データ連携
- `render_topology_graph()` の既存引数（topology, alarms, analysis_results 等）を
  Custom Component の `args` として渡す
- Python 側: `_topology_graph(topology=..., alarms=..., key="topo")`
- JS 側: `Streamlit.onRender()` 内で差分検出 → `nodes.update()` / `edges.update()`
- **キャッシュ署名ロジックは不要になる**（iframe が再生成されないため）

#### Phase 3: 既存機能の移植
- ゾーン描画（`beforeDrawing` コールバック）
- キャンバスリサイズ（`afterDrawing` コールバック）
- ノードスタイリング（アラーム色、予兆アンバー、メンテナンス灰色）
- ツールチップ
- 影響伝搬グラフ（`render_impact_graph` も同様に移行）

### 現在のアーキテクチャ（移行元）

**ファイル**: `ui/graph.py`（1501行）

**主要関数**:
| 関数 | 行 | 役割 |
|---|---|---|
| `render_topology_graph()` | ~260 | エントリポイント。キャッシュ署名 → HTML生成 → `components.html()` |
| `_compute_fixed_positions()` | ~370 | ゾーン情報からノード固定座標を計算（Site C用） |
| `_build_node_data()` | (内部) | アラーム・分析結果からノードの色・形状・ラベルを決定 |
| `_build_edge_data()` | (内部) | トポロジーのリンク情報からエッジデータを構築 |

**キャッシュ機構** (`ui/graph.py:293-314`):
```python
_alarm_sig = tuple(sorted((a.device_id, a.severity.name, getattr(a, 'is_root_cause', False)) for a in alarms))
_analysis_sig = tuple(sorted((r.get("id",""), r.get("classification",""), "P" if r.get("is_prediction") else "", round(r.get("prob",0),1)) for r in analysis_results))
_cache_sig = hash((_alarm_sig, _analysis_sig, len(topology), _maint_sig, _zone_sig))
```
→ Custom Component 移行後は**このキャッシュ機構自体が不要**になる。

**ゾーン描画** (JavaScript `beforeDrawing`):
- `_zones` dict の `grid` キーからゾーン矩形を算出（GML Grid-based Layout）
- `_envelopes` で子ゾーンを包含する親矩形を描画
- `_col_bounds` / `_row_bounds` メタデータを使用

**キャンバスリサイズ** (JavaScript `afterDrawing`):
- hierarchical レイアウト（Site A/B）のみでリフロー実行
- fixed レイアウト（Site C）ではリフロー禁止（設計原則 #5）

### 白いベール問題の5つの発火経路（参考）

| # | 経路 | 対策状態 |
|---|---|---|
| 1 | デルタパス不安定（条件分岐UI） | パッチ済み（`st.empty()`） |
| 2 | キャッシュキー揺れ（float精度） | パッチ済み（`round(prob, 1)`） |
| 3 | 描画サイクル増幅（`fit()` 2回） | パッチ済み（単一 fit） |
| 4 | DOM操作干渉（afterDrawing resize） | パッチ済み（`_canvas_h` 事前計算） |
| 5 | レイアウトモード切替（ゾーン自動生成） | 撤回済み |

**→ これら5つすべてが、Custom Component 移行により構造的に解消される。**

### Streamlit Custom Component の技術参考

- 公式ドキュメント: https://docs.streamlit.io/develop/concepts/custom-components
- コンポーネントテンプレート: `streamlit.components.v1.declare_component()`
- 開発モード: `_component_func = declare_component("name", url="http://localhost:3001")`
- 本番モード: `_component_func = declare_component("name", path="frontend/build")`
- **開発時は npm 不要**: `index.html` + vanilla JS で実装可能（React不要）

### 注意事項

- `ui/graph.py` の `_compute_fixed_positions()` のロジックはそのまま再利用可能（Python側計算）
- vis.js の CDN URL は現在 `ui/graph.py` 内にハードコード（`https://unpkg.com/vis-network/...`）
- `st.components.v1.html()` の呼び出し箇所は `ui/graph.py` に2箇所（トポロジー + 影響伝搬）
- CLAUDE.md の FAD-2（ゾーン表示）の設計原則は Custom Component 移行後も適用

## 未完了・保留タスク

### FAD-1: デバイスタイプレジストリ
- CLAUDE.md に方針記載あり、未着手

### その他（前セッションから継続）
- `simulate_command_execution()` の SSH executor 差し替え（L2移行）
- LLM駆動の診断コマンド計画（現在はルールベース）
- メンテナンスモード永続化（現状 session_state のみ）

## 既知の問題・注意点

- `_col_bounds` / `_row_bounds` は `_compute_fixed_positions()` が zones dict に注入する `_` プレフィックス付きメタデータ
- Site A/B にはゾーン定義 (`_zones`) がないため、vis.js hierarchical レイアウトにフォールバック
- テストファイル（tests/test_digital_twin_v2.py, test_integration_v2.py）はパス不整合で実行不可
