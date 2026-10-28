# Piアプリの署名更新と復旧

BA-DEV-08、FR-014 / AC-014、T-025。[updater](../services/device/src/brainoid_device/updater.py) は Agent tool から独立した Linux 用のローカル supervisor で、[常駐サービス](device-service.md)の起動と切替を所有する。production root、配布URL、公開pipeline、実機への導入は提供しない。信頼rootは導入者が別経路で配備する公開metadataで、秘密鍵は端末に置かない。

## 信頼と配布形式

`python-tuf 7.0.1` の `Updater` に信頼rootの bytes を明示し、root → timestamp → snapshot → targets を refresh して target の長さ・SHA-256を検証する。root rotation、署名threshold、metadata rollback、expiry、delegationの検証はこの既存ライブラリが担当する。root cache を無条件の信頼起点にはしない。信頼rootのSHA-256を最初のjournalに固定するため、設定だけを別rootへ変更して既存installを乗っ取れない。正しい端末時計が前提で、時計異常時にexpiryを無効化しない。

`metadata/` は専用cacheで、同じupdate directory inodeの非blocking flockを保持するprocessだけが利用する。ライブラリのatomic writeに加え、refresh後は検証済み途中metadataを含めfile/directoryをfsyncする。通常の取得はHTTPSとOSのCAを使用し、TUF既定のproxy環境が適用される。socket timeout 3秒、更新準備全体120秒、root rotation最大16、delegation最大8、metadataサイズ制限を設ける。main threadでのみ実行する。

target名は英数字・underscore・hyphenからなるbasenameと `.tar`。配布物は**非圧縮USTAR、通常fileだけ**で、次の内容に限定する。releaseの実行依存は配備済みPython 3.13環境の固定lockとABI `brainoid-device-python313-v1` を使用する。依存パッケージの変更・native binary・venv・OS更新はこの形式では行わない。

