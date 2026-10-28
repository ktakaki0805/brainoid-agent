# 期限走査と通知配送

BA-APP-04 / FR-009 / AC-009 は [通知の正本](../packages/memory/src/brainoid_memory/notifications.py) と [期限走査・worker](../services/worker/src/brainoid_worker/notifications.py) を提供する。固定時計・fake配送先とFirestore emulatorで動かし、BA-APP-05のFCM adapter・通知HTTP APIを下記に接続する。Pi同期はBA-DEV-04、UIはBA-UI-07、実Scheduler資源はBA-OPS-03で扱う。新しい依存やworkspace memberは追加しない。

## 走査・保存・版

`DueReminders(jobs, pages).scan(tenant, limit=100)` はID順に最大100件を読み、`status=scheduled` かつ `due_at <= now` の正本を同transactionで再検査する。tenantごとの `notification_scans/due` にlease token・期限・cursorを保存する。同時走査はbusyを返し、停止後はlease期限で同じcursorから再開する。各候補のtransactionは現在leaseと認可を読み、通知・宛先receipt・配送job/outboxを原子的に作る。queue/providerはtransaction外でだけ呼ぶ。

通知IDは `UUIDv5(reminder_id, "notification:" + schedule_version)`。宛先receiptのキーはkind/idのfingerprintとし、opaque UIDをpathへ埋め込まない。本文は通知・job・receiptにコピーせず、本人の取得時に現在のReminder.titleを読む。

通知sourceはversion=1の固定世代記録で、mutableなreceiptを `notifications/{id}/deliveries/{target-fingerprint}` に分ける。jobはこのsourceの厳密なresource version・scope・主体stampへbindする。既存Jobsのsource.version検査を弱めず、workerが古いgrantを現在版へ書き換えることもない。title編集だけなら同じ通知を使い、日時変更・取消・snoozeは `ScheduleReference/current_schedule` により旧通知を利用不可にする。

公開Notificationのversionはこの固定source版とは別で、通知作成時に必ず1から始まる。receiptには `projection_version=1` と作成時の正本 `reminder_version` を保存する。公開版は `projection_version + 現在Reminder.version - receipt.reminder_version`、公開updated_atは現在Reminderとreceiptの更新時刻の新しい方である。title編集は本文を複製せず公開版へ反映され、同じ時計の連続編集も区別できる。provider結果・初回displayed・初回acknowledgedを保存するときは、その時点の公開版+1と同transaction後のReminder版をreceiptへ記録する。受付/本人確認によるReminder更新を二重計上せず、source/grantは1のまま保つ。offlineの確定もreceipt更新として版を進め、unknownは確定まで進めない。同key再送と別keyによる同じackの再確認は更新せず、現在projectionを返す。単体GETとackは公開versionに一致する引用符付きETagを返す。

候補はサーバーが受理したcanonical Reminderと世代別 `reminder_schedules` のimmutable ScheduleIntentから導き、元HTTP tokenやPi sessionの寿命を延長しない。作成・日時変更・取消・snoozeの受理transactionは受理主体と当時のtenant/membership/account stampを固定する。title編集では変更しない。sharedの作成者が退出した後、現在adminが明示再予定した新世代はそのadminを受理主体とし、旧世代を新主体へ付け替えない。受理時stampと現在のmembership/account/control ledgerを照合して、通知に限定したREAD grantを作る。sourceが新規であるため、既存source専用の `Jobs.add_pending` を偽装せず、正本Reminderの認可と同じtransactionで固定sourceとJobを新規作成する。

ページ上限は全巡回の費用上限ではない。現在はdue/statusをページ内で評価するため、履歴を含むtenant内の全Reminderを一巡し、候補ごとに現在認可を追加読取する。満杯ページのcursorは次回へ保存し、末尾の短いページで先頭へ戻る。cursorより前に追加・変更された予定は次の巡回で検出する。運用の巡回間隔、保持数に応じた費用、実索引・負荷はOPS-03で測定する。

## 配送と現在の認可

構成済みJobs・page adapter・providerを使い、`Worker(jobs, verifier, {"dispatch_notification": DispatchNotification(jobs, provider)})` と既存Dispatcherを構成する。scanが作ったjobを `dispatcher.recover(tenant)` で配送し、localでは `LocalQueue.drain(worker)` を使う。サービス起動時の自動走査や実providerの暗黙接続はない。

lease取得・外部効果の開始・結果保存・完了は既存Jobsの検査を通す。handlerは外部呼出しの直前と業務commitで現在Reminderの予定版、受理した世代の主体とstamp、取消/削除、owner・target、関連Task、tenant/account/membershipと独立control ledgerを再検査する。shared user宛先は明示された現在の同tenant会員として独立に読取認可し、任意のcross-user output grantへ一般化しない。

