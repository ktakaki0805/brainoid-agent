# Calendar操作の固定proposal

BA-CAL-03 / FR-010 / AC-010 / T-015・T-037。確認日時2026-10-27。本人の選択calendarに対するcreate/update/deleteの提案、取得、否認、期限回収、会話checkpointを実装する。本人confirmとprovider書込・照合はBA-CAL-04、確認画面はBA-UI-08で接続する。

## HTTPとmodel tool

| 入口 | 入力と認可 | 結果 |
| --- | --- | --- |
| `POST /v1/tenants/{t}/integrations/{i}/calendar-proposals` | user bearer、または現在のPi private sessionとcalendar.propose。Idempotency-Key必須 | 201、固定proposal |
| `GET /v1/tenants/{t}/calendar-proposals/{p}` | 本人user bearer。admin/owner権限から他人の内容を取得できない | 公開projection。期限超過pendingはexpiredへ収束 |
| `POST /v1/tenants/{t}/calendar-proposals/{p}:reject` | 本人user bearer、Idempotency-Key。bodyなし | declined、期限到達ならexpired。同じ決定は再送可能 |
| `calendar.propose` | integration_idと厳格なchangeのみ。private会話と現在の保存先を検査 | waiting_confirmation、server由来proposal_id |

公開応答はno-store。token、secret_ref、本人識別子、原端末credential、内部権限・出典bindingを返さない。GET/rejectはPi bearerを受け付けない。Piのproposal生成にcalendar.readは要求しない。calendar.proposeの内部target probeをcalendar.listの委任として使わない。未構成runtimeは503で閉じる。calendar.confirmはモデルtoolにもHTTPにも公開しない。

同一integration、actor、keyと正規化bodyの再送は共通24時間receiptから同じproposalを取得する。内容変更には別key・別proposalが必要。別integrationは別namespace。再送でも現在の本人、integration、grant、削除状態を検査し、providerを再読取しない。receiptに予定本文を複製しない。

## 不変内容とprovider参照

create/updateはsummary/start/endを必須とする。updateは最終内容の完全な指定であり、部分PATCHではない。時刻はUTCの瞬間とIANA timezone、終日はdateのみ。start/endの形式を揃え、end > startとする。終日のendは排他的。曖昧なローカル時刻・offsetなし日時をAPI入力として受け付けない。参加者、会議送信、系列引数、owner/run/tool/checkpoint/確認権の入力は拒否する。

新規提案時は実付与済みWRITE scopeと選択calendarを検査し、events.listを1件・1秒区間でprobeして実ACLを確認する。owner/writer/writerWithoutPrivateAccessはcreateを提案可能。update/deleteには詳細を確認できるowner/writerを要求し、writerWithoutPrivateAccessでは隠された属性を「なし」と扱わず拒否する。未知・reader/freeBusyReaderは書込提案できない。

update/deleteはそのcalendar/eventの固定events.get endpointから現在の対象を取得し、event IDとprovider ETagの一致を検査する。別対象のETag文字列だけでは証拠にならない。cancelled、attendees、attendeesOmitted、recurrence、recurringEventId、locked、default以外のeventTypeは対象外。start/endも検証する。HTTPはopaque IDをpath encodeし、UTC応答を要求する。明示IANAがない応答時刻の根拠は要求したUTCであり、local timezoneを推測しない。

secret読出し後・provider送信直前・各応答後・保存時に現在の権限を確認する。provider I/Oは再試行されるDB transactionの外。固定Google HTTPS、10秒timeout、redirectなし、transport retryなし。失敗は共通errorへ正規化し、provider本文・tokenをlogへ出さない。createは最大1回、update/deleteは最大2回のprovider参照が通常の提案コスト。並行した初回要求はそれぞれ参照し得るが、同じreceiptへ収束する。書込APIは呼ばない。

`calendar_proposals/{id}`のargs_hashは、正規化内容、tenant/本人/integration、target/calendar/ETag、実scopeとintegration世代、原producerの権限・出典、生成時刻、初期5分の期限、存在すればrun/tool/checkpointを固定する。期限は生成から5分で再送・再起動・suspendによって延長しない。期限そのもの・hash・内容の改変を拒否する。GETは現在認可された本人にexpired/declined/cancelledの状態と固定内容を返せる。期限は操作確認の期限であり、履歴閲覧の一律禁止ではない。

## 待機と回収