| path | 内容 |
| --- | --- |
| `release.json` | [manifest v1 schema](../packaging/device/update-manifest.schema.json)。単調増加整数version、Linux、現在のarchitecture、protocol 1、SQLite schema 2、runtime ABI |
| `lib/**/*.py` | 配布するPythonソース。`lib/brainoid_device/__init__.py` と `__main__.py` が必須。固定entrypoint `brainoid_device.__main__.main` が `serve` を受ける |
| `lib/brainoid_device/device_protocol.schema.json`、`lib/brainoid_device/run_events.schema.json` | daemon が import する固定の2 schema。正本 specs と同じ内容を同梱する。他の lib JSON は拒否 |
| `ui/**` | [既存static境界](device-service.md#保存領域と配信範囲)で許可された表示asset。`ui/index.html` が必須 |

archive全体96MiB、展開合計64MiB、各file 8MiB、4096 file、深さ16まで。USTARの固定headerを先に検査し、PAX/GNU extension、圧縮、directory entry、symlink/hardlink/device/FIFO、重複、絶対path、`.`/`..`、隠しcomponent、backslashを拒否する。archiveのmode/ownerを適用せず、新しい0700 stagingへ0600 fileを作る。資格情報・SQLite・journal・音声・会話は配布物に含めない。署名されたPython自身は信頼するアプリコードであり、サービスUIDを侵害したコードから秘密を隔離するsandboxではない。

## 起動と更新

DEV-10 の schema 同梱版には、この2 path を許可する updater が必要。旧 updater は安全側に拒否するため、導入者は検証済み checkout/lock の updater を既存 bootstrap 手順で配置・再起動してから新 target を適用する。archive だけで依存や updater 自身を更新した扱いにはしない。初回の `uv sync --all-packages --locked` 環境は今回の直接依存と同じ固定版を既に含む。device だけを独自導入した環境では、導入者が lock の実行依存を事前確認・配置する必要があり、未配置のまま activation を成功扱いにしない。

非root専用UID、Linux 5.4以降のpidfd / waitid(P_PIDFD)、所有processの `/proc` 情報の読取、既存のprivate/state directory、実行依存が前提。初回の空stateは既存 `python -m brainoid_device check` で正常に初期化してから、updaterへ渡す。updaterはDBを作り直したりmigrationしたりしない。state・credential・update directoryは相互に同一・親子にせず、すべてcanonical path、サービスUID所有0700にする。信頼rootは通常fileでgroup/other書込不可とする。root、祖先、既存release/cacheのlinkや危険な権限を検査する。

次は導入者が準備した値によるコマンド形式であり、このリポジトリにproductionのURL/rootは付属しない。

~~~bash
python -B -m brainoid_device.updater \
  --root /var/lib/brainoid-updates \
  --trust-root /etc/brainoid/update-root.json \
  --repository https://updates.example.invalid/brainoid/ \
  --state /var/lib/brainoid-agent-state \
  --credentials /var/lib/brainoid-agent \
  --host 127.0.0.1 --port 8765 \
  --target device-42.tar
~~~

このprocessは切替後もdaemonを所有して待機する。`--target` を省くと保存済み正常版を起動し、中断更新を先に復旧する。次の更新は現在のsupervisorへSIGTERMを送り終了を確認してから、新targetを付けて起動する。既存の単独daemonやsystemd serviceは導入者が事前に正常停止する。updaterは既存PIDを検索して停止したり、systemdへ命令したりしない。二重起動はupdate/state lock、port衝突、child固有healthによって拒否する。既存unitは単独daemonのままで、自動移行しない。

TUFによる取得・展開を完了後、`releases/<version>-<sha256>/` にarchiveと展開treeを配置してfsyncする。単一の `current` symlinkをatomic replaceするのが起動参照の切替である。supervisorはjournalが選んだ同じtreeを直接起動し、起動前に保存archiveのSHA-256と全展開fileを再照合する。`current`だけを手動で差し替えても選択版にはならない。

新候補を起動する前に旧daemonの正常終了を待つ。既存lifespanがlocal sessionを破棄し、Runtime所有の音声task/childをcancel・停止・reapする。音声adapterはまだ未接続だが、追加時もこのRuntime所有契約を守る必要がある。daemonへのSIGTERMは14秒、強制終了・子孫回収はさらに2秒を上限とし、未回収なら新規起動を禁止する。supervisorはLinux subreaperとして所有sessionの孤児だけを回収する。session leaderをreapする前に同じsessionの全子孫を停止する。`setpgrp()` で別groupへ移った子孫も対象にし、pidfdを取得してPID/birth/sessionを再確認した後、そのhandleへsignalとreapを行う。別sessionや再利用されたPIDを対象にしない。daemonize/setsidによる所有sessionからの離脱は許可しない。spawn中のSIGINT/SIGTERMは所有登録まで保留する。supervisorの異常終了時はLinux parent-death SIGTERMがdaemonを停止する。

最小healthには単一のmonotonic期限を設ける。main threadのtimerでconnect・HTTP header・health本文・UI本文のblocking readを中断し、response/socketを閉じてから失敗を返す。byte間隔がsocket timeout未満でも全体8秒を超えて成功せず、読取workerを残さない。成功判定直前にも期限を照合する。確認する内容は8秒以内の生存、子固有のrandom activation header付き `/healthz`、配布したUIの取得である。healthは既存保存層のSQLite読書きを含む。偶然同じportで200を返す別processでは成功しない。cloud認証、実音声device、HA、実browser描画の健康確認は接続済みと主張せず、DEV-10/QA-05へ残す。固定イベント `device_update_serving`、`device_update_recovery_required`、`device_update_shutdown_failed` だけを出し、secret/URL/tracebackは出さない。正常停止exit 0、設定・更新・cleanup失敗exit 1、CLI構文エラーexit 2。

## 永続状態と切戻し

| journal状態 | 再起動時の動作 |
| --- | --- |
| `stable` | 正確に記録された正常版だけを再検証して起動する。初回で正常版がなければtargetが必要 |
| `pending` | 未確定候補を再実行せず、保存済み正常版を起動する。候補versionのfloorは保持 |
| `blocked` | 起動・更新を停止し、運用者による復旧が必要 |

journalには信頼root hash、受入済みversion floor、正常版と候補のversion/hashを保持する。候補を展開した後、旧daemon停止より先に `pending` とfloorをatomic write・fsyncする。起動とhealthの成功後だけ正常版を置換する。最終journalのreplace/fsync失敗では、以前の正常版を保持して再度pendingを永続化してから切り戻す。通常例外のrollback中断も同じjournalから再開できる。起動/health失敗なら旧版へ戻し、旧版も失敗したら `blocked` としてcurrent参照を外す。初回更新の失敗に旧版はないため同じ復旧状態になる。

TUF metadataのversionとアプリの整数version floorは別である。新規取得ではfloor以下を拒否し、失敗版の再配布にはより大きなversionを必要とする。offline再起動/rollbackでは保存済みの正確な正常版のhashだけを例外として許可する。このローカル起動は最新metadataのexpiryに依存しないが、期限切れmetadataによる新規更新は認めない。stageやcurrent.pendingの残骸は候補として起動しない。journal欠落時に既存releaseを新規installとして採用せず復旧待ちにする。releaseの自動削除は行わない。

SQLiteは `APPLICATION_ID=0x42524456`、現行 `user_version=2` のみ許可する。manifestもschema 2に一致しなければ取得を拒否する。新旧アプリともこのschemaを維持する配布契約であり、破壊的migrationをpointer切替で逆転できるとは扱わない。実DBがfuture schemaになった場合は旧版を起動せずblockedにする。command/credential snapshotを戻す処理はない。`accepted` / `started` / `outcome_unknown` とexecution ID、cursor、秘密fileを保持し、updaterはcommand実行APIを呼ばない。reset markerは復旧を遮断し、資格情報を復活させるために削除しない。

blockedの場合はsupervisorを停止し、journal・全release hash・metadata・DB/credentialの整合状態を保護して、容量・権限・媒体・対応schema・署名rootを確認する。正常版自体が検証できなければ自動再試行しない。原因修復後の運用者によるjournal再開は `good`、`floor`、信頼hashを保持した `pending` への変更をatomic write/fsyncして行う個別復旧作業とし、このCLIでは提供しない。DBやreset markerの変更、floorの低下、初期化による復旧はしない。SQLite自体の復旧は[保存層手順](device-storage.md#移行と復旧)に従う。

## 検証と実機電源断の手順

[テスト](../tests/test_device_updater.py)は毎回新しいEd25519鍵で[synthetic TUF repository](../tests/device_update_fixture.py)を生成する。test rootは `brainoid_test_only=true` を持ち、通常CLIでは拒否する。`--test-repository` を明示した試験時だけそのrootとloopback HTTPを使える。production鍵・署名pipelineの代替にはしない。

~~~bash
mise exec -- uv run --all-packages --locked --offline python -B -m unittest discover -s tests -p test_device_updater.py -v
~~~

実署名/hash/期限/metadata rollback/root rotation、OS/architecture/protocol/schema/ABI拒否、archive境界、extract/rename/最終journal replace/fsync失敗、pending各段階の再起動、正常版復帰・復帰失敗を確認する。実childではUI/session/音声相当taskと子の停止、別groupを含む孤児reap・回収不能時の起動遮断、無関係/stale PIDの非操作、遅いheader/health/UI streamの期限・reader閉鎖、別listener拒否、supervisor `_exit` 後の再起動も確認する。実API/worker/Firestore経路と既存依存版は変更しないためEmulator再実行はこの受入に不要。

実Piの物理電源断は未実施。QA-05では次の手順で媒体/filesystem/OS imageごとに測る。

1. 本番から分離したPi・媒体・test rootを用意し、正常版Nと候補N+1のhashを記録する。synthetic資格情報とaccepted/started/outcome_unknown台帳をseedし、外部家電操作を無効化する。
2. download中、展開中、directory rename前後、pending fsync後、current.pending作成後、current replace前後、起動直後、health後、最終journal fsync前後、rollback中に電源を切る。各点を繰り返し、実時刻と停止点を保存する。
3. 再投入後、未検証/未完了候補が起動せず、正常版またはblockedになることを確認する。`current`、journal floor/good/pending、archiveと展開fileのhash、TUF cache、DB integrity/application_id/schemaを確認する。
4. command/execution ID・cursor・結果不明状態・資格情報を照合する。再実行ゼロ、旧private session無効、音声停止を確認する。reset markerを置いたケースでは秘密の復活やmarkerの迂回がないことを確認する。
5. 故障でfsync/atomic renameの前提を満たさない場合はrolloutを止め、媒体と復旧手順を修正する。OS全体のA/B rollbackや実provider/cloud/audio確認の合格とは扱わない。

例外注入とprocess終了の合格は、物理電源断・ストレージcontrollerのflush保証ではない。耐久性はOS/filesystem/媒体がfsync、rename、SQLiteの前提を守ることに依存する。

一次資料（2026-10-28確認）: [TUF 7.0.1 metadata・license](https://github.com/theupdateframework/python-tuf/blob/v7.0.1/pyproject.toml)、[完全なclient workflow](https://theupdateframework.readthedocs.io/en/latest/api/tuf.ngclient.updater.html)、[固定版Updater source](https://github.com/theupdateframework/python-tuf/blob/v7.0.1/tuf/ngclient/updater.py)。TUFは配布検証を担当し、展開・起動・rollbackは本実装の責任である。process handleと期限の契約は[Python os pidfd](https://docs.python.org/3.13/library/os.html#os.pidfd_open)、[signalとtimer](https://docs.python.org/3.13/library/signal.html)も参照する。


## Runtimeのcommand guardianとの境界

BA-DEV-09の[所有処理](device-service.md#ライフサイクルと-unit)は、daemonと同じsessionの信頼済みguardianからcommand専用sessionを作る。guardianは同じ検証済みreleaseのpackage treeをisolated Pythonで読み、親の終了時もcommand子孫を停止してから終了する。updaterはguardianをSIGKILLして先に消さず、SIGUSR1で強制drainを要求する。guardianの異常終了、期限内に空にならないsessionではunreaped leaderと所有状態を保持し、rollback/new launchへ進まない。guardianの成功終了0は子孫消滅の確認後だけで、command自体のexit codeとは別。`brainoid-guard` のprocess roleは同一owned session内の保守的な停止扱いにだけ使い、借用PIDや別sessionを操作する認可に使わない。

guardian初期化中のrole判定とSIGKILLの競合を避けるため、未分類processへ直ちにSIGKILLを送らない。pidfdでSIGSTOPを送り、後続の走査で停止状態を観察してからroleを再判定する。guardianならSIGUSR1とSIGCONTでdrainを進め、停止した通常processだけをSIGKILLする。guardian roleはcommand生成より前に設定し、通常roleへ戻さない。各走査は待機せず、単独leaderや遅れて生成された子も既存の全体期限内で繰り返す。停止状態を確認できなければ所有状態を保持して失敗し、子の数に比例した個別待機期限を追加しない。
