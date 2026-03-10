"""
ui/tuning.py  ―  Streamlit UI 層（Digital Twin Tuning ダッシュボード）
"""
import streamlit as st
import pandas as pd
import sqlite3
import os
import time

from ui.engine_cache import get_dt_engine_for_site

import logging
_logger = logging.getLogger(__name__)


def _auto_label_outcomes(dt_engine) -> dict:
    """提案生成前に未評価の予兆を自動ラベリングする。

    1. 期限切れ (open & eval_deadline 超過) → false_alarm (FP)
    2. 完了済みシミュレーションの対象デバイス × シナリオ → confirmed_incident (TP)

    Returns:
        {"expired": int, "sim_confirmed": int, "total_labeled": int}
    """
    result = {"expired": 0, "sim_confirmed": 0, "total_labeled": 0}

    # 1. 期限切れ予兆を FP に自動変換
    try:
        expire_res = dt_engine.forecast_expire_open()
        result["expired"] = expire_res.get("expired", 0)
    except Exception as e:
        _logger.warning("Auto-expire failed: %s", e)

    # 2. 完了済みシミュレーション結果から TP を自動ラベリング
    #    forecast_ledger に open 状態で残っている予兆のうち、
    #    同じデバイス＋シナリオでストリーム完了 (GNN学習データ) が存在するものを確定
    try:
        from digital_twin_pkg.gnn_trainer import list_training_sessions, GNN_TRAINING_DIR
        sessions = list_training_sessions()
        if sessions and dt_engine.storage._conn:
            import json as _json
            # セッションからデバイス × シナリオのペアを収集
            confirmed_pairs = set()
            for spath in sessions:
                try:
                    with open(spath, 'r') as f:
                        sdata = _json.load(f)
                    dev = sdata.get("target_device", "")
                    scenario = sdata.get("scenario_key", "")
                    if dev and scenario:
                        confirmed_pairs.add((dev, scenario))
                except Exception:
                    continue

            # 各ペアについて open 予兆を confirmed に更新
            for dev, scenario in confirmed_pairs:
                try:
                    n = dt_engine.forecast_auto_confirm_on_incident(
                        device_id=dev,
                        scenario=scenario,
                        note=f"シミュレーション完了による自動確定 (scenario={scenario})",
                    )
                    result["sim_confirmed"] += n
                except Exception:
                    pass
    except ImportError:
        pass
    except Exception as e:
        _logger.warning("Auto-confirm from simulations failed: %s", e)

    result["total_labeled"] = result["expired"] + result["sim_confirmed"]
    if result["total_labeled"] > 0:
        _logger.info(
            "Auto-labeled outcomes: expired→FP=%d, sim→TP=%d",
            result["expired"], result["sim_confirmed"],
        )
    return result


def _get_or_init_dt_engine(site_id: str):
    """共通キャッシュ (engine_cache) 経由で DigitalTwinEngine を取得する。"""
    dt_key = f"dt_engine_{site_id}"
    err_key = f"dt_engine_error_{site_id}"

    if dt_key in st.session_state and st.session_state[dt_key] is not None:
        return st.session_state[dt_key]

    try:
        engine = get_dt_engine_for_site(site_id)
        if not engine:
            st.session_state[dt_key] = None
            st.session_state[err_key] = "topology が読み込めませんでした。"
            return None
        st.session_state[dt_key] = engine
        st.session_state[err_key] = None
        return engine
    except Exception as e:
        import traceback
        st.session_state[dt_key] = None
        st.session_state[err_key] = f"{e}\n{traceback.format_exc()}"
        return None

