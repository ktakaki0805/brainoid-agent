# Piの設定・cursor・command台帳

BA-DEV-02、FR-003/014、T-017。標準Python `sqlite3` による[保存層](../services/device/src/brainoid_device/ledger.py)を[常駐サービス](device-service.md)へ接続する。追加のパッケージ依存はない。cloud・Home Assistantの実呼出し、実行判断、resetの確認UIはDEV-05/06/07/09で接続する。

## 永続化と利用契約

| 保存対象 | 内容 |
| --- | --- |
| settings | protocol版1、保持秒数（既定604800、最低7日）、音量0〜100、マイク消音。任意JSONや本文を保存するAPIはない |
| registration | tenant/device ID、登録ごとのUUID epoch、現在のconnection generation、配送cursor |
| commands | command/execution ID、epoch、immutable payloadのSHA-256、配送seq、受領/開始generation、状態、受領/期限/更新時刻、結果ACK有無 |
| credential_refs | cloud refresh / Home Assistant用途から保護fileのbasenameへの参照。token本文・pathは保存しない |

私的会話、音声、通知本文、session本文、cloud全記憶、LLM cacheは保存しない。秘密は既存の `CredentialStore` に置く。resetは参照先以外の古い資格情報・書込途中の一時fileも消す。

`DeviceStorage.register` は未登録のときだけ新しいepochを作る。同じtenant/deviceの再呼出しは既存登録を返し、別登録への変更はresetを必要とする。epochはローカル登録の寿命、`set_generation` はserverが発行した接続世代で、混同しない。再登録では同じtenant/deviceでもepochが変わる。呼出元は登録時に取得したepochを保持し、古いframeに新しいepochを付け替えない。

`receive_command(CommandDelivery, expected_cursor, now_ms, clock_trusted)` はtenant/device/epoch/現在generationを照合し、commandとcursorを同じtransactionで保存してから返す。呼出元は[端末schema](../specs/device-protocol.schema.json)に従ってframeを検証し、tenant/device/target/operation/created/expires/private sessionを含む全immutable payloadの正規化hashを渡す。connection generationとdelivery情報だけを含むhashでは不十分。同じID/内容の再配送は既存execution IDと状態を返す。変更された内容・期限・配送seq、古いepoch/generation、未知commandのcursor巻戻しは拒否する。再接続後の新しいgenerationで既存commandを受けてもexecution IDは変わらない。

配送cursorは比較更新する。受信順またはsnapshotで未処理commandをすべて保存してから先へ進む。`advance_cursor` はcommand以外の処理済み配送に限り、未保存commandを飛ばす用途に使わない。時刻はUTC epoch milliseconds。時計が信用できないと受領/開始/期限掃除を拒否し、期限切れ新規commandは `expired` として記録して実行しない。

`record_started` はDEV-07が検証したgrantのcommand/execution/generationと期限を照合し、開始状態とgenerationを保存する。一度だけ `accepted → started` をcommitして `True` を返す。再受信は `False`、失敗は例外であり、どちらも実行許可ではない。これ自体は本人session、対象allowlist、操作型・範囲、最新cloud認可の検証を代行しない。`accepted` は成功ではない。開始後のprocess終了でも `started` を維持し、wireへは `outcome_unknown` と対応付ける。再開時に自動でacceptedへ戻さない。

`record_result` はexecution IDに束縛した固定状態だけを保存する。acceptedからexpired/failed、startedからsucceeded/failed/outcome_unknown、照合済みoutcome_unknownからsucceeded/failedを許可する。終端を別状態へ書き換えない。観測値の検証とprotocol応答はDEV-07の責任。`acknowledge_result` は一致する結果のcloud ACKを保存する。`prune_commands` はsucceeded/failed/expiredでACK済み、実行期限経過、最終状態更新から保持期間以上のものだけを削除する。境界時刻を含む。accepted/started/outcome_unknownと未ACK結果は古さだけで消さない。削除した古い配送もcursor/期限検査で再実行を拒否する。

## 保存の失敗と状態

state directoryはサービスUID所有0700、DB/journal/管理markerは0600の通常fileかつhardlinkなしを要求し、static/資格情報とは分離する。Linuxのdirectory FDと `/proc/self/fd` で開く。directory inodeの非blocking flockを生存中保持し、同じstateを2 daemonで開かない。資格情報storeのflockも非blockingで、他processの保持を無期限に待たない。SQLite busy timeoutは200ms。

`BEGIN IMMEDIATE` / `COMMIT` を明示し、rollback journal `DELETE`、`synchronous=EXTRA`、foreign keys、secure delete、trusted schema無効の実効値を検査する。起動時はintegrity/foreign key検査を行う。read/healthも小さなrevision更新をcommitして読書き可能性を確認する。I/O、FULL、read-only、BUSY、破損、所有権やfile置換を検出すると、そのinstanceは `StorageUnavailable("device_storage_recovery_required")` で閉じる。COMMIT後に応答だけ失われた可能性も成功と推測せず、close/reopenして保存済みID/状態と照合する。元のSQL例外・path・秘密を外へ返さない。

`snapshot()` は `unregistered / ready / reset_pending / recovery_required` と、正常時だけ登録・設定を返す。`permits_operations` は保存層の前提のみ。呼出元は毎操作の直前に現在状態を確認し、cached snapshotで通信/操作を続けない。未登録・reset中・復旧中は新規cloud通信/家電操作を始めない。daemonのhealthは未登録でも保存層が正常なら200、reset/復旧待ちなら503。local stateには状態名だけを追加する。

