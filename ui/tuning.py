"""
ui/tuning.py  ―  Streamlit UI 層（Digital Twin Tuning ダッシュボード）
"""
import streamlit as st
import pandas as pd
import sqlite3
import os
import time

from ui.engine_cache import get_dt_engine_for_site


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

    tab1, tab2, tab3, tab4 = st.tabs(["⚡ Auto-Tuning", "📜 Audit Log", "🛑 Maintenance", "🧠 GNN Training"])

    # ── Tab1: Auto-Tuning ──────────────────────────────────
    with tab1:
        st.caption("AIによる閾値自動調整の提案を確認し、適用します。")

        col1, _ = st.columns([1, 3])
        if col1.button("🔄 提案を生成 (Generate)"):
            with st.spinner("Analyzing prediction history..."):
                try:
                    report = dt_engine.generate_tuning_report(days=30)
                    st.session_state["tuning_report"] = report
                except Exception as e:
                    st.error(f"レポート生成エラー: {e}")

        report = st.session_state.get("tuning_report")
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
            # ★ 統計情報を表示して、なぜ提案がないかを説明
            if report and report.get("stats"):
                stats = report["stats"]
                st.info(
                    f"📊 **分析結果**: 過去 **{stats['scan_days']}日間** の予兆履歴を分析しました。\n\n"
                    f"- 予兆予測総数: **{stats['total_predictions']}件**\n"
                    f"- 評価済み（outcome付き）: **{stats['total_with_outcome']}件**\n"
                    f"- ルールパターン数: **{stats['rules_analyzed']}種類**\n"
                    f"- 提案生成に必要な最低サンプル数: **{stats['min_samples_required']}件/ルール**\n\n"
                    "💡 提案が生成されるには、同一ルールパターンで最低"
                    f"**{stats['min_samples_required']}件以上**のラベル付きアウトカム"
                    "（TP: 実障害確定 / FP: 誤検知 / FN: 検知漏れ）が必要です。\n\n"
                    "**次のステップ:**\n"
                    "1. 予兆シミュレーションを複数回実行してください\n"
                    "2. 障害シナリオで予兆が的中したか確認してください\n"
                    "3. 十分なデータが蓄積されると、自動で提案が生成されます"
                )
            else:
                st.info("現在、適用すべき新しい提案はありません。\n\n"
                       "「🔄 提案を生成 (Generate)」ボタンを押して分析を開始してください。")

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

    # ── Tab3: Maintenance ──────────────────────────────────
    with tab3:
        st.markdown("#### System Maintenance")
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
                # グローバルキャッシュ（@st.cache_resource）をクリア
                st.cache_resource.clear()
                st.cache_data.clear()
                
                # セッションステートのポインタもクリア
                for k in [f"dt_engine_{site_id}", f"dt_engine_error_{site_id}"]:
                    if k in st.session_state:
                        del st.session_state[k]
                        
                st.success("キャッシュを完全にクリアしました。次回アクセス時にエンジンが再起動・再初期化されます。")
                time.sleep(1.5)
                st.rerun()

        st.divider()
        st.markdown("#### 📊 Engine Status")
        col_s1, col_s2, col_s3, col_s4 = st.columns(4)
        col_s1.metric("ルール数",   len(getattr(dt_engine, 'rules',   [])))
        col_s2.metric("履歴件数",   len(getattr(dt_engine, 'history', [])))
        col_s3.metric("アウトカム", len(getattr(dt_engine, 'outcomes', [])))
        _llm_name = getattr(getattr(dt_engine, 'llm', None), 'backend_name', '未初期化')
        col_s4.metric("LLMバックエンド", _llm_name.split("(")[0].strip())
        st.caption(f"🤖 {_llm_name}")

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

        if result:
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
                "💡 モデルが保存されました。エンジンを再起動すると自動的にロードされます。\n\n"
                "「🧹 Cache Clear」（Maintenanceタブ）を実行してエンジンを再初期化してください。"
            )
        else:
            st.error("学習に失敗しました。ログを確認してください。")

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

        if result:
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
            st.error("ファインチューニングに失敗しました。")