deviceは現在のconnection generation、reminders scope、display/audio_output能力、表示方針を確認する。privateは現在slotに属する所有者本人の有効reminders sessionが必要で、元の受付sessionは使わない。session失効や現在権限の変更を確認したjobはobsoleteとなる。receiptは本人の未確認一覧に残るが、旧grantを改造して自動配送を再開しない。再予定は新しい世代を作る。

provider portに渡すのはtenant/notification/targetの識別子、音声期限、auto_playと必要な端末generation/sessionだけである。titleをpushやlock画面payloadへ複製しない。実transportはこの内部envelopeを公開protocolへ変換し、本文取得・表示/再生直前にも現在認可と予定版を確認する必要がある。

## 受付・表示・本人確認・音声期限

providerの `accepted` は配信受付であり、`displayed_at`、`acknowledged_at` とは別に保存する。providerがofflineを返した通知はpendingの未確認一覧として残り、再接続時に音声を自動再送しない。受付成功時だけ現在Reminderをdispatchedにし、本人ack時はacknowledgedにする。これらはrecord versionを進めるがschedule_versionを進めない。

内部 `public_notification` と `acknowledge` は現在認証済みFactTransactionを必要とする。user宛は対象本人だけ、device宛は現在の許可されたdeviceまたはreceipt所有者本人だけが取得できる。deviceのdisplayedイベントを本人ackへ昇格させない。ack済み通知は配送前に確認できればproviderへ送らない。古い通知IDは新しい予定の確認・状態変更に使えない。HTTP endpointは下記APP-05で提供する。

音声期限はdue_atから5分。5分ちょうどまでは対象、5分を超えたらauto_play=false。音声能力のない端末もfalse。`expires_at` は自動再生の期限であり、未確認一覧の削除期限ではない。provider/端末は受付時のauto_playだけに依存せず再生時にも期限を守り、offline中に溜まった音声を一斉再生しない。

## 回復・運用・切戻し

外部効果は `execution.effect("deliver", ...)` と同じ `{job_id}:deliver` keyを使う。二重queue名・配送世代・lease再取得は既存Jobsに従い、completed結果は再利用する。provider受付の応答を失いstartedしか残らない場合はoutcome_unknownで停止し、盲目的な再送をしない。

`DispatchNotification.reconcile_unknown(tenant, job_id)` はprovider.lookupで同じkeyの確定受付/確定offlineだけを照合する。未確定・記録なしは保留する。同じ失敗job・lease世代・grant・現在予定/認可を再検査し、確定証拠のcheckpointだけをcompletedにして通常workerへ戻す。provider受付とDBのexactly-onceは保証しない。外部呼出し開始後の取消/失効は既に開始した効果を取り消せないが、その結果を旧予定へ確定しない。

PolicyUnavailable/PersistenceUnavailableは既存workerの再待機・試行予算返却を使い、startedの不明結果は照合待ちとする。DB不可用で再待機記録も書けなければ保存済みleaseの期限切れ回収を待つ。確定した失効を基盤障害へ変えたり、失敗grantを更新して復活させたりしない。

開始時は対応handler/providerを先に構成し、次にdispatcher、最後に明示tenantの走査を開始する。停止時は走査を止め、対応workerで既存pending/runningとunknown照合をdrainしてからdispatcher/workerを停止する。既存DBの一括移行はしない。APP-03以前のScheduleIntentを持たない予定は配送不可とし、現在認証した利用者の明示snooze/日時変更で新世代を受理する。元主体のrejoinやtitle編集だけで旧予定を復活させない。切戻し前も新規admission/走査を止め、対応版で未完了job・receipt・export/purgeを処理する。旧workerに未対応通知jobを渡さず、source/job/checkpoint/ledgerを削除して再実行しない。

## Exportと削除

MEM-09は通知を明示的な公開projectionとcontent-free manifestへ含め、本文は現在Reminderから取得する。target本人以外の通知は管理者のvisible_tenant_dataにも出さない。title編集・予定変更・ack・現在認可変更で既存manifestは再検査される。内部grant、provider key、receipt構造は出力しない。

member/account lifecycleは本人通知sourceと全delivery子record、bindされたjob/checkpoint、本人が受理したreminder_schedulesを消去する。tenant削除は走査cursorも含めて消去する。共有Reminder自体の寄稿保持、他人private、独立DeletionIntentの所有規則は変更しない。Taskの忘却/削除、member/account/tenant fenceがある限り遅い配送・export・ackで内容を復活させない。

[共通fake/Firestoreシナリオ](../tests/fakes/notification_scenarios.py)、[fake応答喪失](../tests/test_notifications.py)、[Firestore結合](../tests/emulator/test_notifications_integration.py)、[実lifecycle purge](../tests/test_notification_lifecycle.py) がT-013/T-014/T-038を検証する。実cloud、実FCM/APNs、Scheduler資源、本番索引・負荷、端末での表示/音声はこのチケットの検証範囲外である。

