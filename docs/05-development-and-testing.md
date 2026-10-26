# 開発・テストガイド

| 項目 | 内容 |
| --- | --- |
| 状態 | 設計ベースライン案。workspace検証、API基盤・テストとPR検証workflowを提供。業務機能は未実装 |
| 版 | 0.2.0 |
| 更新日 | 2026-10-26（日本時間） |
| 責任者 | 技術リード。実装担当者が変更と同じPRで更新する |
| 対象 | スマホ、Pi、API、Worker、インフラの開発者とレビュー担当者 |

このガイドは、同じ契約と検証条件でチーム開発を始めるための実装方針を定める。現時点では設計文書・仕様ファイル、workspace開発・検証環境、API基盤とPR検証workflowを提供する。5節で提供済みのコマンドと今後実装するコマンドを区別する。UI・業務機能は後続チケットで提供する。

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
| `packages/jobs` | 永続job・outbox・lease・checkpoint | 同じtransactionで保存し、外部I/Oはworker handlerから行う |
| `packages/usage` | 本文なしの利用量予約・精算・同時run枠 | 共通transaction、token/音声秒。provider/HTTPを含めない。[利用量契約](usage.md) |
| `packages/persistence` | 構成済みFirestore transaction/page adapter | API/worker共通。credentialを暗黙に探索しない |
| `packages/agent_core` | 会話実行とツール選択 | HTTP、Firebase SDK、Pi固有処理を持たない |
| `packages/model_google` | Google Gen AI の ModelProvider adapter | 明示した Vertex 接続と SDK 変換。Core 最小依存には含めない。[利用・検証契約](google-model-provider.md) |
| `packages/contracts` | ID・UTC時刻・timezone・version・scope・Contextの共通型 | 認可判断はIAM層に委ねる。[通信スキーマ対応表](contracts.md) |
| `packages/memory` | 記憶の保存・検索・グラフ展開 | 認可済みcontextを必須にする。[scope検索と会話接続](memory-retrieval.md) |
| `packages/tools` | ノート・タスク・Calendar・家電のツール | 入力検証、認可、確認、結果の正規化 |
| `infra/gcp` | Terraform | 環境ごとの構成とIAMの正本 |
| `tests` | 契約・結合・E2E・実機シナリオ | 本番データを持ち込まない |

表のディレクトリを作成済み。Pythonの12 memberは `src/brainoid_*` 配下に公開packageを持ち、TypeScriptの4 memberは `src/index.ts` を入口とする。API基盤とBA-IAM-01の[本人認証・プロフィール取得](authentication.md)は `services/api/src/brainoid_api` に配置し、membership認可・他の業務機能は後続チケットで実装する。`infra/gcp` はTerraform用の領域であり、Python/Nodeのmemberには含めない。内部のpackage名は開発用で、OPEN-001の公開識別子を確定しない。

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

JavaとFirebase CLIはBA-FND-05で固定し、Auth/Firestore emulatorの起動に使う。Android SDK、Xcode、Capacitorのnative platform/CLI、Docker、Terraform、Google Cloud CLIは、各コンポーネントの実装時に対応関係を確認してバージョンと導入方法を追加する。iOSビルドの環境と実機署名手順はMobile担当が提供する。Dockerイメージ、GitHub Actions、Pi配布物は再現可能な参照へ固定する。

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

BA-FND-02でAPIが読む設定は次の通り。上表の音声・利用量・秘密参照等は各機能チケットで追加する。`.env` の自動読込みやSDKからのproject推定は行わない。

| 設定 | 必須・制約 |
| --- | --- |
| `APP_ENV` | 必須。`local-fake` / `local-emulator` / `staging` / `production` |
| `AUTH_PROVIDER` | 必須。local-fakeは `fake`、それ以外は `firebase` |
| `MODEL_PROVIDER` | 必須。localは `fake`、staging/productionは `vertex` |
| `GCP_PROJECT_ID` | 必須。小文字英字開始の6〜30文字。localは `demo-` prefix。`GOOGLE_CLOUD_PROJECT` / `GCLOUD_PROJECT` があれば一致必須 |
| `GCP_REGION` / `FIRESTORE_DATABASE_ID` / `MODEL_ID` | 全環境で明示必須。空文字・空白・URLは不可。fakeでも例の固定値を設定し、別project/model/regionへ補完しない |
| `FIRESTORE_EMULATOR_HOST` / `FIREBASE_AUTH_EMULATOR_HOST` | local-emulatorでは両方必須、credential・scheme・pathなしの `host:port`。他環境では空文字を含め存在自体を拒否 |
| `LOG_LEVEL` | 既定 `INFO`。`DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL`。APIアプリログに適用 |
| `READINESS_TIMEOUT_SECONDS` | 既定2秒。0より大きく30以下の有限数。全probeの確認に対する共通期限 |

