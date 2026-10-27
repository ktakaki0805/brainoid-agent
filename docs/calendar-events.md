# Calendar予定参照（BA-CAL-02）

FR-010 / AC-010 / T-020。本人が選択したGoogle Calendarの予定を、HTTPと既定の`calendar.list`から同じ`CalendarReader`で取得する。[OAuth](calendar-oauth.md)の明示compositionが前提であり、既定runtimeは未構成のまま503で閉じる。実provider、実OAuth、cloudへの接続はこの実装の検証には使わない。

## 選択と認可

`PATCH /v1/tenants/{tenant}/integrations/{integration}`は本人user bearer、`Idempotency-Key`、`If-Match`を必要とする。`allowed_calendar_ids`は最大10個の重複しないopaque ID。空配列はすべての参照を拒否する。実付与scopeのtokenで各calendarのeventsを1件・1秒の区間でprobeし、providerのreader/writer/writerWithoutPrivateAccess/owner権限を確認する。none/freeBusyReader/未知のroleは予定詳細の委任として扱わない。readerとwriterWithoutPrivateAccessでは非表示の詳細を補わない。calendarList scopeは追加要求せず、UIからのID選択はBA-UI-08で接続する。probeの予定本文は保存・公開しない。

現在の本人認可と保存済みreceiptの照合をprobeより先に行う。同じkey/body/If-Matchの成功再送はprovider障害時も保存済みreceiptへ収束し、body競合はprovider呼出しなしで409になる。未commitの更新だけtransaction外でprobeし、最終transactionで現在認可・版・連携stampを再検査する。secret取得中や複数probe間の同key並行commitも、送信前の変更検知・I/O失敗時に現在認可とreceiptを再照合して保存済み成功へ戻る。保存済み成功がなければ新たなwriteやprovider再送をせず、失効・body競合・版競合・provider失敗を返す。receiptの24時間保持期限も共通契約どおり再検査する。正常な空/非表示の予定は受理し、壊れたprovider pageは503、閲覧権不足は403に分ける。

現在のaccount、tenant、membership、独立control ledger、lifecycle fence、本人integration、選択版、OAuth起点のbindingと実付与scopeを再確認する。token secretにはintegration、owner authority binding、実付与scopeを埋め込み、DB側の接続済みauthorizationと照合する。LLM引数からowner/tenant/scope/secret referenceを作らない。

deviceは現在のcredential family/epochと本人private_sessionの`calendar.read`を必要とする。共有Pi、別人、別tenant、終了・期限切れsession、範囲外scopeを拒否する。本人スマホからPiへの出力も現在の出力先sessionで検査する。secret取得後、provider送信直前、取得後の公開直前に現在認可を検査し、遅い外部I/Oが失効を越えて予定を返さない。

## HTTP、時刻とページ

`GET /v1/tenants/{tenant}/integrations/{integration}/calendar-events`は`calendar_id`、`time_min`、`time_max`を必須とし、`limit`は1〜100（既定20）、`cursor`は任意。正の区間、最大31日。API入力は明示offsetのRFC3339で、共通型がUTCへ正規化する。

- 終日は`{date}`のまま保存・返却し、排他的な終了日と日付跨ぎを保つ。
- 時刻付き予定はUTCの`date_time`とIANA `timezone`を返す。eventのtimezoneがなければprovider pageのcalendar timezoneを使う。明示offsetとzoneの矛盾、不正zone、DSTの曖昧なlocal時刻・存在しない時刻は失敗する。offset省略はevent自身に明示zoneがあり、一意に決まるlocal時刻だけ受け付ける。
- opaque event/calendar ID、provider ETag、各取得の`fetched_at`を保つ。無題・非表示のsummaryは「予定」と表示し、providerが隠した詳細を補わない。cancelledは一覧から除く。成功一覧の途中に壊れたeventがあれば全pageを失敗させる。
- cursorは本人/tenant/現在actor・session、integration版・scope・選択・secret参照、calendar、区間、limitに署名で束縛。5分で失効し、最大10頁まで。空itemsでもnextPageTokenがあれば継続する。provider tokenは384文字以下とし、符号化後も共通positionの512文字・wireの2048文字上限内である必要がある。表現上限、繰り返しtoken、10頁超はpage全体を503にし、本文の部分成功を返さない。受理したopaque tokenは変形せずproviderへ戻す。上限時に完了一覧を装わず、区間を狭めて再取得する。

