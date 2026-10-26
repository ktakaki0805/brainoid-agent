# 期限走査と通知配送

BA-APP-04 / FR-009 / AC-009 は [通知の正本](../packages/memory/src/brainoid_memory/notifications.py) と [期限走査・worker](../services/worker/src/brainoid_worker/notifications.py) を提供する。固定時計・fake配送先とFirestore emulatorで動かす内部契約であり、FCM・通知HTTP APIはBA-APP-05、Pi同期はBA-DEV-04、UIはBA-UI-07、実Scheduler資源はBA-OPS-03で扱う。新しい依存やworkspace memberは追加しない。

## 走査・保存・版

`DueReminders(jobs, pages).scan(tenant, limit=100)` はID順に最大100件を読み、`status=scheduled` かつ `due_at <= now` の正本を同transactionで再検査する。tenantごとの `notification_scans/due` にlease token・期限・cursorを保存する。同時走査はbusyを返し、停止後はlease期限で同じcursorから再開する。各候補のtransactionは現在leaseと認可を読み、通知・宛先receipt・配送job/outboxを原子的に作る。queue/providerはtransaction外でだけ呼ぶ。

通知IDは `UUIDv5(reminder_id, "notification:" + schedule_version)`。宛先receiptのキーはkind/idのfingerprintとし、opaque UIDをpathへ埋め込まない。本文は通知・job・receiptにコピーせず、本人の取得時に現在のReminder.titleを読む。

通知sourceはversion=1の固定世代記録で、mutableなreceiptを `notifications/{id}/deliveries/{target-fingerprint}` に分ける。jobはこのsourceの厳密なresource version・scope・主体stampへbindする。既存Jobsのsource.version検査を弱めず、workerが古いgrantを現在版へ書き換えることもない。title編集だけなら同じ通知を使い、日時変更・取消・snoozeは `ScheduleReference/current_schedule` により旧通知を利用不可にする。

候補はサーバーが受理したcanonical Reminderと世代別 `reminder_schedules` のimmutable ScheduleIntentから導き、元HTTP tokenやPi sessionの寿命を延長しない。作成・日時変更・取消・snoozeの受理transactionは受理主体と当時のtenant/membership/account stampを固定する。title編集では変更しない。sharedの作成者が退出した後、現在adminが明示再予定した新世代はそのadminを受理主体とし、旧世代を新主体へ付け替えない。受理時stampと現在のmembership/account/control ledgerを照合して、通知に限定したREAD grantを作る。sourceが新規であるため、既存source専用の `Jobs.add_pending` を偽装せず、正本Reminderの認可と同じtransactionで固定sourceとJobを新規作成する。

ページ上限は全巡回の費用上限ではない。現在はdue/statusをページ内で評価するため、履歴を含むtenant内の全Reminderを一巡し、候補ごとに現在認可を追加読取する。満杯ページのcursorは次回へ保存し、末尾の短いページで先頭へ戻る。cursorより前に追加・変更された予定は次の巡回で検出する。運用の巡回間隔、保持数に応じた費用、実索引・負荷はOPS-03で測定する。

## 配送と現在の認可

構成済みJobs・page adapter・providerを使い、`Worker(jobs, verifier, {"dispatch_notification": DispatchNotification(jobs, provider)})` と既存Dispatcherを構成する。scanが作ったjobを `dispatcher.recover(tenant)` で配送し、localでは `LocalQueue.drain(worker)` を使う。サービス起動時の自動走査や実providerの暗黙接続はない。

lease取得・外部効果の開始・結果保存・完了は既存Jobsの検査を通す。handlerは外部呼出しの直前と業務commitで現在Reminderの予定版、受理した世代の主体とstamp、取消/削除、owner・target、関連Task、tenant/account/membershipと独立control ledgerを再検査する。shared user宛先は明示された現在の同tenant会員として独立に読取認可し、任意のcross-user output grantへ一般化しない。

deviceは現在のconnection generation、reminders scope、display/audio_output能力、表示方針を確認する。privateは現在slotに属する所有者本人の有効reminders sessionが必要で、元の受付sessionは使わない。session失効や現在権限の変更を確認したjobはobsoleteとなる。receiptは本人の未確認一覧に残るが、旧grantを改造して自動配送を再開しない。再予定は新しい世代を作る。

