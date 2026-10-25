# 開発・テストガイド

| 項目 | 内容 |
| --- | --- |
| 状態 | 設計ベースライン案。mise・uv/pnpm workspace・固定依存・format/lint/型検査を提供。アプリ機能、CI、製品テストは未実装 |
| 版 | 0.2.0 |
| 更新日 | 2026-10-26（日本時間） |
| 責任者 | 技術リード。実装担当者が変更と同じPRで更新する |
| 対象 | スマホ、Pi、API、Worker、インフラの開発者とレビュー担当者 |

このガイドは、同じ契約と検証条件でチーム開発を始めるための実装方針を定める。現時点で利用できる成果物は設計文書・仕様ファイルとmiseによるworkspace開発・検証環境である。5節で提供済みのコマンドと今後実装するコマンドを区別する。アプリの起動コマンドやCIは初回実装で提供する契約である。

関連文書: [要件定義](01-requirements.md)、[基本設計](02-architecture.md)、[データ・権限](03-data-and-authorization.md)、[インターフェース](04-interfaces.md)、[運用手順](06-deployment-and-operations.md)、[開発参加](../CONTRIBUTING.md)。

## 1. 開発の単位と担当境界

モノレポを採用し、フロントエンドとPythonサービスの依存関係をそれぞれ固定する。共有パッケージは公開APIを通して利用し、別サービスの内部モジュールやFirestoreの任意コレクションを直接操作しない。

| パス | 担当する内容 | 境界 |
| --- | --- | --- |
| `apps/mobile` | CapacitorによるiOS・Androidアプリ | 認証、通知、マイク、画面のネイティブ連携 |
| `apps/display` | PiのChromium向け画面 | 共有表示、音声状態、接続状態、個人セッション表示 |
| `packages/ui` | 共通Reactコンポーネント | 端末固有APIを直接呼ばずadapterを受け取る |
| `packages/api-client` | TypeScriptのAPIクライアント | OpenAPIを基に生成し、独自の型を重複定義しない |
| `services/api` | FastAPIのHTTP・SSE・WebSocket入口 | 身元確認、認可、要求検証、永続化、接続管理 |
| `services/worker` | 非同期ジョブと定期処理 | 出典の現在状態・削除epochを再確認して処理する |
| `services/device` | PiのPython常駐サービス | 音声、クラウド接続、SQLite、Home Assistant操作 |
| `packages/agent_core` | 会話実行とツール選択 | HTTP、Firebase SDK、Pi固有処理を持たない |
| `packages/memory` | 記憶の保存・検索・グラフ展開 | 認可済みcontextを必須にする |
| `packages/tools` | ノート・タスク・Calendar・家電のツール | 入力検証、認可、確認、結果の正規化 |
| `infra/gcp` | Terraform | 環境ごとの構成とIAMの正本 |
| `tests` | 契約・結合・E2E・実機シナリオ | 本番データを持ち込まない |

表のディレクトリを作成済み。Pythonの6 memberは `src/brainoid_*` 配下に公開packageを持ち、TypeScriptの4 memberは `src/index.ts` を入口とする。アプリ・API機能は後続チケットで実装する。`infra/gcp` はTerraform用の領域であり、Python/Nodeのmemberには含めない。内部のpackage名は開発用で、OPEN-001の公開識別子を確定しない。

技術リードは共通契約の変更を管理する。UI担当は画面と音声状態、API担当は認証・認可と永続化、Agent担当は記憶・推論・ツール、Device担当は実機・ローカル処理、Platform担当はGCP・CI・運用に責任を持つ。少人数チームでは兼任できるが、権限・削除・外部操作の変更は実装者以外がレビューする。

## 2. 初回実装で行う作業

次の順序を最初の開発バックログとする。各項目はコードと確認結果が揃って完了とする。

| ID | 作業 | 完了条件 |
| --- | --- | --- |
| B-01 | モノレポ・workspace・依存固定 | PythonとNodeのバージョン、依存lock、formatter、型検査、ライセンス方針をcommit |
| B-02 | 開発用設定とモック | 秘密値のない設定例、Auth/Firestore接続、LLM/音声/家電のfakeで起動 |
| B-03 | 契約検証とクライアント生成 | OpenAPI・JSON Schemaが検証され、生成差分をCIで検出 |
| B-04 | 最小APIと認可 | 2テナントのfixtureで越境を拒否し、PIIを含まないtraceを取得 |
| B-05 | 共通UIと端末shell | スマホとPiで同じAPIへ接続し、エラー・切断・再試行を表示 |
| B-06 | 記憶の縦断実装 | スマホで明示保存、Piで許可された記憶を取得、訂正・忘却が両端に反映 |
| B-07 | 会話run・外部操作・非同期処理 | durable run/job、outbox、重複実行、期限切れ、結果不明、確認フローを検証 |
| B-08 | stagingと実機試験 | 1台以上の対応Pi、iOS、Androidで重要シナリオを実行し結果を記録 |

