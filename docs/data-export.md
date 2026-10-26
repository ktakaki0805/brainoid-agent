# 本人のエクスポート

BA-MEM-09 は FR-013 / AC-013、T-001 / T-022 に対応する。`POST /v1/tenants/{tenant_id}/operations` に `kind=export` と `scope=my_data|visible_tenant_data`、Idempotency-Key を渡す。本人の現在の account・membership を検査し、operation・通常の `export_data` job・一時 object の所在台帳を一つの transaction で保存する。同じ本人・tenant・key の再送は同じ operation、scope の変更は409になる。Pi と個人 session は利用できない。

`my_data` は本人所有の private と本人が作成した共有データを対象とする。会話は現在読める親metadataを含め、共有メッセージは本人投稿と本人が開始した turn の assistant 応答を対象にする。`visible_tenant_data` は閲覧可能な共有データも含む。owner/adminにも他人のprivateを許可しない。

## 公開内容と契約

[OpenAPI](../specs/openapi.yaml) の `application/zip` を維持し、圧縮しないZIPの `data.json` に schema_version、tenant/operation ID、manifest、records を格納する。manifest は kind、resource_id、必要な parent_id、version、deletion_epoch と投影・認可metadataのhashを持つ。各対象の現在の読取り権限と、FactのEntity・出典・前版への依存権限を再検査する。raw document をダンプしない。

| 現在のproducer / 読取り境界 | export対象 |
| --- | --- |
| Conversations.create、turn受付 / conversation・messages | 親のtitle/agent/scope/時刻、user・assistantの公開text/role/run/時刻 |
| RunTurn完了 / assistant messages | 公開textのみ。model_message(s)、署名、内部推論、tool_bindings、memory_recordsを除外 |
| Entities / canonical entities | kind/label/aliases/scope/時刻。provider external_refsを除外 |
| MemoryMutations・Memories / canonical facts | 既存MemoryViewの明示field。source_refsは現在の正本から解決・認可 |
| CanonicalSourcesが扱う既存notes | title/body/scope/時刻。suppressed原本は利用者の読取りとモデル再利用を区別 |
| DueReminders / notifications | [本人通知](notifications.md)の公開projection。titleは現在Reminder、状態は宛先receiptから取得し、他人通知・schedule intent・内部grantを除外 |

tool-roleのモデルプロトコル、run/summary/job/checkpoint、資格情報、OAuth/push、membership管理情報、provider設定は本文へ含めない。未実装のAPP/CAL/UI機能は対象producerとして仮定しない。将来producerの追加時は投影・依存検査・purge・manifestを同時に追加する。

走査は正規のFirestore `list_documents(show_missing=True)` からIDだけを50件ずつ取得し、各正本を認可transactionで読む。5000走査対象、200 manifest対象、JSON/ZIPそれぞれ4MiBを上限とし、超過は失敗にする。上限までの部分成功は返さない。受付から24時間でoperationとobject両方の取得期限が切れる。通常jobが失敗した場合は `failed` / `export_unavailable` にし、同じkeyの再送で別artifactを作らない。

## 生成・取得の認可

`exports` policy resource は本人privateのoperation metadataだけを表す。Action.READ、UserSubject、本人owner、outputなしに限定し、各sourceへの権限を代替しない。生成には通常Jobsの現在grant/account/leaseを使う。削除を受理した専用PurgeJobsの権限で生成することはない。

workerは生成前、各source読取り、manifestのstage前、object put前後、最終commitで検査する。外部object I/Oはtransaction外で行う。遅いI/O後や応答前にもGuardedTransactionsの時刻検査が効く。保存済みmanifestに変更があれば公開しない。

一時的なsource認可台帳障害はworker境界で `AuthorizationUnavailable` へ変換し、Jobsの基盤障害回復へ渡す。durable effect開始前は試行回数を返却して同じoperationを再試行し、storage revoke markerを作らない。put完了checkpoint保存後のcommit障害は、その完了receiptを再利用して復帰する。reconcileや消去の二次障害で元の基盤障害signalを上書きしない。durable `started` が未解決なら、put前の再検査で止まった場合も既存Jobs契約に従って `outcome_unknown` とし、再実行せずregistryから消去する。

`GET .../operations/{operation_id}/download` は通常の本人認証を必須にする。削除中の本人が自分の削除状態だけを参照できる例外を流用しない。毎回account・membership・元のgrant・owner・tenant・成功状態・全manifest・両期限を検査し、object読取り後も再検査する。HTTP headerと64KiBごとの送信直前に再検査する。途中失効は残りのstreamを中断する。既に送信したbytesを取り戻すことはできない。公開URL、署名URLは作らず、応答は `Cache-Control: no-store` とする。

