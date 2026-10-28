# Pi command 実行と再接続照合

BA-DEV-07 / FR-011・FR-014、T-017〜019・T-036。[DeviceApplication](../services/device/src/brainoid_device/app.py) が [CommandExecutor](../services/device/src/brainoid_device/command_executor.py)、WSS transport、SQLite と [Home Assistant adapter](home-assistant.md) を所有する。local HTTP・会話には HA service を直接呼ぶ入口を追加しない。

## 受領・開始・結果

1. installed device schema、tenant/device/登録epoch、WSS generation、operation型・値、期限を確認する。command envelope と execution ID、初回reportを SQLite に同時commitする。command/notification 共用sequenceは欠番・順序逆転を許すhigh-water markであり、未記録commandを飛ばす根拠にしない。重複IDのpayload変更とsequence衝突は拒否する。
2. 現在認可されたcommandをHTTPで再読込し、`GET /v1/tenants/{tenant}/commands/{command}/target` から固定対象のexternal referenceを取得する。この限定readは `home.control`、現在の元actor・削除・端末allowlist・target fingerprintを既存Commandsと同じ境界で検査する。通常entityの `memory.read` は要求しない。返るtenant/device/command/entity、integration、exact external IDをローカル設定と照合する。
3. durable accepted reportのACKを待ち、同じexecution IDで `command_start` を送る。有効grantの同じWSS世代・execution・期限を確認し、SQLiteの開始commitが成功した一度だけadapterへ進む。HAの能力GET後も同executionのgrantを再照合し、その間の現在target・許可・session変更をPOST前に拒否する。
4. HAのHTTP受付をdurable記録し、別GETの観測が要求したon/off・輝度・温度・modeに一致した場合だけsucceededとする。送信前の確定拒否はfailed、POST応答喪失・観測不能・不一致はoutcome_unknown。provider bodyや例外文はreportへ含めない。
5. statusごとの完全report（observed_at・observed_state・固定error code）をSQLiteに保存して送る。ACK記録前の切断・再起動では同じpayloadを再送する。cloudでterminalになりwatch/snapshotから消えた場合も、ローカル未ACKを列挙する。

helloの `command_response_ids` capabilityを新cloudがhello_ackで受諾した接続だけで実行する。応答にはrequest envelopeのmessage_idを `request_message_id` としてechoし、Piはkind・command・execution・generation・request IDを照合する。status ACKにはstatus名がないため、一接続の要求を直列にし、受領ACKを待ってから次へ進む。同じ要求を同じstreamで再送しない。応答timeoutはsocketを閉じ、新WSS世代で保存済み要求を照合する。遅延した受領ACK・重複grant・旧socket callback・異なるexecution・reset後のcallbackは実行権限にならない。capabilityを提示しない旧Piには新cloudも旧payloadを返す。新Piと旧cloudでは交渉が成立しないためcommand実行を停止し、heartbeat・snapshot・通常のcloud bridgeは継続する。cloudを先に更新してからPiを更新できる。取消のcanonical確認は独立したsettled印でpendingを退役し、架空の結果ACKを作らない。

## 中断・時計・不明結果

WSS送信中の通常切断・I/O失敗は再接続で復旧し、Runtimeを恒久的な資源障害状態にはしない。保存済み受領・結果を再利用する。task取消は伝播し、予期しない例外はRuntimeの障害として可視化する。

開始commit前の中断は、同executionのgrantを再取得して開始できる。commit後はHA POSTを二度と行わない。HA受付がdurableに残れば現在状態の観測で照合し、受付の証跡がないcrash/POST応答喪失は、現在状態が偶然一致しても成功へ変えない。確定できなかったoutcome_unknownはそのまま報告する。短いgrantが失効しローカル開始がない場合は、同executionのcloud claimを確認して非実行failedを報告する。自動補償・新executionへの置換は行わない。

server hello/heartbeat/grantの時刻とPi壁時計の差は5秒以内、同一processのwall/monotonic差分の変化は1秒以内を要求する。時計の後退はSQLiteのhigh-waterでも再起動をまたいで拒否する。期限判定にはmonotonic経過と5秒の不確実性を加えた上界を使い、短すぎるgrantは実行しない。reportの時刻は実際に受信したserver時刻を使うため、進んだPi時計でfuture reportを固定保存しない。時計異常は実行を停止する。管理者は時刻同期を修復してdaemonを再起動する。

## 保存・更新互換

[CommandJournal](../services/device/src/brainoid_device/command_journal.py) はbase SQLite schema2へ独立した `command_execution_v1` と `command_clock_v1` を追加する互換extensionである。base列・user_version・updater manifestのschema2 ABIを変更しない。新しい共有package import・依存追加もない。未知のextension版は拒否する。envelope/reportは各8192文字、未解決commandは256件に制限し、満杯・I/O障害では新規実行しない。

両tableはcommands/registrationへのON DELETE CASCADEを使う。旧schema2 daemon/updaterは開け、旧prune/resetも関連extensionを消去できる。旧DEV06版にはcommand実行器がないためrollback中は実行せず、再更新後に同じexecution・結果を照合する。未ACK結果・不明結果は保持し、ACK済みの確定結果とcanonical取消のsettled記録だけを期限後かつ既定7日後に掃除する。SQLiteを古いsnapshotへ戻す操作は許可しない。

旧コードを固定commitから隔離importしたopen・updater check・prune・reset、署名device-only artifactを旧contracts配置で起動する検証を [upgrade test](../tests/test_command_upgrade.py) と [signed artifact test](../tests/test_device_snapshot_upgrade.py) で行う。CIは既存どおり履歴を取得する必要がある。

## 検証範囲

[executor tests](../tests/test_command_executor.py) は実API認可・cloud command transaction・SQLite・HA HTTP fixtureを結合し、重複、grant/ACK/POST/観測喪失、開始前後crash、clock skew/rollback、期限、取消、private失効、世代・allowlist変化、保存障害を注入する。[transport test](../tests/test_command_transport.py) は実DeviceApplicationとloopback WSS gatewayを起動し、結果ACK喪失から新接続での再報告とtask/socket回収を検証する。[official Emulator target test](../tests/emulator/test_command_target_integration.py) はcontrol-onlyの限定解決と現在権限・fingerprint・response releaseを確認する。

実HA・Pi・クラウド・ホスト導入は行わない。物理状態、実機時計同期、ネットワーク障害下の運用確認はQA/配備時の検証に残る。
