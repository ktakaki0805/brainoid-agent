# Piの導入とローカル復旧

BA-DEV-09、FR-003/014。[導入処理](../packaging/device/install.sh)、[復旧画面](../apps/display/src/recovery/index.tsx)、[確認後のreset](../services/device/src/brainoid_device/reset_command.py)を提供する。クラウド登録、音声、家電操作との結合は未提供で、オフラインAIは利用できない。ローカル消去はクラウド登録解除・Home Assistant側の資格情報失効の証明ではない。再読込み後も画面はこの区別を表示する。

## 構成と導入前提

**実機検証済みのOS image・周辺機器の組合せはまだない。** 候補はRaspberry Pi 5 8GB、Raspberry Pi OS Lite 64-bit、systemd/logind、Cage（Wayland）、Chromium。画面/タッチ、USBマイク、スピーカー、推奨電源、冷却、十分な空き容量を持つ記録媒体が必要。音量/マイクの値はSQLiteの保存設定であり、実際の音声deviceの状態ではない。対応機材の拡大はBA-GOV-04、録音/再生はBA-VOICE-04で検証する。

専用の `brainoid-agent` と `brainoid-kiosk` は異なるUID、ログイン不可、追加groupなし。daemonだけが資格情報/SQLiteを読める。kioskは専用PAM sessionからlogindのseat/VTを取得し、Cage配下でChromiumを非root・sandbox有効で起動する。browser profileは `/run/brainoid-kiosk` の0700一時領域、incognitoで、service停止/再起動時に破棄する。端末本文をbrowser storageへ永続化しない。

OS管理者が先に次を準備する。導入scriptはpackage取得、OS設定変更、Wi-Fi設定、秘密入力を行わない。

- Pi OSの提供元、image名/公開日/SHA-256、architecture、kernelを記録する。64-bit、KMS/DRM、logind対応のCage/wlrootsを使用する。
- OSのpackage managerで `cage`、`chromium`、`curl`、`kbd`（chvt）、`libpam-systemd`、D-Bus、systemdを導入する。PAMの `pam_unix.so` / `pam_systemd.so` と `/usr/sbin/nologin` を確認する。CageはMIT、ChromiumはBSD系と第三者ライセンス、OS依存は各packageの `/usr/share/doc/*/copyright` を記録する。OS管理者が更新を保守する。追加Python/Node依存はない。
- tty7をこのkiosk専用に予約する。既存のdesktop、display manager、gettyや別のkioskがtty7/seatを使う構成へ重ねて導入しない。`--reserve-tty7` はこの確認を明示する。unitはgetty@tty7と競合し、起動後にVT7へ切替える。SSH/tty1等、別の管理経路を確保する。
- `/opt/brainoid` に対象commitのcheckoutとPython 3.13を用意する。Pythonは `/usr/bin` または `/opt` 内に配置し、保護されたhome配下のmise interpreterを本番のsymlink先にしない。checkoutと静的assetはroot所有、group/other書込不可とする。専用UIDがコードを書き換えられないことを確認する。

[ツール台帳](toolchain.md)の固定版を使い、対象Pi上の `/opt/brainoid` で `uv sync --all-packages --locked --no-dev --python /usr/bin/python3.13` を行う（OSにそのinterpreterがなければ先に管理者が用意する）。Node側は `pnpm install --frozen-lockfile`、`pnpm --filter @brainoid-internal/display build` を行い、`apps/display/dist` の内容を新しい `/opt/brainoid/display` へ配置する。build/依存準備にはネット接続が必要な場合があるが、準備後の復旧画面はクラウドへ要求しない。開発用fake buildを配備しない。公開署名artifact/updaterとの統合は[別の更新契約](device-updates.md)に従い、未署名のcheckoutを本番配布artifactと呼ばない。

## 導入と再実行

まず隔離した空directoryへ生成内容を確認できる。

~~~bash
stage=$(mktemp -d /tmp/brainoid-image.XXXXXX)
packaging/device/install.sh --root "$stage"
# 任意: このroot内だけで実際のaccount DBと起動enableリンクを作る。hostでは起動しない。
packaging/device/install.sh --root "$stage" --prepare-image
~~~

実機の管理者は前提確認後に対象Piで次を実行する。これは開発検証環境で実行するコマンドではない。

