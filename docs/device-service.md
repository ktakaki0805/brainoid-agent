# Pi 常駐サービスとローカル UI 境界

BA-DEV-01/02/09/10、FR-003/014。`brainoid-device` は Python の独立プロセスとして静的 UI とローカル状態 API を提供する。ページの再読込み・ローカルセッションの終了はプロセスやその所有タスクを再起動しない。[SQLite保存層](device-storage.md)は設定・配送位置・command台帳とresetを提供する。[cloud 接続・ペアリング・型付き会話中継](device-cloud.md)は管理者が明示設定した場合に常駐 task として起動する。音声・家電操作は未接続。更新は[専用supervisor](device-updates.md)が所有する明示的なローカル経路として提供する。

## 起動・設定検査・停止

workspace の `mise run setup` 後、非 root の開発ユーザーで次を実行する。本番は[専用ユーザーの unit](../packaging/systemd/brainoid-agent.service)を使う。ここで用意する HTML は境界確認用の synthetic fixture で、`apps/display` の完成済み UI ではない。

~~~bash
fixture_dir=$(mktemp -d /tmp/brainoid-device-demo.XXXXXX)
mkdir -m 755 "$fixture_dir/display"
mkdir -m 700 "$fixture_dir/private"
mkdir -m 700 "$fixture_dir/state"
printf '%s\n' '<!doctype html><title>Device fixture</title><p>Local service fixture</p>' > "$fixture_dir/display/index.html"
export DEVICE_HOST=127.0.0.1
export DEVICE_PORT=8765
export DEVICE_STATIC_DIR="$fixture_dir/display"
export DEVICE_CREDENTIALS_DIR="$fixture_dir/private"
export DEVICE_STATE_DIR="$fixture_dir/state"
mise exec -- uv run --all-packages --locked --offline python -B -m brainoid_device check
mise exec -- uv run --all-packages --locked --offline python -B -m brainoid_device serve
~~~

別の端末で `curl --noproxy '*' http://127.0.0.1:8765/healthz` を実行すると `{"status":"ok"}` が返る。停止は起動端末の Ctrl-C、または所有している daemon PID への SIGTERM を使う。unit を導入した環境では `systemctl stop brainoid-agent` が同じ終了処理を呼ぶ。HTTP の停止 API は設けていない。

| 必須設定 | 受け入れる値 |
| --- | --- |
| `DEVICE_HOST` | `127.0.0.1` または `::1`。その一方だけで待受ける。DNS 名、wildcard、LAN IP、IPv4 mapped IPv6 は拒否 |
| `DEVICE_PORT` | 1024〜65535 の十進数。0 による自動選択はしない |
| `DEVICE_STATIC_DIR` | `index.html` を含む、存在する絶対 canonical directory |
| `DEVICE_CREDENTIALS_DIR` | サービス実行 UID 所有の mode 0700 directory。空なら未登録状態で起動可能 |
| `DEVICE_STATE_DIR` | 資格情報・staticとは別の既存 canonical directory。サービス UID 所有、mode 0700。SQLiteとその管理fileだけを置く |

root 実行は拒否する。設定不足・不正な値・読めないstatic/credential・危険な権限は、設定名と固定理由を stderr に出して exit 2。渡された値、ファイル内容、URL、traceback は出さない。`check` の成功は `device_configuration_ok` と exit 0 であり、listen しない。`check` もSQLiteの作成/migration・読書き検査・未完了resetの再開を行い、保存層の復旧が必要なら固定 `device_storage_recovery_required` と exit 1。`serve` は保存層の復旧状態でもlocal health/stateを提供し、通信・家電操作の前提を閉じる。開始・終了は固定イベントだけを出す。bind 失敗・startup の例外/期限超過・cleanup 失敗は exit 1、SIGINT/SIGTERM による正常終了は exit 0。起動 hook の待機中でも、停止要求を受けて協調的に cleanup を完了した場合は listen 未開始のまま exit 0 とする。HTTP access log と runtime library の内容を含むログを無効にしている。

## HTTP 契約

公開 cloud OpenAPI とは別の、端末内だけの契約である。共通 ID と時刻は `brainoid-contracts` の UUID/RFC 3339 UTC 表現を再利用する。例の origin は `http://127.0.0.1:8765`。IPv6 の場合は `http://[::1]:8765` とする。