BA-IAM-03 以降、Firebase 利用環境では `CURSOR_SIGNING_KEY`（32〜512文字の秘密値）も明示必須。全 replica・revision で共有する。テスト専用値は `.env.emulator.example` にあり、production に転用しない。[tenant API と検証・配備の詳細](tenants.md)。BA-IAM-04 からこのキーを用途分離して招待コード再導出にも使う。local-emulator では独立台帳の絶対 path `AUTHZ_LEDGER_DIRECTORY`、staging/production では独立 GCS の `AUTHZ_LEDGER_BUCKET` も必須。環境間の fallback はない。[招待の検証・保持・切戻し](invitations.md)。

設定検証はFastAPI lifespanの開始時、依存の初期化より先に行う。失敗時は起動を中止し、入力値や例外traceを含まない `api_startup_failed` を記録する。`load_settings()` は診断用に不正な設定名を含む `ConfigurationError` を返すが、値を出さない。APIの通常ログは `request_completed`、`request_id`、HTTP status、固定のerror codeのみとし、URL/query・header・body・例外本文を渡さない。Uvicornのaccess logは提供する起動コマンドで無効にする。独自の起動方法やproxyのaccess logにも同じ除外方針を適用する。

## 5. 提供する開発コマンド

開発コマンドの入口は `mise run <task>` とする。現在、次のタスクを [mise.toml](../mise.toml) で提供している。初回は[セットアップ手順](toolchain.md)を実行する。

| 提供済みコマンド | 現在の振る舞い |
| --- | --- |
| `mise run setup` | `mise install` で固定版の開発ツールを揃え、`uv sync --all-packages --locked` と `pnpm install --frozen-lockfile` で全memberの依存を導入 |
| `mise run format` | workspaceのPython/TypeScript/対象JSONをRuff・Biomeで整形 |
| `mise run check:format` / `check:lint` / `check:types` | 書換えなしの整形・lint・Python/TypeScript型検査 |
| `mise run check:tickets` | チケットの依存関係・進捗台帳・完了記録を検証 |
| `mise run check:docs` / `check:workflow` | Markdownの相対リンク・見出しをオフライン検証、Actions構文・式を検証 |
| `mise run check:contracts` / `check:client` / `check:compatibility` | OpenAPI/JSON Schema検証、生成差分、固定基準版とのHTTP契約互換性を検証 |
| `mise run generate-client` | 固定OpenAPIからTypeScript HTTP clientを再生成 |
| `mise run check` | format・lint・型・チケット・契約・生成差分・互換性・文書リンク・workflowを検証 |
| `mise run test` | API基盤・共通型・package境界・秘密除外・チケット管理、契約・文書リンク負例と生成clientのfake transportテストを実行 |
| `mise run dev-api` | export済みの設定を検証し、localhost:8000でAPIのhealthを起動。Ctrl-Cで依存を終了 |
| `mise run dev-emulator` | 明示した専用local設定でAuth/Firestoreを起動。Ctrl-Cで両方終了 |
| `mise run seed-demo` | 固定fixtureをupsert。既存のfixture IDを再利用し、アプリの時刻も固定 |
| `mise run reset-demo` | 専用local projectのAuthユーザーとFirestore全データ（子collection含む）を消去 |
| `mise run test-emulator` | 実emulatorを起動し、二重seed・初期化・prod拒否を検証して自動終了 |

現在のPython検証タスクは `uv run --all-packages --locked --offline` を使う。lockfileの整合確認とキャッシュからの環境同期を行い、ネットワークからの依存取得が必要な場合は失敗する。先に `mise run setup` を実行する。

