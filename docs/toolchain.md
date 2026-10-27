# miseによる開発環境

更新日: 2026-10-26。ツール版の正本は [mise.toml](../mise.toml)、Python依存は [pyproject.toml](../pyproject.toml) と [uv.lock](../uv.lock)、Node依存は各 `package.json`・[pnpm-workspace.yaml](../pnpm-workspace.yaml) と [pnpm-lock.yaml](../pnpm-lock.yaml) とする。workspace・固定依存・検証環境、API基盤とPR検証workflowを提供する。業務API・画面は後続チケットで実装する。

## 1. miseの導入

Gitと `mise.toml` の `min_version` 以上のmiseを用意する。Linux・macOS向けの手順で、確認済み環境はLinux x86_64。WindowsはWSL2を使う。macOS・WSL2・Piの実行は未検証。

miseが未導入の場合は[公式インストール手順](https://mise.jdx.dev/getting-started.html)に従う。公式インストーラーを使う例:

~~~bash
curl -fsSL https://mise.run | sh
export PATH="$HOME/.local/bin:$PATH"
mise --version
~~~

`mise run` と `mise exec` はシェルの有効化なしで使える。対話シェルで `uv`・`node` 等を直接使う場合は、Bashなら `eval "$(~/.local/bin/mise activate bash)"`、Zshなら `eval "$(~/.local/bin/mise activate zsh)"` をシェル設定へ追加する。別の方法で導入した場合はmiseの実際のパスに読み替える。

## 2. 初回セットアップと検証

リポジトリのルートで設定内容を確認して実行する。`mise.toml` の変更後も再度trustが必要になることがある。

~~~bash
mise trust
mise install
mise run setup
mise run format
mise run check
mise run test
~~~

`setup` は最初に `mise install` で指定版のPython・Node・uv・pnpm・Java・oasdiff・lychee・actionlintを導入する。既に導入済みの版は再利用し、導入できなければmiseのエラーで停止する。その後、miseの実行環境で `uv sync --all-packages --locked` と `pnpm install --frozen-lockfile` を実行する。初回はネットワークとローカルディスク書込が必要で、秘密値・クラウド接続は不要。

`check` は整形、lint、Python/TypeScript型、チケット管理、契約validator、生成差分、HTTP契約の互換性、文書リンクとworkflowを検査する。`format` は手書きソースを整形し、`check:format` は書き換えない。Pythonの検証は `uv run --all-packages --locked --offline`、Nodeの検証はインストール済みツールを `pnpm run/exec` で実行する。キャッシュ・依存不足時は先にsetupを行う。API起動は [dev-apiの手順](05-development-and-testing.md#51-api基盤の起動)、clientは [契約検証の手順](contracts.md#httpクライアントと契約検証)、CIは [PR検証の手順](05-development-and-testing.md#53-pr検証と失敗の再現)を参照する。画面の起動はまだ提供しない。

uvが `.venv` を作成・同期する。miseは `UV_PYTHON` で固定Pythonのパスを渡し、uvのPython自動ダウンロードとmiseタスクの自動ツール導入は無効にする。固定ツールの導入は `setup` 内の明示的な `mise install` が担当する。IDEにも `.venv` を指定する。

~~~bash
mise ls --current
mise tasks ls
mise exec -- uv run --all-packages --locked --offline python --version
mise exec -- uv run --all-packages --locked --offline python -c 'import sys; print(sys.executable)'
mise exec -- node --version
mise exec -- uv --version
mise exec -- pnpm --version
~~~

通常の実行も `uv run --all-packages ...` を使う。サービス単独の依存だけが必要な場合は `uv run --package brainoid-api ...` のように指定する。単独の `mise exec -- python` にはworkspace依存は入らない。[uvのworkspaceと同期](https://docs.astral.sh/uv/concepts/projects/workspaces/)、[lockの仕様](https://docs.astral.sh/uv/concepts/projects/sync/)

## 3. workspaceと境界

| 領域 | 定義・入口 |
| --- | --- |
| Pythonルート | 非パッケージの開発用project。共通dev依存だけを持つ |
| `services/api`・`worker`・`device` | uv member。`src/brainoid_api`・`brainoid_worker`・`brainoid_device` |
| `packages/agent_core`・`memory`・`tools` | uv member。`src/brainoid_agent_core`・`brainoid_memory`・`brainoid_tools` の公開API |
| `packages/model_google` | Google Gen AI adapter の uv member。Core の依存に追加せず SDK と HTTP transport を隔離 |
| `packages/contracts` | uv member。`src/brainoid_contracts` の共通型。PydanticはAPIと同じ固定版を使用 |
| `apps/mobile`・`display` | 非公開pnpm member。`src/index.ts`。UI機能はBA-UI-01以降 |
| `packages/ui`・`api-client` | 非公開pnpm member。`exports` が `src/index.ts` だけを公開。api-clientは生成SDK・型と注入factoryを提供 |
| `packages/openapi-generator` | 開発専用pnpm member。生成器とそのcompiler API用TypeScriptを固定し、ルートdev依存から利用 |
| `infra/gcp` | Terraform用の領域。Python/Nodeのmemberではなく、配備定義はBA-OPS-01以降 |
| `tests` | package解決・秘密ファイル除外、API起動・health・エラーの受入テスト |

サービス間の内部importは置かず、共通実装は共有packageへ移す。Pythonは各memberの `dependencies` と `tool.uv.sources` の `workspace = true`、Nodeは `workspace:*` を使う。Pythonの公開名からimportし、別memberの `src` への相対パス・`sys.path` 変更・TypeScriptのpaths aliasでpackage境界を迂回しない。Pythonはuvが依存隔離を強制しないため、他memberの未宣言依存・内部moduleを使っていないこともレビューする。

Pythonの `brainoid-*`、Nodeの `@brainoid-internal/*` はローカル開発用識別子で、公開レジストリの所有権やOPEN-001の配布IDを確定しない。Pythonは `Private :: Do Not Upload`、Nodeは `private: true` とし、Nodeの `UNLICENSED` は公開許諾をまだ宣言しないことを示す。リポジトリの最終ライセンスはOPEN-004・BA-GOV-02で別途判断する。

秘密値の `.env`、鍵拡張子、既知のcredentialファイル名、`secrets/`、Terraform state等は `.gitignore` で除外する。任意名のJSON内容を自動判別するものではないので、ローカルの秘密ファイルは `secrets/` 配下に置く。`.env.example` は共有可能なfake値だけにする。`.venv`、`node_modules`、cache、build成果物は追跡しない。

## 4. Python依存の追加・削除・更新

以下はルートで、miseを有効化したシェルから実行する。未有効化なら `mise exec --` を前置する。

| 操作 | コマンド例 |
| --- | --- |
| 共通dev依存の追加・削除 | `uv add --dev <package>` / `uv remove --dev <package>` |
| member実行依存の追加・削除 | `uv add --package brainoid-api <package>` / `uv remove --package brainoid-api <package>` |
| member間の依存追加 | `uv add --package brainoid-api brainoid-memory`（workspaceとして解決されることを確認） |
| 意図した依存更新 | `uv lock --upgrade-package <package>` |
| 全member同期 | `uv sync --all-packages --locked` |
| Node共通dev依存 | `pnpm add -Dw --save-exact <package>` / `pnpm remove -Dw <package>` |
| Node実行依存 | `pnpm --filter @brainoid-internal/mobile add --save-exact <package>` |
| Node共有依存 | `pnpm --filter @brainoid-internal/mobile add '@brainoid-internal/ui@workspace:*'` |

Pythonの追加・削除はuvコマンドで行う。変更したmanifestとlockを同じコミットに含め、setup/check/testを実行する。lock更新のために通常setupの固定オプションを外さない。Python build backendは各memberで `uv_build` を完全固定し、同版のuvに内蔵されたbackendを使う。[uv build backend](https://docs.astral.sh/uv/concepts/build-backend/)

## 5. ツールの更新方法

1. 公式情報で対応Python/Node・peer依存・ライセンス・保守状況を確認し、miseの完全版を更新する。
2. `mise install` を実行する。Python対応範囲を変更する場合は全memberの `requires-python` とlockを更新する。uv変更時は各memberのbuild backendも確認する。
3. Node/pnpm変更時はルート `package.json` の `engines`・`packageManager` と `pnpm-workspace.yaml` の `nodeVersion` を同期する。設定間の版の一致は更新時に確認する。
4. 新規環境でsetup/format/check/testを実行し、通常導入でlockが変化しないことを確認する。

pnpm 11のstandalone版には独自のNodeが内蔵されている。アプリの互換性はmiseのNodeを基準にするため、`nodeVersion` と `engineStrict: true` で依存のengineを判定し、タスクのNodeはmiseの固定版を使用する。`pmOnFail: error` によりpnpmの暗黙の版変更・取得を停止する。`verifyDepsBeforeRun: error` はrun/exec中の暗黙installを防ぎ、依存不整合時はsetupを要求する。install lifecycle scriptは許可しない。Firebase CLI経由のRE2 native buildとprotobufjsのnotice scriptはAuth/Firestoreで不要なので `allowBuilds` にfalseを明示する。新しく必要になったscriptは追加時に確認する。[pnpm 11の設定変更](https://pnpm.io/blog/releases/11.0)、[engineとpackage manager設定](https://pnpm.io/settings)、[実行前の依存確認](https://github.com/pnpm/pnpm/blob/main/pnpm/docs/settings/build.md#verifydepsbeforerun)

Java・Firebase CLIはBA-FND-05で下記の通り導入する。Android SDK・Xcode・Docker・Terraform・Google Cloud CLIとネイティブCapacitor buildは、それぞれの実装チケットで対応版と導入方法を追加する。今回のNode依存固定だけではiOS・Android・Pi実機の対応を保証しない。

## 6. 採用依存と確認根拠

2026-10-26に公式PyPI/npm metadataの `Requires-Python`、`engines`、peer依存、licenseとリリース情報を確認した。下表の直接依存は開発基盤の初期採用値で、自動的に最新版へ追従しない。解決された推移依存・integrity/hashは両lockを正本とし、配布物全体のライセンス台帳はBA-GOV-02で整備する。

| 直接依存・採用版 | 用途・配置 | 互換性とライセンス（公式資料） |
| --- | --- | --- |
| Ruff 0.17.2 | ルートdev。Python format/lint | Python >=3.7、MIT。[PyPI](https://pypi.org/project/ruff/0.17.2/) |
| mypy 2.4.0 | ルートdev。Python strict型検査 | Python >=3.10、MIT。[PyPI](https://pypi.org/project/mypy/2.4.0/) |
| HTTPX2 2.16.0 | ルートdev。Starlette TestClientによるAPI・lifespan検証 | Python >=3.11、BSD-3-Clause。Pydanticが保守するstable版。[PyPI](https://pypi.org/project/httpx2/2.16.0/)、[公式source](https://github.com/pydantic/httpx2) |
| uv_build 0.12.19 | 各Python memberのbuild backend | 固定uvの内蔵backendと同版、MIT OR Apache-2.0。[PyPI](https://pypi.org/project/uv-build/0.12.19/) |
| FastAPI 0.162.0・Pydantic 2.14.1 | API memberの実行依存。Pydanticはcontractsでも使用 | Python >=3.10、MIT。HTTP入口・型付きvalidation用。contracts追加による実行権限の増加なし。[FastAPI](https://pypi.org/project/fastapi/0.162.0/)、[Pydantic](https://pypi.org/project/pydantic/2.14.1/) |
| Uvicorn 0.54.0 | API memberのASGI実行依存 | Python >=3.10、BSD-3-Clause。setupではサーバーを起動しない。[PyPI](https://pypi.org/project/uvicorn/0.54.0/) |
| google-cloud-firestore 2.34.1 | API memberの実行依存。冪等記録と業務writeの原子的transaction | Python >=3.10（3.13対応）、Apache-2.0、Google保守のProduction/Stable SDK。[PyPI](https://pypi.org/project/google-cloud-firestore/2.34.1/)、[現行source](https://github.com/googleapis/google-cloud-python/tree/main/packages/google-cloud-firestore) |
| google-cloud-storage 3.17.1 | API memberの実行依存。独立認可台帳の条件付き追記・読取 | Python >=3.10（3.13対応）、Apache-2.0、Google保守のProduction/Stable SDK。[PyPI](https://pypi.org/project/google-cloud-storage/3.17.1/)、[現行source](https://github.com/googleapis/google-cloud-python/tree/main/packages/google-cloud-storage) |
| firebase-admin 7.7.0 | API memberの実行依存。Firebase ID token署名・project・失効・ユーザー状態の検証 | Python >=3.9（3.13対応）、Apache-2.0、Firebase保守のProduction/Stable SDK。2026-09-23リリース。[PyPI](https://pypi.org/project/firebase-admin/7.7.0/)、[固定source](https://github.com/firebase/firebase-admin-python/tree/v7.7.0) |
| TypeScript 7.0.2 | ルートdev。全Node memberのstrict型検査 | Node >=16.20.0、Apache-2.0。[npm](https://www.npmjs.com/package/typescript/v/7.0.2) |
| Biome 2.6.3 | ルートdev。TypeScript/JSON format/lint | Node >=14.21.3、MIT OR Apache-2.0。[npm](https://www.npmjs.com/package/@biomejs/biome/v/2.6.3) |
| Vite 8.3.5 | ルートdev。後続UI用buildツール | Node ^20.19.0 または >=22.12.0、MIT。[npm](https://www.npmjs.com/package/vite/v/8.3.5)、[公式ガイド](https://vite.dev/guide/) |
| React・React DOM 19.3.1 | 両appの実行依存、UIのReact peer | React DOMのpeer ^19.3.1に一致、MIT。[React](https://www.npmjs.com/package/react/v/19.3.1)、[React DOM](https://www.npmjs.com/package/react-dom/v/19.3.1) |
| @types/react・@types/react-dom 19.3.0 | ルートdev。Reactの型 | React 19系、相互peer ^19.3.0に一致、MIT。[React型](https://www.npmjs.com/package/@types/react/v/19.3.0)、[DOM型](https://www.npmjs.com/package/@types/react-dom/v/19.3.0) |
| @capacitor/core 8.5.3 | mobileの実行依存 | Node engine制約の宣言なし。今回のNodeでimport確認、MIT。native platform/CLIは後続。[npm](https://www.npmjs.com/package/@capacitor/core/v/8.5.3) |

公式metadata上、Python側の上表の開発ツール・API依存は2026年9〜10月にもリリースされている。Node側も各公式配布元の採用版を固定し、現在のtoolchainで型検査とimportを実行する。これらは通常のローカルコード実行権限を持ち、setupではダウンロード・環境作成を行う。クラウドIAM・認証情報・外部サービスへの実行権限は追加しない。アプリの配布に含まれる依存と、開発時だけ使う依存を上表の配置で区別する。

BA-FND-02で追加したHTTPX2はテスト専用で、APIの実行依存へは追加しない。lockには推移依存のHTTPcore2（BSD-3-Clause）とtruststore（MIT）も追加される。2026-10-26に導入したdistribution metadataでPython要件・保守元・ライセンスを確認した。TestClientはアプリ内ASGI transportを使い、通常テストに外部HTTP接続やクラウド権限を追加しない。

BA-FND-08で追加したFirestore SDKはAPIの配布依存となり、gRPC、google-auth、protobuf等の推移依存もuv.lockで固定する。2026-10-26に公式PyPIと導入済みdistribution metadataで版・Python要件・Apache-2.0を確認した。以前の単独repositoryはarchive済みで、保守先は表の `google-cloud-python` 配下へ移管されている。SDKは構成済みのDB clientを注入された場合にその接続先へ読み書きするが、導入だけでcloud IAM権限を付与しない。通常check/testでは接続せず、明示的なemulator testは固定loopbackへ限定する。credential、SDKのpayload debugログ、外部providerの実行を共通処理から有効にしない。配布時はSDKと推移依存のlicense表示も保持する。利用境界と検証は[共通処理](common-mutations.md)を参照する。

BA-IAM-05は既に Firebase Admin の推移依存にある Storage SDK 3.17.1 を `uv add --package brainoid-api google-cloud-storage==3.17.1` で直接依存へ固定した。新しい版・推移依存の追加はない。2026-10-26に公式配布元・導入済み metadata と source で保守先、Apache-2.0、Python 要件を確認した。明示注入した bucket にだけ object create/read/list を行い、bucket 作成・IAM 付与・credential 検索をしない。GCS 書込は[公式 SDK](https://docs.cloud.google.com/python/docs/reference/storage/latest/google.cloud.storage.blob.Blob)の generation 条件と条件付き retry を使う。配布時は既存の SDK/推移依存の license 表示を保持する。[台帳の利用契約](control-ledger.md)に実 bucket と後続 API 接続の境界を記録する。

BA-RUN-01 は `brainoid-jobs` / `brainoid-persistence` のworkspace memberを追加した。外部distribution・版の追加はなく、workerが既存固定 `google-auth[requests]==2.62.0`、FastAPI、Pydanticを直接宣言し、persistenceが固定Firestore SDKを利用する。用途・Apache-2.0を含むライセンス・保守元・外部接続権限は[永続job契約](jobs.md)に記録する。

BA-IAM-07 は既存推移依存の `cryptography==50.0.2` と `pyjwt==2.15.1` を `uv add --package brainoid-api` で直接依存にした。新しい推移依存・版の追加はなく、専用RS256署名とAES-GCM receiptに使う。両者の公式API資料、実行権限・配布上の境界は[端末資格情報契約](device-pairing.md)に記録する。

BA-IAM-01のFirebase Admin SDKは `uv add --package brainoid-api firebase-admin==7.7.0` で導入し、推移依存もuv.lockへ固定する。2026-10-26に公式PyPIと導入済みsourceで互換性・ライセンス・token検証経路を確認した。API配布にHTTPX、PyJWT、CacheControl、Storage SDK等が加わるため、それぞれのlicense表示も保持する。Storage・Messaging等の機能は呼ばず、今回の実行経路はAuth token検証とユーザー照会のみ。cloud IAM権限を付与せず、新しいcredentialの作成や書込も行わない。通常テストは一時RSA鍵とネットワーク代替、emulatorテストは固定loopbackを使う。[本人認証の利用契約](authentication.md)に環境境界・追加ユーザー照会・未実施の本番確認を記録する。

BA-FND-04では次の既存ツールを2026-10-26の公式docs・npm/PyPI metadata・release assetで確認し固定した。

| 採用依存・版 | 理由・互換性・ライセンス |
| --- | --- |
| @redocly/cli 2.71.0 | OpenAPI 3.1の標準構造とoperationId/path/security/参照を既存rulesetで検査。Node >=22.12または20.19系、MIT。継続リリースされる公式CLI。[npm](https://www.npmjs.com/package/@redocly/cli/v/2.71.0)、[rules](https://redocly.com/docs/cli/rules/built-in-rules) |
| openapi-spec-validator 0.9.0 | ルートdev。OpenAPI内Schema Objectの2020-12メタスキーマ適合を既存CLIの `check_schema` で検証。Python >=3.10,<4.0、Apache-2.0。3.1のdialect/メタスキーマ対応を含む現行版をuvで固定し、backendはjsonschemaへ明示固定する。[PyPI](https://pypi.org/project/openapi-spec-validator/0.9.0/)、[CLI](https://openapi-spec-validator.readthedocs.io/en/latest/cli.html) |
| ajv-cli 5.0.0・ajv-formats 3.0.1 | Draft 2020-12メタスキーマと参照をcompileし、標準formatも検査。MIT。CLIのリリース間隔は長いが、lockのAjv 8.20.0を利用する安定したコマンド入口。[CLI](https://ajv.js.org/packages/ajv-cli)、[formats](https://ajv.js.org/packages/ajv-formats.html) |
| @hey-api/openapi-ts 0.99.0 | 3.1からSDK・要求/応答型とfetch runtimeを生成。Node >=22.18、MIT。公式の現行plugin構成を固定。[npm](https://www.npmjs.com/package/@hey-api/openapi-ts/v/0.99.0)、[fetch](https://heyapi.dev/docs/openapi/typescript/clients/fetch) |
| eventsource-parser 4.1.2 | api-clientの実行依存。SSEのCRLF/UTF-8分割・複数data行を既存parserで処理し、event/id/retryを保持。Node >=22.12、MIT。公式実装のchunk境界処理と現行版のlicenseを確認し、pnpm memberへ完全固定。[npm](https://www.npmjs.com/package/eventsource-parser/v/4.1.2)、[公式source](https://github.com/rexxars/eventsource-parser) |
| TypeScript 5.9.3（生成器内部のみ） | compiler APIを必要とする生成器と同じ開発専用memberに完全版を宣言し、peerを解決する。Node >=14.17、Apache-2.0。workspace compilerは既存の7.0.2を維持。[npm](https://www.npmjs.com/package/typescript/v/5.9.3)、[pnpmのpeer解決](https://github.com/pnpm/pnpm/blob/main/pnpm/docs/settings/peer-dependencies.md#resolvepeersfromworkspaceroot) |
| yaml 2.9.1 | 契約負例fixtureでYAMLを解析するテストdev依存。独自文書validatorには使わない。Node >=14.6、ISC。[npm](https://www.npmjs.com/package/yaml/v/2.9.1) |
| oasdiff 1.34.1 | OpenAPI 3.1/2020-12対応の破壊的HTTP契約差分を検出する既存CLI。Apache-2.0。miseのGitHub backendで公式tar archiveを導入し、Linux x64/arm64とmacOS universalのasset名・SHA256を固定。[release](https://github.com/oasdiff/oasdiff/releases/tag/v1.34.1)、[breaking](https://github.com/oasdiff/oasdiff/blob/v1.34.1/docs/BREAKING-CHANGES.md) |

各ツールはローカルで契約・Git履歴・一時生成物を読み書きする開発権限だけを使う。setupは公式配布元から取得するが、check/testはcredentialや外部APIを必要としない。Redocly telemetryは無効、oasdiffのアップロード機能は使用しない。Nodeのinstall lifecycle script許可は追加しない。アプリには生成されたfetch runtime・型とSSE parserが含まれ、開発ツール自体は配布しない。parserは受信テキストを処理するだけで、独自の外部接続・認証・実行権限を追加しない。MIT表示はapi-clientの [Hey API](../packages/api-client/LICENSE.hey-api)・[eventsource-parser](../packages/api-client/LICENSE.eventsource-parser) に同梱する。oasdiffの確認環境はLinux x64で、他platformの実行は未確認。

openapi-spec-validatorの追加は開発環境だけに適用し、APIサービスの実行依存には加えない。JSON Schemaの規則は推移依存のopenapi-schema-validator/jsonschemaが担当する。独自にSchema Objectを列挙したり、個別keywordを判定するコードは追加しない。メタスキーマは依存packageに同梱されたものを使い、検証時の追加ダウンロードやクラウド権限は不要。

sandboxがNodeの子process出力captureを制限すると契約負例テストは `spawnSync EPERM` になる。既存API TestClientの通知制限と同様に、通常のローカル実行環境で `mise run test` を行う。検証処理にクラウド接続や権限付与は不要。

BA-FND-05では2026-10-26に公式配布元で次の版・互換性・ライセンスを確認した。

| 採用依存・版 | 理由・互換性・ライセンス |
| --- | --- |
| Firebase CLI (`firebase-tools`) 15.34.1 | ルートdev依存としてpnpmで固定。Node >=20（固定Node 24対応）、MIT。Firebase公式が継続保守し、Auth emulatorとFirestore lifecycleを提供。[npm metadata](https://registry.npmjs.org/firebase-tools/15.34.1)、[公式source](https://github.com/firebase/firebase-tools/tree/v15.34.1) |
| Eclipse Temurin 21.0.13+9 LTS | Firestore emulatorのJava runtime。miseでdistributionを含む完全版を固定。GPL-2.0 with Classpath Exception。Adoptiumが継続提供するOpenJDK build。[公式FAQ](https://adoptium.net/docs/faq/)、[Emulator導入要件](https://firebase.google.com/docs/emulator-suite/install_and_configure) |

Firebase CLIに同梱された[emulator metadata](https://github.com/firebase/firebase-tools/blob/v15.34.1/src/emulator/downloadableEmulatorInfo.json)がFirestore JARの版とchecksumを固定する。JARは初回の明示的な `dev-emulator` / `test-emulator` で公式配布元から取得し、Gitやアプリ配布物に含めない。Auth/Firestore以外のサービス・デプロイ・Firebase loginはこのタスクから呼ばない。CLIの本来のcloud操作機能は開発依存に含まれるが、今回の実行経路は固定demo projectのloopback起動に限定する。Java/CLI/JARは開発専用で、mobileやPiの配布依存へは追加しない。

固定CLIで使わないHosting用RE2のnative installとprotobufjsのinstall noticeは、pnpmの `allowBuilds: false` で明示的に実行を拒否する。通常のfrozen installで承認待ちにせず、Auth/Firestoreの起動・seed・resetを実測する。実行手順は[開発ガイド5.2節](05-development-and-testing.md#52-authfirestore-emulatorの起動と初期化)。通常のcheck/testにJAR取得やlocalhost server起動を隠さない。

BA-FND-06では2026-10-26に公式release metadata・source・licenseで次の版を確認した。ツールはmiseのGitHub backendでLinux x64/arm64・macOS x64/arm64の公式archive名とSHA256を固定する。実測した環境はLinux x64。Node・Python packageの追加はない。

| 採用依存・版 | 用途・互換性・ライセンス |
| --- | --- |
| lychee 0.24.2 | Markdownの相対パス・見出し検証。開発専用のstandalone binary、MIT OR Apache-2.0。継続保守され、オフライン検査と日本語見出しを利用。[release](https://github.com/lycheeverse/lychee/releases/tag/lychee-v0.24.2)、[CLI・license](https://github.com/lycheeverse/lychee/blob/lychee-v0.24.2/README.md) |
| actionlint 1.7.12 | Actions構文・式の検証。開発専用のstandalone binary、MIT。ShellCheck/Pyflakes連携は無効にして、ローカルとCIの追加ツール有無に依存させない。[release](https://github.com/rhysd/actionlint/releases/tag/v1.7.12)、[usage](https://github.com/rhysd/actionlint/blob/v1.7.12/docs/usage.md)、[license](https://github.com/rhysd/actionlint/blob/v1.7.12/LICENSE.txt) |
| actions/checkout 7.0.1 | workflow内だけで使用。Node 24 runtime、MIT。完全commit SHAへ固定し、互換性比較に必要な全履歴を取得、credential永続化を無効化。[source・license](https://github.com/actions/checkout/tree/v7.0.1)、[inputs](https://github.com/actions/checkout/blob/v7.0.1/action.yml) |
| jdx/mise-action 5.2.3 | workflow内だけで使用。Node 24 runtime、MIT。完全commit SHAへ固定し、mise本体もworkflowの完全版で固定。cacheとtoken永続化は無効。[source・license](https://github.com/jdx/mise-action/tree/v5.2.3)、[inputs](https://github.com/jdx/mise-action/blob/v5.2.3/action.yml) |

両ActionsはUbuntu 24.04のGitHub hosted runnerで動かす定義とし、self-hosted runnerは使わない。checkoutとツール導入に必要な自動tokenは `contents: read` だけに限定する。mise・lychee・actionlintは開発環境のファイルを読む権限を使い、アプリ配布物へ含めない。lycheeは `--offline` でネットワーク要求を禁止し、文書中の外部リンクへ接続しない。通常テスト・検証にクラウドIAM・secret・有料providerの権限は追加しない。更新時はこの確認と[PR検証の負例](05-development-and-testing.md#53-pr検証と失敗の再現)を再実行する。

BA-RUN-02 は Core 最小 install に SDK を加えないため `brainoid-model-google` を独立 workspace member とした。2026-10-26 に公式 PyPI JSON metadata と導入済み source/metadata を確認し、`uv add --package brainoid-model-google` で以下を完全固定した。SDK は Google が継続保守する公式 Gen AI Python library。[公式ガイド](https://googleapis.github.io/python-genai/)、[2.33.0 source](https://github.com/googleapis/python-genai/tree/v2.33.0)。

| 直接依存・採用版 | 目的・互換性・ライセンス |
| --- | --- |
| google-genai 2.33.0 | Vertex の generateContent 非同期 stream。Python >=3.10、3.13 対応、Apache-2.0。[公式 metadata](https://pypi.org/pypi/google-genai/2.33.0/json) |
| google-auth 2.62.0 | 呼出元が事前更新した credential から token snapshot を作成。既存固定版、Python >=3.10、Apache-2.0。[PyPI](https://pypi.org/project/google-auth/2.62.0/) |
| HTTPX 0.28.1 | SDK の明示 HTTP transport、fake 注入、接続解放。既存固定版、Python >=3.8、BSD-3-Clause。[PyPI](https://pypi.org/project/httpx/0.28.1/) |
| jsonschema 4.26.0 / referencing 0.37.0 | 構造化応答の検証と外部参照取得を禁止する registry。既存固定版、Python >=3.10、各 MIT。[jsonschema](https://pypi.org/project/jsonschema/4.26.0/)、[referencing](https://pypi.org/project/referencing/0.37.0/) |
| Pydantic 2.14.1 | 型付き設定と共通 JSON 検証。既存固定版、MIT |

新しい推移依存は distro 1.9.0（Apache-2.0）、sniffio 1.3.1（MIT OR Apache-2.0）、tenacity 9.1.4（Apache-2.0）、websockets 17.3（BSD-3-Clause）。SDK とこれらは adapter を配布する環境の実行依存で、license 表示と lock の hash を保持する。従来 dev 検証だけで使った JSON Schema 関連もこの member の配布依存になる。既存依存版の更新は行っていない。ライセンス値と Python 要件は各導入済み distribution metadata でも確認した。

導入による IAM 付与・credential 作成・認証探索はない。モデル要求だけが明示した Vertex endpoint へ HTTPS 接続でき、外部 tool・音声・ファイル API・WebSocket API は呼ばない。SDK の HTTP retry、automatic function calling、automatic continuation、record/replay を明示無効化し、認証 refresh は構成側の事前更新に分離する。通常 check/test は HTTP fake だけを使う。実 provider の課金、モデル本文、署名、credential をログへ出さない。[adapter 契約と上限付き疎通手順](google-model-provider.md)に機能範囲、実疎通未実施理由、環境設定・後続会話層への引継ぎを記録する。

BA-DEV-01 は device member に `uv add --package brainoid-device` で `starlette==1.10.0`、`uvicorn==0.54.0`、workspace `brainoid-contracts` を直接宣言した。既存 lock の版を再利用し、API member や Google/Firebase SDK を device へ依存させない。Starlette は route/response/Cookie/lifespan、Uvicorn は HTTP parser と単一 listener の寿命を担う。2026-10-27 に固定版の導入済み source/METADATA と [Starlette 公式資料](https://starlette.dev/lifespan/)・[Uvicorn 公式資料](https://uvicorn.dev/settings/)を確認した。両者は継続保守される BSD-3-Clause、Starlette は Python >=3.11、Uvicorn は >=3.10 で固定 Python 3.13 と適合する。共通型は UUID と UTC 時刻を再利用する。

device の配布にはこれらと既存 lock の anyio/h11/click、contracts の Pydantic 等の license notice を含める。外部 package の版追加・更新はなく、device の依存閉包が増える。追加の実行権限は指定 loopback TCP listener、built UI の読取、private credential directory の読書き、所有 child process の生成/終了だけ。cloud credential・IAM・特権 port・browser への秘密配布は不要。[サービス契約と検証](device-service.md)に実行境界、session、格納先と未実施の実機確認を記録する。

BA-DEV-02はPython標準の `sqlite3` / `fcntl` を使用し、新しいpackageやlock更新はない。SQLiteのtransaction・migration・command台帳と、Linux directory flockによる保存領域の単独所有を担う。実行権限はサービスUIDが所有する別の0700 state directoryと既存credential directoryの読書き・fsync/unlinkに限定する。Python同梱SQLiteの配布条件を既存Python runtimeのnoticeと併せて保持する。[保存層契約](device-storage.md)にSQLite設定、reset、復旧と実機未検証の条件を記録する。

BA-DEV-03のAPI実行依存は既存固定版 `anyio==4.15.1`（MIT、request cancel scopeに対するcleanupの保護）、`jsonschema==4.26.0`（MIT）と `websockets==17.3`（BSD-3-Clause）を直接宣言する。前者は同梱WSS schema検証、後者はUvicornの専用transportに使う。推移依存版の変更や外部権限の追加はない。[接続・ログ契約](device-gateway.md)を参照する。
