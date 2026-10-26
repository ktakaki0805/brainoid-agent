# Google Gen AI ModelProvider

BA-RUN-02 の内部 Python adapter。[実装](../packages/model_google/src/brainoid_model_google/provider.py)は独立 workspace member `brainoid-model-google` に置き、[AgentCore 契約](agent-core-contracts.md)へ SDK 型を持ち込まない。API/worker の起動への接続と会話ループは BA-RUN-03 以降の責務。OPEN-005 のモデル・地域選定をこの実装で確定しない。

## 明示設定と認証

`GoogleModelSettings` は `provider="vertex"`、`project`、`location`、裸の `model_id`、`timeout_seconds`、`max_output_tokens` をすべて必須とする。timeout は有限の 0 より大きく 300 秒以下、最大出力量は整数 1〜65536。要求側の最大出力量との小さい方を送る。URL・完全修飾モデルパスを拒否し、指定 project/region の Google publisher model だけを宛先にする。`global` は明示時だけ許可。複数地域 endpoint（`us` / `eu`）の自動選択や別 provider への fallback は実装しない。モデルが指定地域や機能に非対応なら失敗する。

`GoogleModelProvider(settings, credentials=..., clock=...)` に構成側が事前更新した短命 credential を渡す。呼出し開始時に token が空・期限切れなら `permission_denied` とし送信しない。有効 token は refresh 不可能な呼出し専用 snapshot にコピーし、SDK に ADC 探索・認証 HTTP・背景 thread による refresh をさせない。要求中に token が失効しても再認証せず失敗する。短命認証の取得・更新、更新の timeout、IAM と課金権限は配置側の別の責務。credential/token を provider state に格納しない。

固定 SDK の `Client(vertexai=True, project=..., location=..., credentials=...)` と `HttpOptions(api_version="v1", base_url=...)` を明示する。HTTPX client は呼出しごとに所有し、環境 proxy、redirect、transport retry は無効。送信直前にも宛先 host と API key header の不在を検査する。`GOOGLE_API_USE_CLIENT_CERTIFICATE` の有効化は拒否し、mTLS endpoint への環境切替も宛先検査で拒否する。`GOOGLE_API_KEY` / `GEMINI_API_KEY`、SDK の Vertex 選択・project/region・base URL、record/replay の環境設定で実宛先・認証・本文保存を変更できない。SDK debug/replay 設定も明示する。独自 transport 注入は信頼済みのテスト境界。

SDK 自動 function calling と automatic continuation を無効化し、`HttpRetryOptions(attempts=1)` とする。429、切断、打切りを adapter 内で再送しない。`retryable=True` は分類であり、再試行の実行許可ではない。再送判断と回数予算は BA-RUN-03 が担う。

## 入出力と次の turn

- 先頭 system は `system_instruction`、user は `user`、assistant は `model`、tool 応答は `user` の `function_response` に変換する。tool 宣言は JSON Schema を持つ関数宣言だけ。Python callable・MCP・検索・コード実行を渡さず、tool 応答の error/output を分ける。
- 表示用テキストだけを `TextDelta` にする。`thought=True` の本文・署名を表示しない。サポート外マルチモーダル part は安全に失敗する。
- function call の `args` は完全な JSON object を要求する。部分引数 streaming を無効化し、`partial_args` / `will_continue` が届いたら失敗する。chunk をまたぐ引数を推測・結合しない。複数 call を保持し、重複 ID・欠けた名前・壊れた args を拒否する。名前の許可・引数 schema・認可・確認・冪等性は ToolExecutor/会話層の責務で、adapter は候補を実行しない。
- provider ID は `ToolCall.call_key` に保持する。ID の無い call は応答内順序から `google-call-0` 等を生成するが、provider への再送は元の ID 不在を保持する。永続 UUID ではない。
- `Usage` は累積値。加算せず、後続報告で省略された項目は直前の既知値を保持する。最初から不明な項目は `None`。input は prompt、output は candidates、cached input は cached content の報告値。思考用 token などを含む課金総量の保証には使わない。最後にも snapshot を返し、finish 後の usage 専用 trailer を読む。
- `STOP` は `stop` または `tool_calls`、`MAX_TOKENS` / `CONTINUATION` は `length`、安全性・転載等の抑止は `content_filter`。未知理由や不正構造は非再試行の `Failed(invalid_arguments)`。429 は `rate_limited`、401/403 は `permission_denied`、408/504・HTTP timeout は `TimedOut`、接続障害は `unavailable`。SDK 例外文・body をイベントやログに出さない。
- 明示 finish と正常 EOF の両方が必要。finish のない EOF は `stream_interrupted`、finish 後の切断も失敗。打切り・block・途中失敗では tool 候補や履歴を確定しない。通常テキスト差分は先に届き得るため、表示側は不完全な出力を完了扱いしない。