## APP-05 HTTPとpush登録

`POST /v1/users/me/push-installations`、`PATCH/DELETE /v1/users/me/push-installations/{id}` はFirebase本人だけを受理する。現在accountを毎transactionで確認し、Idempotency-Keyで同じ書込とreceiptを原子的に再利用する。PATCH/DELETEはIf-Match必須。応答はid/platform/version/created_atだけで、token、account stamp、登録generationは返さない。POSTもETagを返す。

登録はglobalな `push_installations/{UUID}` に保存し、tokenのfingerprintをキーとする `push_token_claims/{hash}` が現在のid/user/generationを一つだけ指す。UIDをpathへ連結せず、既存の任意profile document keyとも独立させる。再登録/refreshは新generationを作り、同じtokenの旧登録は停止してtokenを消す。別利用者が同tokenを登録した場合も旧所有者の経路を停止する。無効tokenの遅い応答は、送信時generationと現在claimが一致する場合だけ停止できる。解除はtoken/claimを消し、tokenなしのremoved記録で同keyの再試行を処理する。activeは本人ごとに最大20件で、上限超過は403。既存tokenの置換とrefreshは上限内で可能で、21件目を黙って配送対象から切り捨てない。

`GET /v1/tenants/{t}/notifications`、追加した `GET /v1/tenants/{t}/notifications/{n}`、`POST .../{n}:ack` は既存のuser/device認証とX-Private-Session境界を通す。本文は毎回現在Reminderから読む。現在予定、関連Task、受理主体、宛先本人、所属/account、独立認可台帳と表示方針を検査する。一覧cursorも現在の表示内容・認可へbindし、編集/ack/失効後は再取得が必要となる。limitは1〜100であり、候補読取はtenantの通知履歴数に比例する。未登録・OS通知拒否・offlineでもアプリ内一覧は利用できる。ackはIdempotency-Key必須で、同keyの再試行にも現在認可を適用する。deviceはdisplayedだけを記録でき、本人ackへ昇格できない。APIのackはinstallationを更新せず、旧通知IDから新しい予定を確認できない。

ScheduleIntentは受理時のuser宛先membership/account stampも固定し、走査後のNotificationにも保存する。宛先が退出→再参加した後の初回走査も旧予定を受理し直さない。title編集はstampを更新せず、明示的な日時変更/snoozeだけが新しい受理になる。APP-04以前の宛先stampを持たないuser予定/通知は配送・通知本文取得・ackを拒否する。元Reminderの通常取得/取消は可能で、現在認証した利用者が明示再予定して移行する。既存sourceのversionや保存済grantを書き換える移行は行わない。

公開版の基準を持たない既存receipt（projection_version/reminder_versionが欠落）も一覧・取得・ack・配送を拒否する。正本より未来のreminder_versionも不整合として拒否する。現在値から過去の公開版を推定して補完しない。元Reminderの取得/取消は利用でき、明示的な再予定で新しい通知IDと初回公開版1を作る。旧receipt/source/grantを書き換えず、旧IDのackは同key再送でも新予定へ適用しない。旧exportは公開projectionを再検査するため利用不可となり、現在データから再生成する。

## FCM transportと部分結果

[PushNotifications](../services/worker/src/brainoid_worker/push_notifications.py) は明示した1tenantとJobsとtransportを受け取る。`DispatchNotification(jobs, PushNotifications(jobs, tenant, transport))` を通常Workerのhandlerへ注入する。API起動やimportで送信providerを生成しない。device宛先はこのFCM adapterの対象外で、DEV-04のtransportへ明示的にroutingする必要がある。

providerはjobの固定grant・現在予定・宛先を再検査し、内部envelopeのjob_lease_generationを現在のleaseと照合する。各installationを最大20件snapshotし、`notifications/{id}/push_batches/{effect-key-hash}` にid/generation/状態だけを保存する。各送信の直前に現在registration/claim・現在権限・leaseを再検査し、startedをcommitしてから外部I/Oを行う。leaseを端末ごとに更新し、途中の世代交代・解除・refreshは旧宛先へ送らない。snapshot後の新registrationを同じbatchへ追加しない。

FCMデータpayloadはnotification_id、tenant_id、固定の汎用bodyだけで、OS notificationにも同じ汎用bodyだけを載せる。tokenはFCMの宛先欄だけに必要であり、data/body、job、batch、export、通常logへコピーしない。予定・記憶・private session・音声設定・leaseはFCM payloadに含めない。本文の取得は上記APIで行う。