~~~bash
sudo packaging/device/install.sh --root / --activate --reserve-tty7
sudo packaging/device/install.sh --root / --activate --reserve-tty7
systemctl is-enabled brainoid-agent brainoid-kiosk
systemctl status brainoid-agent brainoid-kiosk
curl --noproxy '*' http://127.0.0.1:8765/healthz
sudo reboot
~~~

`systemd-sysusers` は専用accountを一回作り、systemdはmulti-user.targetの両unitをenableする。daemonはnetwork-onlineを待たず、kioskはlocal HTTP応答を待つ（復旧中の503も画面を開く）。起動中のunitに再度startしても再起動しない。固定loopback entryは `http://127.0.0.1:8765/`。資格情報は `/var/lib/brainoid-agent`、台帳は `/var/lib/brainoid-agent-state` の0700領域をsystemdが作る。設定は `/etc/brainoid/device.env` の0600。任意のHost/Originやbodyを受け入れる変更はない。

選択root自身はstickyでもgroup/other書込不可を必須にする（私有rootの祖先の `/tmp` は許可）。新しいdirectoryは0700で作ってから0755へ、fileは0600で作ってから所定modeへ明示設定し、callerのumaskに依存しない。既存directoryのmodeを緩めない。scriptは全出力先を先に検査し、symlink/hardlink、危険なowner/権限、別unit/同名account、固定entryと不整合な設定を拒否する。管理manifestを先に書き、作成前の中断は再実行可能。書込途中の不完全fileや編集済みunitは上書きせず停止する。管理者が保存済みsourceと照合して該当する管理fileだけを隔離し、同じ版で再試行する。manifestの内容も不完全なら自動採用しない。DB/資格情報/設定を「再インストールのため」に消さない。設定の既存コメントは保持する。管理下unitの更新は、この同一版の再実行機能とは別にレビューする。

## 切断時の確認と回復

画面の「状態を再確認」でローカルsessionを取り直し、service/保存層、link、OSの時刻同期、空き容量、保存済み音量/マイク設定を観察する。linkありはインターネット到達性を意味しない。`timedatectl` が使えなければ同期は未確認。cloud/audioは未提供のまま。通信断からのクラウド自動復帰・ペアリングは後続adapterの実装を必要とする。

~~~bash
ip -brief link
ip route
timedatectl status
df -h /var/lib/brainoid-agent-state
systemctl status brainoid-agent brainoid-kiosk
journalctl -u brainoid-agent -u brainoid-kiosk --since today
loginctl seat-status seat0
ss -ltnp
~~~

