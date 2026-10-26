# 忘却の物理消去と再開

BA-MEM-08 / FR-013 / T-011・T-022。受付契約は[忘却受付と削除台帳](memory-forgetting.md)、認可と保持は[データ設計](03-data-and-authorization.md#8-削除忘却保持)を参照する。忘却の入口は `forget_memory`。退出・アカウント・テナント削除は[BA-IAM-10の受付と専用scope handler](account-deletion.md)が同じwalkerを再利用する。会話削除の受付、Calendar credential、通知配送、export生成、端末cache同期、backup復元訓練は提供しない。

## 実行と権限

`PurgeJobs` は独立 `DeletionLedger` と受付済みoperation、正確なFact tombstone・target・epochを検査する削除専用executorである。元requesterのmembership/account失効やFactの物理消去後も受理済み意図を完遂する。通常の `Jobs` の認可、provider実行、tool権限は変更しない。専用workerには `memory_purge` だけを登録し、専用queueで配送する。caller検証、dispatch generation、lease generation、lease期限は通常workerと同じである。削除executorはGuardedTransactionsで旧lease期限のno-I/O検査を全write・callback完了・応答releaseまで保持する。台帳/DB読取り中の期限切れはwriteを拒否し、commit後の応答遅延は確定済み進捗を保持して旧leaseでの次step/hook/完了を止める。

各transactionで独立台帳の不変recordを読取り照合する。台帳追記・invalidationはtransaction外。独立台帳が読めない場合は実行しない。自Fact markerはoperationと完全一致が必要。残すsourceは先行forgetのsuppress markerを共有でき、後続のdelete markerへ強化されても先行purgeを止めない。sourceを消去できるのはそのsourceを `delete_source` として受理した意図だけで、現在のcanonical tenant・ID・版・authorも検査する。

履歴経由の推移依存も消去する。対象Factを直接使用したrunを見つけたstepで、その会話・最小input sequence・起点run IDを `operations/{id}/purge_conversations/{conversation_id}` に本文なしで保存してからrunを縮退する。後続passはこのmarkerを使い、受付前に該当履歴を消費し得た後続turnのrun/events/job/assistant本文も処理する。走査順で起点を後から見つけても再走査で回収し、markerの新規保存も変更ゼロ確認をやり直す理由にする。自己forgetで直接snapshotを消去したrunは、同operationに束縛された `memory_forget_effects` の正規proofから選択する。

自己forgetは固定応答に移る前に、同じtransactionで全 `memory_records` / 正規 `ResultBinding` の消去依存を `purge_dependencies` に退避する。保存するのはschema・tenant・run・Fact ID・version・deletion epochだけで、本文・owner UID・model再利用権限は含めない。これは別Factの自己forgetでも失ってはならない証拠であり、model用snapshotは従来どおり空にする。runを物理縮退するときも全Factの依存と正規tool IDを保持する。最初の `purged_by` は安定させ、重複operationは残した依存で祖先を再検査する。A/Bを交互に走査してもstatus markerを書き換え続けない。

旧自己forget runには次の互換処理を適用する。残っているcanonical tool receiptは、tenant/run/tool ID/effect IDと構造化 `MemoryRecord` を検査し、本文なし依存をrunへ同一transactionで移送してから消す。receiptで分かる部分集合を `complete=true` に昇格させない。新しい自己forget時の完全manifestだけが完全性を示す。旧model checkpointは生成 `Outcome` しか持たず、失ったdirect contextのFact IDを復元できない。本文やUUID部分一致で推測せず、対象が証明できない旧runは消さず `legacy_provenance_missing` としてoperationを未完了にする。

未知の旧runに出会ってもbounded scanを続け、当該operationが正当に選べるcopyは消去する。その範囲の変更ゼロ確認とcache/export両hookが済んだ場合だけ内部 `purge.locally_verified=true` を保存する。これは公開 `succeeded` ではなく、不明依存が残ればcursorを先頭へ戻して再配送する。既存count・receiptは保持する。別operationが縮退した旧runを解決済みと扱うには、その別operationの独立immutable ledger、受付operation、専用job、exact tombstone、確認pass・両hook receipt、runの正規依存または自己forget proof、本文なし状態まで再照合する。bare DB flagや別backendで読めないintentは証拠にしない。新たな変更があればlocal確認を取り消す。この仕組みで複数旧自己forgetも各自の正当な消去を進められ、相互待ちを解消できる。未知の依存がすべて解消するまでは完了を返さない。

旧workerの `verified/succeeded` だけは新しい確認証拠に置き換えない。既完了のoperationが未知依存の解消を妨げる場合、運用側は `PurgeJobs.revalidate_completed(tenant_id, purge_job_id)` を明示実行してから通常dispatcherへ渡す。この入口は独立intent・受付・exact tombstoneを再確認し、旧版の成功した `memory_purge` jobだけを再開する。受付時の範囲・countは保持し、旧verificationと両hook receiptは無効にして現inventoryを再走査・再照合する。通常Jobsのterminal規則は変えない。旧runが縮退済みでもproposal等が残っていればAの成功を拒否し、正規B権限の再検証で残存copyを除去してからAを再開する。

forget受付は `purge_barriers/history` のepochを増加し、turn受付は同じdocumentをtransaction内で読んでrunへ `purge_epoch` を保存する。競合時はcallbackを再実行するため、同じ時計時刻でも受付後の新turnは新epochとなる。複数forgetではepochが単調増加し、各operationの `purge_admission_epoch` より前のrunだけ履歴範囲へ含める。既存runにepochがない場合は保存済み受付時刻を使用する。barrier導入前のreceiptにはepochを推測して書き足さず、新turnが保存する `history_run_ids`（実投影に含まれたassistant/toolのrun IDだけ）で旧起点の消費を確認する。保持した直接user原文のIDから依存を捏造しない。旧run/旧receipt両方の時刻境界と新turnの同時刻負例を検証する。

受付時にmessage親の `next_sequence - 1` を `purge_bounds` に保存し、親の同時更新とtransactionを競合させる。独立台帳I/O待ち中に旧sourceを消費したrunは含み、受付後の独立した新turnは含めない。旧受付recordや合成fixtureに `next_sequence` がない場合は、保存済み受付 `operation.updated_at` を初回進捗に固定して使用する。intentの作成時刻を受付完了時刻の代用にはしない。

## 現行の保存先と処理

| 保存先 | 選択と処理 |
| --- | --- |
| `facts/{id}` | intentの1Factと全子階層を物理消去。tenant・ID・owner・版を検査 |
| `notes/{id}`、`conversations/{id}/messages/{id}` | `delete_source` の正確なtargetだけ消去。`suppress_reuse` の元本文・UI履歴は保持 |
| `memory_vectors_<configSHA32>/{fact_id}` | tenant配下のcollection IDをページ列挙し、全世代の該当IDと子階層を消去。稼働中の1モデル設定には限定しない |
| `conversations/{id}/summaries/{id}` | canonical conversation IDとincluded message manifestから対象source由来の要約を消去 |
| 会話のassistant/tool messages | `memory_records`・tool resultの正規参照または対象sourceを消費し得た受付前runから選択。本文、model_messages、署名、依存snapshotをdocumentごと消去 |
| `runs/{id}/events` とrun内payload | 対象runの子階層を先に消去し、runを本文なしの `failed/source_deleted` と `purged_by` に縮退。continuation、memory_records、tool_bindings、streamed/completed textを残さない |
| `jobs/{id}` | `grant.resource.ref`、summary manifest、対象runで選択。obsolete化しleaseを閉じ、checkpoint/resultを消去。purge job自身と別operationのpurge jobは保持 |
| `tool_executions/{id}`、`run_proposals/{id}` | 正規resultのmemory参照・canonical runから選択し、結果・tool requestと子階層を消去 |
| `memory_effects/{id}` | `record` または `previous` の対象Fact snapshotを持つeffect proof全体を消去。別の後継Fact原本を連鎖削除しない |
| 削除tombstone、削除台帳、forget reservation、operation | 永続fence・再開情報として保持。物理原本消去後も同じIDの再作成を拒否 |
| `idempotency_records`、`memory_forget_effects`、監査 | 本文を持たない識別子・hash・固定結果を保持。本文を持つmemory effectとは区別 |

同じ会話の直接user原文、別tenantの同じID、無関係な共有寄稿は派生判定だけでは消さない。依存証拠だけを消して本文を再利用可能にする処理は行わない。会話parentを消去しなくても選択済み子documentは処理し、既に存在しない親の配下も走査する。

将来の通知preview・DB cache・export metadataには `ForgetHandlers({"notifications": ReferenceCopies(), ...})` で `notifications` / `caches` / `exports` に限定したcollection handlerを登録できる。この拡張契約は `tenant_id` と `purge_sources: [{kind, resource_id, parent_id}]` を保存する派生copy専用であり、実サービスの既存schemaを仮定していない。登録producerは作成時にcanonical sourceのtombstoneを検査する必要がある。原本・資格情報・任意collectionをこの登録点へ渡せない。

## ページ・再試行・完了

`FirestorePurgePages` は固定SDKの `ListCollectionIds` と `ListDocuments(show_missing=True)` の1ページだけを取得する。`page_size` は1〜100、既定50。後者はdocument本文のないancestorを含む。Firestore SDKと同じRPC metadataを渡し、本文はprojectionで取得せず、削除判定はtransaction内のcanonical読取りで行う。全collectionを読んでsliceするpagingではない。

operationの内部 `purge` に階層stack、各ページtoken・残りpath、processed数、pages数、passes数、固定retry_reason、hook receipt、overdue、verifiedを保存する。保存cursorを新しい削除権限として信頼せず、tenant・原本targetまたは正規参照で選択した派生ancestorの範囲を各stepで再検査する。各stepはlease・最新operation進捗を再検査し、削除/縮退と進捗保存を同一transactionでcommitする。途中停止・commit応答喪失後は保存済み進捗から継続する。pageを削除してtoken境界が移動した場合に備え、全走査後にcursorを最初へ戻し、変更ゼロの確認走査を終えるまで完了しない。原本の子階層はpostorderで処理する。

別の受理済みpurgeが選択済み派生ancestorを先に消した場合、祖先欠損を削除権限として扱わず、残ったdocument/collection cursorを読取だけで走査する。空の枝では進捗だけを進め、processed数を増やさない。実在する子documentが見つかれば消さず `derived_ancestor_missing` で未完了にする。各commitで祖先を再検査するため、走査中に祖先が変わっても旧選択で消さない。確認passも登録済み保存先のghost祖先を辿り、古いpage位置より前に残る根拠不明childを単純skipしない。正確なaccepted原本/vector IDの子と、生存する正規派生ancestorの既存束縛は別途維持する。

既定の1deliveryは最大1,000 step。予算到達は `page_budget` としてattemptを返却して再配送する。handler失敗、DB/台帳障害、cache/export失敗も本文を含まない理由をoperationに残し、完了扱いにしない。process停止によるlease期限切れも専用 `recover_expired` が独立intentを再確認してattemptを返却し、`purge_recoveries` と `lease_expired` を保存する。旧版が `attempts_exhausted` にしたpurgeも同じ入口で回収する。startedを含む外部effect checkpointがあるjobは回収せず、通常Jobsの試行上限・unknown規則は変更しない。通常jobの外部呼出し `started/outcome_unknown` の規則を緩めない。

cache/export hookは必須の明示注入で、`operation_id:cache:v1` / `operation_id:export:v1` を冪等keyとする。hookはすべての対象copyの消去・失効を確認してから本文なしreceiptを返す。応答喪失時は同じkeyで安全に再照合・再実行できる削除adapterに限定する。失敗中はoperation succeededにしない。`NoExternalCopies` は当該backendを持たない現在のローカル構成を明示するadapterで、遠隔objectやoffline端末の消去を主張しない。backendを導入する前に実adapterへ交換する。

既存embedding/summary/runのwrite pathはcanonical tombstone・source版・現在leaseを再検査する。purgeは対象jobもobsolete化するため、古いdeliveryや進行中provider結果から派生本文を復活させない。確認走査が収束しない場合は完了させず、監視と原因修正へ回す。

## 構成と監視

以下はcomposition rootに渡す実装入口。既存credential・queue設定を明示注入し、インポート時にclient/credential/外部資源を生成しない。

```python
from brainoid_jobs import Jobs
from brainoid_persistence.firestore import FirestoreTransactions
from brainoid_persistence.purge import FirestorePurgePages
from brainoid_worker.app import create_app
from brainoid_worker.entry import Worker
from brainoid_worker.purge_authority import PurgeJobs
from brainoid_worker.purge_resource import PurgeDispatcher, PurgeResource

store = FirestoreTransactions(configured_firestore_client)
normal_jobs = Jobs(store, control_ledger)
jobs = PurgeJobs(normal_jobs, deletion_ledger)
handler = PurgeResource(jobs, FirestorePurgePages(configured_firestore_client),
                        hooks={"cache": cache_invalidation, "export": export_invalidation})
worker = Worker(jobs, worker_caller_verifier, {"memory_purge": handler})
app = create_app(worker)
dispatcher = PurgeDispatcher(handler, store, dedicated_queue)
# Tenant inventory/scheduling is owned by the deployment, not request payloads.
page = dispatcher.recover(tenant_id, limit=100)
monitoring = handler.monitor(tenant_id, store, limit=100)
```

workerにはHTTP向け例外へ変換するAPI adapterでなく共通persistence adapterを使う。schedulerはrecoveryとmonitorの `next_cursor` を進め、全tenantを一巡して繰り返す。`monitor` は未起動・失敗済みoperationも走査し、24時間を超えた未完了を `purge_monitor.overdue=true` にする。運用はこの状態、固定retry_reason、進捗、最古ageから通知する。通知先・実Scheduler・実cloud監視の作成は本チケットでは行っていない。

## 検証・展開・戻し方

`tests/test_memory_purge.py` はページ途中停止、lease競合、再配送、requester失効、別tenant/author、残すsource、全世代vector、階層残存、hook応答喪失、受付境界、old embedding/summary生成競合、24時間監視を合成データで検証する。`tests/test_purge_cross_forget.py` は通常API/RunTurnでdirect context・検索結果を使って別Factを自己forgetし、処理順・交互page・部分legacy証拠・不明legacy回復・hook失敗・別ledger負例を検証する。`tests/emulator/test_purge_integration.py` は実SDKのページ境界・欠損parent・commit retry/応答喪失・並列leaseを検証する。共通の実行は `mise run setup` / `mise run check` / `mise run test`。`.env.emulator.example` を明示export後、`mise run test-emulator` でAuth/Firestoreの統合検証を行える。実provider・実cloud・backup復元は対象外。

`tests/test_purge_overlap.py` は正規HTTP/RunTurnのreceiptを持つA/Bについて、Aの子階層cursor停止→B完了→Aの通常recoveryを検証する。document/collection、ページ順、未知child・tenant/原本cursor負例を含む。receipt下のcopyだけは子階層契約を試す合成fixtureであり、現検索producerが作成するという主張ではない。実Firestoreの境界testは型付きcanonical metadata fixtureを使い、祖先消失後の `show_missing` と空枝再開・残存child拒否を確認する。

受付recordの追加metadataとturnのpurge_epoch/history_run_idsは内部の後方互換fieldで、公開Operation/Run DTOは変更しない。standalone note/conversation削除の共通authorizerは未提供で、APP-01等は独立typed intentと受付束縛を追加して共通walkerへ委譲する。架空Factを追加して既存forget型を流用してはならない。既存waiting envelopeはactivation時に独立intentとtombstoneを照合する。展開ではhandler/hookと専用queueを構成後にrecoveryを起動する。停止は専用queue/dispatcherを止め、既存tombstone・ledger・operation進捗を保持する。物理消去した本文はrollbackできない。旧binaryへ戻してもtombstoneを外してはならず、旧binaryには削除完了権限を委譲しない。進捗schemaを理解するworkerを再投入し、同じoperationを継続する。