| method / path | 条件・応答 |
| --- | --- |
| `GET/HEAD /healthz` | loopback/Host/Origin 検査後、session 不要。正常 200 `{"status":"ok"}`、停止中・所有 task の異常・保存層の復旧待ち/reset未完了時は 503 `{"status":"unavailable"}` |
| `GET/HEAD /`、`/index.html`、明示された asset path | 検査済み UI snapshot を配信。session 不要。未知 path は 404。SPA fallback・directory listing は行わない |
| `POST /local/session` | 正確な Origin と `X-Brainoid-Local: 1` が必須。200 で `csrf_token` と残秒 `expires_in`、HttpOnly Cookie を返す |
| `GET/HEAD /local/state` | 現在の Cookie が必須。`status`（running/stopping）、`instance_id`（プロセス生成ごとの UUID）、`started_at`（UTC）、`software_version`、`storage_status`（unregistered/ready/reset_pending/recovery_required）を返す。tenant・cursor・資格情報参照は返さない |
| `DELETE /local/session` | Origin、現在の Cookie と、その session に結び付く `X-CSRF-Token` が必須。204 で session を消去し Cookie を期限切れにする |

updater所有の起動では `/healthz` にその子を識別する `X-Brainoid-Activation` headerを付ける。これは認可tokenではなく、別listenerの200応答を更新成功と誤認しないための識別子である。通常の単独起動では付けない。

BA-DEV-09は `GET /local/diagnostics`（session必須）、`POST /local/reset/prepare` と `POST /local/reset`（session/CSRF必須）を追加する。prepareは60秒のsession束縛tokenを発行する。実行は `X-Brainoid-Reset-Token` と `X-Brainoid-Confirm: local-administrator` を必要とする。完了200は `local_reset: complete`、未完了503は `local_reset: incomplete` とし、両方で `remote_revocation: unconfirmed` を返す。確認不足/期限切れは403。成功後はlocal sessionを破棄する。診断はservice/storage/reset、OS link・時刻同期・空き容量、保存音量/マイク設定だけを返す。cloud/audioはunavailable。導入と運用は[Pi導入手順](device-setup.md)に従う。

全要求は body なし。Content-Length は省略または 0 に限り、transfer encoding は拒否する。WebSocket と CORS は提供しない。アプリケーションのエラー本文は固定 `{"error":"code"}`。未対応 method は 405 `{"error":"method_not_allowed"}` と `application/json` を返し、`Allow` は対象 path の対応 method を保持する。状態変更の Origin 不足/不一致と CSRF 不備は 403、session 不備は 401、session 上限は 429、重複 security header は 400。HTTP 構文の不正や runtime の過負荷拒否は h11/Uvicorn がアプリケーションより先に応答する場合があり、その応答は JSON 契約の対象外とする。

セッション Cookie `brainoid_local` は Domain 指定なし、Path=/、HttpOnly、SameSite=Strict、最大30分。HTTP loopback のため Secure 属性は使用しない。256-bit random token と独立した CSRF token をメモリだけに持ち、最大32 session、期限延長なし。期限内の同じ Cookie に対する bootstrap は同じ CSRF token と残期限を返す。daemon 再起動時にはすべて失効し、UI は再 bootstrap する。cloud refresh token や HA token を含まない。

ブラウザー側の最小接続例:

~~~javascript
const session = await fetch('/local/session', {
  method: 'POST', headers: {'X-Brainoid-Local': '1'}, credentials: 'same-origin'
}).then(response => response.json());
const state = await fetch('/local/state', {credentials: 'same-origin'}).then(response => response.json());
// 明示的にローカル UI session を終了するときだけ実行する。
await fetch('/local/session', {
  method: 'DELETE', headers: {'X-CSRF-Token': session.csrf_token}, credentials: 'same-origin'
});
~~~

同一 origin の POST/DELETE にはブラウザーが Origin を付ける。GET/HEAD は Origin がない場合を受け入れ、存在すれば完全一致を必要とする。Host は実際の設定 port を含む数値 authority と完全一致。Origin の null、末尾 slash、別 scheme/port、複数値を拒否する。Host/Origin/Cookie/CSRF/bootstrap/Fetch Metadata/長さ指定の重複も拒否し、`Sec-Fetch-Site` があれば same-origin または none に限る。forwarded header を認可や peer の上書きに使わない。

アプリケーション応答に no-store、nosniff、DENY、no-referrer と同一 origin に限定した CSP を設定する。外部埋込み、inline script、外部 resource、service の cloud 接続を UI に委ねる構成は許可していない。未実装の音声・操作 API は 404/405。

この境界は悪意ある Web origin と DNS rebinding からローカル HTTP を保護する。OS 上の別プログラムの本人認証ではない。ローカルプログラムは HTTP header を作成でき、root またはサービス UID はサービスの秘密を読める。配備時は kiosk/browser のユーザーと daemon 専用ユーザーを分け、同じ origin で配信する UI を信頼する。信頼できない UI や browser 拡張をここに配置しない。