正常完了時、`ModelResponse(message=...)` を `Completed` の直前に返す。`ModelMessage.provider_state` は SDK 型を含まない不透明な JSON で、model/project/region の binding、元の part 配列（thought signature を含む）、call key 対応を持つ。次 turn では **この assistant message を変更せず履歴へ戻す**。差分だけから履歴を再構成すると署名が失われる。署名だけの part や思考用 part の順序も保ち、再入力時に visible parts と binding を照合する。provider state は認可済み内部履歴専用で、HTTP 入力・SSE・tool 引数・ログ・検索記憶へ渡さない。`repr` からも除外する。

BA-RUN-03 は `Completed(stop/tool_calls)` と現在認可の再確認後に限り snapshot を確定する。今回の文字 Core は tool 候補を実行せず、BA-RUN-06 が schema・現在権限・確認・冪等性を検証して tool 応答へ進む。candidate/snapshot と Completed の間にも取消・期限は成立し得るため、それ以前の候補を実行しない。異なる model/region/project に state を再利用しない。署名往復は fake で検証済みであり、特定実モデルの利用可能性・品質の認定ではない。[Google の thinking 仕様](https://ai.google.dev/gemini-api/docs/thinking)では generateContent の署名は各 part に付随するため、この保存境界を設けた。

`response_schema` 指定時は終端まで buffer し、JSON 構文と Draft 2020-12 schema の確認後に一つの `TextDelta` を返す。不正出力を途中表示しない。入力の tool 引数・tool 応答・schema・provider state と SDK 応答では、ネストを含む `NaN` / `Infinity` / `-Infinity` および `1e400` 等の数値 overflow を拒否する。JSON serialization 前と文字列 parse 後に有限数を検査し、不正値を省略・`null` へ置換して通過させない。入力は HTTP dispatch 前、構造化応答は表示・履歴確定前に非再試行の `invalid_arguments` で終了する。外部 `$ref` / `$dynamicRef` を禁止し、ローカル参照にも外部取得不能 registry を使う。schema/参照不正も非再試行の失敗。応答の SDK JSON 合計が 8 MiB を超えても失敗する。tool 宣言 schema も dispatch 前に検査する。

Core の JSON container は構築後も変更できるため、tool 応答・schema・provider state は deepcopy/serializer より先に raw 値を検査する。bytes・任意 object・非文字列 key・JSON 内の tuple 等へ後変更された値は、警告や例外へ本文を出さず非再試行の `invalid_arguments` で拒否する。警告の抑止や型の自動変換で送信を継続しない。この JSON 検査は Core message の tuple や SDK native な thought signature bytes に適用せず、署名を保持する境界を維持する。

## 期限・取消・解放

注入した `Clock.monotonic()` 上で caller deadline と設定 timeout の早い方を使う。caller deadline が無くても設定 timeout は有効。待機中は最大 50 ms 間隔で live cancellation と期限を検査し、同時成立は取消を優先する。送信直前・各出力前にも再確認する。Python task の `CancelledError` は解放後に伝播する。

完了・失敗・取消・期限・利用側の `aclose()` で SDK generator、取得済み HTTP response、同期/非同期 client を閉じる。SDK の入れ子 generator の終了だけに response 解放を依存させない。利用側は `contextlib.aclosing` を使う。送信済みモデル生成の課金取消を保証するものではない。共通終端は一回だけ。

## 検証と実疎通の外部前提

[test_google_model.py](../tests/test_google_model.py) は固定 SDK の実 serializer/SSE parser に HTTPX fake transport を注入する。通常文・tool 区別、署名往復、引数破損、usage 欠落・分割報告、途中切断、429、打切り・自動継続禁止、構造化応答、環境上書き、認証 refresh 禁止、期限、取消、早期 close、asyncio debug ログの本文非漏出を外部 API なしで検査する。

```sh
mise exec -- uv run --all-packages --locked --offline python -B -m unittest discover -s tests -p test_google_model.py
mise run check
mise run test
```

2026-10-26 時点で OPEN-005 の model/region、課金上限、staging の短命認証が未確定のため実 provider 疎通は未実施。架空の `fixture-model` を実在・採用済みと扱わない。前提確定後、次の上限付き手順で別記録にする。

1. staging の明示 project・採用 model/region、IAM、課金/quota 上限を担当者が確定する。通常 setup/test では認証・生成をしない。
2. 配置側で短命 token を事前更新し、秘密値を出力せず credential を注入する。`timeout_seconds=10`、`max_output_tokens=64`、caller deadline も 10 秒以内。外側 process にも 30 秒の上限を置く。
3. 合成の短い文字要求を **1 call** のみ実行し終端理由と token 数を記録する。tool 往復を確認する場合は合成の宣言・固定応答で **追加 2 calls まで**。実 tool は実行しない。429/timeout/構造不正なら停止し、反復しない。
4. UTC 日時・SDK版・model/region・総呼出回数・終端理由・usage の既知/不明・上限遵守・結果を記録する。本文・署名・credential は記録せず、不明 usage をゼロ請求と解釈しない。

モデル品質・日本語比較は BA-QA-03、音声は BA-VOICE-01/02。公開 HTTP/SSE・DB の変更はなく移行不要。切戻しはこの member の利用を停止し fake に明示接続する。model/region を自動変更しない。将来の履歴保存では provider state を含む内部形式の版管理を別途行う。
