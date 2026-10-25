# AgentCore の port と fake

BA-FND-07 の内部 Python 契約。公開入口は `brainoid_agent_core.ports` と `brainoid_agent_core.types`。実装は [agent_core](../packages/agent_core/src/brainoid_agent_core/)、検証は [test_agent_core.py](../tests/test_agent_core.py) にある。HTTP/SSE の通信契約とは別の層であり、[run-events.schema.json](../specs/run-events.schema.json) の置き換えではない。

## 1. 差し替える境界

| port | 入力 | 出力 |
| --- | --- | --- |
| `ModelProvider.stream` | `ModelRequest`、`CallControl` | `ModelEvent` の非同期 stream |
| `MemoryStore.search` | `MemorySearch`、`CallControl` | `MemoryMatches` または共通失敗 |
| `MemoryStore.remember` / `revise` | `MemoryRemember` / `MemoryRevise`、`CallControl` | `MemorySaved` または共通失敗 |
| `MemoryStore.forget` | `MemoryForget`、`CallControl` | `MemoryForgetAccepted` または共通失敗 |
| `ToolExecutor.execute` | `ToolExecution`、`CallControl` | `ToolResult` または共通失敗 |
| `SpeechProvider.recognize` / `synthesize` | 音声 bytes / テキスト、format、locale、`CallControl` | `Transcript` / `AudioChunk` と共通終端の非同期 stream |
| `Clock` | `sleep` の有限・非負秒数 | UTC の `now()`、経過秒の `monotonic()`、非同期 `sleep()` |

port は構造的な `Protocol`。adapter は継承を必要とせず、同じ引数と戻り値を実装する。core の実行依存は既存の `brainoid-contracts` と Pydantic のみ。FastAPI、Firebase、Google Gen AI、Pi モジュールを import しない。外部 SDK の要求・応答・例外は adapter 内で変換する。`ModelRequest.response_schema` と tool schema も JSON 値で渡す。

`ResourceId`、`FirebaseUid`、`Scope`、`TenantContext`、`UtcTimestamp`、`Version` は [共通型](contracts.md) を再利用する。モデルへ渡すメッセージ・tool 宣言には認可済みの内容のみを選び、`TenantContext` や credential を含めない。モデルの `ToolCall.call_key` は会話内の tool 応答用の不透明な対応キーであり、永続リソースの UUID ではない。後続の会話実行層が UUID の `tool_call_id`、認可済み context、冪等性キーを結び付けて `ToolExecution` を作る。

`ToolCall.arguments_json` は未検証の文字列で、不正 JSON や許可外の tool 名も候補として表せる。ToolExecutor はその候補を直接外部 provider へ中継せず、現在の schema・権限・確認・冪等性を検証する。ここでは業務ごとの検証器や実行処理は実装しない。

MemoryStore の全要求は context と閲覧 `scopes` が必須。戻り値は出典・版・scope・有効期間・状態を含む。実 store は現在の権限と削除状態を照合し、許可された記録だけを返す。`MemoryContent` は内部の記憶内容 DTO であり、永続化・HTTP 入力の完全な validator ではない。predicate ごとの qualifiers 検証、出典・owner の検査と認可は後続の store が担う。`MemoryForgetAccepted` は永続 operation の受付を表し、物理削除完了を表さない。

## 2. stream・取消・期限・副作用

モデルは `TextDelta`、`ToolRequested`、`Usage` を返す。`Usage` は当該呼出しの累積値であり、複数の usage を加算しない。未報告の token 数は `None` とする。生成の終了理由は `Completed.finish_reason` の `stop` / `tool_calls` / `length` / `content_filter` に正規化する。

stream は `Completed`、`Failed`、`Cancelled`、`TimedOut` のいずれかで一回だけ終了し、その後にイベントを返さない。終端のない EOF は成功に変換せず、`Failed(code="stream_interrupted")` とする。provider の生の例外文を `Failed` に含めない。モデル内部イベントから永続 run/SSE イベントへの変換、seq、batch、認可再確認は BA-RUN-03 の責務。

`CallControl` は呼出しごとに明示する。`deadline` は adapter に注入した Clock と同じ単調時計上の絶対秒数で、現在値と等しい場合も期限切れ。`None` は期限なし。`Cancellation.cancelled` は呼出し中に変化する live signal で、provider へ serialize しない。両方が dispatch 前に成立した場合は取消を優先する。実 adapter は I/O 待機中も取消と期限に応答し、終了時に SDK stream 等を解放する。

返す stream は `aclose()` を持つ `AsyncGenerator`。利用者は早期終了時も `contextlib.aclosing` で閉じる。Python task 自体の `CancelledError` は解放後に伝播し、業務の `Cancelled` イベントと区別する。Speech の取消後は新しい音声を返さず、利用側は既に受け取った再生待ち buffer も破棄する。端末の再生キューやマイク制御はこの port の外にある。

取消・timeout は実行済み副作用の rollback を意味しない。外部操作が実行された可能性がある場合、ToolExecutor は `outcome_unknown` と operation/command ID を返し、照合まで再実行しない。確認待ちは proposal ID を必須とする。`succeeded`、`Failed`、`waiting_confirmation`、`outcome_unknown` を区別する。fake は副作用を実行せず、これらを設定した応答として再現する。

## 3. 決定的な fake

[tests/fakes](../tests/fakes/) はテスト専用。アプリの production 起動設定へ接続しない。

- `FakeClock` は固定 UTC と 0 秒から開始し、`sleep` は論理時間だけを進める。実時刻・乱数・外部通信を使わない。
- `Step(value, delay)` の列が 1 回分の応答。呼出し順に script を消費し、未設定の呼出しはテストエラーにする。複数の呼出しは逐次再生を前提とし、同時実行 scheduler は模擬しない。
- モデルと音声は stream、記憶と tool は一つの応答を設定する。遅延の前後・各出力直前に取消と期限を検査し、期限を超える遅延は期限までに制限する。終端を省けば途中停止を再現できる。
- 履歴には要求、deadline、開始・終了の論理時刻、出力イベント、close を残す。script・履歴・返却値の JSON を deep copy し、呼出し元の変更で過去の履歴が変わらないようにする。DTO の frozen はフィールドの再代入を禁止するもので、ネストした JSON の不変性は保証しない。
- [scenarios.py](../tests/fakes/scenarios.py) に不正引数、禁止 tool、結果不明、別 tenant の private 候補を明示する。`OUT_OF_SCOPE_CANDIDATES` は契約に違反した検索 adapter の故障を再現する fixture。認可された検索結果を意味せず、fake に認可判定はない。候補が LLM/SSE/TTS へ漏れないことの検証は後続チケットで行う。

`test_agent_core.py` の `model_contract` は `ModelProvider` のみを受け取り、異なる fake を同じ検査へ差し替えられる。通常完了、tool 要求、usage、取消、timeout、途中停止、明示失敗、音声停止、記憶の出典・版、結果不明と履歴の再現性を検証する。隔離した subprocess では SDK・framework の import を禁止する。

実行コマンドは `mise run check` と `mise run test`。対象のみの再実行は以下。

```sh
mise exec -- uv run --all-packages --locked --offline python -B -m unittest discover -s tests -p test_agent_core.py
```

Google Gen AI adapter は BA-RUN-02、会話ループは BA-RUN-03、IAM・記憶・外部操作の実装は各機能チケットで追加する。今回の変更には公開 API・保存形式の移行や実 provider の導入はない。
