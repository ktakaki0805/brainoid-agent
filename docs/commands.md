# 家電 command の受付

BA-APP-06 は FR-011 / AC-011、T-018 / T-036 の API 側を実装する。[共有サービス](../packages/memory/src/brainoid_memory/commands.py)を HTTP と既定 `home.set_state` が使う。BA-DEV-05 が永続配送・start grant・結果ACKを接続する（[gateway契約](device-gateway.md#durable-command-boundary-ba-dev-05)）。HA呼出し・Pi実行はDEV-07、inventory連携はDEV-06で接続する。

## HTTP と状態

`/v1/tenants/{tenant_id}/commands` の POST は `Idempotency-Key` と `device_id`、`target_entity_id`、`operation` を受け、202 と ETag を返す。actor は現在の本人または検証済み端末資格情報から保存する。作成時刻から30秒の期限を固定し、初期状態は `queued`。同keyの24時間以内の再送は同じcommandの現在状態を返し、期限を延長しない。異なる本文は409。端末・会員・台帳・対象の現在認可は再送でも検査する。

GET `/commands/{id}` は現在の状態と ETag を返す。未claimのまま期限へ達した文書はtransactionで `expired` に進め、versionとupdated_atを更新する。clockだけで公開statusを変え、ETagを固定することはしない。期限遷移はlazyであり、将来の配送/claim consumerはGETの実施に依存せず自分のtransactionでも `now < expires_at` を検査する。

POST `/commands/{id}:cancel` は元のactor、現在の権限、`queued/delivered/accepted`、execution IDなし、未期限切れを同じtransactionで検査する。Idempotency-Keyは必須、If-Matchは任意。指定した古い版は412、claim済みや取消済みの別要求は409 `conflict`、未claimの期限切れはGETや作成再送によるexpired保存の前後を問わず410（古いIf-Matchより優先）。claim済みは期限後も409。同keyの取消し再送は現在認可で同じ結果を取得する。claim側も同じcommand文書をtransactionで読み更新することで競合を直列化する。取消しが先ならgrantを出せず、claimが先なら取消しは409になる。

保存・配送・受領は物理成功ではない。`outcome_unknown`、`failed`、`running` を `succeeded` に変換しない。保存された `succeeded` の公開には観測状態が必須。DEV-05の結果writerはclaim済みexecution IDと操作に一致する観測を検査し、ACKだけを物理成功にしない。

## 対象・権限と入力

canonical entityは同tenant、未削除、未抑止のapplianceで、外部参照が一件、外部ID索引が同じentityを指すことを要求する。名前やaliasで対象を勝手に選ばない。toolにもUUIDが必要で、同名だけの入力は実行しない。integrationのtenant/provider/device/status/所有者、信頼済みinventoryのentity/device/reference/許可操作も検査する。

家電制御は `home.control` を使う。policyの `Action.CONTROL` はEntityだけに使用でき、canonical scope・所有者・device allowlistを検査する。memory.read/writeの権限を暗黙に要求・付与しない。shared端末には `shared` と `home.control`、privateには現在本人の `home.control` sessionと `authenticated_session` を要求する。端末資格情報は自身のdeviceだけを操作できる。

[共通device境界](../packages/policy/src/brainoid_policy/devices.py)を既存DeviceAuthorizationと共有する。独立認可台帳、現在tenant/device状態、認可版、connection_generation、scope、entity allowlist、必要なsessionを検査する。commandのactor stamp、account stamp、device認可版/generation、entity/integration/inventory fingerprintは受理時のまま固定する。失効・変更後に新しいgrantへ付け替えず、旧commandの取得・再送も拒否する。新しい操作は現在の利用者が別keyで明示する。

端末起点は秘密を含まない元family ID・外部credential epoch・tenant/device・認可版/generation・familyの絶対期限も `actor_credential` に固定する。API credential認証、RunState、commandは同じ `current_family` を使い、active状態・元familyの対応・期限・独立設定の現在epochを同transactionで再検査する。現在のreaderがownerや別端末でも元起点の検査を省略しない。token再利用によるfamily失効、期限切れ、epoch変更、再登録/別familyへの置換は旧commandを許可しない。通常のrefresh rotationはfamilyを維持するので、refresh世代/hashや短いaccess token期限へcommandを固定しない。HTTP/RunTurnは検証済み資格情報とruntime epochを注入する。exportを構成するadapterも `Exports(credential_epoch=...)` に独立設定値を渡す必要があり、未設定時は端末起点commandの収録・既存archiveの再利用をfail closedにする。family/epochの保存値をruntime設定の代用にしない。


許可操作は `light.turn_on`（brightness_pctは任意の整数1〜100）、`light.turn_off`（引数なし）、`climate.set_temperature`（数値16〜30℃）、`climate.set_hvac_mode`（off/heat/cool/auto）。文字列数値・bool・null・未知field、toggle、任意service/URL、危険な操作は拒否する。操作はinventoryのallowlistにも含まれなければならない。

## 信頼済み inventory と未構成時

`home_targets/{external_key}` は公開API/toolから書き込めない内部snapshot。external_keyはMEM-02のtenant/integration/provider/opaque external IDのhashで、本文にはtenant_id、device_id、entity_id、唯一のreference、version、active/revoked、allowed_actionsを保存する。integrationにも同じdevice_idを必要とする。書込元は将来のDEV-06 provider検証adapterであり、利用者の申告やモデルJSONから生成しない。今回その実adapterや自動importは実装しない。snapshotがなければfail closedでcommandを作成しない。fake/emulator検証だけが合成snapshotを投入する。

DEV-06は現在integration・device認可とprovider inventoryを確認し、versionを単調に進め、失効snapshotをactiveへ戻さず新しい検証を必要とする。entity・索引・inventoryの対応は原子的に保ち、削除fenceを検査して旧snapshotの復元を拒否する。command側は削除fenceと現在のsnapshotを毎回確認する。対象照合は固定pathのpoint readで、機器一覧を毎要求scanしない。DEV-05の端末限定watchはcommandを監視する。provider呼出し・クラウドresource・依存追加はない。

## 会話・削除・export

既定toolの現行受付文法は `Set home state {argumentsの全JSON、キーsort、Unicode保持}` または `家電の状態を設定 {同JSON}`。canonicalな今回の直接user発言と引数が完全一致する場合だけ受け付ける。否定、引用、名前だけの曖昧な指示、検索資料、過去turn、モデルが作った文言は権限の根拠にならない。広い自然言語での確認は将来の明示確認UIへ引き継ぐ。

toolのeffect_kindはcommand、result.command_idはcommand IDに一致する。toolのsucceededは受付成功を意味し、resultは `accepted: true` とcommand_idだけ。runへ原子的な受理markerを残し、既存入力の削除provenanceを保持して派生文脈を退役する。home許可turnのモデル前置きは中間read toolのconsume・再開を含め公開せず、内部continuationだけに保持する。今回のcanonical直接user発言が上記の完全JSON文法に一致するかを、tool候補・受理marker・agentのhome許可とは独立に識別する。この要求を持つturnもstreamとread toolの中間consumeをbufferする。対応する永続commandとtool receipt・現在認可の検証が通る受理markerがないCompletedは、最終transactionで「家電操作は受け付けていません。実行結果は確認できません。」に置き換える。runのcompletedはこの会話応答の完了だけを示し、commandの受付・実行成功ではない。read後のlease再開やmodel結果のcheckpoint再利用でも同じ検査を行い、同key turn再送と反復SSE・公開historyは固定文だけを返す。元のモデル本文・continuationは公開messageや後続モデルhistoryへ引き継がず、削除provenanceを保存して退役する。文法に一致しない通常会話は最終commitで本文を公開する。home試行が拒否された場合はbufferを公開せず失敗で終了し、受理後は「家電操作を受け付けました。実行結果はまだ確認されていません。」で終端化する。モデルの成功文を物理実行の証拠として使用しない。

commandはentityのscopeを継承し、toolでは会話scopeとも一致を要求する。APIの明示要求・toolの直接user要求は独立した受理済み操作であり、出典の記憶を消去しただけで物理副作用を巻き戻したことにはしない。既存のrun/tool receipt purgeはそのまま適用する。private commandとhome_targetsはmember/account削除で消去し、他人のprivateとshared寄稿を保持する。tenant削除では全command/inventoryを消去する。外部ID索引は既存仕様どおりreferenceを消し、再生成防止の最小記録を残す。

exportは現在読めるcommandの公開projectionを収録し、内部binding・actor認可stamp・inventory/資格情報を出さない。my_dataは本人作成分、visible_tenant_dataは現在読めるsharedも含む。版・状態・依存・現在の認可をmanifest検証し、取消しやtarget変更後の古いarchive取得を拒否する。現在読めないcommandは新しいexportの対象から除く。各受付記録は現在の権限を代替しない。

## 検証・移行・切戻し

[HTTP/RunTurn](../tests/test_commands.py)、[端末token/session](../tests/test_commands_device.py)、[lifecycle/export](../tests/test_commands_lifecycle.py)、[Firestore競合](../tests/emulator/test_commands_integration.py)を使用する。通常suite、全Emulator、setup/checkと生成client/互換checkで既存notes/tasks/reminders/認可を回帰確認する。実HA・Pi・cloudの試験は未実施で、DEV-05/06/07およびQAへ引き継ぐ。

既存SDK更新は不要。端末起点でactor_credentialがない旧commandは取得・再送・reconcile・exportを拒否し、新しいreaderの資格情報で暗黙補完しない。user起点文書は従来どおり検査する。旧queued文書は初期30秒期限を更新せず保持し、consumerは期限と現在認可を独自に検査する。自動backfillや再実行は行わない。既存entityには信頼済みhome_targetsがない限り操作能力を付与しない。切戻しはcommand受付を止め、queued/unknown文書・独立台帳・削除fenceを保持して互換版へ戻す。DB復元で期限・認可版・generationを巻き戻して再実行しない。GETの初回期限遷移に一回の文書write、受付/cancelにcommandと共通冪等receiptのwrite、認可・inventoryのpoint readが加わる。実行費用や接続費用は今回発生しない。