## 保存領域と配信範囲

static・credential・state directory は相互に同一・親子関係を禁止する。各 directory の全祖先を directory FD から `O_NOFOLLOW` で開く。祖先は root またはサービス UID 所有で、group/other に書込可能なものを拒否する。root 所有 sticky directory（開発用 `/tmp` 等）は例外とし、その内部のサービス所有 entry は他 UID が差し替えられない。設定検査後に祖先を symlink へ差し替えても開かない。資格情報は保持した directory FD に対して操作するため、後から pathname を差し替えても別領域には書き込まない。

credential file はサービス UID 所有、mode 0600、通常 file、hardlink なし、64KiB 以下。起動時と read 時に検査する。`CredentialStore.write` は同じ private directory に mode 0600 の一時 file を作り、fsync/atomic replace で置き換える。read/write/resetはdirectory inodeの非blocking flockで直列化し、競合時は有限に失敗する。reset中の専用markerは既存・再生成・別processのstoreを遮断する。未完了resetで残った不正fileは読まずに削除を再試行する。現在のサービスに credential の HTTP read/write route はない。今後のadapterは保存層のreadyと現在の認可を確認した上でstoreを使う。

static は root/サービス UID 所有、group/other 書込不可、通常 file のみ。symlink、hardlink、隠し entry、許可外拡張子を拒否する。HTML/JS/CSS/SVG/PNG/JPEG/WebP/AVIF/ICO/WOFF/WOFF2 を受け入れる。1 file 8MiB、総量64MiB、directoryを含め4096 entry、深さ16まで。設定 JSON、source map、資格情報を配信物に含めない。起動時に検査済み bytes を読み込み、要求時に filesystem path を開かない。起動後の file/symlink 差替えも配信内容を変更しない。UI の更新には service restart が必要である。URL の percent escape、backslash、`.`/`..` component を拒否し、path+query 2048 bytes、header合計8KiB に制限する。

## ライフサイクルと unit

所有タスクと子プロセスは `Runtime.start_task` / `start_child` に登録する。完了 task は保持集合から除き、例外内容をログに出さず回収する。起動 hook 自体も所有 task とし、最大5秒の完了期限を設ける。SIGINT/SIGTERM は Linux event loop の signal handler に登録し、selector を起こして起動待機へ停止 event を通知する。周期 worker がなく Event だけを待つ場合も、起動期限を待たず cleanup に進める。終了時には元の signal handler を復元する。停止要求を受けた協調的な起動中止と、起動例外・期限超過・取消拒否による異常終了を区別する。起動途中の失敗も同じ cleanup を通る。child 生成呼出元の取消が発生しても、内部生成 task を shield して所有登録を完了してから cleanup する。BA-DEV-09では小さな信頼済みguardianがcommandを専用Linux sessionに置き、async-facing handleで終了を観察する。handleの `pid` はdaemonと同じsessionに残るguardianを指す。`wait()` / `returncode` は直接の子の終了結果を返すが、子孫が残る間は `waitid(WNOWAIT)` によりleaderをreapせずPID/sessionの同一性を保持する。呼出元が `waitpid` / `Popen.poll` でleaderを回収したり、子孫を別sessionへdaemonizeすることは禁止する。通常終了でsession全体が空になれば監視taskがleaderを回収し、所有集合から除くため、短命childのzombieを溜めない。 commandのexit codeとguardianの成否は専用pipeで区別し、guardianの異常終了をcommand成功にしない。guardianは実行中daemonと同じ検証済みpackage treeを明示してisolated Pythonで起動し、PYTHONPATHに依存しない。親の終了でもPDEATHSIGでcommand sessionの消去を継続する。

