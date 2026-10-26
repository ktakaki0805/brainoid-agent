# 型付き tool の実行境界

BA-RUN-06 の [brainoid-tools](../packages/tools/src/brainoid_tools/__init__.py) は、モデルの tool 候補を検証し、サーバーが登録した型付き handler だけを実行する。文字 Core / 永続 run loop への接続、業務 handler、確認後の resume は後続チケット。現在の文字 run が tool を実行できるという意味ではない。

## スキーマと登録

[Registry](../packages/tools/src/brainoid_tools/registry.py) は [tool-contracts.schema.json](../specs/tool-contracts.schema.json) 全体を Draft 2020-12 で検証する。`name` / `arguments`、再帰的な未知 field、HomeAction の許可値・範囲、Calendar の各 action、private/display policy、UUID・日付・明示 offset 時刻・IANA timezone を検査する。JSON は最大64KiB、重複 key、非有限数、深すぎる構造を拒否する。Calendar confirm は正本 schema に存在せず登録もできない。

schema は package resource として同梱し、実行 cwd や repo の存在に依存しない。変更時は正本と同梱版を同時更新する。完全一致を unit test で強制する。実行依存に `jsonschema==4.26.0`（MIT、既存 workspace/lock で使用中の版）を宣言した。外部サービス権限は追加しない。時刻・timezone の形式検査は既存 contracts に委ね、任意 format 用 dependency の偶然の導入に依存しない。

登録には `Registration(name, arguments_type, handler, effect_kind)` を使う。`arguments_type` は `Arguments` を継承し、strict 型・未知 field 拒否を維持する。canonical schema 検査後にその業務型を構築する。handler は `Invocation[Arguments型]` を受け、必要な属性だけを業務 API へ写す。モデルの JSON や SDK call object を provider へ直接転送しない。登録済み handler が業務操作、対象、意図、結果 payload の意味を保証する責務を持つ。

## サーバー側の認可・確認・予算

[Executor](../packages/tools/src/brainoid_tools/executor.py) は Core の `ToolExecutor` port を実装する。サーバーが割り当てた UUID の `tool_call_id` と `idempotency_key` を受け、モデルの不透明な `call_key` を永続 ID に転用しない。`TenantContext` 内の ActorContext は envelope にあり、引数に追加しても許可されない。

必須の `Authorizer.check(tx, request, typed_arguments)` はサーバー組成側の read-only hook である。単なる scope 比較や保存済み grant の返却では実装してはならない。業務 adapter は同じ現在 transaction で次を行う。

- 現在 account、membership/control ledger、device credential family/epoch/generation、private session、run lease/取消、出力先を検査する。既存 `transaction_policy` と account/credential 検査を再利用し、古い receipt を現在の権限として採用しない。
- 引数中の ID から tenant 内の正本対象・出典・版を読み、必要な操作権限を検査する。作成・検索は親/検索 filter、Calendar は integration、家電は device/entity の現在の許可を業務ごとに解決する。モデルが提案した visibility や source は許可の証拠ではない。
- 現在 agent の許可 tool とサーバー allowlist を交差し、明示ユーザー意図、現在の確認状態、proposal の本文 hash・対象・期限を検査する。`Authority.receipt` はこれらと account/対象/出典版を含む本文非保持の SHA-256 とする。
- サーバー正本 run から `run_id` と `max_calls` を決める。呼出しごとに新しい run ID や大きな予算を採番しない。`Authority.context` は request の context と一致させ、モデルの要求で authority を広げない。

Executor は開始予約、外部 I/O 直前、結果保存、保存結果の再利用・返却前に hook を呼ぶ。Authority 全体の変更は拒否し、古い許可を新しい receipt へ差し替えない。実行中の失効では本文を破棄して `Failed(permission_denied)` を返し、実行済み効果を取消したとは扱わない。外部 provider と DB 間を原子的にできないため、直前検査後の失効は handler/provider の許可機構と結果の再検査でも扱う。

業務自身が対象 version を進める場合、入力の期待版と現在版の単純比較だけでは完了保存も拒否される。handler は変更と同じ業務 transaction で、固定 effect ID・入力 fingerprint・変更前の対象 metadata・確定後 version を結び付ける operation 証拠を保存する。hook は毎回現在 IAM を検査した上で、正本の現在版がその確定後版と一致し、権限 metadata の変更も当該 operation の許可内容どおりの場合だけ、元の入力 binding の receipt を維持する。他者による後続更新や削除を自己操作の証拠で許可しない。削除操作の再取得は業務の tombstone/operation を対象に現在の本人・元 scope・出力権限を再検査し、削除済み resource の通常 read を許可する抜け道を作らない。fake 試験は version 1→2 の自己操作成功・再送と、他操作で version 3 になった後の拒否を確認する。

同じ transaction で `tool_budgets/{run_id}` の使用回数、tool ID/key の衝突、現在認可を読んでから開始 marker と予算を保存する。新規 call は一回分を予約し、再送・再照合で追加予約しない。予約後の失敗・応答喪失でも返金しない。同じ run 内の上限変更を拒否する。handler に渡す確認状態はサーバー由来の `not_required` または `confirmed` だけである。