def render_tuning_dashboard(site_id: str):
    st.subheader("🔧 Digital Twin Tuning & Audit")

    dt_engine = _get_or_init_dt_engine(site_id)

    if not dt_engine:
        err_detail = st.session_state.get(f"dt_engine_error_{site_id}", "不明なエラー")
        st.error("Digital Twin Engine unavailable. (エンジンモジュールがロードされていません)")
        with st.expander("🔍 エラー詳細（デバッグ用）", expanded=True):
            st.code(err_detail or "詳細情報なし", language="text")

        col_retry, _ = st.columns([1, 3])
        if col_retry.button("🔄 再試行"):
            for k in [f"dt_engine_{site_id}", f"dt_engine_error_{site_id}"]:
                if k in st.session_state:
                    del st.session_state[k]
            st.rerun()
        return

    try:
        from registry import get_display_name
        display_name = get_display_name(site_id)
    except Exception:
        display_name = site_id
    st.caption(f"対象拠点: **{display_name}** | テナントID: `{site_id}`")

    tab1, tab2, tab3, tab4 = st.tabs(["⚡ Auto-Tuning", "📜 Audit Log", "📊 Engine Status", "🧠 GNN Training"])

    # ── Tab1: Auto-Tuning ──────────────────────────────────
    with tab1:
        st.caption("AIエージェントが自動的に予兆のアウトカムをラベリングし、閾値最適化の提案を生成・適用します。")

        # ★ 自動チューニング最終実行状態を表示
        _auto_status = dt_engine.storage.load_state_sqlite("auto_tuning_last_run", None)
        if _auto_status:
            _last_ts = _auto_status.get("timestamp", 0)
            _last_result = _auto_status.get("result", {})
            _elapsed = time.time() - _last_ts
            if _elapsed < 60:
                _elapsed_str = f"{int(_elapsed)}秒前"
            elif _elapsed < 3600:
                _elapsed_str = f"{int(_elapsed / 60)}分前"
            else:
                _elapsed_str = f"{int(_elapsed / 3600)}時間前"

            _applied_count = len(_last_result.get("auto_applied", []))
            _expired_count = _last_result.get("expired", 0)
            _proposals_count = _last_result.get("proposals_generated", 0)

            st.markdown(
                f"**自動チューニング最終実行**: {_elapsed_str} | "
                f"期限切れ→FP: {_expired_count}件 | "
                f"提案生成: {_proposals_count}件 | "
                f"自動適用: {_applied_count}件"
            )

        col1, _ = st.columns([1, 3])
        if col1.button("🔄 提案を生成 (Generate)"):
            with st.spinner("予兆データの自動ラベリング＆分析中..."):
                try:
                    auto_result = _auto_label_outcomes(dt_engine)
                    report = dt_engine.generate_tuning_report(days=30)
                    if auto_result.get("total_labeled", 0) > 0:
                        report["auto_label_result"] = auto_result
                    st.session_state["tuning_report"] = report
                except Exception as e:
                    st.error(f"レポート生成エラー: {e}")

        report = st.session_state.get("tuning_report")
        # ★ 自動ラベリング結果を表示
        if report and report.get("auto_label_result"):
            alr = report["auto_label_result"]
            st.success(
                f"🤖 **自動ラベリング実行済み**: "
                f"期限切れ→誤検知(FP): {alr.get('expired', 0)}件 / "
                f"シミュレーション結果→実障害確定(TP): {alr.get('sim_confirmed', 0)}件"
            )
        if report and report.get("tuning_proposals"):
            for p in report["tuning_proposals"]:
                rule_pattern = p.get('rule_pattern', '不明')
                rec          = p.get('apply_recommendation', {})
                stats        = p.get('current_stats', {})
                proposal     = p.get('proposal', {})
                impact       = p.get('expected_impact', {})

                with st.expander(f"📦 {rule_pattern} ({rec.get('apply_mode', '-')})", expanded=True):
                    c1, c2, c3 = st.columns(3)
                    c1.metric("Recall (再現率)", f"{stats.get('recall', 0):.2f}")
                    c2.metric("New Threshold",   f"{proposal.get('paging_threshold', 0):.2f}")
                    c3.metric("FP Reduction",    f"-{impact.get('fp_reduction', 0)*100:.0f}%",
                              delta_color="normal")
                    st.markdown(f"**理由:** {rec.get('shadow_note', '-')}")
                    if rec.get('apply_mode') == 'auto':
                        st.success("✅ Auto-Eligible (推奨)")
                    if st.button("承認して適用 (Apply)", key=f"ap_{rule_pattern}"):
                        try:
                            res = dt_engine.apply_tuning_proposals_if_auto([p])
                            if res.get('applied'):
                                st.success(f"適用完了: {res['applied']}")
                            else:
                                st.error(f"適用失敗/スキップ: {res.get('skipped', [])}")
                        except Exception as e:
                            st.error(f"適用エラー: {e}")
        else:
            if report and report.get("stats"):
                stats = report["stats"]
                st.info(
                    f"📊 **分析結果**: 過去 **{stats['scan_days']}日間** の予兆履歴を分析しました。\n\n"
                    f"- 予兆予測総数: **{stats['total_predictions']}件**\n"
                    f"- 評価済み（outcome付き）: **{stats['total_with_outcome']}件**\n"
                    f"- ルールパターン数: **{stats['rules_analyzed']}種類**\n"
                    f"- 提案生成に必要な最低サンプル数: **{stats['min_samples_required']}件/ルール**\n\n"
                    "**自動運用状態**: アウトカムの自動ラベリングは有効です。\n"
                    "- 障害発生時 → 該当予兆を **TP (実障害確定)** に自動更新\n"
                    "- 予兆の評価期限超過 → **FP (誤検知)** に自動変換\n"
                    "- 5分ごとのバックグラウンドサイクルで提案を自動生成・適用\n\n"
                    f"現在、同一ルールパターンで **{stats['min_samples_required']}件以上**の"
                    "ラベル付きアウトカムが蓄積されると提案が自動生成されます。"
                )
            else:
                st.info(
                    "自動チューニングが有効です。\n\n"
                    "予兆の蓄積に応じてアウトカムを自動ラベリングし、"
                    "提案の生成・適用をバックグラウンドで実行します。\n\n"
                    "「🔄 提案を生成 (Generate)」で即時分析も可能です。"
                )

    # ── Tab2: Audit Log ────────────────────────────────────
    with tab2:
        st.caption("システムに加えられた変更の監査ログ（SQLite）を表示します。")
        db_path = dt_engine.storage.paths.get("sqlite_db", "")

        if db_path and os.path.exists(db_path):
            try:
                conn = sqlite3.connect(db_path)
                df = pd.read_sql(
                    "SELECT timestamp, event_type, actor, rule_pattern, status "
                    "FROM audit_log ORDER BY timestamp DESC LIMIT 50",
                    conn,
                )
                conn.close()
                if not df.empty:
                    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='s')
                    st.dataframe(df, use_container_width=True, hide_index=True)
                else:
                    st.info("監査ログはまだありません。")
            except Exception as e:
                st.error(f"ログ読み込みエラー: {e}")
        else:
            st.warning(f"監査データベースが見つかりません。\n\nパス: `{db_path}`")

    # ── Tab3: Engine Status ──────────────────────────────────
    with tab3:
        st.markdown("#### 📊 Engine Status")
        col_s1, col_s2, col_s3, col_s4 = st.columns(4)
        col_s1.metric("ルール数",   len(getattr(dt_engine, 'rules',   [])))
        col_s2.metric("履歴件数",   len(getattr(dt_engine, 'history', [])))
        col_s3.metric("アウトカム", len(getattr(dt_engine, 'outcomes', [])))
        _llm_name = getattr(getattr(dt_engine, 'llm', None), 'backend_name', '未初期化')
        col_s4.metric("LLMバックエンド", _llm_name.split("(")[0].strip())
        st.caption(f"🤖 {_llm_name}")

        # 開発者向けツール（通常運用では不要）
        with st.expander("🔧 開発者向けメンテナンスツール", expanded=False):
            st.caption("通常運用では使用しません。DB不整合やキャッシュ問題のトラブルシュート用です。")
            col_m1, col_m2 = st.columns(2)

            with col_m1:
                if st.button("🚑 DB Repair (Self-Healing)"):
                    try:
                        if dt_engine.repair_db_from_rules_json():
                            st.success("DBを rules.json から復元しました。")
                        else:
                            st.error("復元に失敗しました。rules.json が存在しない可能性があります。")
                    except Exception as e:
                        st.error(f"DB修復エラー: {e}")

            with col_m2:
                if st.button("🧹 Cache Clear"):
                    st.cache_resource.clear()
                    st.cache_data.clear()
                    for k in [f"dt_engine_{site_id}", f"dt_engine_error_{site_id}"]:
                        if k in st.session_state:
                            del st.session_state[k]
                    st.success("キャッシュをクリアしました。再起動します…")
                    time.sleep(1.5)
                    st.rerun()

    # ── Tab4: GNN Training ──────────────────────────────────
    with tab4:
        _render_gnn_training_tab(site_id, dt_engine)