[FCM HTTP v1 adapter](../services/api/src/brainoid_api/adapters/fcm.py) は明示projectとGoogle credentialを要求し、ADC探索・default Firebase appを使わない。API packageが既に持つ固定google-auth/requests依存を使い、新規依存・lock更新はない。資格情報にfirebase.messaging scopeを設定し、HTTPS POSTを1回、timeout=10秒、redirect無効で呼ぶ。専用AuthorizedSessionは認証refreshによる再送を無効化し、HTTPAdapterのretryも0とする。所有者が終了時にcloseする。Google credential自体の取得/更新は送信と区別され、実credentialの運用はOPEN-006/OPS側で構成する。

firebase-admin 7.7.0のlocal `firebase_admin/_http_client.py:39` と `messaging.py:526` ではPOST/read/500/503の自動retryが既定であることを確認した。この用途は未知の送信結果を再試行できないため、SDK sendを呼ばず、公開HTTP v1の小さなallowlist JSONを組み立てる。private encoderやSDK内部改変に依存しない。200とmessage nameを受付証拠とし、構造化FCMエラーUNREGISTEREDの404だけがtoken停止対象。一般の400/401/403/404/429は拒否記録でtokenを停止せず、timeout/503/redirect/不正応答はunknownとする。

端末ごとにaccepted/invalid/rejected/unknown/skippedを分け、全件の結果が確定した場合だけ既存aggregate receiptへacceptedまたはofflineを返す。1台成功と別の1台unknownの場合もjob全体はoutcome_unknownで停止する。確定acceptedは保存するが、未確定のaggregateをsent/displayed/acknowledgedへ変更しない。FCM受付を端末表示や本人確認とみなさない。

FCMにはこのアプリのeffect keyで受付を照会するAPIがない。lookupは保存した全端末の確定結果だけを読み、応答喪失・started・未完了batchが残ればNoneを返す。すべての確定結果の保存後にaggregateの応答だけを失った場合は、APP-04の同key照合で回復できる。それ以外は運用上の照合待ちとして保持し、同じ通知を自動で再送しない。別端末への途中成功を理由に不明端末を再送したり、offlineと推定したりしない。Jobsもstarted付き期限切れをoutcome_unknownにし、新leaseへ自動再実行しない。FCMとDBのexactly-onceは保証しない。

## APP-05削除・移行・検証

通知配下のbatchは既存sourceのmember/account/tenant purgeと一緒に消す。global installation/claimはaccount削除だけで消し、1tenant退出やtenant削除で他tenant用の登録を消さない。既存の受理済みlifecycle intentでもbounded walkerの次の全走査が両global rootを確認する。別本人へ移動済みのclaimを旧本人の削除で消さない。account fenceと現在権限を送信・結果保存で読み直すため、遅いworkerや無効token応答は消去済み記録を復活させない。exportは既存の明示Notification projectionだけで、token・claim・batch・stampを出力しない。受付/ack/本文変更によるmanifest再検査を維持する。

導入は先にAPI/common service/対応workerを配置し、明示tenantとtransportを構成し、最後に走査を開始する。実FCM接続前はfake transportを使う。停止/切戻しは登録変更と新規走査を止め、対応版でpending/unknown/削除/exportを確認してからworkerを停止する。unknownを消して再送したり、旧版へ新batchの処理を渡したりしない。旧版は新stampの防御を持たないため、旧コードへ戻す際は通知配信/APIを停止したままとする。登録tokenを別の旧保存場所へ複製するmigrationは行わない。

[共通HTTP/配送シナリオ](../tests/fakes/push_scenarios.py) と [Firestore結合](../tests/emulator/test_push_notifications_integration.py) がT-030/T-038、現在本人認可、再登録、部分成功、未知結果、lease、idempotency応答喪失、実HTTP transport境界を検証する。[device取得](../tests/test_notification_devices.py)、[実purge](../tests/test_notification_lifecycle.py)、[実export](../tests/test_notification_export.py) も確認する。実FCM/APNs、実機表示、staging、cloud設定/配備は未実施で、OPEN-006およびOPS-09/10/UI-07の外部結合範囲である。

公開契約の確認日: 2026-10-27。[HTTP v1送信・scope](https://firebase.google.com/docs/cloud-messaging/send/v1-api)、[send API](https://firebase.google.com/docs/reference/fcm/rest/v1/projects.messages/send)、[FCMエラー](https://firebase.google.com/docs/cloud-messaging/error-codes)、[token管理](https://firebase.google.com/docs/cloud-messaging/manage-tokens)。現在のtoken契約を維持し、FIDへの移行は本変更に含めない。

BA-UI-07の[通知画面とnative登録](ui-reminders.md)は本文の再取得、旧予定ID拒否、表示と明示ackの分離、global installationの所有期間をこの契約へ接続する。実機OS権限・FCM/APNs確認は同文書の未完了条件を満たす必要がある。
