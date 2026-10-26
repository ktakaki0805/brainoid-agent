# 会話・message・永続文字run

BA-RUN-04 は [Conversation API](../services/api/src/brainoid_api/routes/conversations.py) から会話の作成・一覧・取得・設定更新、message一覧、turn受付とrun取得を提供する。入力と出力は現在 `text` のみ。BA-RUN-05は`events_path`から認証付きSSEを提供する。音声・取消・確認待ち・tool実行は後続チケット。API応答後の背景処理には依存しない。

## 受付と会話内の順序

turn受付は現在account、membership/control ledger、device credential family/epoch/generation、指定private session、親会話、出力先を同じtransactionで再検査する。user message、`queued` run、`pending` job、24時間の冪等記録を原子的に保存して202を返す。同じkey/bodyの再送は同じrun IDを返し、本文変更は409。再送時にも現在認可を要求する。

会話は一つの `active_run_id` を持つ。別keyのturnが進行中なら409とし、同じhistoryから複数回答を同時に作らない。受付時のagent完全設定/版、内部履歴の内容hash、履歴revision、入力message ID/sequenceをrunへ固定する。worker開始時に最新版へ差し替えない。入力messageは `conversations/{id}/messages/{id}`、runは `runs/{id}`、jobは `jobs/{id}` に置く。

job sourceは親conversationの現在version/deletion epochで、actionはPOST。新しいmessage/runの親・tenant・ID・sequence・内容とactive slotの対応は [共有RunState](../packages/runs/src/brainoid_runs/state.py) で検査する。受付では会話のsettings/completed-history versionを維持し、active slotとnext sequenceだけを変更する。完了commitは会話version/history revisionを一つ進める。GETのETagはこの会話版を示し、message一覧cursorはmessage集合の内容にも束縛する。

会話PATCHはtitle/display_policyだけを受け、ユーザーの現在編集権限とIf-Matchを検査する。private所有者以外のadminにも内容を返さない。設定変更は会話版を進めるため、既存jobの旧source grantが失効する。旧grantを新しい版へ更新して生成を続けない。visibilityの変更は提供しない。cursorは現在scope・actor権限・resource版/一覧内容へ束縛し、変更後の古いcursorを拒否する。

Piの明示的な新規入力だけが、同じtransactionでprivate sessionのidle期限を延長する。private-session hookの読取準備を先に行い、Jobs.add_pendingの全readが終わってからsession/business/outboxを書き込む。再送、GET、worker完了、モデル出力は活動として数えない。未識別Piは許可されたshared + shared_displayだけを利用できる。

## workerと現在の認可

[RunTurn](../services/worker/src/brainoid_worker/run_turn.py) は同期workerのhandler。Core全体を `execution.effect("model", ...)` の一つのstepで囲む。外部モデル呼出し前にstartedを保存し、結果をcompleted checkpointに保存してから最終業務commitを行う。completedは再配送で再利用し、startedしか残らない停止・通信断・lease喪失は `outcome_unknown` とする。Google生成を安定keyだけで冪等とみなして再呼出ししない。

CoreのContextAuthorizerは各checkで正本jobのrunning状態・lease世代/期限、保存grant、現在account/control ledger/device family/private session/output、agent完全設定、親会話と正本history/inputを同一transactionで検査する。入力版や現在権限が変われば出力を捨てる。検索前に全要求scopeを `policy.search` の現在filterと照合する。shared会話にはprivate記憶scopeを要求しない。MEM-04まではEmptyMemoryを使用し、予期しないmemory候補は一件でも拒否する。実検索や出典readerを提供したとは扱わない。

`conversation` だけのPi個人sessionは記憶検索なしで利用できる。受付は現在actorと出力先の `memory.read` を交差し、両方に許可がある場合だけ `policy.search` で記憶scopeを選ぶ。それ以外は空scopeを保存し、worker/CoreはMemoryStore.searchを一度も呼ばない。スマホ本人にmemory.readがあっても、出力Piの個人sessionがconversationだけなら同じ動作となる。空scopeでも会話・出力先・family/session・lease・期待版の検査は省略しない。明示された非空scopeはworkerが従来どおり検索認可し、不足を空scopeへの置換やPolicyDeniedの握り潰しで回避しない。