def _render_gnn_training_tab(site_id: str, dt_engine):
    """GNN事前学習UIタブ"""
    st.caption(
        "ChiGADウェーブレットGNNの事前学習を実行します。"
        "EscalationRuleから合成データを生成し、モデルを学習させます。"
    )

    from digital_twin_pkg.gnn_trainer import (
        get_pretrained_model_path,
        pretrain_gnn,
        DEFAULT_MODEL_PATH,
    )
    from digital_twin_pkg.gnn import HAS_PYTORCH_GEOMETRIC

    if not HAS_PYTORCH_GEOMETRIC:
        st.error("PyTorch Geometric がインストールされていません。GNN学習は利用できません。")
        return

    # 現在のモデル状態
    model_path = get_pretrained_model_path()
    col_status1, col_status2 = st.columns(2)
    with col_status1:
        if model_path:
            file_size = os.path.getsize(model_path) / 1024
            mod_time = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(model_path)))
            st.success(f"✅ 学習済みモデル: `{os.path.basename(model_path)}`")
            st.caption(f"サイズ: {file_size:.0f} KB | 更新: {mod_time}")
        else:
            st.warning("⚠️ 学習済みモデルなし（GNNはランダム重みで動作中）")

    with col_status2:
        gnn_engine = getattr(dt_engine, 'gnn', None)
        gnn_model = getattr(gnn_engine, 'model', None) if gnn_engine else None
        if gnn_model is not None:
            st.info(f"🧠 GNN: アクティブ (weight={0.3 if not model_path else 0.3})")
        else:
            st.info("🧠 GNN: 無効")

    st.divider()

    # 学習パラメータ
    st.markdown("#### 学習パラメータ")
    col_p1, col_p2, col_p3 = st.columns(3)
    with col_p1:
        epochs = st.number_input("エポック数", min_value=10, max_value=300, value=80, step=10)
    with col_p2:
        samples = st.number_input("サンプル/ルール", min_value=10, max_value=200, value=50, step=10)
    with col_p3:
        lr = st.select_slider("学習率", options=[0.0001, 0.0005, 0.001, 0.005, 0.01], value=0.001)

    # 学習実行
    if st.button("🚀 事前学習を開始", type="primary"):
        topology = dt_engine.topology
        children_map = dt_engine.children_map

        progress_bar = st.progress(0, text="学習準備中...")
        status_text = st.empty()
        loss_chart_data = []

        def on_progress(epoch, total, loss):
            progress_bar.progress(epoch / total, text=f"Epoch {epoch}/{total} | Loss: {loss:.4f}")
            loss_chart_data.append({"epoch": epoch, "loss": loss})

        with st.spinner("GNN事前学習を実行中..."):
            result = pretrain_gnn(
                topology=topology,
                children_map=children_map,
                epochs=int(epochs),
                lr=float(lr),
                samples_per_rule=int(samples),
                progress_callback=on_progress,
            )

        if result and "error" not in result:
            progress_bar.progress(1.0, text="完了!")
            st.success(
                f"✅ **学習完了** | "
                f"最良Loss: {result['best_loss']:.4f} | "
                f"所要時間: {result['elapsed_sec']:.1f}秒 | "
                f"サンプル数: {result['total_samples']}"
            )

            # Loss曲線の描画
            if result.get('loss_history'):
                import pandas as pd
                loss_df = pd.DataFrame({
                    "epoch": list(range(1, len(result['loss_history']) + 1)),
                    "loss": result['loss_history']
                })
                st.line_chart(loss_df, x="epoch", y="loss", height=250)

            st.info(
                "💡 モデルが保存されました。エンジンを再初期化して新しいモデルをロードします…"
            )

            # 学習完了後、エンジンを自動的に再初期化
            st.cache_resource.clear()
            for k in [f"dt_engine_{site_id}", f"dt_engine_error_{site_id}"]:
                if k in st.session_state:
                    del st.session_state[k]
            time.sleep(1.0)
            st.rerun()
        else:
            # エラー詳細を表示
            error_msg = result.get("error", "不明なエラー") if isinstance(result, dict) else "不明なエラー"
            st.error(f"学習に失敗しました: {error_msg}")
            if isinstance(result, dict) and result.get("traceback"):
                with st.expander("🔍 詳細エラーログ", expanded=False):
                    st.code(result["traceback"], language="python")

    # ── 蓄積データによるファインチューニング ──
    st.divider()
    _render_finetune_section(dt_engine)