秘密や個人本文を診断資料へ貼らない。LAN側からlocal APIへ接続できず、127.0.0.1:8765だけで待受けることを確認する。OS network/時刻/容量を修復し、必要なら `sudo systemctl restart brainoid-agent`、表示だけなら `sudo systemctl restart brainoid-kiosk` を行う。serviceが停止している場合はHTTP自体を使えないため、管理経路から修復する。再起動で登録済み台帳のcommandを未実行へ戻したり、期限切れ操作を再送しない。[SQLite復旧](device-storage.md#移行と復旧)を優先し、markerやhot journalを手で消さない。

## 本人確認と初期化

画面で「初期化の内容を確認」を押し、消去内容と管理者であることをcheckboxで確認して60秒以内に実行する。キーボードではTab/Space/Enterで操作できる。「戻る」は消去しない。確認tokenは現在のlocal sessionに束縛され、一回限り。Origin/Host/session/CSRFを満たさない要求では処理しない。この境界はローカルOS account本人の追加認証ではない。OSと同一originのUIを信頼する[DEV-01境界](device-service.md#http-契約)の上で、端末を管理する本人が画面を操作する。

resetは新規work/遅延callbackの世代を無効化し、DEV-02のdurable intentを作り、メモリの私的表示/音声/tokenを消して所有taskと専用session内のchild/子孫を停止する。信頼済みguardianがPID再利用を防ぐためcommand leaderを保持し、pidfdで同一性を照合して子孫まで停止/回収を確認する。guardianはupdaterから見えるdaemonのsessionに残り、親の強制終了でも消去を続ける。Runtime/updaterはguardianを先にSIGKILLせず強制drainを要求し、未完了では成功/rollbackを認めない。取消finallyの後にもcleanupを行い、[既存のlocal reset](device-storage.md#local-reset)で資格情報・SQL登録/cursor/待機commandを消す。HTTP切断でresetを取消さない。終了に失敗したらmarkerを保持して操作を遮断する。再起動時はDEV-02が消去を再開し、旧startup hookを再開しない。成功時はlocal sessionを破棄し、同じプロセスは未登録の復旧画面だけを提供する。接続adapterを自動再開しない。

future adapterは `Runtime.start_task/start_child` にすべての処理を登録し、`register_private_cleanup` に冪等なメモリ/一時file消去を登録する。非同期処理は開始時の `generation` を保持し、結果の公開/実行前に `permits_work(generation)` と現在の保存層/認可を検証する。未登録task、daemon化child、取消を無視して外部操作を続けるadapterは契約違反。`start_child` の返すhandleで `wait()` しても、子孫が残る間のleaderの回収はRuntimeだけが行う。通常完了sessionは自動回収する。現在の本番entryに私的本文/音声のdisk cacheや実cloud/audio adapterは存在しない。

応答喪失や消去失敗は成功と表示しない。状態を再確認し、未完了ならserviceを再起動して再試行する。intent自体の書込に失敗した場合も完了とは扱わず、容量/権限を修復して明示的に再実行する。初期化後に旧backupを戻さない。unlinkを媒体の物理消去保証としない。

端末を渡す前に、**クラウド管理側の端末登録解除**、**Home Assistant側のtoken失効**、local reset、service再起動後の未登録を別々に確認する。画面の外部失効状態は常に「未確認」であり、local reset成功から変更しない。

## アンインストール

~~~bash
sudo packaging/device/install.sh --root / --activate --uninstall
# 隔離imageの登録解除は --root "$stage" --prepare-image --uninstall
~~~

両unitを停止/disableして、一致する管理fileだけを削除しdaemon-reloadする。異なる内容のfileは削除せず停止する。account、ownership manifest、設定、app、SQLite、資格情報は保持する。manifestを残すことで同じ版の再導入時に既存専用accountを識別できる。秘密の消去は先に上記resetを実施する。account削除やapp/媒体の撤去は管理者が残存process/UID利用を確認した後に行う。アンインストールはdevice登録の遠隔失効ではない。

## 検証記録と実機gate

ローカル試験は [reset/HTTP/私的fixture](../tests/test_device_recovery.py)、[隔離rootの実sysusersとsystemctl enable](../tests/test_device_provision.py)を `mise run test` で実行する。built displayは実local serviceとbrowserで、CSP、通信先、keyboard、checkbox、cancel/error/reset/reloadを確認する。既存のDEV-01/02の実process・中断reset回帰も実行する。Firestore/provider経路は変更しないためEmulator再実行はこの変更の根拠には不要。

以下は**未実施の外部条件**。Pi5 8GB実機で担当者が次を記録するまで対応OS初期状態/物理再起動/周辺機器の受入を完了扱いしない。

| 記録 | 必要な証拠 |
| --- | --- |
| 環境 | 実施者/UTC、Pi型番/RAM、OS image名/SHA-256、kernel/aarch64、systemd/Cage/wlroots/Chromium/Python/app commit・asset hash、電源/冷却/媒体/画面/タッチ/マイク/スピーカーの型番 |
| 初期導入 | 新規OSから前提準備、導入2回のexit、専用UID/GID、unit/権限、boot enable、tty7/logind seat、LAN非到達 |
| 再起動/ネット断 | 電源再起動後に両processが非rootで起動、loopbackのbuilt UI、LANを切断しても復旧画面が表示される写真/ログ、cloudを利用可能と誤表示しないこと |
| 初期化 | synthetic private表示/音声buffer/待機commandを使い、確認cancel/実行、中断/再起動、未登録、旧token/epoch/action再利用拒否、remote失効未確認の継続 |
| 限界 | 音声/クラウド/HAとの結合が未提供なら未実施と記録。実電源断とprocess終了を同一の測定と扱わない |

一次資料（2026-10-28確認）: [Cageのsystemd起動とPAM/logind](https://github.com/cage-kiosk/cage/wiki/Starting-Cage-on-boot-with-systemd)、[systemd-sysusersとroot指定](https://www.freedesktop.org/software/systemd/man/latest/systemd-sysusers.html)。Cageのseat起動方式を採用し、専用tty7、既存multi-user.targetへのenable、個別unit名で既存display managerを置き換えない。実機のpackage build/権限差は上のgateで確認する。