既定RunTurnがcalendar.proposeを登録する。tool effect IDをproposal IDとして使い、canonical run/tool executionからrun_id/tool_call_idと次のcheckpoint_versionを導く。checkpoint_idはrunとcheckpoint版に束縛したserver UUID。モデルが関係を指定できない。

handlerが固定proposalを保存した後、Confirmations.suspendが同じ期限と関係をrun_proposalsへ写す。pendingならrunをwaiting_confirmation、jobをwaitingにしてleaseを解放し、既存契約のconfirmation.required（calendar_write、ID、期限だけ）を保存する。生成handlerはresume_mode=result。将来のconfirmで生成handlerを外部書込handlerとして再実行しない。

canonical proposalの保存とsuspendの間でもreject/expiryを受理できる。suspendはその決定を取り込み、toolの固定拒否結果、checkpoint、resume jobを同じtransactionで保存する。通常のrejectも共有DB primitiveで両proposal/tool/run/jobを一回更新する。任意caller resultを受け取らない。run cancelは生成中も含め未確定canonical proposalをcancelledにし、再開を抑止する。

境界はnow >= expires_at。`CalendarProposals.expire_unsuspended`は最大100件のページで単独proposalとsuspend前の停止窓を回収する。待機後は既存`Confirmations.expire`がcanonicalとrun_proposalsを同じtransactionでexpiredにし、認可されたrunだけを再開する。失効済みはjobをobsoleteへ進める。どちらも返るcursorを追う。scheduler配備は既存RUN07と同様に後続の運用接続で行う。本人GETにも同じ期限処理がある。

worker停止が5分を越えるとleaseも失効する。旧workerはsuspendできず、通常のlease回収・新workerが固定済みresultを再利用して同じ期限のexpiredへ収束する。確認待ちworkerを保持しない。拒否・期限の再開はモデルを呼ばず未実行の説明を保存してcompleted。応答だけを失っても永続receipt/決定を再読取し、二重消費しない。

## 私的出典、export、消去

proposalは原producerのsubject、session、credential familyと独立runtime epoch、account/member/tenant/control ledger、選択とgrantを現在状態で検査する。run由来では元会話・入力messageとFact/Note/Task/Reminder依存を検査する。受理済みpurge epochが変わったproposalは保守的に再利用・表示を止め、本人は新しい提案を作る。

runのcalendar_sourcesへproposal IDを保存し、tool reuse、次turnの履歴、run GET、SSE、exportにも同じ原出典検査を適用する。後続出典を現在runから再帰的に取り込まず、生成時の既存calendar_sourcesを固定する。原session/family失効後に別端末から古い私的内容を再公開しない。既存summaryはdirect user入力のみを対象にし、assistant/tool由来のproposal内容を要約へ混入させない。

export inventoryはcalendar_proposalsの公開projectionを含み、取得とdownload時に現在の本人・原出典を再確認する。secret/内部originはarchiveへ出さない。新collectionは本人member/account/tenant purgeのinventoryとrun由来のsource purge inventoryに含む。単独HTTP生成にもownerを保存し、runがないことを消去漏れの理由にしない。

### 出典の現在検査と再送

作成の再送検索、canonical 保存直前、結果の最終公開は同じ原出典検査を使う。
現在の caller と原 producer の session / credential family / output の双方を検査し、
正常な同一キー再送は provider I/O なしで同じ immutable proposal を返す。
古い proposal の origin / purge epoch を再採番しない。provider 送信前に採った origin は
各応答後と保存 transaction でも一致しなければならない。

worker は provider 送信直前、各応答後、保存 transaction で既存 RunState の
current / history / tool 結果 validator を実行する。これが成功した入力からのみ
strict な complete dependency inventory を作る。消去用 manifest 単体を認可証明とはしない。
Fact の参照 entity・訂正リンク・source_refs は MemoryRepository と共有する有限な検査を通す。
room / appliance の allowlist 投影も共通化し、モデル・preview・export で原 caller と output の両方へ適用する。
Note / Task / Reminder、入力 message、以前の Calendar 出典も現在状態へ照合する。
原 origin は以前の Calendar 出典だけを保持し、自分の proposal を再帰参照しない。

出典検査は入口呼出しごとに独立した iterative DFS を使い、同じ祖先 proposal は一度だけ展開する。
source grant の共有は同じ tenant / 現在 principal subject・stamp・context / 正確な source に限定する。
原 subject / output は各 immutable proposal で検査し、mode（モデル再利用か閲覧か）・runtime epoch・
policy・source resolver・clock は一つの検査 pass 内で固定する。結果を次の呼出しや transaction / retry へ
持ち越さない。重複排除したノードの時刻 guard も pass 終了と親 transaction の保存・応答返却まで保持する。

