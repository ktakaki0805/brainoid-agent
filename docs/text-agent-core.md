# 人格設定と文字 AgentCore

BA-RUN-03 は既存の初期 agent を [Agent API](../services/api/src/brainoid_api/routes/agents.py) から共有し、[TextAgentCore](../packages/agent_core/src/brainoid_agent_core/core.py) で一回の文字生成を実行する。永続 conversation/message/run、SSE、tool 実行、実記憶検索はこの実装に含めない。

後続BA-RUN-04の[会話・永続文字run](conversations-and-runs.md)は、このCoreへ現在の親会話/history/agent/credential/leaseを読むContextAuthorizerを接続し、最終保存時にも再認可する。BA-RUN-05は任意の非同期`on_text` observerへ採用済み文字差分だけを渡す。observer待機にも同じ期限と有限cleanupを適用する。tool実行は[RUN-06](tool-execution.md)、実記憶検索は[MEM-04](memory-retrieval.md)で接続した。

## 人格の取得と更新

`GET /v1/tenants/{tenant_id}/agents` と `GET /v1/tenants/{tenant_id}/agents/{agent_id}` は現在の user membership または device credential を再確認する。同じ tenant/agent/version はスマホ・Pi とも同じ `name` / `persona` / `allowed_tools` になる。個別取得は `ETag` を返す。現在版だけを提供し、旧版の履歴取得は実装しない。Core の `expected_agent_version` と取得版が違えば `Failed(conflict)` でモデルを呼ばない。

`PATCH` は user bearer の owner/admin に限定し、`name` と `persona` の一つ以上の非 null フィールドを受ける。`If-Match` 欠落は428、不一致は412。既存の24時間冪等処理で同じ key/body/version は一回だけ更新し、再送時にも現在管理権限を確認して現在の Agent を返す。人格文や `allowed_tools` を認可入力として使わず、`allowed_tools` の HTTP 更新は受け付けない。これは将来のモデル向け候補制限であり、実行側 allowlist・現在 actor policy との交差を増やせない。

取得・ページング・更新は同じ transaction 内で account、membership、control ledger を確認する。device は毎 transaction で credential family / epoch / authorization version / connection generation と現在 device state を確認する。`X-Private-Session` は device bearer との併用だけを認め、指定時は現在 session・member・account を確認する。失効 session を無視して通常 device に戻さない。cursor は現在の認可と agent 集合の版に束縛する。内容は共通 middleware の `Cache-Control: no-store` と本文非記録の対象である。

## Core の注入と現在認可

Core の実行依存は contracts と Pydantic のみ。`ModelProvider`、`MemoryStore`、`Clock` と必須の `ContextAuthorizer` を構成側から注入する。実 model SDK、FastAPI、Firebase、端末モジュールを import しない。`ContextInput` は認証済み context、正本から読んだ agent と期待版、認可対象 conversation の tenant/owner/scope/version/deletion epoch と内部履歴、今回の user text、要求する記憶 scope を受ける。HTTP body から内部履歴・provider state を直接構築しない。

記憶利用は任意で、`ContextInput.memory_scopes=()` は検索を無効にする。この場合は `MemoryStore.search` 自体を呼ばず、会話・人格・履歴・現在権限の検査と生成は通常どおり行う。`conversation` と `memory.read` は独立した許可であり、会話だけを許可したPi個人sessionでも文字会話を実行できる。非空scopeを明示した要求は従来どおり検索前に現在認可し、不足時に空scopeへ置き換えて続行しない。実際に検索する `MemorySearch.scopes` の非空制約は維持する。

[ContextAuthorizer.check](../packages/agent_core/src/brainoid_agent_core/context.py) は既存 IAM を使う信頼済み port。Core 内の scope 比較を IAM の代わりにしない。adapter は一つの現在 transaction で次を確認する。

- account/credential と現在 principal・session・出力先。要求されたすべての memory scope は `policy.search` の現在 filter（private owner、display policy、端末 room/entity 制限を含む）の範囲内であること。人格から検索権限を増やさない。
- 正本 agent の完全な設定と版、conversation の owner/scope/version/deletion epoch と内部履歴の内容・版が一致すること。
- 返された全 memory と全出典に `MODEL_READ` を適用し、tenant/owner/scope、記憶・出典版、削除・訂正・suppress_reuse・有効期間、正本内容を確認すること。message/import の出典はそれを所有する conversation 等の認可も必要。

最初の `check` が content を含まない認可 receipt を返す。検索結果を受けて prompt を作る直前、モデルの各イベント処理前、正常 snapshot 確定前、stream cleanup 後の返却直前にも同じ receipt で再確認する。記憶結果の検査対象は増やせるが、古い authority や入力版を新しい grant へ差し替えない。変更・拒否は `permission_denied`、buffer 内の本文・snapshot は返さない。実source/history readerはBA-RUN-04と[BA-MEM-04](memory-retrieval.md)がこのportに接続している。今回の fake は既存 `Evaluator` と架空の正本を用い、検索 port 呼出し前の未知 Pi/private 拒否も検査する。

`ContextAuthorizer.release(receipt)` は必須の同期・no-I/O portで、直前の成功した認可transaction/attemptが消費した全証跡と期限を保持する。receipt不一致・未取得・期限切れは拒否し、DB再読取やgrant更新は行わない。Coreは認可awaitの子task cleanup後、stream生成直前、最初を含む各provider iterator実行Task内でreleaseを呼ぶ。workerのReadBridgeからasync callerへの受渡し後も同じ全証跡を検査する。さらにMeteredModelのbyte計量後・started設定/実provider呼出し直前にも同じcapabilityを確認し、拒否時の使用量は未開始として扱う。新規検索結果が空でも、履歴・tool結果・自己訂正の過去入力とcaller/output期限を失わない。