## 永続結果と再照合

tenant 配下の `tool_executions/{tool_call_id}` は canonical 引数/context/key の hash、固定 Authority、決定的な effect ID、started/result を保存する。`tool_keys/{run_id}-{key hash}` は別 tool ID による同 key の再実行を拒否する。operation と command は登録側の `effect_kind` で分け、モデルから指定できない。引数本文・provider call key・秘密の idempotency key はこの記録に保存しない。業務結果本文は内部 result だけに保持し、再取得にも現在認可が必要となる。

handler を呼ぶのは開始 transaction の commit 後、transaction 外だけである。transaction callback の再試行でも外部呼出しは増えない。共通結果は次の通り。

| 結果 | 意味 |
| --- | --- |
| `ToolResult(succeeded)` | handler が確定成功を返し、保存と最終認可が完了 |
| `ToolResult(failed, error_code)` | handler が明示的な確定失敗を報告。operation/command と対応 |
| `ToolResult(waiting_confirmation, proposal_id)` | 永続 proposal の確認待ち。worker を保持する待機はしない |
| `ToolResult(outcome_unknown)` | 外部呼出し・保存の応答喪失、例外、不正な結果、効果開始後の timeout/cancel、または started だけの再送 |
| `Failed` / `Cancelled` / `TimedOut` | schema/権限/衝突/予算の拒否、または予約前の停止。権限失効時の拒否も副作用 rollback の保証ではない |

handler の結果は有限 JSON・64KiB 上限・共通型を検証し、独立 snapshot を保存する。handler の返した構造や再取得結果を書き換えても永続結果へ影響しない。例外本文は保存・返却しない。初回 started commit の応答喪失では呼出しをせず unknown を返す。最終 commit の応答喪失は unknown を返し、次の認可済み再送で保存済みの確定結果を回収する。

started/unknown に対する `execute` の再送は元の効果を実行しない。`reconcile` は同じ効果 ID の provider/業務状態を読み取る専用 hook を呼ぶ。lookup は元の操作を再発行してはならない。並行する元操作が未完了なら unknown を返し、存在しないという一時的な観測を確定失敗とみなさない。再照合の `failed(not_found/unavailable)` は Executor でも unknown へ正規化する。他の明示失敗は provider が永久的拒否等を確定した場合だけ使う。先に保存された確定結果は後発結果で上書きしない。

`tool_audit/{effect_id}-{status}` には tool_call ID、effect ID、登録済み tool 名、固定結果分類、時刻だけを記録する。未知 tool 名は null にし、本文・token・例外・model call key を含めない。同じ結果の再記録は同じ audit key を使う。この metadata と実行記録から call/結果を追跡できる。

## 停止・確認後の引継ぎ

Executor は予約前、I/O 直前、handler が返った後の `CallControl` を確認する。外部 handler は渡された deadline/cancellation を I/O 待機中にも監視し、SDK timeout と取消可能な await を設定する。Executor は handler の await を直接待つため、非協調 handler を強制終了する保証はない。Python task の取消は伝播し、started を保持する。実行中の取消・期限切れ後の結果を取消成功に変換しない。

TransactionStore は同期 port であり、SDK transaction の timeout/retry 上限は構成済み persistence adapter が管理する。停止不能な同期 SDK をこの executor が強制中断するとは扱わない。RUN-07 の非同期 run 接続では既存 RUN-04/05 の有限 bridge/lease と組み合わせ、deadline を超えた処理を新しい世代へ流用しない。

`waiting` の Authority と `confirmed` は異なるため、通常の `execute` 再送で確認済みに切り替えて実行できない。RUN-07 は永続 proposal/tool_call/run checkpoint を同じ transaction で一回だけ消費する専用の確認遷移を追加し、確認済み効果への固定 ID と開始権限を引き継ぐ必要がある。既存 started/unknown を削除して再実行する実装にはしない。今回の executor はその遷移や Calendar confirm の公開を提供しない。

## 検証と復旧

[fake 試験](../tests/test_tool_execution.py) は実 IAM evaluator/control ledger/account と copy-on-commit transaction fake を用い、T-018/T-029 の注入・未知 tool・禁止値、現在権限/対象変更、再送/競合/予算、確認待ち、明示失敗、応答喪失、再照合の並行完了、取消、snapshot と監査の非漏出を検査する。schema 同梱の整合と timezone/時刻形式も検査する。

Firestore emulator は今回未実施。共有 transaction/persistence adapter を変更せず、read-before-write を強制する fake、callback 再試行、開始/完了 commit 応答喪失を注入してこの共通実行層を検証する。実業務 handler と Firestore を結ぶ際の結合試験は後続チケットで行う。

```sh
mise run check
mise run test
```

新しい内部 collection だけを使い、公開 HTTP/JSON Schema の変更や既存 DB の移行はない。戻す場合は tool を登録・呼出ししない構成へ切り替え、実行/key/budget/result/audit を保持する。unknown や開始 marker の消去・新しい ID での再送を復旧手順にしない。実 provider、業務ごとの認可 adapter、実 run への接続は今回未実施であり、後続の MEM/APP/CAL/DEV/RUN チケットで検証する。