provider portに渡すのはtenant/notification/targetの識別子、音声期限、auto_playと必要な端末generation/sessionだけである。titleをpushやlock画面payloadへ複製しない。実transportはこの内部envelopeを公開protocolへ変換し、本文取得・表示/再生直前にも現在認可と予定版を確認する必要がある。

## 受付・表示・本人確認・音声期限

providerの `accepted` は配信受付であり、`displayed_at`、`acknowledged_at` とは別に保存する。providerがofflineを返した通知はpendingの未確認一覧として残り、再接続時に音声を自動再送しない。受付成功時だけ現在Reminderをdispatchedにし、本人ack時はacknowledgedにする。これらはrecord versionを進めるがschedule_versionを進めない。

内部 `public_notification` と `acknowledge` は現在認証済みFactTransactionを必要とする。user宛は対象本人だけ、device宛は現在の許可されたdeviceまたはreceipt所有者本人だけが取得できる。deviceのdisplayedイベントを本人ackへ昇格させない。ack済み通知は配送前に確認できればproviderへ送らない。古い通知IDは新しい予定の確認・状態変更に使えない。HTTP endpointは未提供である。

音声期限はdue_atから5分。5分ちょうどまでは対象、5分を超えたらauto_play=false。音声能力のない端末もfalse。`expires_at` は自動再生の期限であり、未確認一覧の削除期限ではない。provider/端末は受付時のauto_playだけに依存せず再生時にも期限を守り、offline中に溜まった音声を一斉再生しない。

## 回復・運用・切戻し

外部効果は `execution.effect("deliver", ...)` と同じ `{job_id}:deliver` keyを使う。二重queue名・配送世代・lease再取得は既存Jobsに従い、completed結果は再利用する。provider受付の応答を失いstartedしか残らない場合はoutcome_unknownで停止し、盲目的な再送をしない。

`DispatchNotification.reconcile_unknown(tenant, job_id)` はprovider.lookupで同じkeyの確定受付/確定offlineだけを照合する。未確定・記録なしは保留する。同じ失敗job・lease世代・grant・現在予定/認可を再検査し、確定証拠のcheckpointだけをcompletedにして通常workerへ戻す。provider受付とDBのexactly-onceは保証しない。外部呼出し開始後の取消/失効は既に開始した効果を取り消せないが、その結果を旧予定へ確定しない。

PolicyUnavailable/PersistenceUnavailableは既存workerの再待機・試行予算返却を使い、startedの不明結果は照合待ちとする。DB不可用で再待機記録も書けなければ保存済みleaseの期限切れ回収を待つ。確定した失効を基盤障害へ変えたり、失敗grantを更新して復活させたりしない。

開始時は対応handler/providerを先に構成し、次にdispatcher、最後に明示tenantの走査を開始する。停止時は走査を止め、対応workerで既存pending/runningとunknown照合をdrainしてからdispatcher/workerを停止する。既存DBの一括移行はしない。APP-03以前のScheduleIntentを持たない予定は配送不可とし、現在認証した利用者の明示snooze/日時変更で新世代を受理する。元主体のrejoinやtitle編集だけで旧予定を復活させない。切戻し前も新規admission/走査を止め、対応版で未完了job・receipt・export/purgeを処理する。旧workerに未対応通知jobを渡さず、source/job/checkpoint/ledgerを削除して再実行しない。

## Exportと削除

MEM-09は通知を明示的な公開projectionとcontent-free manifestへ含め、本文は現在Reminderから取得する。target本人以外の通知は管理者のvisible_tenant_dataにも出さない。title編集・予定変更・ack・現在認可変更で既存manifestは再検査される。内部grant、provider key、receipt構造は出力しない。

member/account lifecycleは本人通知sourceと全delivery子record、bindされたjob/checkpoint、本人が受理したreminder_schedulesを消去する。tenant削除は走査cursorも含めて消去する。共有Reminder自体の寄稿保持、他人private、独立DeletionIntentの所有規則は変更しない。Taskの忘却/削除、member/account/tenant fenceがある限り遅い配送・export・ackで内容を復活させない。

[共通fake/Firestoreシナリオ](../tests/fakes/notification_scenarios.py)、[fake応答喪失](../tests/test_notifications.py)、[Firestore結合](../tests/emulator/test_notifications_integration.py)、[実lifecycle purge](../tests/test_notification_lifecycle.py) がT-013/T-014/T-038を検証する。実cloud、FCM/APNs、Scheduler資源、本番索引・負荷、端末での表示/音声はこのチケットの検証範囲外である。