さらに Core は tenant/scope/confirmed 状態/出典存在/有効期間/件数を検査し、故障 MemoryStore が権限外候補を混ぜた場合は要求全体を拒否する。許可候補だけへの曖昧な fallback はしない。model request には人格、承認された履歴、記憶内容、今回の user text だけを選ぶ。`TenantContext`、UID、credential、現在認可 receipt は serialize しない。記憶は明示的なmodel projectionでresource ID・version・記録時刻・origin・content/source_refsを選び、tenant_id/owner_user_id/created_byを含めない。full canonical MemoryRecordとtool result/bindingはサーバーに保持し、投影済みToolReplyも正規resultから同じ投影を再計算して完全一致を検査する。モデル入力上の人格・記憶はデータとして区切り、実際の権限はサーバー境界で保証する。

## 予算と終端

`RunBudget` は timeout（既定30秒）、最大出力token（1024）、最大表示文字（16000）、最大モデルイベント数（10000）、検索件数（10）を指定する。caller deadline と timeout の早い方を採用し、取消を優先する。記憶・認可・モデルの待機を注入 Clock で監視するため、deadline を無視して待つ fake も停止する。取消は外部 provider で発生した課金を取り消す保証ではない。

stream の `aclose()` にも独立した **0.25秒** の上限を設け、turn deadline の早い方と live cancellation を監視する。正常生成後でも close 上限を超えれば `TimedOut` とし、本文・snapshot を破棄する。子task回収は cancel 後 **0.25秒**、未完了なら再 cancel 後さらに **0.25秒** の二段階に限定し、無期限の `gather` / `wait_for` に依存しない。最初の取消で adapter の `finally` に入り、そこで待機した場合も再取消で解放できる。猶予はすべて同じ注入 Clock で計測し、資源回収のためturn期限後にこの有限猶予を使うことがある。Python task自体の取消は回収後に伝播し、cleanup中のtimeoutや通常例外で業務結果へ置き換えない。

adapter は cleanup 中も Python task 取消に協調し、その際に資源を解放する契約である。繰返し取消を握りつぶす非協調的adapterには資源解放・残taskゼロを保証できない。Coreは二段階の猶予後も無期限に待たず失敗し、残taskの後発例外を本文非出力で回収する。協調的adapterの通常close待機・生成取消後のcleanup待機では、資源解放と残taskゼロを回帰試験で確認する。

文字数・イベント数・報告された累積出力tokenの上限超過は `Completed(length)` とし、内部 snapshot を確定しない。文字は上限内に切る。token 最大値は provider にも渡すが、未報告の token を推定して課金上限とみなさない。`Usage` は累積 snapshot で、欠落値は直前の既知値を保持する。自動再試行はゼロ回。途中 EOF、provider 失敗、取消、timeout は成功へ変換せず、provider の例外本文を結果やログへ出さない。asyncio の内部 task は本文・例外を戻り値にしない。

`ModelResponse.message` がある場合、表示差分・tool候補との一致を確認し、`Completed(stop/tool_calls)` と最終再認可が揃って初めてその message を保持する。`provider_state` の署名・part順序・call対応を差分から再構成しない。snapshot/候補と `Completed` の間、cleanup 中にも取消・失効・期限は有効。JSON container は deepcopy/serializer より先に型・有限数を検査する。`TextRunResult.response` は内部専用で `repr` と JSON serialization から除外する。HTTP/SSE、表示、tool引数、ログ、検索記憶へ転用しない。[Google adapter 契約](google-model-provider.md)も適用する。

FND-07 の snapshot を持たない provider は `TextDelta → Completed` だけでも正常に文字生成でき、その場合 `response=None` とし内部 snapshot を捏造しない。tool 候補は今回いずれも実行しない。`tool_calls` は終端理由として返せるが、候補は表示用結果へ含めず、snapshot がある場合だけ内部 response に保持する。BA-RUN-06 は候補を実行する前に schema、実行 allowlist、現在権限、確認、冪等性を改めて検証する。

Core の最終確認から外部配送・永続保存までには新しい待機が生じる。BA-RUN-04/05 はその transaction / SSE 配送 / 表示先でも現在認可と同じ入力版を確認し、失効後に保存・配信しない。Core の完了だけを永続 run の完了や配信許可とは扱わない。

## 検証と移行

[Core fake 試験](../tests/test_text_agent_core.py) は T-005 人格共有、権限外記憶/出典版/未知 Pi、予算、署名往復、cleanup・途中取消、待機中失効、ログ非漏出、SDK 禁止 import を検査する。[Agent API 試験](../tests/test_agents.py) と [emulator 試験](../tests/emulator/test_agents_integration.py) は role、ETag、冪等再送、credential/session、同時更新、commit 応答喪失を検査する。

```sh
mise run setup
mise run generate-client
mise run check
mise run test
mise run test-emulator
```

OpenAPI の既存 Agent/AgentPatch 形は維持する。IAM-03 作成済み agent はそのまま利用できる。開発用 demo fixture の旧 `enabled_tools` を `allowed_tools` へ統一し、persona を追加したため emulator の reset/seed を行う。実運用の旧形式移行や履歴版の保存は今回発生しない。切戻しは agent route/Core の利用を停止する。既存の設定更新は版を戻さず、必要なら現在 ETag を使う新しい更新で元の人格へ戻す。実 provider、実端末、本番索引・配信の確認は今回未実施で、それぞれ後続チケットの範囲である。