最初の縦断シナリオは「スマホで就寝前の照明設定を共有記憶に保存し、Piから呼び出して対象の照明を設定し、スマホで訂正する」とする。先にsharedの権限で成立させ、その後privateの拒否・個人セッションのケースを追加する。

## 3. 開発環境と依存の固定

開発ツールとコマンドはmiseで管理する。Python、Node.js、uv、pnpmは [mise.toml](../mise.toml) で完全なバージョンを固定し、[toolchain.md](toolchain.md) に初回セットアップ、対応環境、更新方法を記録する。最新版への自動追従は行わない。

Python依存は [pyproject.toml](../pyproject.toml)・[uv.lock](../uv.lock) で管理し、`mise run setup`（`uv sync --all-packages --locked`）でuvが `.venv` を作成・同期する。現在のチケット管理スクリプトに外部パッケージ依存はない。依存の追加・削除は `uv add`・`uv remove`、実行は `uv run` を使う。BA-FND-01でこのuvプロジェクトをworkspaceへ拡張済み。共通devツールはルート、実行依存は各memberに宣言する。JavaScript/TypeScript依存はpnpm workspaceと `pnpm-lock.yaml` で管理する。ツール版の正本はmise設定、パッケージ依存の正本はworkspace設定・lockfileとし、CIもこれらに合わせる。ドキュメントでは同じバージョン一覧を手作業で複製しない。

Java、Android SDK、Xcode、Capacitorのnative platform/CLI、Docker、Terraform、Google Cloud CLI、Firebase CLIは、各コンポーネントの実装時に対応関係を確認してバージョンと導入方法を追加する。iOSビルドの環境と実機署名手順はMobile担当が提供する。Dockerイメージ、GitHub Actions、Pi配布物は再現可能な参照へ固定する。

開発環境を3段階に分ける。

| 環境 | 用途 | 外部接続とデータ |
| --- | --- | --- |
| `local-fake` | 通常のUI・状態遷移・Agent開発 | fake model/speech/tools、架空のfixtureのみ。クラウド費用を生じさせない |
| `local-emulator` | 認証・DB・ジョブの結合確認 | Firebase Auth/Firestore emulatorとローカルdispatcher。外部APIはfake |
| `staging` | GCP、OAuth、通知、索引、実機の確認 | 本番とは別project・credential・テストアカウント。外部API呼出しを上限管理 |