OpenAPI・JSON Schema検証とclient生成・互換性比較はBA-FND-04で提供する。[通信契約の手順](contracts.md#httpクライアントと契約検証)を参照する。BA-FND-06の文書リンク検査は既存のlycheeを使用し、チケットの意味上の整合は引き続き `check:tickets` が担当する。

Ruff・mypyは `tests`・Python member・`scripts`、Biome・TypeScriptはNode memberと設定JSONを検査する。既存のチケット管理Skillは既存テストで回帰確認し、このチケットで一括整形・型移行しない。以下は後続チケットで実装・拡張するmiseタスクの契約である。check・testも、現在は上表の範囲だけを実行する。

| 予定コマンド | 実装する振る舞い |
| --- | --- |
| `mise run dev` | fakeを既定としてAPI・worker・UIを起動。終了時に子processを停止 |
| `mise run test`（拡張） | 外部APIに接続せず、実装した機能のunit・contract・local integrationを追加 |
| `mise run test-staging` | 明示したstagingで外部API・索引・IAM確認を実行。件数上限と後片付けを伴う |
| `mise run test-device` | 対応Piとテスト用Home Assistantへの実機試験を開始 |

各タスクには前提・終了コード・出力先を定義する。インストールや課金を伴う処理を通常のテストに隠さない。タスクを追加・拡張した時点で、この節の提供済み範囲を更新する。

### 5.1 API基盤の起動

setup後、リポジトリのルートで安全な設定例を明示的にexportする。実値を入れたファイルはGitへ追加しない。

~~~bash
set -a
. ./.env.example
set +a
mise run dev-api
~~~

別terminalから `curl -i http://127.0.0.1:8000/health/live` と `curl -i http://127.0.0.1:8000/health/ready` を確認する。起動失敗は非ゼロ終了、通常停止はCtrl-Cを使う。ログはstderrへ出力する。現在は本人認証・業務API・クラウド接続・fake業務adapterを持たず、ready=200は起動済みであることだけを示す。local-emulator等の設定検証も接続テストの代わりにはならない。

`brainoid_api.app.create_app(settings_loader=..., dependency_lifespan=...)` で設定loaderと依存のasync context managerを注入できる。context managerは検証済み `Settings` を受け取り、起動した依存の `ReadinessProbe.is_ready()` 一覧をyieldし、終了時にリソースを解放する。実adapter追加時にこの境界へ登録する。HTTP handlerは `Depends(get_settings)` で起動時の同じ設定を受け取る。probe異常・例外・timeoutはreadinessの503へ変換し、livenessには伝播しない。[FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/)

BA-FND-02のテストは `tests/test_api_foundation.py`。T-027のproduction/stagingでのfake・emulator拒否、必須設定、矛盾、起動終了、probe異常・復旧・timeout、共通エラー、成功・失敗両方のログ非出力を外部APIなしで検証する。sandboxでAnyIOのthread通知が制限される環境ではTestClientが停止するため、通常のローカル実行環境で `mise run test` を行う。

### 5.2 Auth・Firestore emulatorの起動と初期化

`mise run setup` で固定Java・Firebase CLIを導入後、リポジトリのルートで専用設定を明示的にexportする。Firebase login・実GCP project・service accountは不要。初回起動時だけ、CLIが対応版のFirestore JARを公式配布元から取得して `~/.cache/firebase/emulators` に保存する。通常の `mise run test` はemulatorを起動・取得せず、設定拒否とfixtureの静的検証だけを行う。

~~~bash
set -a
. ./.env.emulator.example
set +a
mise run dev-emulator
~~~

Authは `127.0.0.1:9099`、Firestoreは `127.0.0.1:8080`、管理用hub/loggingは4400/4500、Firestoreのwebsocketは9150を使う。UIは無効。port使用中は起動に失敗するため、他のemulatorを停止してから再実行する。起動完了後、別terminalでも上記の設定exportを行い、次を実行する。

~~~bash
mise run seed-demo
mise run seed-demo
# この専用projectの手動追加データもすべて消去する。
mise run reset-demo
mise run seed-demo
~~~

成功時は `demo_seed_complete` / `demo_reset_complete` と終了コード0を返す。不正設定・接続失敗は本文や設定値を出さず非ゼロで終了する。seedはfixture IDの文書を置き換え、同じAuth UIDを再利用する。fixture外の追加データはseedでは消さない。AuthとFirestore間の一括transactionはないので、途中失敗時は原因を解消してseedを再実行するか、reset後にseedする。resetの完了応答を待ってから次の操作へ進む。

起動terminalのCtrl-CでAuth/Firestoreが終了する。import/exportを使わないため再起動時は空になる。実データのexportを持ち込まない。実emulatorを使う自動検証は、手動起動を止めた状態で同じ設定をexportして `mise run test-emulator` を実行する。`emulators:exec` が起動・テスト後の停止を担当し、テスト失敗も非ゼロで返す。fixtureの件数・ID・時刻、二重seed、子collectionを含むresetと再seed、不正設定によるseed/reset拒否、直接clientからのFirestore読取拒否を検証する。localhost bindが `EPERM` になるsandboxでは、通常のローカル実行環境で行う。

4タスクはAPIと同じ設定検証を通した上で、`APP_ENV=local-emulator`、`GCP_PROJECT_ID=demo-brainoid`、default DB、`MODEL_PROVIDER=fake` / `MODEL_ID=fake-model`、上記の固定loopback hostを要求する。production設定やproject aliasの不一致を黙って上書きせず、socket接続より前に拒否する。seed/resetは標準HTTP clientで数値loopbackへ直接接続し、環境proxy・redirect・SDKのcredential探索・cloud fallbackを使わない。将来の外部provider設定も指定する場合はfakeに限る。APIの業務adapter・会員認可はまだ接続しておらず、この環境の成功はそれらの動作保証ではない。

ログはterminalとGit除外済みの `firebase-debug.log` / `firestore-debug.log` に出る。fixtureには実ユーザー・秘密値・tokenを入れない。Firestore Rulesはclientの直接接続を拒否し、seedだけがemulator専用の固定 `owner` 資格値を使う。API/workerのserver側認可はRulesとは別にBA-IAM-02以降で検証する。

emulatorは複合索引の要否、全transaction挙動、document/transaction等の本番制限を再現しない。索引・同時更新・上限・IAMはBA-OPS-02等のstaging検証に残す。今回のローカル成功を本番索引や役割ごとの実認可の合格扱いにしない。[Firestore emulatorとproductionの差](https://firebase.google.com/docs/emulator-suite/connect_firestore#how_the_cloud_firestore_emulator_differs_from_production)、[起動・停止の公式仕様](https://firebase.google.com/docs/emulator-suite/install_and_configure)

### 5.3 PR検証と失敗の再現

[check.yml](../.github/workflows/check.yml) は `pull_request` とmainへの `push` で、Ubuntu 24.04の `check` jobを実行する。変更パスによる省略はしない。ローカルでも同じ順序で `mise run setup`、`mise run check`、`mise run test` を使う。setupは固定ツールとlockfileから導入し、check/testはインストール済み依存を利用する。契約互換性の固定基準commitを読むため、CIはGit履歴を取得する。shallow cloneでは [openapi-baseline.ref](../specs/openapi-baseline.ref) のcommitも取得してから検証する。

CIログはsetup/check/testのstepと、`[check:docs]` 等のタスク名、失敗したファイル・診断を表示し、非ゼロ終了でjobを失敗させる。checkは独立タスクを並列実行するため、最初の失敗で他の実行を中断する場合がある。該当する個別コマンドで修正を確認し、最後にcheck/test全体を通す。

| 失敗したタスク | 再実行と対処 |
| --- | --- |
| `setup` | `mise run setup`。ネットワーク・固定版の入手可否を確認。manifestの意図した変更なら対応lockを更新する。locked/frozen指定を外して回避しない |
| `check:format` / `check:lint` | `mise run check:format` / `mise run check:lint`。整形は `mise run format`、lintは診断箇所を修正 |
| `check:types` | `mise run check:types`。Python・TypeScriptの型診断を修正 |
| `check:contracts` / `check:compatibility` | 同名の `mise run` タスク。validator診断を修正し、破壊的変更は契約の移行方針を確認 |
| `check:client` | `mise run generate-client`、差分レビュー、`mise run check:client`。固定生成器の出力をcommitへ含める |
| `check:docs` | `mise run check:docs`。表示された文書・行・リンク先を修正。Git管理対象と未追跡の `.md` を含め、Gitのignore対象・依存物は除く。削除済み入力は飛ばすが、その文書へのリンクは失敗する |
| `check:workflow` | `mise run check:workflow`。固定actionlintでYAML・Actions式・job参照を修正。任意導入のShellCheck/Pyflakesは無効にして環境差を避ける |
| `check:tickets` / `test` | 同名の `mise run` タスク。既存validatorまたは失敗テストの診断を確認する |

リンクのパース・見出しID解決はlycheeに任せる。同一文書と別文書の見出し、日本語見出しを検査し、HTTP等の外部リンクは `--offline` で接続せず除外する。外部サイトの存続はこの検査の対象外。リンク負例の回帰は [test_pr_checks.mjs](../tests/test_pr_checks.mjs) が未追跡の一時文書を作成し、相対パス欠落、別文書と同一文書の見出し欠落を失敗させ、修正後の成功と後片付けまで確認する。

試験PR相当のローカル確認では、下表の変更を一件ずつ入れて該当タスクの非ゼロ終了を確認し、その変更だけを取り消して同じタスクの成功を確認する。作業中の別変更を `git restore` 等で消さない。

| 負例 | 変更と確認コマンド |
| --- | --- |
| Python型 | `services/api/src/brainoid_api/ci_type_probe.py` に `ci_type_probe: str = 1` を置き `mise run check:types`。確認後はその一時ファイルを削除 |
| TypeScript型 | `apps/mobile/src/ci_type_probe.ts` に `export const ciTypeProbe: string = 1;` を置き `mise run check:types`。確認後はその一時ファイルを削除 |
| 生成差分 | `packages/api-client/src/generated/index.js` へ一時コメントを追加して `mise run check:client`。コメントだけを取り消す |
| 相対リンク | `docs/ci-link-probe.md` に `[probe](missing-ci-target.md)` を置き `mise run check:docs`。確認後はその一時ファイルを削除 |
| 見出しリンク | 同じ一時文書に `[probe](05-development-and-testing.md#missing-ci-heading)` を置き `mise run check:docs`。確認後はその一時ファイルを削除 |

workflowのtoken権限は `contents: read` のみ。forkでも実行する `pull_request` を使い、checkout後にcredentialを残さず、tool導入用tokenも後続stepへ永続化しない。deploy credential、repository secret、OIDC権限、environmentは設定しない。cacheの読書きも無効。setupの公式ツール・依存取得を除き、通常のcheck/testはfakeとローカルファイルを使い、cloudの書込・有料provider・emulator起動・署名build・deployを呼ばない。

GitHub上でのActions有効化、fork実行の承認方針、必須checkとbranch保護はBA-GOV-01で実repoへ設定する外部前提。ローカルの合格は実GitHub上の実行や保護設定の完了を意味しない。署名build・deployはBA-OPS-05・09・10で追加する。

## 6. モックとfixture

BA-FND-03の共通型は [contracts.md](contracts.md) にOpenAPI/JSON Schemaとの対応を記録する。`tests/test_contracts.py` はID・UTC日時・IANA名・version・scopeの境界、Contextの形と不変性を検証し、`mise run test` に含まれる。認可済みContextの生成前に必要な会員権限照合は [共通認可](authorization.md) の責務である。BA-IAM-02は現在のmembership/device/sessionと正本resourceをtransaction内で再検査し、表形式unitと実Firestore emulatorで検証する。

BA-FND-07の [AgentCoreのportとfake](agent-core-contracts.md) はprovider非依存のPython型、論理時計、モデル・記憶・tool・音声のscripted fakeを提供する。`tests/test_agent_core.py` は差し替えたfakeで通常完了・tool要求・usage・取消・timeout・途中停止と呼出し履歴を検証する。権限外候補は故障fixtureとしてのみ再現し、認可や会話ループの実装・検証は後続チケットで行う。

BA-RUN-03 の[文字 Core と人格 API 試験](text-agent-core.md)は同じ agent/version のスマホ・Pi 相当入力、現在認可と出典版、権限外記憶候補のモデル入力前拒否、時間・出力予算、署名保持と取消を fake で検査する。agent の競合更新と冪等再送は Firestore emulator でも検査する。

BA-RUN-01 の[永続job試験](jobs.md)は固定時計・fake queueでenqueue前後、leaseとcheckpoint、現在認可を検査し、Firestore emulatorでは別processの再起動後回収と同時leaseを確認する。業務handlerと本番Tasks配備は後続チケットで接続する。

BA-RUN-06 の[型付き tool 実行試験](tool-execution.md)は既存 schema 全体、型付き登録、現在認可、永続予算、重複実行、応答喪失と再照合、本文を含まない監査を fake で確認する。BA-RUN-07の[確認checkpoint試験](../tests/test_run_checkpoint.py)は取消、確認結果の重複、署名を保持した継続、session失効と期限回収を検査し、Firestoreでは別process再開と確認/取消競合を検査する。実業務handlerは後続チケットで接続する。

BA-IAM-07 の[端末ペアリング試験](device-pairing.md)は fake clock で5分/60秒/15分の境界、5回失敗、専用 token と秘密再送を確認し、Firestore emulatorで同時交換・同時rotation・commit応答喪失を検証する。APIの端末provider/keyring/epochは同文書に従って明示し、合成鍵をstaging/productionへ持ち込まない。

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

BA-FND-05の[固定fixture](../tests/fixtures/tenants/README.md)はtenant A/B、それぞれのowner/admin/member、private/shared会話・ノート、登録/失効Pi、agentを提供する。UID・UUID・アプリ時刻を固定し、本文は `FIXTURE_` 識別語だけで構成する。同名の別人・別室の照明や記憶の派生データは、対応する機能チケットで追加する。権限のない事実を検索候補に混ぜ、最終回答だけでなくLLM入力・SSE・TTS・ログにも漏れないことを後続の認可・機能テストで検証する。

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
| T-010 | FR-006/013 | 訂正とembedding/要約jobが同時に動く | stale versionを再保存せず、古い内容が検索に戻らない |
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