終了時は listener を閉じ、HTTP 処理を最大3秒待ち、所有 task を cancel、guardianへSIGTERMを送り、guardianが専用command session全体を停止する。pidfdを取得後にPID/birth/sessionを照合してsignalを送り、process groupを変えた子孫も扱う。processをsubreaperにするが、回収対象は所有session内だけに限る。全child共通の期限で最大3秒待ち、残るguardianへSIGUSR1で強制停止を要求し、guardianがcommand sessionへSIGKILLを送り、遅れてforkされた子孫も再走査して最大3秒で回収する。子孫の消滅を確認してからcommand leaderをreapし、guardianがclosedを返して終了する。その後Runtimeがguardianもreapする。期限内に消滅を確認できない場合はguardianを殺さずleader/handleを保持して安全な再試行を可能にし、resetのdurable fenceも外さない。task（起動 hook を含む）の取消完了は最大3秒、event loop の最終 drain は1秒。停止要求からの待機上限は HTTP 3秒 + 子の TERM/KILL/reap 6秒 + task 3秒 + 最終 drain 1秒で計13秒。起動中は HTTP 待機を必要とせず、起動期限5秒を停止要求後に追加しない。繰返しの停止信号でも lifespan cleanup を省略しない。取消を無視する task が残れば正常終了を主張せず exit 1 とする。拡張は cancel に協力し、未登録・daemonize した子を作らない。CPU を同期的に占有する拡張や OS 側で停止不能なプロセスの最終上限は unit の `TimeoutStopSec=15` と `KillMode=control-group` が担い、契約に反して別sessionへ離脱した子孫も最終的にcgroup全体で回収する。ローカルresetはdaemonを生かすため、この最終unit停止に依存せず、サポートする所有session全体の停止確認を消去完了の条件とする。

unit は `brainoid-agent` 専用 User/Group、UMask=0077、資格情報用 `brainoid-agent` とSQLite用 `brainoid-agent-state` の2つの StateDirectory（mode 0700）、読取専用 system/home 保護、権限昇格禁止、空 capability set、core dump 禁止を定義する。`serve` 自体が設定検査するため、復旧用local APIを妨げる `ExecStartPre check` は置かない。[環境設定例](../packaging/systemd/device.env.example)は秘密値を含まない。`/opt/brainoid/.venv/bin/python`、built display directory、`/etc/brainoid/device.env` と専用ユーザーの準備が導入前提。実機導入・kiosk・audio 権限調整は BA-DEV-09/BA-VOICE-04 の範囲とする。このチケットは host のユーザー作成、unit install/start を実行しない。

DEV-01からの更新はcredential directoryをそのまま維持し、新しいstate directoryと `DEVICE_STATE_DIR` を用意する。SQLiteの初回作成・旧版migration・切戻し/破損復旧は[保存層の運用手順](device-storage.md#移行と復旧)に従う。UI snapshot と session は再作成される。credentialやcommand台帳を削除する切戻しはしない。

## 検証と限界

`mise run test` に [境界と Linux 実プロセス試験](../tests/test_device_service.py)を含む。focused 実行は次の通り。外部 API や秘密値は不要。

~~~bash
mise exec -- uv run --all-packages --locked --offline python -B -m unittest discover -s tests -p test_device_service.py -v
~~~

Linux 実プロセス試験は IPv4/IPv6 listener、実 interface の非 loopback 接続拒否、Origin/HTTP parser、UI の繰返し HTTP GET と同一 instance、SIGINT/SIGTERM、取消無視 child の強制回収、task finally、部分起動失敗、起動待機中の SIGINT/SIGTERM と TERM 無視 child、協調/取消無視の起動 hook、無信号での起動期限超過、周期処理なしの起動待機に対する両 signal の応答と handler 復元、取消無視 task の有限終了を確認する。fixture と異なる OS の実プロセス試験は skip として明示される。ASGI 正負例だけを LAN 非到達やプロセス寿命の根拠にしない。

Linux x86_64 での HTTP 再読込み検証を、実 GUI browser、Raspberry Pi、systemd によるインストール/起動の検証として扱わない。DEV-01時点の描画確認は未実施だった。BA-DEV-09ではbuilt UIをlocal headless browserで検証する。実機の性能・権限・OS統合は引き続き未実施。unit は `systemd-analyze verify` で静的確認する。未設置の `/opt/brainoid/.venv/bin/python` は元 unit の検査で失敗するため、開発環境では executable path だけを存在する Python に置き換えた一時 copy の検査と区別して記録する。

この変更は cloud API/永続層・Firestore transaction・認可 repository を変更しない。従って device の受入に Emulator は不要で、ローカル OS process/socket と fixture で独立検証する。device/cloud bridge の追加境界と Emulator 検証は [BA-DEV-10](device-cloud.md) を参照する。

設計確認の一次資料（2026-10-27）: [Starlette lifespan](https://starlette.dev/lifespan/)、[response/Cookie](https://starlette.dev/responses/)、[Uvicorn 設定](https://uvicorn.dev/settings/)、[固定版 server source](https://github.com/Kludex/uvicorn/blob/0.54.0/uvicorn/server.py)、[systemd process 終了](https://www.freedesktop.org/software/systemd/man/latest/systemd.kill.html)、[Python event loop の Unix signal](https://docs.python.org/3.13/library/asyncio-eventloop.html#asyncio.loop.add_signal_handler)。依存の条件は[ツール台帳](toolchain.md)に記録する。