読取待ちは最大4件のread-only daemon threadに制限し、待ちqueueを増やさない。飽和はPersistenceUnavailableとなる。Coreのdeadline/cleanup取消はasync待機を終了でき、`asyncio.run` のdefault executor終了を待たない。残るSDK読取はDB/control ledgerへ書き込めず、終了したloopへ結果を再配送しない。Pythonは停止不能SDK呼出しを強制終了できないため、遅延読取はSDKが返るまでslotを占有する。この場合はworkerを交換して復旧する。これはprocess全体の停止期限保証ではない。通常のtransaction/commitとSDKの再試行時間は基盤側の境界で、lease期限を越えた結果はcommitしない。

PolicyUnavailable/PersistenceUnavailableはCoreの通常例外正規化とは別に保持し、workerへ既知基盤障害として戻す。モデルstep開始後に現在権限の照合が不能になった場合も、startedだけなら保守的にoutcome_unknownで止める。completed結果が既にあればそれを保持して再待機する。確定失効と照合不能を混同して成功にしない。

最終callbackは外部I/Oを行わず、現在認可・lease・親/history/agent/family/outputを再確認した同一transactionでfinal message、run、会話、jobをcommitする。旧世代結果は採用しない。handlerに届く前の失効・未知handler・試行上限・outcome_unknownなどでjobが終端になった場合は、認可済みrun GETまたは次のturn受付でrunをfailedへ反映する。古い結果を復活させず、queued/runningを公開し続けない。

## 表示と内部履歴

公開Messageは8000文字以内。stopは非空応答だけcompleted、lengthは予算内に切った最終本文でcompleted、content_filter・空応答・provider失敗・未対応tool_callsはfailedとしassistant成功messageを作らない。tool候補を実行済みと表示しない。失敗後の入力messageは発話履歴として残る。

提供された完全なModelResponseは、stopと最終認可の後だけ内部checkpoint/historyへ明示的に保存する。TextRunResult.responseは通常serializationから除外されるため、署名付きsnapshotを差分から再構成しない。ModelResponseなしのproviderは表示文字だけを保存し、provider_stateを捏造しない。署名、tool候補、内部history、credential receiptは公開DTO/HTTP/logへ出さず、HTTP入力からも受け付けない。

## 組成・検証・切戻し

共有の [brainoid-runs](../packages/runs/pyproject.toml) はcontracts/policy/jobs/Coreだけに依存し、workerはAPI repositoryをimportしない。APIは既存認証lifespanへ接続する。workerは構成済みのJobs、明示ModelProvider、credential epochをRunTurnへ注入し、既存Worker/Dispatcherを利用する。`text_worker` は明示local caller専用の組成で、本番Tasks verifierの代替にはしない。

workerのJobsには `brainoid_persistence.firestore.FirestoreTransactions` を渡す。API側の互換adapterはPersistenceUnavailableをHTTP用APIErrorへ変換するため、workerへ流用しない。

[fake試験](../tests/test_turn_run.py) はT-001/T-005/T-031の冪等・private/tenant/失効、同時配送、署名履歴、provider失敗、予算、session活動、checkpoint再利用、lease期限、読取bridgeを検査する。[emulator試験](../tests/emulator/test_turn_run_integration.py) は実SDKの同時turn、commit応答喪失、APIが202を返してprocess終了した後の別process worker実行、再配送と現在認可を検査する。

```sh
mise run setup
mise run generate-client
mise run check
mise run test
set -a
source .env.emulator.example
set +a
mise run test-emulator
```

外部distributionの追加はなく、workspace memberだけを追加する。既存DBの移行は不要で、新規会話は内部sequence/history metadataを作る。配布demo fixtureも現在形式へ更新した。旧fixtureが残るemulatorはreset/seedして、履歴用failed runとmessage sequenceを揃える。切戻しではrun受付と対応worker/dispatcherを停止し、既存message/run/job/checkpointを保持する。started不明を削除して再生成しない。本番Tasks/索引/負荷、実provider/端末、音声・tool結果照合はこの検証範囲に含まない。

## SSEの差分・再開・保持

BA-RUN-05の[配送入口](../services/api/src/brainoid_api/routes/run_events.py)は `GET /v1/tenants/{tenant_id}/runs/{run_id}/events` を提供する。user/device bearerはAuthorization headerで送り、Pi個人sessionは `X-Private-Session` も必要。本文は[run-events.schema.json](../specs/run-events.schema.json)に一致し、SSE `id` はseq、`event` はtype、`data` はJSON。初回はseq 1から、`Last-Event-ID`指定時はその次から配送する。未来seqは409、不正形式は400。run内seqは1ずつ増加し、heartbeatは15秒目安のSSEコメントでseqを消費しない。

