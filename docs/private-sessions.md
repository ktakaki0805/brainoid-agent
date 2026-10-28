# Pi個人セッション

BA-IAM-09 / FR-012 / T-020・T-021 のAPI側契約。本人のFirebase認証、現在のmembership、対象Piの現在の許可範囲を確認して短命な委任を作る。画面・音声消去はBA-UI-10・BA-VOICE-05、session_startedとsnapshotの配送は[端末の状態復旧](device-snapshot.md)で接続する。

## HTTP契約

| 操作 | 契約 |
| --- | --- |
| `POST /v1/tenants/{t}/devices/{d}/private-sessions` | 本人のuser bearer、`Idempotency-Key`、1〜9個の重複のない`allowed_scopes`、booleanの`acknowledge_shared_space: true`を要求する。bodyで別userは選べない。201と現在状態・ETagを返す |
| `GET /v1/tenants/{t}/private-sessions/{s}` | 開始した本人のuser bearerのみ。現在状態・ETagを返す。取得によるidle延長はない |
| `DELETE /v1/tenants/{t}/private-sessions/{s}` | 開始した本人のuser bearer、`Idempotency-Key`、`If-Match`。204。管理者でも他人のsessionをこの入口で終了できない |

scopeは `conversation`、`memory.read`、`memory.write`、`notes`、`tasks`、`reminders`、`calendar.read`、`calendar.propose`、`home.control`。要求全体がPiの現在のscopeに含まれなければ拒否する。Calendar実行確認、membership/端末管理、export、削除受付のscopeはない。それぞれの本人スマホ用入口は端末bearerを受け付けない。

各Piでactiveは一つ。同じ本人でも新しい開始キーは既存activeがある間409。同じ開始キー・同じscope集合なら同じsession IDの現在状態を返す。bodyの変更は409。同じキーの再送が終了・失効後に届いても、`ended`/`expired`を返し、期限を更新しない。新しい明示同意には新しい開始キーを使う。別userの同時開始も同じPiのslotで直列化される。

`version`/ETagは同意と終了状態の版であり、活動によるidle期限更新では増やさない。終了・失効は版を増やす。終了の同一キー再送は204で、新sessionを終了させない。If-Matchが現在の保存版と異なる新要求は412。取得で失効を確定した後はGETで得たETagを使う。応答は`Cache-Control: no-store`。

## 正本と期限

`tenants/{t}/private_sessions/{s}`に公開状態と内部の認可bindingを保存する。`private_session_slots/{device_id}`は最後に開始したsession IDを参照し、終了後も参照を保持する。開始・slot更新・開始receipt・監査を一つのFirestore transactionでcommitする。新sessionの開始時には旧sessionの失効状態も同じtransactionで確定する。古い終了要求はslotを書き換えない。

開始時にtenant/user/device、membership認可版、device認可版・接続generation、account状態stampを束縛する。account stampは現在のuser status/version/deletion epochを含み、未作成profileは未作成状態に束縛する。opaque Firebase UIDをdocument pathへ展開しない。各利用で現在値と独立認可台帳を再検査し、role変更・revoke→再招待・device再登録・account状態変更によって旧同意を復活させない。

期限はサーバーUTCで`created_at + 5分`を固定し、初回idle期限は`created_at + 60秒`。`now >= min(expires_at, idle_expires_at)`で拒否する。TTL清掃やPiの時計には依存しない。権限・slotの不一致も利用を拒否し、状態取得時に`expired`を正規化する。終了/失効した正本は再active化しない。認可bindingやslotを持たない旧・不正recordはfail closedで拒否し、自動的に委任へ移行しない。既存製品データには旧session実装がないため、fixtureを新bindingに更新する。後からデータを移す場合も本人の新規同意が必要。

開始/終了receipt (`private_session_requests`) は本文やtokenを含まない。終了後の遅延再送を新規作成に扱わないため、この実装ではreceiptとsessionをTTL削除しない。将来の保持削減では、キーの再利用拒否を維持するtombstoneを設計する。監査はactor/対象session ID/結果/request ID/時刻のみ。

## 認証・認可と利用活動

`private_session_context`依存は、IAM-07の専用端末bearerと単一の`X-Private-Session`を併せて検査する。ID単独やFirebase bearerでは委任されない。handlerの入口で得たcontextだけを後続処理の権限に使わず、保護するtransaction内で`PrivateSessions.current`を再実行する。この境界はtoken期限・credential family・外部epoch・DB/台帳・session・membershipを再照合する。対象userが既知の処理には`expected_user_id`も指定できる。

resource/search/output/workerには共通policyを使用する。privateはsession本人の`authenticated_session`だけで、shared表示の条件は変更しない。前回のgrantや出力先stampを持つ処理も毎回正本を読み直し、終了・期限切れ後の遅延結果を拒否する。通常のidle延長はstampを変えず、最新期限の検査を毎回行う。session ID自体が一つの委任インスタンスを識別するため、新sessionへ旧runやgrantを移さない。

`accept_activity(tx, device_identity, session_id, activity_id, grant)`は未公開の内部受付境界。新しい利用者操作を受付する業務サービスだけが、その操作と同じtransactionで使う。サーバーのdurable operation IDと現在のresource grantを渡し、全業務readの後・業務writeの前に呼ぶ。再認可後に`min(固定5分期限, now + 60秒)`まで延長する。activity receiptはtenant/device/session/resource/actionへ束縛し、同一IDの同じ活動は延長しない。同一IDの別活動は409。古いsessionの活動は新sessionへ転用できない。

heartbeat、認証確認、snapshot、状態GET、リクエスト再送、worker/model/output完了を活動として扱わない。任意のtouch/heartbeat HTTP入口は提供しない。後続の会話・音声サービスは、再送時にも同じdurable operation IDを渡す責任を持つ。

`PrivateSessions.snapshot(tx, device_identity)`はDEV-04向けの内部adapter。端末認証を同じtransactionで再検査し、現在activeのsessionか`None`を返す。必要なterminal状態書込みを行うため、他のsnapshot readを終えた後・writeの前に呼ぶ。これ自体はidleを延長せず、配送も画面/音声buffer消去も行わない。

## 検証と復旧

fake clockで60秒・5分の直前/一致境界、scope不足・禁止scope、別Pi/別user、header単独、再送・活動dedupe・新sessionへの旧grant転用・終了後出力・権限変更を検証する。Firestore emulatorでは同時開始、異なる利用者の競合、終了と活動の競合、commit応答喪失、期限cleanupと新規開始の競合を検証する。実行は[開発・テスト手順](05-development-and-testing.md)と[emulator設定](../.env.emulator.example)に従う。

応答喪失時は同じキーで再送し、取得した現在状態を表示する。DB backup復元は通常のAPI公開前gateと端末credential epoch更新を必須とする。sessionの5分期限を巻き戻してはならない。旧APIへ戻す場合は新session受付を止め、全sessionが失効するまで最大5分待つ。旧bindingを許す互換コードは追加しない。
