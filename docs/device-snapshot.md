# 端末の状態復旧

BA-DEV-04 / FR-012・FR-014、T-021・T-030・T-035。スマホで確定した個人session、Piに割り当てられたrun、未完了command、未表示notificationを [snapshot API](../specs/openapi.yaml) と [WSS gateway](device-gateway.md) で届ける。

`GET /v1/devices/{device_id}/snapshot` は専用device bearerだけを受け取り、毎回現在のcredential・membership・session・出典を検査する。sessionだけの同期はconversationやmemory権限を要求しない。期限切れsessionの状態保存は全ての読み取りの後に行う。snapshot、heartbeat、再接続はidle期限を延長しない。

1頁はrun・command ID・notification IDの候補を合計20件まで走査する。Firestoreのtransaction内でdeviceの等価条件、document ID順、start_after、limitを適用してからmaterializeする。3種類を跨ぐlookaheadを含めても1要求23候補以下であり、失効した候補も走査予算に数える。session metadataは各頁に付く。通知本文は既存通知APIから個別に現在権限で取得する。署名cursorは5分期限でtenant・device・credential family/epoch・認可・sessionへ束縛し、collection phaseと最後のdocument IDを保持する。scope変更は400となり、端末は部分結果を捨てて再開する。

これは複数transactionを跨ぐ時点固定snapshotではない。走査中の新規追加は次の一巡で回復する。全頁のIDは `POST /v1/devices/{device_id}/snapshot:validate` に送り、現在の元の依頼者・出力先・conversation/Calendar出典・reminder・command権限を再検証してから公開する。先行頁で認可済みでも、最終検証時に失効したIDは除外する。最終検証はdevice認証と同じscope stampを必須とし、UUID型の重複なしIDを全種類合計128件まで受け付ける。履歴queryは行わず、指定IDのcanonical recordと各出典だけを読む。run候補ごとの既存出典検査は省略しない。最終状態も128件かつ64KiB以内で、command受領cursorは進めない。

| 値 | 意味 |
| --- | --- |
| snapshot `connection_generation` | device credentialの認可世代 |
| WSS `connection_generation` | 再接続のたび増えるtransport世代 |
| run `assignment_generation` | 不変なjob.outputへの初回割当は1。job再試行・credential更新・再接続では増やさない |
| `delivery_cursor` | commandとnotificationが共有する永続配送sequenceの観測値 |

snapshot cursorやnotificationの表示receiptはcommand受領の証明ではない。端末はsnapshot取得でSQLiteのcommand delivery cursorを進めない。並行watchによる順序逆転・欠番はpending IDから照合し、家電実行はBA-DEV-07の責務とする。

Gatewayは確定済みcanonical DBの有界な1頁と現在sessionを1秒ごとに読み、session_started・run_available・session_revoked・notificationを送る。送信直前にはleaseと対象1件の現在の出典権限だけを確認し、hintごとの全履歴走査を行わない。通知sequenceは既存delivery receiptに保存し、commandと同じcounterからtransaction内で採番する。各API replicaは再起動後にも同じcanonical stateを読める。プロセス内の通知履歴は復旧の正本にしない。

Piの [SnapshotSync](../services/device/src/brainoid_device/snapshot_sync.py) はhello_ack後に全頁を取得し、全頁と最終検証が揃った時だけ状態を置換する。1回に16頁まで進め、未完了ならready=falseのまま次回へ継続する。本文を含まない途中頁のrun/command/notification IDとcursorを0600の `snapshot-recovery-v1` に保存し、通信断・プロセス再起動後に再開する。device・tenant・登録epoch・API originが違うjournalは使わない。端末DTOは署名artifact内のsnapshot_models.pyにも含め、API DTOとのschema一致を検証する。旧bootstrapのcontracts packageに新moduleがなくても更新版が起動する。旧cloud journalやSQLite schemaの変更はなく、旧版へのrollbackは新ファイルを無視できる。登録resetは既存保護store全体の消去に含める。