会話の run GET / messages は raw store を注入しても、この読取の transaction を guard 付きで実行し、
その成功 attempt の no-I/O guard を HTTP の threadpool 復帰後にも検査する。
SSE は取得と最終 delivery の両 transaction の guard を保持し、threadpool 復帰後の yield 前に検査する。
原 Pi の session / family と全祖先の期限を、現在本人の user bearer の期限で置き換えない。
履歴・export の最初の出典検査で拒否した item は従来どおり除外し、失敗した pass の途中 guard は
残りの item へ持ち越さない。一度採用した item が最終返却までに失効した場合は、その応答全体を拒否する。
遅延 guard の認可拒否・認可基盤障害も既存の公開 error に正規化し、開始済み SSE は本文を追加せず閉じる。
同期の原出典検査でも、原deviceなどの認可台帳の一時障害は再試行可能な503として扱う。
現在本人の台帳読取が成功していても、原出典の障害を「対象なし」の404や空結果へ変換しない。

proposal の create（初回と再送）/ GET / reject と run cancel も、最後の読取の成功 attempt から
結果と ReleaseGuard を一緒に返し、HTTP threadpool 復帰後まで保持する。最後の guard は no-I/O であり、
別 transaction の認可結果を使い回したり、provider を再実行したりしない。遅延中の原出典失効で応答を拒否しても、
既に保存した canonical proposal / receipt / declined / cancelled は残る。再送も現在の原出典検査を通す。
Calendar 由来の export ZIP は header 前の最終 guard 拒否を404、認可基盤障害を503へ正規化する。
header 送信後の拒否では次のchunkを送らず中断し、既に始まった200を後から4xxへ変えたとは扱わない。

一つの pass は最大64個の unique proposal、重複を含む4,096 source edgesまでとし、循環も拒否する。
超過時は現在認可の拒否として扱い、新規 proposal / receipt を保存せず、閲覧も返さない。
過去の origin や stamp を付け替えたり、祖先を切り捨てたりして上限を回避しない。
上限は履歴の長い会話にも適用される。実 provider 読取後の保存前検査で上限へ達した場合も、
その読取を成功した保存とは扱わない。検査回数とfake/EmulatorのDB呼出回数を検証するが、
Firestore本番のlatency・費用を計測したとは扱わない。

新規保存・モデル再利用では Fact の confirmed / deletion epoch / 有効区間を検査し、
その時刻 guard を最後の read から write と応答返却まで保持する。
本人の履歴閲覧は Fact.valid_to だけでは拒否しないが、出典の訂正・抑止・削除や
原認可の失効による表示停止は引き続き適用する。応答後の guard 拒否は既に durable な
commit を rollback したと主張せず、再送も原出典の現在検査で止める。

## 検証・導入・切戻し

`tests/fakes/calendar_proposals.py`のHTTP・scope/ETag・改変・期限・応答喪失ケースを通常fakeとFirestore Emulatorで共用する。default tool、早期reject、同期限、再起動・失効lease回収、Pi propose単独scope、原family失効、exportとmember/account/tenant消去を追加検証する。Emulatorは本番制限・実Google ACLを完全には再現しない。

既存CAL02のsourceにoperationがない場合はcalendar.readとして扱う。新しいproposal producerのため既存データ移行は不要。旧/不完全なproposalの権限・出典を推測して補わず拒否する。依存追加はない。独立削除・認可台帳を先に再適用してから復旧する。切戻し前にcalendar.proposeをagent allowlistから外し、未確定proposalを拒否/取消/期限回収する。新collectionのexport/purge handlerは残存データの消去が終わるまで維持する。古いworkerへ戻して未認識のproposal本文を残したまま消去完了扱いにしない。

実Google OAuth/provider、cloud、staging索引・競合・費用、実Pi/mobile UI、本人confirm/外部書込/結果不明照合はこのチケットで実行しない。検証はsynthetic資格情報とlocal fake/Emulatorを使う。

一次資料（2026-10-27確認）: [events.get](https://developers.google.com/workspace/calendar/api/v3/reference/events/get)、[Event resource](https://developers.google.com/workspace/calendar/api/v3/reference/events)、[ACL role](https://developers.google.com/workspace/calendar/api/v3/reference/acl)。
