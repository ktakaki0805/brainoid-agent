# 忘却受付と独立削除台帳

BA-MEM-07はFR-006/FR-013の忘却受付を提供する。`POST /v1/tenants/{tenant_id}/operations` は忘却として `kind=forget_memory` を実装し、`target_id`、`expected_version`、`Idempotency-Key` を必要とする。`source_policy` の既定は `suppress_reuse`。`202` と `status=pending` は検索・モデル再利用停止の受付確定を表し、物理消去完了ではない。本人は同じtenantのoperation GETで状態を確認できる。物理purgeはBA-MEM-08が提供し、退出・account・tenant削除の受付は[BA-IAM-10](account-deletion.md)が提供する。復元時の一括台帳適用、UIは後続チケットである。

## 受付と障害復旧

現在の本人認証、account、membership、端末/session、出力scope、FactのWRITE権限と期待版、sourceの現在の親・scope・WRITE権限を検査する。message全体の抑止も本人の原発言に限定し、READだけから他人の再利用を変更する権限を導かない。private Factからshared source全体へ抑止範囲を広げない。sourceを削除するときはsource親のWRITE権限に加え、本人による原発言/原本である証拠が必要で、tenant ownerの管理権限だけで他人のsourceを削除しない。

1. actor・keyから安定したoperation IDを導き、正規化要求hashを束縛する。Factの `memory_forget_reservations` とsourceごとの `source_forget_reservations` を同transactionで予約する。予約は期限で解除しない。Factの訂正・撤回と別の忘却は予約を尊重し、同sourceへの同時忘却も直列化する。現在のsource writerは忘却だけであり、将来のsource編集もこの予約を確認する必要がある。
2. transactionの外で、独立Objectsへ `deletion/v1/operations/{operation_id}.json` をcreate-onlyで記録する。authz台帳のintent/commitとは別プロトコルで、削除意図は権限証明ではない。本文・UID・認証値は含めず、opaque ID、版、epoch、source方針、要求hash、時刻を保持する。既存のlocal fsync/atomic publicationまたはGCS generation=0 transportを使用する。
3. 現在の権限と予約、全source版を再検査して、独立した `deletion_tombstones`、Factのdeleted/epoch、source抑止または削除、operation、purge envelopeを同transactionで確定する。これらが揃って初めて202を返す。

台帳失敗は503。台帳成功後のDB失敗、予約commit/受付commitの応答喪失は同じactor/key/bodyで再試行する。認証期限や失効は毎回検査する。予約と台帳のhash/対象が異なる要求は409、期待版競合は412。同キーで古いDB snapshotから予約を再構築する場合も既存の台帳時刻・operation IDを保持する。認可を失ったrequesterの受付再開は拒否し、削除意図は残る。DBを外部から書き換えて予約対象の版を変えた場合はfail closedとし、予約をTTLで解除して別要求へ譲渡しない。

APIは既存 `AUTHZ_LEDGER_DIRECTORY` / `AUTHZ_LEDGER_BUCKET` の独立制御storageへ別prefixで記録する。環境間のfallbackはない。workerは同じstorageの `DeletionLedger` を `RunTurn` / `text_worker` に注入する。未構成なら忘却は成功しない。新しい外部サービスや課金providerは不要である。

## 再利用と履歴表示

現SourceRefには安全なspan証明がないため、対応するmessage/note全体を抑止する。`suppress_reuse` は本文をUI/exportに残すが、query、context、再抽出、summaryには使わせない。source版を増加し、直接userの新しい発言だけを残す既存の履歴projectionを通す。既存summaryと生成中summaryはsource manifest不一致で破棄する。`delete_source` は本人sourceにもdeleted/epochを設定し、UI/export対象から外す。

独立tombstoneはtenant・種別・parent・IDから安定導出したIDで保存し、本文を持たない。古い正本documentを再配置してもpolicy、CanonicalSources、履歴、summaryの同transaction fenceを通す。Factを物理削除した後も同IDの再作成を拒否する。Factの正規markerはdeleteのみであり、誤ったsuppress_reuse markerでもUI/export参照を許可しない。export実装は `brainoid_contracts.deletion.exportable` を、現在の所有者/scope認可と併用する。保持したsourceの抑止をtombstoneの期限切れで解除しない。

## 型付きtoolと実ユーザー意図

