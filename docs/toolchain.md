# miseによる開発環境

更新日: 2026-10-26。ツールのバージョンの正本はルートの [mise.toml](../mise.toml)、Python依存の正本は [pyproject.toml](../pyproject.toml)・[uv.lock](../uv.lock) とする。現在は文書・通信スキーマ・チケット管理スクリプトの検証環境を提供する。アプリ用workspace・依存・CIはBA-FND-01以降で追加する。

## 1. miseの導入

Gitと、`mise.toml` の `min_version` 以上のmiseを用意する。以下はLinux・macOS向けの手順で、動作確認環境はLinux x86_64。Windowsで同じ手順を使う場合はWSL2内で実行する。macOS・WSL2・Piでの実行は未検証。

miseが未導入の場合は[公式インストール手順](https://mise.jdx.dev/getting-started.html)に従う。公式インストーラーを使う例:

~~~bash
curl -fsSL https://mise.run | sh
export PATH="$HOME/.local/bin:$PATH"
mise --version
~~~

以降の `mise run` と `mise exec` はシェルの有効化なしで使える。対話シェルで `uv` や `node` を直接使う場合は、利用するシェルの設定ファイルに次の対応する1行を追加し、シェルを開き直す。

~~~bash
# Bash: ~/.bashrc
eval "$(~/.local/bin/mise activate bash)"
# Zsh: ~/.zshrc
eval "$(~/.local/bin/mise activate zsh)"
~~~

別の方法でmiseを導入した場合は、実際の実行ファイルのパスに読み替える。

## 2. 初回セットアップと検証

このリポジトリのルートで `mise.toml` のツール・環境変数・タスクを確認し、次を実行する。

~~~bash
mise trust
mise install
mise run setup
mise run check
mise run test
~~~

`mise install` は指定版のPython・Node.js・uv・pnpmを導入する。`setup` は `uv sync --locked` を実行し、uvが `.venv` を作成してlockfileどおりに依存を同期する。文書検証用のPyYAMLは `dependency-groups.dev` に含まれ、既定で導入される。初回のツール・依存ダウンロードにはネットワーク接続が必要。[uvのプロジェクト管理](https://docs.astral.sh/uv/guides/projects/)

`check` と `test` は `uv run --locked --offline` を通して実行する。uvはlockfileとの整合と環境の同期を確認し、必要ならローカルキャッシュから依存を導入する。ネットワークからの取得が必要な場合は失敗するため、先に `mise run setup` を実行する。`--locked` により、古いlockfileを検証時に黙って更新しない。miseのツール自動インストールも無効なので、ツール不足時は `mise install` を実行する。[lockと同期の仕様](https://docs.astral.sh/uv/concepts/projects/sync/)

miseは `UV_PYTHON` で固定版Pythonのパスをuvへ渡す。`.venv` の作成・同期・コマンド実行時の有効化はuvが担当する。従来のmise設定で作った `.venv` も、`mise run setup` でuvの管理へ移行できる。

~~~bash
mise ls --current
mise tasks ls
mise exec -- uv run --locked --offline python --version
mise exec -- uv run --locked --offline python -c 'import sys; print(sys.executable)'
mise exec -- node --version
mise exec -- uv --version
mise exec -- pnpm --version
~~~

`uv run` 内のPythonがリポジトリ内の `.venv` を指すことを確認する。IDEでもこの環境をPython interpreterに指定する。スクリプトは `uv run python ...`、miseのシェルを有効化していない場合は `mise exec -- uv run python ...` で実行する。単独の `mise exec -- python` はmiseが導入したPythonを使うため、プロジェクトの依存を使うコマンドには `uv run` を付ける。

## 3. 管理するもの

| 対象 | 正本・役割 |
| --- | --- |
| Python・Node.js | `mise.toml` の完全なバージョン指定。検証スクリプトと今後のAPI・UI開発に使う |
| uv・pnpm | `mise.toml` で固定するパッケージマネージャー。Pythonの導入元はmiseに統一する |
| Python依存・仮想環境 | `pyproject.toml` で依存と対応Python範囲を宣言し、`uv.lock` で解決済みの版を固定。`.venv` はuvが管理する |
| 文書検証の依存 | `dependency-groups.dev` にPyYAMLを登録。ルートは開発用の非パッケージプロジェクト（`tool.uv.package = false`）とする |
| 開発コマンド | `mise.toml` のタスク。現在の一覧と予定は[開発ガイド](05-development-and-testing.md#5-提供する開発コマンド)を参照 |
| ローカル専用ファイル | `.venv`、`node_modules`、`.env`、`mise.local.toml` 等は [.gitignore](../.gitignore) で除外。共有するツール版や検証コマンドは `mise.toml` へ記録する |

Java・Android SDK・Xcode・Capacitor・Docker・Terraform・Google Cloud CLI・Firebase CLIは、各コンポーネントの実装時に対応版と導入方法を追加する。miseで管理できるCLIは `mise.toml` へ追加し、OS・IDE・Docker daemon等の前提はこの文書へ記録する。現在の文書検証には不要。

## 4. Python依存の追加・削除・更新

miseを有効化したシェルで、通常のuvコマンドを使う。未有効化の場合は各コマンドの前に `mise exec --` を付ける。

| 操作 | コマンド例 |
| --- | --- |
| 開発用依存の追加 | `uv add --dev <package>` |
| 開発用依存の削除 | `uv remove --dev <package>` |
| 宣言を手で変更した後のlock更新 | `uv lock` |
| PyYAMLの更新 | `uv lock --upgrade-package pyyaml` |
| lockfileから環境を同期 | `uv sync --locked` |
| スクリプトの実行 | `uv run python scripts/validate_docs.py` |

`uv add`・`uv remove` は依存宣言・lockfile・環境を更新する。変更した `pyproject.toml` と `uv.lock` を同じコミットに含め、`.venv` はGitへ入れない。`uv lock` だけで更新した場合は `mise run setup` で環境を同期して検証する。後続のアプリ実装もこのプロジェクトを基にuv workspaceへ拡張する。

## 5. ツールの更新方法

1. 対象ツールの互換性を確認し、`mise.toml` の完全なバージョンを更新する。`latest` やmajorのみの指定は使わない。
2. `mise install` を実行する。Pythonの対応範囲を変える場合は `pyproject.toml` の `requires-python` と `uv.lock` も更新する。
3. `mise run setup`、`mise run check`、`mise run test` を実行する。uvがmiseの指定Pythonを使って環境を同期する。
4. ツール版、検証結果、対応OS・コンポーネントへの影響を変更に記録する。CIを導入する際も同じ `mise.toml` とタスクを使う。

Node.jsとpnpmの互換性、Pythonとライブラリの対応をセットで確認する。アプリ・モバイル・実機の対応確認は各実装チケットの受入条件に従う。今回の設定追加だけではBA-FND-01全体の完了とはしない。