forget受付時のtenant purge epochをsnapshotに束縛する。forgetは当該tenantの以前のexport全体を即時に失効させ、間接的なassistantコピーも遮断する。新しいexport生成はそのtenantのforget purge完了を待つ。保持された `suppress_reuse` のmessage/noteはpurge完了後の新exportに含められるが、忘却したFactや消去済み派生コピーは含まれない。scope/version/owner変更、退出、account/tenant/resourceの永続fenceも取得時に検査する。

## 一時保存と物理消去

`export_artifacts/{opaque_operation_id}` はtenant階層の外に置く本文なしの所在台帳である。元の通常job delegation、manifest、期限、digest、状態だけを持ち、最初のputより先に保存する。tenant purgeが `exports` metadataを先に消しても、外部objectを見失わない。

`ExportObjects` は暗号化されたprivate一時領域の専用portで、既存の独立WORM認可/削除台帳とは別物である。putはimmutableかつ冪等、revokeは削除前に永続tombstoneを保存し、遅いput・再配送・process再起動でも同じkeyが復活しないことを要求する。未知のput応答を成功と推測せず、既知registryから回収する。

提供する `LocalExportObjects` は明示された専用directory（0700）と256-bit鍵を使う。AES-GCMでmetadataとZIPを認証付き暗号化し、flock・fsync・atomic replaceと永続revoke markerで競合を制御する。暗号文・lock/markerは0600。鍵の自動探索、WORM directory/bucketの流用、本番GCS接続は行わない。これはlocal adapterの検証であり実GCSの検証ではない。

`Exports.invalidate` をPurgeResourceの `export` hookへ接続する。forget/退出/account/tenant削除のすべてで同じ `operation:export:v1` keyを使用し、objectの永続revokeと物理消去を終えてからreceiptを返す。一回最大10ページでcursorを `export_erasure` に保存し、長い回収はpurge retryで続ける。forget後に受理された新exportや正規rejoin後の新delegationを旧削除意図へ巻き込まない。

`ExportReaper.step()` は50件ずつ所在台帳を走査し、`export_maintenance/reaper` にcursorを保存する。失効・期限・孤児・失敗jobを、download要求がなくても回収する。通常jobの失効済みgrantに依存しない、既知一時artifactだけの削除権限で動く。途中障害はcursorを進めず再試行し、各pass完了後は先頭へ戻る。revoke済みregistryとstorage tombstoneは遅延putの拒否のため保持する。削除receiptは本文を含まない。

## 明示local構成と運用境界

現在、APIの標準起動でexport backendは有効にしない。未構成の作成・downloadは503になる。fake/emulatorのcompositionは次を一組として注入する。

1. `Exports(normal_jobs, temporary_objects, FirestorePurgePages(client), CanonicalSources(SavedMessages()), now=clock)` を構築する。
2. `AuthServices.exports` に `ExportRequests(exports, tenants)` を注入する。
3. 通常workerに `export_data: ExportData(exports)` を登録する。
4. forget用とlifecycle用の両PurgeResourceに `hooks={"cache": configured_cache, "export": exports}` を渡す。実export backendのある構成で `NoExternalCopies` をexport hookへ渡さない。
5. 信頼されたmaintenance runnerが `ExportReaper(exports).step()` を定期実行する。全registry passが24時間以内に完了する実行頻度・処理能力を設定し、失敗と最古期限超過を監視する。localテストはこのrunnerを明示呼出しする。

実非公開一時bucket、鍵管理・IAM・scheduler/監視・負荷/索引・実ファイル取得の結合はBA-OPS-02等の外部前提であり未実施。課金project、外部設定、deployを推測しない。この全構成と実環境検証が揃うまで本番受付は有効にしない。スマホ導線はBA-UI-12、公開共有リンクは対象外。

## 移行・切戻し・検証

既存forget/lifecycle Operationは任意fieldを追加せず従来JSONのまま扱う。exportのみdownload_expires_atを返し、失効時にerror_codeを返す。既存台帳形式や認可fenceは変更しない。新しいexports/registry/jobを作るだけで既存データのbackfillは不要である。

切戻しでは新規受付と生成を停止し、旧URLを拒否したまま、現在版reaperとpurge hookを全artifact消去まで維持する。先にregistryや鍵を消さない。鍵紛失・object領域障害は取得不可とし、保存成功を推測しない。storage revoke markerの掃除は全遅延producerを停止したことを証明できる将来の保持設計が必要になる。

fake/HTTP試験は本人/tenant境界、owner/adminと他人private、ZIP投影、遅延I/O、期限、各stream境界、scope変更、物理purge接続、再起動後の回収を扱う。実Firestore試験は同時受付、commit応答喪失、生成中forget、manifest変更、tenant purgeと独立registryを扱う。検証コマンドは `mise run setup`、`mise run check`、`mise run test`、明示emulator設定で `mise run test-emulator`。実行結果の正本は[作業ログ](implementation-log.md)とする。

BA-APP-01の[ノートproducer](notes.md)は既存note公開投影へ接続する。noteのdelete_resourceもpurge_epoch失効・生成待機・export消去hookの対象とする。