`memory.forget` は既存tool schemaのUUID・期待版・source方針を検証する。canonical direct-user発話と現在の会話scopeを確認する。初版の明示文法は `Forget memory <UUID>`、`Please forget memory <UUID>`、`記憶 <UUID> を忘れて`。`delete_source` は末尾 `and delete its source` または `記憶 <UUID> を元の出典ごと忘れて` を必要とする。引用、質問、否定、別対象、検索結果にある命令、モデル引数だけでは承認しない。より広い自然言語は今後の確認workflowで扱う。

予約時と台帳I/O後のforget transactionの両方で、当該runの全memory snapshots・tool結果・出典・history・summary・期限を通常の現在状態検査に通す。台帳I/O中に別operationによる忘却・訂正・期限切れが起きた場合は対象の受付commitを拒否し、古い証跡を消去してrunを成功にしない。記録済みintentと予約は未確定受付として残す。

成功時は同じforget transactionで、当該run/effect/operationに束縛した本文なし証拠を保存し、検査済みの旧memory snapshots・tool bindings・continuationを除去、抑止後のhistory hashを設定する。以後モデルを呼ばず、独立operationと自己effect証拠を再検査した固定の受付通知で終端する。同effect再試行も再束縛後の現在状態を通常検査する。外部forgetを受けた他runの古いcontextには例外を設けない。

snapshot除去と同じtransactionで、全消費Factのtenant/run/ID/version/deletion epochを本文なし `purge_dependencies` に保持する。別Factの忘却処理が、このrunのmodel checkpoint・既にstreamした本文の消去根拠を失わないための記録であり、モデル再利用や通常Jobsの認可を許可しない。旧runの不明依存、部分receiptの移行、複数operationの消去確認は[物理消去の互換契約](memory-purge.md#実行と権限)に従う。

通知は「再利用停止・消去処理待ち」を示し、完了や完全に同じ事実を知り得なくなる保証をしない。忘却以前に既にUIへstreamしたprefixは取り消せないためその表示を保ち、追記する通知には忘却内容を再記載しない。最終assistant message全体を再利用抑止し、保存済みprefixや応答を後続モデルへ再提示しない。台帳/DBが未確定なら成功通知を生成しない。

## Purge引継ぎ・互換・切戻し

`memory_purge` jobは `status=waiting` と `attributes.dispatch_queue=memory_purge` の未activation envelopeである。一般dispatcher/workerは配送・処理しない。通常JobのFact grantは受付時の監査情報であり、削除後のsourceや元actorの現権限を再認可するpurge権限として使用しない。

BA-MEM-08は独立intent・operation・tombstoneの一致から対象ID/epochに限定した削除実行権限を構成し、専用queueをactivationする。元requesterが後に失効しても既に受け付けた削除を完遂し、子階層/vector/summary/cache等の処理を確認するまでoperationをsucceededにしない。本文の再生成や対象追加をこの権限で許可しない。BA-OPS-07は復元後、最新台帳の適用完了までサービスを開放しない。

HTTP/toolの既存request shapeを保持した追加実装である。新しい会話messageには本人 `created_by` を保存し、旧messageは対応するcanonical run/jobのinput identityとinitiatorから本人性を確認する。証明できない旧sourceの削除は拒否する。

切戻し時に予約・tombstone・台帳・source抑止・operationを削除しない。旧版は独立tombstoneを認識しないため、受付機能とmodel/workerを停止した状態で戻し、最新fence対応版へ戻すまで再開しない。実GCS retention/IAMと本番復元は本チケットでは変更・実行していない。

## 検証

`tests/test_memory_forget.py` はT-011/T-022の台帳前後/DB前後障害、応答喪失、同キー再試行、独立台帳からの同ID復旧、権限・scope・intent負例、即時検索/summary/history抑止、固定tool終端を検証する。`tests/emulator/test_forget_integration.py` は実Firestore transactionの競合・予約とimmutable local ledgerの組合せを確認する。GCS transportは課金接続を使わず既存のSDK HTTP fakeで検証する。

物理消去の提供済み範囲、専用executor、ページ再開、cache/export hookと監視は[忘却の物理消去と再開](memory-purge.md)を参照する。受付済みoperationは関連する原本・派生情報の確認が終わるまでpending/runningのままで、MEM-08 workerがsucceededへ更新する。