`next_cursor`がある限り会話toolも未完のpageとして扱い、同じ引数で次pageを要求する。GoogleのtimeMinは予定endの排他的下限、timeMaxは予定startの排他的上限である。providerはミリ秒を無視するため、秒未満の境界精度は保証しない。[events.list仕様](https://developers.google.com/workspace/calendar/api/v3/reference/events/list)

具体HTTP adapterは固定Google endpointへIDをpath encodingしてGETする。timeout 10秒、redirect禁止、自動retryなし、環境proxy/暗黙credential探索なし。401は認証エラー、403/404は拒否、それ以外の失敗・timeout・不正応答は503。providerのエラー本文・token・予定は通常ログや公開エラーへ出さない。自動refreshは実装せず、期限切れtokenは再連携が必要である。

## 会話、出典、消去とexport

既定ToolExecutorに`calendar.list`を登録する。会話は本人privateで、表示方針がneverまたはauthenticated_sessionである必要がある。結果にはserver由来のintegration/calendar/版hashを保持し、runの`calendar_sources`へ本文なしの出典一覧を記録する。最終messageと後続turnにも重複を除いて継承する。

結果のモデル再入力、checkpoint復帰、stream保存、最終保存では既存`ResultAuthorizer`に加えて現在のCalendar出典を検査する。message履歴、run GET、SSEの各配送batch、export生成・downloadも現在の出典を検査する。連携の失効・削除・選択更新後は旧出典の結果を返さず、履歴から該当assistant本文を除く。古い会話の再実行は拒否するため、新しい会話で再取得する。deviceのcalendar.readを後から外した場合も、conversationだけの権限で旧予定を読み返せない。

providerのイベントは取得時点のsnapshotであり、ETagとfetched_atがその版・時点を示す。provider側の後続変更や削除をpush同期で検知する機能はない。連携が有効な間はそのsnapshotを本人が履歴参照できる。providerの現在状態を必要とする場合は再取得する。assistant/tool由来本文は既存の明示記憶・要約の直接user出典として扱わない。

新しい永続producer collectionは増やさない。private会話、run、tool receipt、checkpoint、SSE childは既存のlifecycle producer inventoryで本人/tenant削除時に消去される。Calendar secret hookとexport hookも必須で、成功証明前に外部copyを失効する。exportは現在の本人に表示できるassistant本文だけを含み、内部tool binding、secret、provider continuationを出力しない。連携失効後は既存export downloadも拒否する。

## 導入、復旧、切戻しと検証

依存追加なし。`AuthServices.calendar_events`へ`CalendarEvents`を、workerの`RunTurn(calendar_reader=...)`へ同じ構成のreaderを明示注入する。API/workerは同一store/独立認可ledger/secret registryを使い、provider transportは構成者がcloseする。無条件のfake fallbackはない。実環境公開はOAuth登録・Secret Manager配線・lifecycle hookを一体で確認した後とする。

OAuth token secretとauthorizationへscope/bindingを追加した。旧CAL-01で作った連携は必要な証拠がなく予定参照を拒否するため、再連携して本人がcalendarを再選択する。秘密の自動移行や旧scopeの推定は行わない。OpenAPIの`fetched_at`は追加の任意fieldで、現在の実装は毎回出力する。toolのlimit/cursorも任意追加。

rollbackではまずCalendar HTTP/workerの明示compositionとagentのcalendar.listを無効化する。Calendar出典検査を持たない旧版へ戻す場合は、Calendar由来private会話とそのrun/tool/checkpoint/SSE/exportを受理済みlifecycle手順で消去・失効した後に戻す。DB snapshotだけで古いtoken/連携を復活させない。provider失敗は空成功や自動再送にせず、利用者が範囲を狭めるか再連携し、新しいrunで再取得する。選択の応答喪失は同じkey/body/If-Matchで再送する。

fake検証はOAuth producer→本人選択→一覧、終日・両DST境界・日付跨ぎ・cursor・不正応答・遅延失効、既定tool→private会話→次turn、Pi本人session、export失効、実際の生成物のlifecycle purgeを含む。Emulatorでは同じ選択/取得scenarioとFirebase bearer→HTTPを確認する。実Google Calendar、Google scope審査、実端末表示消去、cloudの索引・性能・費用は未検証。

local fake/Emulatorのprovider料金は発生しない。実構成の1pageはsecret readとevents GETを各1回、選択は最大10個のprobe、最大10頁で1000予定まで。再認可のDB/ledger読取、会話本文の保存、exportには実環境の利用量・保持費用が生じる。具体的な金額・project・domainはここでは決めない。
