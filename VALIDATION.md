# 文書と契約ファイルの検証記録

対象版0.2.0、最終確認日2026-10-26。対象はこの文書パッケージです。アプリ本体、GCPの構築、実機性能の合格記録ではありません。

## 1. 含まれる設計

| 対象 | 内容 |
| --- | --- |
| 開発文書 | 要件、基本設計、データ・権限、インターフェース、開発・テスト、配備・運用の6種類 |
| 要件の追跡 | FR-001〜015、AC-001〜015、NFR-001〜009 |
| 設計判断 | 基本設計内のADR-001〜011 |
| 製品の検証計画 | T-001〜038。実装後に行う具体的なケース |
| 実装チケット | 96件。担当ロール、依存PR、変更範囲、受入条件、検証、外部前提を記載 |
| OSS公開用 | README、LICENSE候補、CONTRIBUTING、SECURITY |
| 通信契約 | OpenAPI、device protocol、run events、tool contracts |
| HTTP契約の規模 | 64 paths、84 operations、91 schemas |

## 2. 行った静的検証

- 文書のUTF-8読込み、必須ファイル、相対リンク先ファイルの存在を確認。
- OpenAPI YAMLとJSON Schemaファイルが構文解析できることを確認。
- パッケージ内のスキーマ参照が実在する定義へ解決されることを確認。
- OpenAPIのoperationIdの一意性、pathパラメーターの一致と必須指定、responsesと認証方式の定義を確認。
- 各JSON Schemaが選択したDraft 2020-12を宣言していることを確認。
- 内部スキーマ参照280件の解決と、22件の限定的な正例・負例を確認。目的語ID/valueの排他、text/audioの排他、private表示の禁止、Calendar ETag、command generation、観測なしの成功報告、モデル引数へのtenant注入などを対象とした。
- 要件・受入・テストIDの対応と、文書間の主要な状態名・保持期間・数値目標をレビュー。
- Brainoid Agentへの名称統一と、文書版0.2.0への更新を確認。
- 実装チケット96件のID一意性、依存先の存在、280件の依存関係に循環がないことを確認。
- FR-001〜015の全15機能要件が実装チケットへ対応し、各チケットに受入条件があることを確認。
- 実装チケット内の見出しリンクとテストIDが、定義済みの見出し・検証計画を参照することを確認。

再実行方法（miseの導入は[セットアップ手順](docs/toolchain.md)を参照）:

~~~bash
mise trust
mise install
mise run setup
mise run check:docs -- --json
mise run check:tickets
mise run test
~~~

スクリプトの終了コード0とstatus=PASSは、上記の構造検証の結果を表します。スキーマを満たす入力が、利用者の権限や業務条件まで満たすことを保証するものではありません。

## 3. 文書間で整合させた重要事項

| 項目 | 設計上の扱い |
| --- | --- |
| 個人情報と管理者 | owner/adminであっても他人のprivate内容は取得しない |
| 管理範囲 | tenant ownerは他テナントや他人のグローバルアカウントを削除しない |
| Piへの引継ぎ | 個人sessionと出力runを通知し、再接続snapshotから再同期する |
| 家電操作の取消し | 実行直前のclaim/grantと取消しを同じ正規状態で競合させる |
| Calendarの確認待ち | checkpointを保存してworkerを終了し、確認後のresume jobで再開する |
| スヌーズ | 新しい予定版を発行し、古い通知・job・ackを現行予定へ適用しない |
| 忘却と元会話 | 残した履歴についても、対象内容のモデル・要約・検索への再利用を抑止する |
| export | 生成後も取得時に認可・削除状態・期限を確認する |
| backup復元 | DB外の削除・認可台帳を再適用し、失効した権限を再ログインだけで戻さない |

これらは仕様として整合を確認した事項です。実装の正しさは[開発・テストガイド](docs/05-development-and-testing.md)の回帰ケースで検証します。