Firestore emulatorは本番の全制限や複合索引を再現しないため、索引、同時更新、制限に関わる確認はstagingでも実施する。[公式仕様](https://firebase.google.com/docs/emulator-suite/connect_firestore#how_the_cloud_firestore_emulator_differs_from_production)

## 4. 設定と秘密情報

設定は起動時に型付きモデルで検証する。不足・矛盾があれば起動に失敗させ、黙って別のproviderやprojectへ接続しない。

| 分類 | 予定の設定名・例 | 扱い |
| --- | --- | --- |
| 実行環境 | `APP_ENV`、`LOG_LEVEL`、`API_BASE_URL` | 公開可能。クライアントに入れてよい値を明示する |
| GCP配置 | `GCP_PROJECT_ID`、`GCP_REGION`、`FIRESTORE_DATABASE_ID` | 接続先の識別。秘密値として扱う必要性と権限は別に判断する |
| モデル・音声 | `MODEL_PROVIDER`、`MODEL_ID`、`STT_REGION`、`TTS_VOICE` | 環境ごとの採用値。暗黙のglobal endpoint切替を禁止 |
| 開発接続 | `FIRESTORE_EMULATOR_HOST`、`FIREBASE_AUTH_EMULATOR_HOST` | localのみ。本番設定に存在したら起動を拒否 |
| 利用量 | tenant単位の日次回数、音声時間、同時run数、graph予算 | サーバー側設定。クライアント値を信用しない |
| 秘密の参照 | OAuth client secret、token署名鍵、APNs鍵の参照ID | 本番はSecret Managerなどの管理先から取得。値をGitに入れない |
| 端末固有 | device credential、Home Assistant token、ローカルセッション鍵 | Piサービスの制限付き領域。ブラウザbundleへ渡さない |

`.env.example` は名前・説明・安全なfake値のみとし、実値を含むファイルはcommitしない。サーバーのservice account key JSONをスマホ・Pi・コンテナイメージに組み込まない。ローカルのGCP接続は開発用projectへの個別認証、CIは短命の認証を用いる。GitHub ActionsからGCPへの認証はOIDC/Workload Identity Federationを使う方針とする。[公式ガイド](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-google-cloud-platform)

ログに音声本体、会話本文、記憶本文、OAuth token、Authorization header、ペアリング秘密値を出さない。デバッグのために内容が必要な場合は架空fixtureを使い、productionのログレベルを上げて代用しない。

## 5. 提供する開発コマンド

開発コマンドの入口は `mise run <task>` とする。現在、次のタスクを [mise.toml](../mise.toml) で提供している。初回は[セットアップ手順](toolchain.md)を実行する。

| 提供済みコマンド | 現在の振る舞い |
| --- | --- |
| `mise run setup` | toolchainを検査し、`uv sync --all-packages --locked` と `pnpm install --frozen-lockfile` で全memberの依存を導入 |
| `mise run format` | workspaceのPython/TypeScript/対象JSONをRuff・Biomeで整形 |
| `mise run check:toolchain` | 実行中のPython/Node/uv/pnpmと宣言の固定版を確認 |
| `mise run check:format` / `check:lint` / `check:types` | 書換えなしの整形・lint・Python/TypeScript型検査 |
| `mise run check:tickets` | チケットの依存関係・進捗台帳・完了記録を検証 |
| `mise run check` | toolchain・format・lint・型・チケットを検証 |
| `mise run test` | 基盤のtoolchain拒否・package解決・秘密ファイル除外と既存チケット管理テストを実行 |

現在のPython検証タスクは `uv run --all-packages --locked --offline` を使う。lockfileの整合確認とキャッシュからの環境同期を行い、ネットワークからの依存取得が必要な場合は失敗する。先に `mise run setup` を実行する。

文書リンク・OpenAPI・JSON Schemaの自動検証は現在提供していない。BA-FND-04で契約用validator、BA-FND-06で既存のMarkdownリンク検証ツールを導入する。導入までは変更したリンク・契約と、要件・テストIDの対応をレビューで確認する。

Ruff・mypyは `scripts`・`tests`・Python member、Biome・TypeScriptはNode memberと設定JSONを検査する。既存のチケット管理Skillは既存テストで回帰確認し、このチケットで一括整形・型移行しない。以下は後続チケットで実装・拡張するmiseタスクの契約である。check・testも、現在は上表の範囲だけを実行する。

| 予定コマンド | 実装する振る舞い |
| --- | --- |
| `mise run dev` | fakeを既定としてAPI・worker・UIを起動。終了時に子processを停止 |
| `mise run dev-emulator` | emulatorを明示して起動。専用fixture以外に接続しない |
| `mise run seed-demo` | 2テナントの架空データをlocal専用環境へ投入。prodなら拒否 |
| `mise run generate-client` | OpenAPIからTypeScript clientを再生成 |
| `mise run check` | format、lint、型検査、schema検証、生成差分確認 |
| `mise run test` | 外部APIに接続せずunit・contract・local integrationを実行 |
| `mise run test-staging` | 明示したstagingで外部API・索引・IAM確認を実行。件数上限と後片付けを伴う |
| `mise run test-device` | 対応Piとテスト用Home Assistantへの実機試験を開始 |

各タスクには前提・終了コード・出力先を定義する。インストールや課金を伴う処理を通常のテストに隠さない。タスクを追加・拡張した時点で、この節の提供済み範囲を更新する。

## 6. モックとfixture

fakeは本番adapterと同じinterfaceを実装する。LLMの自由回答を固定文字列に置き換えるだけでなく、ツール要求、遅延、タイムアウト、壊れた引数、途中停止を制御可能にする。モデルの非決定性に依存しないテストで、アプリケーションの認可と状態遷移を検証する。

| 対象 | fakeで再現する条件 |
| --- | --- |
| `ModelProvider` | 通常回答、不正tool schema、禁止tool、別tenant IDの出力、長いstream、呼出し失敗 |
| `SpeechProvider` | 認識成功、無音、認識失敗、再生途中停止、マイク権限拒否 |
| `MemoryStore` | 現在/期限切れ/訂正済み/削除済みfact、曖昧な人名、検索候補の権限差 |
| `ToolExecutor` | 成功、明示失敗、完了直後の応答喪失、確認待ち、失効したactor |
| Calendar | OAuth期限切れ、権限不足、ETag競合、同一提案の再確認、結果不明 |
| Home Assistant | 存在しないentity、接続切断、受付後状態不変、観測遅延 |
| 時刻・配送 | タイムゾーン、日付跨ぎ、DST、時計ずれ、重複配信、順序入替、再接続 |

共通fixtureはtenant A/B、Aのowner/member、Bのowner、A内のprivate/shared会話、同名の別人・別室の照明、登録/失効Piを含める。権限のない事実を検索候補に混ぜ、最終回答だけでなくLLM入力・SSE・TTS・ログにも漏れないことを検証する。fixtureの時刻とIDを固定し、テストがwall clockや乱数に依存しないようにする。

## 7. テストの層と責任

| 層 | 対象 | 実行タイミング | 主担当 |
| --- | --- | --- | --- |
| Unit | 認可、期限、version、状態遷移、retrieval予算、確認hash | 関連PRごと | 各実装担当 |
| Contract | OpenAPI、JSON Schema、API client、device protocol、tool schema | 全PR | API・Device |
| Integration | DB、outbox、jobs、削除epoch、再接続、模擬外部API | 関連PR・main | Backend |
| E2E | スマホからPiまでの利用シナリオ | main・release候補 | QA相当の担当 |
| 実機 | マイク、スピーカー、電源断、署名更新、OS権限、push | Device/Mobile変更・release候補 | Device・Mobile |
| 実provider評価 | 日本語の回答、検索精度、実行引数、遅延、費用 | model/prompt変更・release候補 | Agent |

実装をそのままなぞるテストより、利用者に見える結果と境界条件を優先する。品質判断を行数coverageだけに委ねない。全PRで実機を占有する構成にはせず、変更範囲とrelease gateに応じて実行する。

## 8. 必須の受入・回帰ケース

以下は実装するテストケースであり、合格済みという記録ではない。FR番号に対応する受入基準は[要件定義](01-requirements.md)を正本とする。

| Test ID | 対応要件 | 入力・状況 | 合格条件 |
| --- | --- | --- | --- |
| T-001 | FR-001/002 | tenant AのtokenでBのIDを指定 | resourceを返さず、会話・検索・操作・exportの全経路で拒否 |
| T-002 | FR-002/006 | Aのadminがmemberのprivate記憶を照会 | 管理者という理由だけでは取得できず、存在も漏らさない |
| T-003 | FR-003 | ペアリングcodeの再利用・期限切れ・総当たり | 登録できず、rate limitを適用。別tenantへ移せない |
| T-004 | FR-003/014 | 接続中Piを失効し、既存tokenで操作 | 60秒以内の設計目標で接続/操作を拒否し、再登録を要求 |
| T-005 | FR-004 | 同じagentへ別端末からprivate/shared会話 | 人格設定を共有しつつ、履歴とoutput deviceを混同しない |
| T-006 | FR-005 | マイク許可拒否、無音、音声API失敗 | テキスト入力に戻れ、意図しない録音・無限待機がない |
| T-007 | FR-005 | TTS中に停止、tool実行中にキャンセル | 発話停止と操作の取消可能性を区別し、実行済みを取消済みと表示しない |
| T-008 | FR-006 | 明示的な記憶保存直後に再検索 | 永続化したfactの出典・scopeが一致し、保存前に成功を通知しない |
| T-009 | FR-006 | 別室の同名照明、同名の別人 | 表示名だけでentityを統合しない。曖昧なら解決を求める |
| T-010 | FR-006/013 | 訂正とembedding jobが同時に動く | stale versionを再保存せず、古い内容が検索に戻らない |
| T-011 | FR-006/013 | forget後に古いjobを再送 | epoch/tombstoneを再確認し、要約・vector・cacheから復活させない |
| T-012 | FR-007 / FR-008 | shared項目の作成者とadminが同じversionを更新 | 両者の更新権限を確認した上で最初のみ成功し、古いIf-Matchは412。権限のないmemberは別途拒否 |
| T-013 | FR-009 | 同一reminderを二重配送 | 通知の重複を抑え、配信受付と利用者の確認を分けて保存 |
| T-014 | FR-009 | 端末offline後、期限超過して再接続 | 再配信方針に従い、古い音声通知を一斉再生しない |
| T-015 | FR-010 | Calendar書込proposalを引数改変して確認 | hash不一致を拒否。確認済み権限を別イベントへ転用しない |
| T-016 | FR-010 | Calendar書込後に応答だけ失う | provider状態とoperationを照合し、同じ予定を盲目的に再作成しない |
| T-017 | FR-011 | 同じcommand_idを再送 | 一度の状態設定として扱い、SQLiteの記録を再起動後も参照 |
| T-018 | FR-011 | 期限切れcommand、禁止service、任意引数 | APIとPiがそれぞれ検証し、Home Assistantを呼ばない |
| T-019 | FR-011 | HAは受付したが家電状態が確認できない | succeededと偽らず、失敗またはoutcome_unknownを返す |
| T-020 | FR-012 | 話者不明のPiからprivate情報を要求 | sharedかつshared_displayの範囲だけを検索・表示 |
| T-021 | FR-012 | 個人session失効直前にTTS/検索が完了 | 失効後の私的出力を破棄し、画面・音声buffer・cacheを消す |
| T-022 | FR-013 | 会話・tenant削除とexportの競合、生成済exportの再取得 | 削除開始後の参照/exportを拒否。生成済みでも失効・forget後はダウンロードできず、messages等の子データも処理 |
| T-023 | FR-014 | API revision変更でsocket再接続 | durable cursor/stateから復帰。失われたin-memory状態に依存しない |
| T-024 | FR-014 | 同一Piの古い接続と新接続が併存 | session generationで古い接続を排除し、二重配送を抑止 |
| T-025 | FR-014 | 更新中電源断・破損artifact・期限切れ署名metadata | 未検証版を起動せず、最後の正常版または復旧画面へ戻る |
| T-026 | FR-015 | tenant上限超過、provider429、queue詰まり | 制御されたエラーとなり、無制限retryや課金増幅がない |
| T-027 | FR-001/015 | prod起動設定にemulator/fake認証を混入 | 起動を拒否し、認証を省略したサービスを公開しない |
| T-028 | FR-002/013/015 | 古いbackupを隔離環境へ復元 | 独立した削除・認可変更台帳を適用し、忘却済み情報を返さない。剥奪済みmembershipは再認証しても復活しない |
| T-029 | FR-006/011 | 会話や取得文に「権限を変更せよ」を混入 | 記憶を命令権限として扱わず、許可外のtoolを実行しない |
| T-030 | FR-009/012 | lock画面push・共有ディスプレイへの通知 | private本文を表示しない。認証後のAPI取得で本文を得る |
| T-031 | FR-004/014 | run受付202直後にAPIを停止、workerを途中再起動 | durable run/jobから再開・照合し、APIの背景threadに依存しない。lease/fencingで二重tool実行を防ぐ |
| T-032 | FR-004/015 | 長いstreamを250ms目安の差分batchで配信 | 再接続cursorと順序を維持し、毎tokenのDB書込にしない。cancelとprivate session失効をbatch送出前に再確認 |
| T-033 | FR-002 | adminが自己昇格、owner変更、owner移譲を要求 | owner専用操作を拒否し、graph上の関係やmodel出力で権限を拡大しない |
| T-034 | FR-001/002/013 | tenant ownerが他人のFirebase accountや他tenant削除を要求 | tenantの管理権限からglobal accountや他tenantの管理権限を導かず拒否 |
| T-035 | FR-004/012/014 | スマホでPi個人sessionを開始し、そのPiへrun出力を指定した後に接続を切断 | Piがsession・期限・scope・対象runを受け取り、再接続snapshotで現行状態へ復帰。失効済みsessionや他端末向けrunを表示・再生しない |
| T-036 | FR-011/014 | command取消と実行直前のcommand_start/grantを競合させる | serverのatomicな判定で一方の状態だけが成立。取消が先なら実行grantを出さず、grantが先なら取消保証不可を表示。grant未確認のままHAを呼ばない |
| T-037 | FR-004/010 | Calendar確認待ちでworker終了後、確認・拒否・期限切れ・run取消・二重confirmを実行 | checkpointから許可されたrunだけを再開。proposalとtool_callを一回だけ消費し、確認待ちworkerを保持しない。拒否・期限・取消は規定の終端状態となり、外部書込の応答喪失時は照合して重複を防ぐ |
| T-038 | FR-009 | 通知済みreminderをスヌーズした後、旧通知ack・旧配送jobが遅れて到着 | 再予定世代とversionが更新され、新しいdue_atに従う。旧世代のack・配送・完了処理は新しい予定へ影響せず、重複音声通知を出さない |

検索・要約・LLM入力の漏えい確認には、fixture内だけの識別語を用いる。認可はサーバー側で確認する必要がある。FirestoreのサーバークライアントはSecurity Rulesを迂回するため、Rulesのテストだけでは本APIのテナント分離を証明できない。[公式仕様](https://firebase.google.com/docs/firestore/security/rules-conditions)

## 9. モデルと音声の評価

日本語の固定評価セットに、通常会話、呼び方、訂正、曖昧な人名、日付表現、方言・雑音のある音声、外部サービス失敗を含める。会話本文の完全一致ではなく、取得した出典、許可された記憶、tool名・引数・対象、確認の有無、最終状態を評価する。

model/prompt/tool/schemaの組合せにversionを付ける。変更前後で同じ評価セットを走らせ、失敗例、実行回数、測定条件、p50/p95、token量・音声秒数・DB読取数を保存する。測定用会話は架空データとし、一般の利用者会話を評価セットへ自動流用しない。

要件の遅延値は未測定の目標である。テキストの最初のdelta、音声終了から最初の音、操作要求から観測結果までを別々に測る。run queue待ち、STT、検索、LLM、tool、TTS、Firestore差分の保存・配送、ネットワーク待ちに分解し、成功した一部の要求だけを測定母集団にしない。push配信依頼完了とOSによる実際の表示も分ける。

## 10. CIとPRの完了条件

CIは次の順序で実装する。PR段階では本番credentialを使わず、forkからの未信頼コードにはクラウド書込権限を渡さない。

1. Markdownリンク・schema・format・lint・型を確認する。
2. client再生成後に未commit差分がないことを確認する。
3. unit・contract・認可・local integrationを実行する。
4. dependency/secret検出とライセンス一覧の更新を行う。
5. mainの信頼された変更からimmutable artifactをbuildし、stagingへ配備する。
6. release候補で実provider・実機・復旧試験を行い、結果とartifact digestを紐付ける。

実装完了の条件は、受入基準を満たすこと、必要な境界テストがあること、契約と文書が一致すること、ログとエラーが運用可能なこと、破壊的変更に移行・復旧手順があることとする。UI変更ではスマホ・ディスプレイ双方の状態を確認し、private情報を表示する変更では認可と消去も確認する。

PRには「解く問題」「利用者に見える変化」「影響するFR/API/schema」「実施した検証」「未実施とその理由」「展開・切戻し上の注意」を記載する。未実装のテストを合格扱いにせず、実行結果のURLまたは再現手順を付ける。

## 11. 開発開始時の未確定事項

| 項目 | 担当 | 決める時期と条件 |
| --- | --- | --- |
| OPEN-001: repository・配布識別子 | Product/Tech Lead | 名称Brainoid Agentは確定。repository作成前にパッケージIDやapp IDを固定する |
| OPEN-002: 担当者・レビュー担当 | Tech Lead | 最初の実装PR前。兼任するroleを明記する |
| OPEN-003: 対応OS・実機・音響部品 | Device/Mobile | B-01と実機調達時。Pi5 8GBを基準に組合せを記録する |
| OPEN-005: SDK/model/regionと費用 | Agent/Platform | B-01〜B-02。公式対応状況と小規模計測を確認する |
| OPEN-006: OAuth/APNs・ストア設定 | Mobile/Platform | 外部結合前。テスト環境と配布環境を分ける |
| OPEN-007: 目標の校正 | QA/Tech Lead | staging評価後、公開β前。母集団・成功率・実機条件を残す |

## 12. 一次資料

本書のB-01〜B-08は初回開発の作業群であり、1PRの境界ではない。具体的な変更単位と依存関係の正本は[実装チケット](08-implementation-tickets.md)のBA-* IDとする。各チケットの検証は本書のT-001〜038と対応させ、機能PRの検証を最後の総合QAへ先送りしない。

公式仕様の確認日: 2026-10-25。設計上の採用値やテスト条件は本プロジェクトの案である。

- [Firebase emulatorと本番の差異](https://firebase.google.com/docs/emulator-suite/connect_firestore#how_the_cloud_firestore_emulator_differs_from_production)
- [Firestore Security Rulesとサーバークライアント](https://firebase.google.com/docs/firestore/security/rules-conditions)
- [GitHub ActionsからGCPへのOIDC認証](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-google-cloud-platform)
- [Cloud Tasksの重複実行などの制限](https://docs.cloud.google.com/tasks/docs/common-pitfalls)
