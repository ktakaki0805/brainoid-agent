# 本人確認後のCalendar実行

BA-CAL-04 / FR-010 / AC-010 / T-015・T-016・T-037。固定[proposal](calendar-proposals.md)を本人が確認し、非同期workerがcreate/update/deleteを実行する。検証はsynthetic providerとFirestore Emulatorで独立して完了させる。実Google書込には変更してよい専用calendarとread_write委任が必要であり、このチケットでは実provider・cloud・実機を操作しない。確認UIはBA-UI-08、招待送信と系列一括操作は対象外。

## 受付と公開

`POST /v1/tenants/{tenant}/calendar-proposals/{proposal}:confirm`は本人user bearerとIdempotency-Key、`{"args_hash":"...","confirmed":true}`のみ受け付け、202 Operationを返す。Pi bearer、model tool、run/tool/checkpointや変更内容の入力は拒否する。current owner/account/member/tenant/ledger、原producer/session/output/source、選択calendar、実付与済みWRITE、固定hashと初回5分期限を同じtransactionで検査する。期限到達は新規受付を拒否する。受付済み同一proposalへの同key・別key再送は現在の権限検査を通して同じoperationへ収束し、期限を延長しない。workerは受付後に5分が過ぎても現在権限があれば処理できる。

operation IDはproposal IDで固定し、canonical proposal確認状態、本文なしのoperation、Jobs outbox、receiptを同一transactionで保存する。proposal grantのversionをその変更と同時に束縛する。run由来は既存run/tool/checkpointだけからExternalClaimを作り、RUN07の結果受付に接続する。producer保存後・suspend前のconfirmはclaimを保持し、suspendが両者を結合するまでwrite jobは副作用を開始できない。APIからworker内部への依存はない。

operation GETは現在の本人と元proposalの出典を検査し、ReleaseGuardをHTTP threadpool復帰まで保持する。Calendar operationと判定した後の元proposal不在・抑止による404やsource検査の可用性エラー503は、そのまま返す。他のoperation readerへ流さない。Calendar未構成時も既存memory readerは他kindを404で拒否する。proposal GET/exportにもoperation_idと状態、固定error_codeだけを追加する。結果本文・token・secret参照をoperation/jobに複製しない。原出典が失効したrunは再開せず、内容なしの実行事実だけを保持する。現在権限のない閲覧を過去のconfirmによって許可しない。

## 実行と照合

明示構成した`CalendarWrite`を`calendar_write` handlerとして登録する。APIの`AuthServices.calendar_write_enabled`は既定falseで、worker/providerを用意したcompositionだけが有効化する。default runtimeに暗黙のcredential探索や実書込接続はない。

workerはcurrent source/output、元run、job leaseを検査し、secret読出し後とprovider参照後にも検査する。実ACLとupdate/delete対象の現在ETag・対象外属性を再読取する。STARTED checkpointと送信許可は別物であり、STARTED保存後も送信直前に現在認可を再検査する。取消・失効後に新しい副作用を開始しない。provider I/Oはtransaction外。固定HTTPS endpoint、opaque IDのpath encoding、10秒timeout、redirectなし、transport/auth retryなし。

createはoperationから導くbase32hex互換の安定IDを使用し、private extended propertyにtenant/operation/固定hashのdigestを保存する。updateは固定calendar/eventとIf-MatchでPATCHし、確認対象のsummary/description/start/endを変更する。他の既存属性とprivate propertiesは保持する。description省略は空へ変更する。deleteは固定対象とIf-Matchを使う。すべてsendUpdates=noneであり、参加者・系列・特殊eventType・locked等は直前にも拒否する。

成功したcreate/updateはIDだけでなくoperation marker、正規化内容、更新ETagを照合する。409/412はconflict、確定的provider拒否はfailed。timeout/5xx/不正応答は、現在認可のもとで一度read-only照合する。markerが同じでも内容が違えばoutcome_unknown。同じIDの存在や404だけを成功としない。deleteの204は成功事実だが、応答喪失後の404/410は他者の削除やACL喪失と区別できずoutcome_unknown。deleted eventはmarker保持が保証されないため、削除成功を推測しない。

実応答が返った後のlease喪失・取消・失効でも、実行済み結果を内容なしの事実として記録する。確認済み失敗や成功を後のunknownで上書きしない。unknownの後に強い成功証拠が得られた場合は現在のoperationとproposalを改善し、双方の古いerror_codeを除去する。runへ渡した結果は一回消費し、unknownは既存RUN07どおりrunをfailed/outcome_unknownで終端にする。後の改善で過去の結果を変更したりmodelを再開し直したりしない。

## 停止・回復・消去

プロセスがSTARTED後に停止するとJobsは期限切れjobをfailed/outcome_unknownへ進め、通常dispatcherは再配送しない。この経路をWorker.finallyだけでは回復できないため、scheduler/recovery担当は`CalendarWrite.recover(tenant, pages, after=cursor, limit=100)`を明示的に呼び、返ったcursorを終端まで追う。これはIDだけのjob inventoryを最大100件ずつ走査し、canonical operationを読んで未確定のprovider状態をread-only照合し、必要なRUN07結果を一度配送する。mutationを再試行しない。schedulerのcloud配備は後続運用接続で行う。

JobsのSTARTEDとoperationの送信開始markerの間で停止した場合、marker不在は外部mutationが未送信だった証拠になる。recoveryは同一transactionで現在jobが終端、またはSTARTED/試行回数上限により再配送できない期限切れleaseであることを確認し、operationとproposalをfailed/not_startedへ収束させる。pendingの再試行、live lease、suspendを待つ早期confirmは、この条件で失敗にしない。marker保存済みは従来どおり結果不確定として照合する。runが取消・失効済みなら結果を記録しても再開しない。

受付・結果commitの応答だけを失った場合もcanonical receipt/operationを再読取する。実行中snapshotから消去済みproposalを復元しない。新collectionは作らず、既存calendar_proposals・jobs・tool/run履歴のbounded export/purge inventoryを使用する。operationsは既存の内容なし復旧制御記録として保持する。削除・認可台帳を復元後に再適用するまで実行を再開しない。

新しいデータ移行や依存追加はない。旧CAL03 proposalは既存owner metadataをjob認可へ投影し、固定hashと現在出典を検査する。rollbackでは先に新規confirm受付を止め、未開始jobを取消/失効に収束させ、STARTEDはこの版のread-only recoveryで照合する。旧workerへ未解決writeを渡して再実行しない。結果の成功はGoogle側変更をrollbackした意味ではない。新形式を持つproposalと結果のexport/purgeコードは残存データの処理が終わるまで維持する。

通常createはACL確認1回とwrite1回、update/deleteは対象GETが追加される。曖昧結果はGETが追加され、recoveryは呼出しごとに最大100jobを読む。PATCHはGoogleの3 quota unitsを消費する。fake/Emulatorの検証は実Google ACL・quota、staging索引・性能・費用や実端末UIの検証を代替しない。

一次資料（2026-10-27確認）: [insert](https://developers.google.com/workspace/calendar/api/v3/reference/events/insert)、[patch](https://developers.google.com/workspace/calendar/api/v3/reference/events/patch)、[delete](https://developers.google.com/workspace/calendar/api/v3/reference/events/delete)、[条件付き変更](https://developers.google.com/workspace/calendar/api/guides/version-resources)、[エラー](https://developers.google.com/workspace/calendar/api/guides/errors)、[extended properties](https://developers.google.com/workspace/calendar/api/guides/extended-properties)。
