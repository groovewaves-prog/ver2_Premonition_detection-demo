# ui/components/report_builders.py — LLMプロンプト構築関数
from .helpers import sanitize_prediction_context
from .command_popup import format_triage_results_for_llm


def build_prediction_report_scenario(cand: dict, signal_count: int = 1) -> str:
    """
    ② 確認手順レポート（診断ワークブック）

    役割: 「本当に危ないのか？ どう判断する？」
    ・① 推奨アクションの初動調査を「実行済み」前提で、その結果の読み方を詳説
    ・show系コマンドの出力例 + OK/NGの判定基準表を提示
    ・エスカレーション判断のデシジョンツリーを含む
    ・config系（変更系）コマンドは含めない
    """
    dev_id    = cand.get('id', '不明')
    prob      = cand.get('prob', 0)
    dev_type  = cand.get('type', 'UNKNOWN')

    pred_state    = cand.get('predicted_state') or cand.get('label', '').replace('🔮 [予兆] ', '') or '不明'

    ttf_hours     = cand.get('prediction_time_to_failure_hours', 0)
    failure_dt    = cand.get('prediction_failure_datetime', '')
    timeline      = cand.get('prediction_timeline', '')
    affected      = cand.get('prediction_affected_count', 0)
    early_hours   = cand.get('prediction_early_warning_hours', 0)
    ttc_min       = cand.get('prediction_time_to_critical_min', 0)

    # RUL表示
    if ttf_hours >= 24:
        ttf_display = f"{ttf_hours // 24}日後"
        if failure_dt:
            ttf_display += f" ({failure_dt})"
    elif ttf_hours > 0:
        ttf_display = f"{ttf_hours}時間後"
    else:
        ttf_display = "障害が切迫"

    initial_triage_summary = ""
    initial_triage_detail = ""
    rec_actions = cand.get('recommended_actions', [])
    if rec_actions:
        _items = []
        _detail_items = []
        for i, ra in enumerate(rec_actions, 1):
            cmd  = ra.get('command', ra.get('action', ''))
            eff  = ra.get('effect', '')
            pri  = ra.get('priority', '')
            reas = ra.get('reasoning', '')
            steps = ra.get('steps', [])
            _items.append(f"- {pri}: {cmd} (効果: {eff})")
            _detail_items.append(f"【アクション{i}】")
            _detail_items.append(f"  優先度: {pri}")
            _detail_items.append(f"  コマンド/操作: {cmd}")
            _detail_items.append(f"  期待効果: {eff}")
            if reas:
                _detail_items.append(f"  根拠: {reas}")
            if steps:
                _detail_items.append(f"  手順:")
                # steps は文字列（改行区切り）またはリスト。文字列の場合は分割する。
                if isinstance(steps, str):
                    steps = [s.strip() for s in steps.split('\n') if s.strip()]
                for si, s in enumerate(steps, 1):
                    _detail_items.append(f"    {si}. {s}")
            _detail_items.append("")
        initial_triage_summary = "\n".join(_items)
        initial_triage_detail = "\n".join(_detail_items)

    parts = [
        f"[診断ワークブック] {dev_id}の予兆検知に対する確認手順書を作成してください。",
        "",
        f"【予兆概要】",
        f"・デバイス: {dev_id} (タイプ: {dev_type})",
        f"・予兆状態: {sanitize_prediction_context(pred_state, 200)}",
        f"・信頼度: {prob*100:.0f}%",
        f"・推定RUL: {ttf_display}",
        f"・急性期進行: {ttc_min}分",
        f"・影響範囲: {affected}台",
        f"・検出シグナル数: {signal_count}件",
    ]

    if initial_triage_detail:
        parts.extend([
            "",
            f"【★★★ ステップ①「初期確認（推奨アクション）」の全内容 ★★★】",
            f"以下は運用者の画面に既に表示済みの初期確認です。",
            f"ステップ②では、これらの初動調査コマンドを「実行した後」の結果をどう読むかを解説してください。",
            "",
            initial_triage_detail,
        ])
    elif initial_triage_summary:
        parts.extend([
            "",
            f"【ステップ①の初期確認】",
            initial_triage_summary,
        ])

    # ★ トリアージ実行結果の自動連携: 実際にコマンド実行した結果があればプロンプトに含める
    _triage_output = format_triage_results_for_llm(dev_id)
    if _triage_output:
        parts.extend([
            "",
            "【★★★ トリアージコマンドの実行結果（実機出力）★★★】",
            "運用者が初期確認のコマンドを実行した結果です。",
            "この実機出力を踏まえて、OK/NG判定と次のステップの診断手順を作成してください。",
            "",
            _triage_output,
        ])

    parts.extend([
        "",
        "【作成すべき内容（ステップ②: 確認手順書）】",
        "これはステップ①「初期確認」の次に実施する、より詳細な診断手順書です。",
        "★★★ 重要: ステップ①の内容を繰り返さないでください。ステップ①は完了済みとして扱い、その結果を踏まえた「次のステップ」を記載してください。★★★",
        "",
        "1. ステップ①の結果に対するOK/NG判定基準（表形式）",
        "   - show系コマンドの出力例（正常時 vs 異常時）を対比表示",
        "   - 具体的な閾値・パターンでNG判定する基準を明記",
        "",
        "2. 深掘り診断コマンド（ステップ①より踏み込んだ調査）",
        "   - 各コマンドの「何を見るか」「NGの場合の意味」を明記",
        "   - コマンド出力のどの行・どの値に注目すべきかを具体的に指示",
        "",
        "3. エスカレーション判断のデシジョンツリー",
        "   - 結果の組み合わせパターンごとに「現場対応」vs「エスカレーション」を判定",
        "   - エスカレーション先（ベンダーTAC、上位運用者等）と伝達すべき情報を明記",
        "",
        "★ 注意: config系（設定変更系）コマンドは含めないでください。それはステップ③で扱います。",
        "★ 全体の構造: ①初期確認→②確認手順書(本書)→③予防措置プラン(次ステップ)",
    ])

    return "\n".join(parts)