[専用writer](../services/worker/src/brainoid_worker/run_event_writer.py)はCoreが採用した表示文字だけを受け取り、最初の未保存文字から250ms、またはUTF-8で1KiBを目安に確定batchを保存する。providerが次のtokenを返さなくてもtimerがflushする。Unicode code pointを分断せず、各eventは1KiB以下かつschemaの2000文字以下。1provider eventが大きい場合も複数batchへ分割する。tokenごとのDB書込みは行わない。1batch保存のrun metadata更新でversionも進み、GET ETagが変わる。

各writeはlive lease/世代、固定入力、現在の会話・account・credential family・session・出力先認可を同じtransactionで確認する。read-only bridgeとは別の最大1件のwrite bridgeを使い、停止済みcallbackはwrite前に拒否する。待機取消はdefault executor終了を待たない。すでに送信されたSDK commit自体をPythonから取り消す保証はなく、並行失効・旧leaseはtransaction競合と再検査で防ぐ。未確定応答を成功とはみなさず、started checkpointの不明結果は従来どおり再生成しない。

最終成功commitは保存済みprefixを最終文字列と照合し、残る文字、最終message、`run.completed`、run/job終端を原子的に保存する。checkpoint replayでも同じ処理を使い、commit応答喪失後にseqやmessageを増やさない。失敗は`run.failed`だけを確定し、成功messageを作らない。handler前にjobがobsolete/failedとなる場合も、認可済みrun GET・次turn受付・SSEのreconcileがrun終端と失敗eventを同時に確定する。

差分は暫定表示であり、`run.completed`まで成功回答とは扱わない。`length`終端は上限内の部分回答を最終messageとして保存し、配信文字の連結と一致する。失敗・timeout・失効・tool未対応では暫定表示を破棄する。終端前のEOFは成功を意味せず、明示再接続またはrun GETで確認する。Core内部のModelResponse、provider_state、署名、tool候補は公開eventにしない。音声/tool/取消の未実装イベントは発行しない。

配送はイベント1個を1batchとし、保存eventの読取り後に新しいtransactionで読者の現在の会話認可と保存済み出力先の現在認可を確認する。認証token期限、private session期限、会話削除、membership/control ledger、device family/接続generation、出力sessionの失効を既存接続にも適用する。正常完了で会話versionが進んだ後の履歴には現在の読取権限を用い、生成時の古い入力版を配送認可に流用しない。共有会話の他の現在memberも会話権限に従って閲覧できる。初回の拒否はheaders前に4xx/503を返す。送信開始後の拒否・基盤障害は本文errorを追加せず接続を終了し、再接続時にHTTP状態を返す。GET/SSE/heartbeat/replayはsession idle期限を延長しない。

履歴期限はrun作成時刻から24時間で固定し、各eventにFirestore TTL用の`expires_at`を保存する。TTL物理掃除の遅延にかかわらずAPIで期限を強制する。期限切れ・次seq欠損・SSE実装前の履歴は410。クライアントはrun GETとmessage一覧で`final_message_id`を取得して表示を置換する。ポーリングで期限を更新しない。TTL設定と本番負荷の調整は配備工程で行う。現在の単純pollは100msごとに認可付き読取を行うため、本番容量を計測せず高負荷保証とはしない。

TypeScript clientの`RunEventCursor`はschema検証済みeventを受け、同じrunの既読seqを無視し、欠番と別runを拒否する。同期描画に成功してからcursorを進める。UIは描画状態と最後のseqを一緒に保存し、その`lastEventId`を再接続headerへ使う。自動retryはなく、401/403では再認証・表示破棄を利用者導線へ反映する。wrapperのdecoded dataは引き続きunknownで、cursorは完全なschema検証の代替ではない。

[T-023/T-032試験](../tests/test_run_events.py)は固定tokenstreamの保存回数、Unicode/schema、時間flush、最終一致、切断再開、期限/欠損、現在認可・token期限、heartbeat、遅延write、checkpoint/commit応答喪失を確認する。切戻しではSSE routeと新規受付を停止し、既存のevents/run/message/jobを保持する。旧runに架空の途中差分を生成しない。
