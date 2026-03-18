# Session Handover

## 日付・ブランチ
- **日付**: 2026-03-18
- **ブランチ**: `claude/continue-from-handover-MeV8M`

## 完了したタスク（今回セッション）

### ゾーン重なり問題の根本解決 — GML Grid-based Layout への移行
- **ファイル**: `ui/graph.py` — `_compute_fixed_positions()` + beforeDrawing JS
- **根本原因**: 旧アルゴリズムはボトムアップ方式（ノード BB → 矩形算出 → midpoint snapping で衝突解消）で、origBounds クランプが衝突解消を打ち消し、ゾーン重なりが構造的に解消不能だった
- **解法の理論的根拠**:
  - GML grid-based layout (PLOS ONE, 2019): グリッドセル境界からモジュール矩形をトップダウンに決定 → 構造的非重複保証
  - VPSC (Dwyer & Marriott, 2005) の分離制約は不要（グリッド構造が既に存在するため）
- **修正内容**:
  1. Python 側 Pass 4 で `_col_bounds` + `_row_bounds` メタデータ（グリッドセル境界）を zones に注入
  2. JS 側 beforeDrawing を全面書き換え:
     - Pass 1: `grid: [col, row, colspan, rowspan]` + `_col_bounds` + `_row_bounds` からゾーン矩形をトップダウン算出
     - Pass 1b: 安全ネット — ノード BB がセル外にはみ出す場合のみ拡張
     - Pass 2: エンベロープ = 子ゾーンの和集合 + パディング（衝突解消不要）
     - Pass 3: 描画
  3. 旧コードの衝突解消ロジック（Pass 2: midpoint snapping, Pass 2.5: origBounds clamp, Pass 3: envelope vs zone collision resolution）を全削除
- **結果**: 全ゾーンペア間に 92px のギャップを構造的に保証。衝突解消ロジック自体が不要になった
- **検証データ**:
  - web_tier: x=0..430, app_tier: x=522..952, db_tier: x=1044..1474 (各間 92px gap)
  - dc_core: x=0..1474 (3列スパン), aws_cloud: x=1566..1996

### 固定レイアウトの白余白問題の修正
- **ファイル**: `ui/graph.py` — afterDrawing リフロー + キャンバス高さ計算
- **原因**: afterDrawing リフローは hierarchical レイアウト（Site A/B）用。固定座標レイアウト（Site C）で実行されると `moveNode()` がグリッド配置を破壊し、巨大な白余白が発生
- **修正**:
  1. `_useFixed` フラグを JS に渡し、固定レイアウトではリフローをスキップ → `network.fit()` のみ実行
  2. `_canvas_h` を `_row_bounds` からタイトに算出（ゾーンパディング考慮）
  3. beforeDrawing / afterDrawing / IIFE 全体に try-catch ガードを追加（障害シナリオでの白画面防止）

### エッジ直線化（Site C）
- 固定レイアウトのエッジを `cubicBezier` → `smooth: false`（直線）に変更

### ゾーンラベル位置の微調整
- ラベルをボックス外部からボックス内部（上端パディング領域）に移動

### 過去セッションでの修正（参考）
- ELK.js 依存の完全排除 → Python側 `_compute_fixed_positions()` に一本化
- トラフィックモニタ改善（4件）

## 未完了・保留タスク

### FAD-1: デバイスタイプレジストリ
- CLAUDE.md に方針記載あり、未着手

### その他（前セッションから継続）
- `simulate_command_execution()` の SSH executor 差し替え（L2移行）
- LLM駆動の診断コマンド計画（現在はルールベース）
- メンテナンスモード永続化（現状 session_state のみ）

## 既知の問題・注意点

- `_col_bounds` / `_row_bounds` は `_compute_fixed_positions()` が zones dict に注入する `_` プレフィックス付きメタデータ。beforeDrawing のゾーン列挙ループでは自動スキップ
- グリッド情報（`grid` キー）がないゾーンはフォールバックとしてノード BB ベースで描画（旧方式）
- Site A/B にはゾーン定義 (`_zones`) がないため、vis.js hierarchical レイアウトにフォールバック
- テストファイル（tests/test_digital_twin_v2.py, test_integration_v2.py）はパス不整合で実行不可（本セッションの変更とは無関係）

## 次セッションへの推奨アクション
1. ブラウザで Site C の「WAN全回線断」シナリオを確認し、白余白なく表示されるか検証
2. Site A/B の hierarchical レイアウトが従来通り正常に動作するか確認（リフローは Site A/B のみで実行）
3. 必要に応じて FAD-1（デバイスタイプレジストリ）の実装に着手