## local reset

内部API `DeviceStorage.local_reset() -> bool` を提供し、HTTP routeや人の確認UIはまだ提供しない。呼出元は通信・音声・操作を停止し、取得済みtokenなどのメモリも捨ててから呼ぶ。既存のローカルUI sessionはcleanup callbackで破棄する。新しいprivate cacheを追加するadapterは、保存領域の削除とメモリ消去を冪等なcallbackへ必ず組み込む。現在のdaemonにprivate本文/音声のdisk一時cacheはない。

1. 資格情報directoryの予約file `.reset-pending` とdirectoryをfsyncし、durable intentを確定する。すべてのCredentialStoreのread/writeがmarkerを確認する。既存handle、新規handle、別processも旧tokenを取得できない。
2. markerを除く全資格情報fileをunlinkしdirectoryをfsyncする。参照から漏れた旧cloud/HA fileやwrite途中fileも含める。symlinkは辿らずentryだけ消す。
3. private一時データ/メモリcleanup callbackを呼ぶ。例外なら未完了のまま止める。
4. command、credential参照、登録/cursorを一つのSQL transactionで消し、設定を初期値に戻してcommitする。
5. 他の資格情報fileが残っていないことを確認しmarkerをunlink、directoryをfsyncしてから成功を返す。

起動時にmarkerがあれば、通常操作より先に同じ手順を再実行する。どの段階でも中断/失敗は成功とせず、残るmarkerにより消去完了まで通信/操作を遮断する。読書き失敗・破損DBでも資格情報と一時データの消去を試み、SQL消去ができない間はmarkerを外さない。future schemaを削除/再作成しない。intentの作成自体が失敗したときは破壊的処理を始めず、呼出元へ失敗を返して再試行を必要とする。marker削除後のfsync失敗でも成功と返さず、旧資格情報とSQLは既に消去済みである。

これは稼働中processが既に取得したtokenを取り消す仕組みではない。remote側の登録解除・HA credential失効も[運用手順](06-deployment-and-operations.md#53-端末移譲紛失廃棄)に従う。unlink/SQLite secure deleteをSSDの物理消去保証と呼ばない。

## 移行と復旧

初回は空のstate directoryへDBを作り、[version付きmigration](../services/device/src/brainoid_device/migrations.py)を一つのtransactionで適用する。v1は基本台帳、v2はACK列と保持indexの追加。旧commandはACKなしとして保守的に保持する。v1→v2は既存ID/cursor/状態を維持する。新しいuser_version、他application ID、非空の未version DBは拒否する。migration自体のprocess中断はSQLiteのrollbackから再開する。

最初の正常初期化をfsyncした `.initialized` で記録する。初回作成中の空DBは再開可能だが、初期化済みのDB欠落/0byte化を新規installと解釈しない。DBの安全な権限・空き容量・filesystem障害を修復した後、serviceを再起動して検査し、保存済みのcommand IDを照合する。`started` / `outcome_unknown` を再実行する復旧はしない。

DEV-01からは従来の `DEVICE_CREDENTIALS_DIR=/var/lib/brainoid-agent` を維持し、別の `/var/lib/brainoid-agent-state` と環境変数を追加する。unitの2つのStateDirectoryが新規領域を作る。任意の旧SQLiteは取り込まない。更新前はserviceを停止し、DBとjournal・marker・資格情報を整合した組として保護する。稼働中のDBだけをcopy/renameしたりhot journalを消したりしない。

rollbackは保存formatを読める検証済み版へ限定する。v2の追加列は既存値を壊さないが、future version拒否を無効にして旧版を起動しない。DB非対応版へ戻してcommand台帳を無視する運用も認めない。破損時はserviceを止め、command台帳を失わない整合済みsnapshotから復旧する。reset済み端末へreset前backupを戻して資格情報を復活させない。復旧できないDBは隔離して運用者が登録/remote credentialを失効させ、新規端末として再導入する。reset markerだけを手動削除する回復は行わない。

## 検証

`mise run test` に[SQLite/実process試験](../tests/test_device_storage.py)を含む。新規/旧版/future、実processのtransaction/migration途中終了とcommit後再起動、重複command/grant、保持境界、未完了保持、read-only/破損/0byte/欠落、SQLITE_BUSY/FULL、commit応答喪失、reset各段階の終了・unlink/fsync/cleanup/SQL/marker解除失敗、別processの資格情報遮断と有限lock競合を確認する。DEV-01の実processとHTTP境界も回帰検証する。

~~~bash
mise exec -- uv run --all-packages --locked --offline python -B -m unittest discover -s tests -p test_device_storage.py -v
~~~

外部provider/Firestore経路を変更せず、Emulatorはこの受入に不要。実Pi/HA/cloud、systemd install/start、実電源断/媒体故障は未実施。process終了での原子性を物理電源断の測定と混同しない。耐久性はfilesystem/OS/媒体がfsyncとSQLiteの前提を守ることに依存する。

一次資料（2026-10-28確認）: [Python sqlite3](https://docs.python.org/3.13/library/sqlite3.html)、[SQLite transactionとCOMMIT BUSY](https://www.sqlite.org/lang_transaction.html)、[atomic commitとhot journal](https://www.sqlite.org/atomiccommit.html)、[PRAGMA実効値・synchronous](https://www.sqlite.org/pragma.html)、[expand migrationのADD COLUMN](https://www.sqlite.org/lang_altertable.html)。
