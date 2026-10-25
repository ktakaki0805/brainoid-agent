# miseによる開発環境

更新日: 2026-10-26。ツール版の正本は [mise.toml](../mise.toml)、Python依存は [pyproject.toml](../pyproject.toml) と [uv.lock](../uv.lock)、Node依存は各 `package.json`・[pnpm-workspace.yaml](../pnpm-workspace.yaml) と [pnpm-lock.yaml](../pnpm-lock.yaml) とする。workspace・固定依存・検証環境とAPI起動・health・共通エラーを提供する。業務API・画面・CIは後続チケットで実装する。

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

`setup` は最初に `mise install` で指定版のPython・Node・uv・pnpmを導入する。既に導入済みの版は再利用し、導入できなければmiseのエラーで停止する。その後、miseの実行環境で `uv sync --all-packages --locked` と `pnpm install --frozen-lockfile` を実行する。初回はネットワークとローカルディスク書込が必要で、秘密値・クラウド接続は不要。

`check` は整形、lint、Python/TypeScript型、チケット管理を検査する。`format` はソースを整形し、`check:format` は書き換えない。Pythonの検証は `uv run --all-packages --locked --offline`、Nodeの検証はインストール済みツールを `pnpm run` で実行する。キャッシュ・依存不足時は先にsetupを行う。API起動は [dev-apiの手順](05-development-and-testing.md#51-api基盤の起動)を参照する。画面の起動と契約validatorはまだ提供しない。

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
| `apps/mobile`・`display` | 非公開pnpm member。`src/index.ts`。UI機能はBA-UI-01以降 |
| `packages/ui`・`api-client` | 非公開pnpm member。`exports` が `src/index.ts` だけを公開。client生成はBA-FND-04 |
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

pnpm 11のstandalone版には独自のNodeが内蔵されている。アプリの互換性はmiseのNodeを基準にするため、`nodeVersion` と `engineStrict: true` で依存のengineを判定し、タスクのNodeはmiseの固定版を使用する。`pmOnFail: error` によりpnpmの暗黙の版変更・取得を停止する。今回の依存にはinstall lifecycle scriptが不要なので `allowBuilds: {}` とし、新しく必要になったscriptは追加時に確認する。[pnpm 11の設定変更](https://pnpm.io/blog/releases/11.0)、[engineとpackage manager設定](https://pnpm.io/settings)

Java・Android SDK・Xcode・Docker・Terraform・Google Cloud CLI・Firebase CLIとネイティブCapacitor buildは、それぞれの実装チケットで対応版と導入方法を追加する。今回のNode依存固定だけではiOS・Android・Pi実機の対応を保証しない。

## 6. 採用依存と確認根拠

2026-10-26に公式PyPI/npm metadataの `Requires-Python`、`engines`、peer依存、licenseとリリース情報を確認した。下表の直接依存は開発基盤の初期採用値で、自動的に最新版へ追従しない。解決された推移依存・integrity/hashは両lockを正本とし、配布物全体のライセンス台帳はBA-GOV-02で整備する。

| 直接依存・採用版 | 用途・配置 | 互換性とライセンス（公式資料） |
| --- | --- | --- |
| Ruff 0.17.2 | ルートdev。Python format/lint | Python >=3.7、MIT。[PyPI](https://pypi.org/project/ruff/0.17.2/) |
| mypy 2.4.0 | ルートdev。Python strict型検査 | Python >=3.10、MIT。[PyPI](https://pypi.org/project/mypy/2.4.0/) |
| HTTPX2 2.16.0 | ルートdev。Starlette TestClientによるAPI・lifespan検証 | Python >=3.11、BSD-3-Clause。Pydanticが保守するstable版。[PyPI](https://pypi.org/project/httpx2/2.16.0/)、[公式source](https://github.com/pydantic/httpx2) |
| uv_build 0.12.19 | 各Python memberのbuild backend | 固定uvの内蔵backendと同版、MIT OR Apache-2.0。[PyPI](https://pypi.org/project/uv-build/0.12.19/) |
| FastAPI 0.162.0・Pydantic 2.14.1 | API memberの実行依存 | Python >=3.10、MIT。HTTP入口・型付きvalidation用。[FastAPI](https://pypi.org/project/fastapi/0.162.0/)、[Pydantic](https://pypi.org/project/pydantic/2.14.1/) |
| Uvicorn 0.54.0 | API memberのASGI実行依存 | Python >=3.10、BSD-3-Clause。setupではサーバーを起動しない。[PyPI](https://pypi.org/project/uvicorn/0.54.0/) |
| TypeScript 7.0.2 | ルートdev。全Node memberのstrict型検査 | Node >=16.20.0、Apache-2.0。[npm](https://www.npmjs.com/package/typescript/v/7.0.2) |
| Biome 2.6.3 | ルートdev。TypeScript/JSON format/lint | Node >=14.21.3、MIT OR Apache-2.0。[npm](https://www.npmjs.com/package/@biomejs/biome/v/2.6.3) |
| Vite 8.3.5 | ルートdev。後続UI用buildツール | Node ^20.19.0 または >=22.12.0、MIT。[npm](https://www.npmjs.com/package/vite/v/8.3.5)、[公式ガイド](https://vite.dev/guide/) |
| React・React DOM 19.3.1 | 両appの実行依存、UIのReact peer | React DOMのpeer ^19.3.1に一致、MIT。[React](https://www.npmjs.com/package/react/v/19.3.1)、[React DOM](https://www.npmjs.com/package/react-dom/v/19.3.1) |
| @types/react・@types/react-dom 19.3.0 | ルートdev。Reactの型 | React 19系、相互peer ^19.3.0に一致、MIT。[React型](https://www.npmjs.com/package/@types/react/v/19.3.0)、[DOM型](https://www.npmjs.com/package/@types/react-dom/v/19.3.0) |
| @capacitor/core 8.5.3 | mobileの実行依存 | Node engine制約の宣言なし。今回のNodeでimport確認、MIT。native platform/CLIは後続。[npm](https://www.npmjs.com/package/@capacitor/core/v/8.5.3) |

公式metadata上、Python側の上表の開発ツール・API依存は2026年9〜10月にもリリースされている。Node側も各公式配布元の採用版を固定し、現在のtoolchainで型検査とimportを実行する。これらは通常のローカルコード実行権限を持ち、setupではダウンロード・環境作成を行う。クラウドIAM・認証情報・外部サービスへの実行権限は追加しない。アプリの配布に含まれる依存と、開発時だけ使う依存を上表の配置で区別する。

BA-FND-02で追加したHTTPX2はテスト専用で、APIの実行依存へは追加しない。lockには推移依存のHTTPcore2（BSD-3-Clause）とtruststore（MIT）も追加される。2026-10-26に導入したdistribution metadataでPython要件・保守元・ライセンスを確認した。TestClientはアプリ内ASGI transportを使い、通常テストに外部HTTP接続やクラウド権限を追加しない。