def build_prevention_plan_scenario(cand: dict) -> str:
    """
    ③ 予防措置プラン（メンテナンス作業計画書）

    役割: 「修復するにはどうする？」
    ・config系の予防コマンドを含む具体的なメンテナンス計画
    """
    dev_id    = cand.get('id', '不明')
    prob      = cand.get('prob', 0)
    dev_type  = cand.get('type', 'UNKNOWN')

    pred_state    = cand.get('predicted_state') or cand.get('label', '').replace('🔮 [予兆] ', '') or '不明'

    ttf_hours     = cand.get('prediction_time_to_failure_hours', 0)
    failure_dt    = cand.get('prediction_failure_datetime', '')
    timeline      = cand.get('prediction_timeline', '')
    affected      = cand.get('prediction_affected_count', 0)

    # RUL表示
    if ttf_hours >= 24:
        ttf_display = f"{ttf_hours // 24}日後"
        if failure_dt:
            ttf_display += f" ({failure_dt})"
    elif ttf_hours > 0:
        ttf_display = f"{ttf_hours}時間後"
    else:
        ttf_display = "障害が切迫"

    # 推奨アクション一覧
    rec_actions = cand.get('recommended_actions', [])
    triage_summary = ""
    if rec_actions:
        _items = []
        for ra in rec_actions:
            cmd  = ra.get('command', ra.get('action', ''))
            eff  = ra.get('effect', '')
            pri  = ra.get('priority', '')
            _items.append(f"- {pri}: {cmd} (効果: {eff})")
        triage_summary = "\n".join(_items)

    parts = [
        f"[予防措置プラン] {dev_id}の障害予兆に対するメンテナンス作業計画書を作成してください。",
        "",
        f"【予兆概要】",
        f"・デバイス: {dev_id} (タイプ: {dev_type})",
        f"・予兆状態: {sanitize_prediction_context(pred_state, 200)}",
        f"・信頼度: {prob*100:.0f}%",
        f"・推定RUL: {ttf_display}",
        f"・影響範囲: {affected}台",
    ]

    if triage_summary:
        parts.extend([
            "",
            f"【初期確認で実施済みの内容】",
            triage_summary,
        ])

    # ★ トリアージ実行結果の自動連携
    _triage_output = format_triage_results_for_llm(dev_id)
    if _triage_output:
        parts.extend([
            "",
            "【★★★ トリアージコマンドの実行結果（実機出力）★★★】",
            "運用者が初期確認のコマンドを実行した結果です。",
            "この実機出力の内容を考慮して、具体的な予防措置コマンドを作成してください。",
            "例: show interfaces で入力エラーが多い場合 → ケーブル交換手順を優先",
            "例: show environment で温度異常がある場合 → 冷却系の対処を含める",
            "",
            _triage_output,
        ])

    parts.extend([
        "",
        "【作成すべき内容（ステップ③: メンテナンス作業計画書）】",
        "1. 作業前準備（バックアップ手順、影響確認、メンテナンスウィンドウ設定）",
        "2. 具体的な予防措置コマンド（config系を含む、手順順序を明記）",
        "3. 作業後の確認手順（正常性確認コマンド、サービス影響チェック）",
        "4. ロールバック手順（作業失敗時の復旧方法）",
        "5. 作業完了報告テンプレート",
        "",
        "★ 全体の構造: ①初期確認(完了)→②確認手順書(完了)→③予防措置プラン(本書)",
        "★ ①②は完了済みです。それらの結果を踏まえた具体的なメンテナンス手順を作成してください。",
    ])

    return "\n".join(parts)