現在の端末メタデータ上限は有効項目128件、未確認表示receipt128件。過去の期限切れrunと表示済み通知は有効項目数に数えない。上限超過時は不完全なsnapshotの公開を止め、retry可能なcloud_unavailableとして後から再取得する。資格情報・ペアリングは消さない。履歴が増えると一巡の頁数と回復時間は増えるが、1要求の候補materializationとrun読取りは20件以下に制限され、cursorから続けられる。全128候補の最終検証も各ID一回の検査で、全履歴再走査はしない。通知取得は最大128 pending IDと最大128未確認receipt IDを重複排除して個別に検証する。

WSS frameは再同期のきっかけであり、そのpayloadを直接表示権限にしない。2秒ごとのHTTP再同期でフレーム喪失も回復する。個人sessionの終了・置換・期限切れとrun割当の消失はoutput世代を変え、最初の頁・続きの頁・最終検証・通知本文の遅いHTTP応答とSSE queueを公開不可にする。失効時は保存済みの部分metadataも消し、新sessionの応答へ旧requestの世代を引き継がせない。100ms周期の期限監視は、ネットワークが無応答でも個人本文を保持し続けない。メモリ内だけの通知本文を消し、音声を自動再生しない。

ローカルUI境界の `GET /local/cloud/snapshot` は公開直前に現在権限で全頁を再取得し、session・run・IDで一意な通知inboxと `play_speech: false` を返す。同じIDの再同期は別の通知を追加しない。表示側が実際に受け入れた後、既存local session/CSRFを使って `POST /local/cloud/notifications/{id}/displayed` を呼ぶ。ID receiptを先に永続化し、cloudへのdisplayed ACKが失われても再送する。通知のexpires_atは音声自動再生期限であり、未表示テキストと表示receiptの寿命には使わない。5分以上offlineでも現在認可されたpendingテキストを復旧する。未確認receiptは期限で捨てず、ACK成功またはcanonical GETのdisplayed/acknowledgedを確認した時だけ除く。失効中にACKできないIDは保持し、再認可後も二重表示しない。自動再送のGETとACKの間で出典・sessionが失効した場合、403・404・410等の対象単位の拒否は本文を消してreceiptを残し、資格情報・接続・Runtime healthを失効させない。device/family失効と予期しない例外は既存の停止・障害通知経路で処理する。表示受付時は対象IDの実行中の通知GETを無効にし、ACK成功でreceiptを除いた後に古いpending応答が戻っても本文を復活させない。保持するのは同期lock下の読み取り1件と表示世代カウンターだけで、無期限の表示済みID集合は作らない。ローカルsnapshotは生成時の表示・client・output世代を応答と共に引き渡し、ASGI送信直前にも検査する。生成後に表示受付やscope失効があれば古いsnapshotは409となり、再取得する。sessionやrunの世代を表示受付のために変更しない。通常の型付きoperation（会話・message・runの一覧/取得、作成等）も処理開始前のclient/output世代と明示session IDを保持し、owned task完了から応答送信まで再検査する。新sessionの世代を古い本文へ付け替えない。通常operationやSSEに通知表示のカウンターは適用しない。deviceのdisplayedは本人のacknowledgedにはならない。表示用UIはBA-UI-10で実装する。

検証は [復旧テスト](../tests/test_device_snapshot.py) 、[ローカル同時操作テスト](../tests/test_device_snapshot_local.py)、[端末接続テスト](../tests/test_device_snapshot_transport.py) と [公式Emulatorテスト](../tests/emulator/test_device_snapshot_integration.py) に置く。実ローカルHTTP/WSS、フレーム欠落、2回目のtransport世代、途中頁の再起動、歴史データ、通知ACK喪失、session失効と遅いcallbackを含む。実クラウド・実Pi・音声・Home Assistantはこの変更では使用しない。
