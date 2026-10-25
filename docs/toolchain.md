# miseによる開発環境

更新日: 2026-10-26。ツールのバージョンの正本はルートの [mise.toml](../mise.toml) とする。現在は文書・通信スキーマ・チケット管理スクリプトの検証環境を提供する。アプリ用workspace・依存lock・CIはBA-FND-01以降で追加する。

## 1. miseの導入

Gitと、`mise.toml` の `min_version` 以上のmiseを用意する。以下はLinux・macOS向けの手順で、動作確認環境はLinux x86_64。Windowsで同じ手順を使う場合はWSL2内で実行する。macOS・WSL2・Piでの実行は未検証。

miseが未導入の場合は[公式インストール手順](https://mise.jdx.dev/getting-started.html)に従う。公式インストーラーを使う例:

~~~bash
curl -fsSL https://mise.run | sh
export PATH="$HOME/.local/bin:$PATH"
mise --version
~~~

以降の `mise run` と `mise exec` はシェルの有効化なしで使える。対話シェルで `python` や `node` を直接使う場合は、利用するシェルの設定ファイルに次の対応する1行を追加し、シェルを開き直す。

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

`mise install` は指定版のPython・Node.js・uv・pnpmを導入する。miseはプロジェクトの `.venv` を作成・有効化し、`setup` はそこへ文書検証用のPyYAMLを導入する。初回のツール・依存ダウンロードにはネットワーク接続が必要。システムのPythonへpip installする必要はない。[miseのvenv仕様](https://mise.jdx.dev/lang/python.html)

`check` と `test` はセットアップ後にオフラインで実行できる。タスク実行時のツール自動インストールは無効にしているため、ツールが不足する場合は先に `mise install` を実行する。PyYAMLが不足する場合は `mise run setup` を再実行する。検証タスクから依存導入は行わない。

~~~bash
mise ls --current
mise tasks ls
mise exec -- python --version
mise exec -- python -c 'import sys; print(sys.executable)'
mise exec -- node --version
mise exec -- uv --version
mise exec -- pnpm --version
~~~

Pythonの実行パスがリポジトリ内の `.venv` を指すことを確認する。IDEでもこの環境をPython interpreterに指定する。シェルを有効化していない場合、スクリプトは `mise exec -- python ...` で実行する。

## 3. 管理するもの

| 対象 | 正本・役割 |
| --- | --- |
| Python・Node.js | `mise.toml` の完全なバージョン指定。検証スクリプトと今後のAPI・UI開発に使う |
| uv・pnpm | `mise.toml` で固定するパッケージマネージャー。Pythonの導入元はmiseに統一する |
| 文書検証の依存 | [scripts/requirements-docs.txt](../scripts/requirements-docs.txt) でPyYAMLを完全指定。アプリの依存管理はworkspace実装時にuv.lock・pnpm-lock.yamlへ追加する |
| 開発コマンド | `mise.toml` のタスク。現在の一覧と予定は[開発ガイド](05-development-and-testing.md#5-提供する開発コマンド)を参照 |
| ローカル専用ファイル | `.venv`、`node_modules`、`.env`、`mise.local.toml` 等は [.gitignore](../.gitignore) で除外。共有するツール版や検証コマンドは `mise.toml` へ記録する |

Java・Android SDK・Xcode・Capacitor・Docker・Terraform・Google Cloud CLI・Firebase CLIは、各コンポーネントの実装時に対応版と導入方法を追加する。miseで管理できるCLIは `mise.toml` へ追加し、OS・IDE・Docker daemon等の前提はこの文書へ記録する。現在の文書検証には不要。

## 4. 更新方法

1. 対象ツールの互換性を確認し、`mise.toml` の完全なバージョンを更新する。`latest` やmajorのみの指定は使わない。
2. `mise install` を実行する。Pythonの版を変更した場合は既存の `.venv` をリポジトリ外へ退避し、次の手順で新しいPythonから作り直す。
3. `mise run setup`、`mise run check`、`mise run test` を実行する。依存を更新する場合はrequirements・今後のworkspace lockfileも同時に更新する。
4. ツール版、検証結果、対応OS・コンポーネントへの影響を変更に記録する。CIを導入する際も同じ `mise.toml` とタスクを使う。

Node.jsとpnpmの互換性、Pythonとライブラリの対応をセットで確認する。アプリ・モバイル・実機の対応確認は各実装チケットの受入条件に従う。今回の設定追加だけではBA-FND-01全体の完了とはしない。