## 4. 今後の実装で検証すること

本パッケージの簡易検証スクリプトは、OpenAPI/JSON Schemaの全メタスキーマ適合検証を代替しません。[実装チケット](docs/08-implementation-tickets.md)のBA-FND-04・06で専用validatorとクライアント生成をCIへ導入し、正例・負例・互換性を継続検証します。

チケットの依存関係に循環がなく要件IDが対応していても、実装工数や全ての結合条件を保証するものではありません。調査・実装で変更が必要になった場合は、PRの境界と依存先を更新します。S/Mは変更範囲の目安であり、日数の見積りではありません。

製品の認証・認可・状態遷移・外部API呼出し、Firestoreの実索引、Cloud Runの実接続、料金、音声品質、Pi更新と復旧はまだ実行していません。T-001〜038は検証計画であり、実行済みの合格数ではありません。

Mermaid図は編集可能なソースとして同梱しています。外部レンダラーによる画像化と、全外部URLの自動死活監視はこの静的検証の対象外です。重要なサービス制約には公式資料へのリンクを付け、採用版・地域・提供条件は実装開始と公開前に確認します。

## 5. mise初回導入時の検証（2026-10-26）

初回導入コミット `ffcd047` での記録です。Linux x86_64、mise 2026.9.13と [mise.toml](mise.toml) の固定ツール版で確認しました。uvプロジェクト管理へ移行した後の結果は次節に記録します。

| 検証 | 結果 |
| --- | --- |
| `mise install` | 指定した4ツールを解決。実行環境ではすべて導入済み |
| `mise run setup` | 新規の `.venv` を固定版Pythonから作成し、指定版PyYAMLを導入。再実行も成功 |
| `mise tasks validate` | 5タスクの定義が正常 |
| `mise run check` | 文書・契約検証PASS。96チケットの依存関係と進捗台帳が正常 |
| `mise run test` | チケット管理スクリプトの単体テスト18件が成功 |
| `mise -C docs run check:docs -- --json` | サブディレクトリからの実行と引数転送が成功し、JSONのstatus=PASS |
| `mise exec` での版・実行パス確認 | Python・Node.js・uv・pnpmが指定版と一致。Pythonはプロジェクトの `.venv` を使用 |

ツール本体を未導入の環境でのダウンロード、macOS・WSL2・Piでの動作、アプリ用workspace・CI・製品テストは未検証です。

## 6. uvプロジェクト管理への移行検証（2026-10-26）

同じLinux環境で、[pyproject.toml](pyproject.toml)・[uv.lock](uv.lock) による依存管理とuvによる仮想環境の作成・同期を確認しました。

| 検証 | 結果 |
| --- | --- |
| `mise run setup` | `uv sync --locked` で既存の `.venv` を同期し、PyYAMLをdev依存として利用可能 |
| `mise exec -- uv lock --check --offline` | 依存宣言とlockfileが一致 |
| `mise tasks validate`・`mise run check`・`mise run test` | 5タスクの定義、文書・契約・96件のチケット台帳が正常。単体テスト18件が成功 |
| `mise -C docs run check:docs -- --json` | uv経由でもサブディレクトリからの実行・引数転送が成功し、status=PASS |
| `mise exec -- uv run --locked --offline python` での実行パス確認 | プロジェクトの `.venv` とmiseの指定Python、lockfileのPyYAMLを使用 |
| `/tmp` の新規環境で `uv sync --locked --offline` を2回実行 | コピーしたpyproject・lockfileとローカルキャッシュから環境を作成。再実行も成功 |
| `/tmp` のプロジェクト版だけを変更して `uv sync --locked --offline` | 古いlockfileの更新が必要というエラーで停止。lockfileの内容は変化しない |

新規環境の確認は使い捨てディレクトリで行い、作業中の `.venv` を削除せずに検証しました。macOS・WSL2・Pi、アプリ用workspace・CI・製品テストは引き続き未検証です。