def _render_finetune_section(dt_engine):
    """蓄積データ（ストリーム劣化シミュレーション結果）でGNNをファインチューニングするUI"""
    st.markdown("#### 📦 蓄積データでファインチューニング")
    st.caption(
        "連続劣化ストリームの実行結果から蓄積された学習データを使い、"
        "GNNモデルを追加学習します。合成データのみの学習より、"
        "実際の劣化パターンに適応したモデルになります。"
    )

    from digital_twin_pkg.gnn_trainer import (
        list_training_sessions,
        finetune_gnn,
        GNN_TRAINING_DIR,
    )
    from digital_twin_pkg.gnn import HAS_PYTORCH_GEOMETRIC

    sessions = list_training_sessions()

    if not sessions:
        st.info(
            "📭 蓄積データがありません。\n\n"
            "連続劣化ストリームを実行・完了すると、自動的にGNN学習データが "
            f"`{GNN_TRAINING_DIR}/` に保存されます。"
        )
        return

    st.success(f"📂 **{len(sessions)} セッション**が利用可能")

    # セッション一覧
    with st.expander("蓄積セッション一覧", expanded=False):
        for i, s in enumerate(sessions[:20]):
            parts = s.replace(".json", "").split("_")
            scenario = parts[0] if parts else "?"
            st.caption(f"{i+1}. `{s}` ({scenario})")
        if len(sessions) > 20:
            st.caption(f"... 他 {len(sessions) - 20} セッション")

    if not HAS_PYTORCH_GEOMETRIC:
        st.error("PyTorch Geometric が必要です。")
        return

    # ファインチューニングパラメータ
    col_f1, col_f2 = st.columns(2)
    with col_f1:
        ft_epochs = st.number_input(
            "エポック数 (FT)", min_value=10, max_value=100, value=40, step=10,
            key="ft_epochs",
        )
    with col_f2:
        ft_lr = st.select_slider(
            "学習率 (FT)",
            options=[0.0001, 0.0003, 0.0005, 0.001],
            value=0.0005,
            key="ft_lr",
        )

    if st.button("🔄 蓄積データでファインチューニング", type="secondary"):
        topology = dt_engine.topology
        children_map = dt_engine.children_map

        progress_bar = st.progress(0, text="蓄積データを読み込み中...")

        def on_ft_progress(epoch, total, loss):
            progress_bar.progress(epoch / total, text=f"FT Epoch {epoch}/{total} | Loss: {loss:.4f}")

        with st.spinner("ファインチューニング実行中..."):
            result = finetune_gnn(
                topology=topology,
                children_map=children_map,
                session_files=sessions,
                epochs=int(ft_epochs),
                lr=float(ft_lr),
                progress_callback=on_ft_progress,
            )

        if result and "error" not in result:
            progress_bar.progress(1.0, text="完了!")
            st.success(
                f"✅ **ファインチューニング完了** | "
                f"最良Loss: {result['best_loss']:.4f} | "
                f"ストリームデータ: {result['stream_samples']}件 | "
                f"合成データ: {result['synthetic_samples']}件 | "
                f"所要時間: {result['elapsed_sec']:.1f}秒"
            )

            if result.get('loss_history'):
                loss_df = pd.DataFrame({
                    "epoch": list(range(1, len(result['loss_history']) + 1)),
                    "loss": result['loss_history']
                })
                st.line_chart(loss_df, x="epoch", y="loss", height=250)

            st.info(
                "💡 「🧹 Cache Clear」（Maintenanceタブ）を実行して"
                "エンジンを再初期化するとモデルが反映されます。"
            )
        else:
            error_msg = result.get("error", "不明なエラー") if isinstance(result, dict) else "不明なエラー"
            st.error(f"ファインチューニングに失敗しました: {error_msg}")
            if isinstance(result, dict) and result.get("traceback"):
                with st.expander("🔍 詳細エラーログ", expanded=False):
                    st.code(result["traceback"], language="python")
