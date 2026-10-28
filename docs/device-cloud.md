# Pi のクラウド接続と型付きローカル中継

BA-DEV-10、FR-003/004/014、T-004/023/024。[常駐サービス](device-service.md)の `DeviceApplication` が [CloudClient](../services/device/src/brainoid_device/cloud_client.py)、[DeviceTransport](../services/device/src/brainoid_device/device_transport.py)、[UIBridge](../services/device/src/brainoid_device/ui_bridge.py) を所有する。ブラウザーの終了・再読込みは pairing、資格情報更新、WSS を再起動しない。会話画面 UI-03、登録画面 UI-09、snapshot の session/run 割当解釈 DEV-04、家電実行 DEV-07、音声再生は別の境界である。この変更に frontend source の変更はない。

## 起動と信頼境界

OS 管理者が既存の `/etc/brainoid/device.env` に `DEVICE_CLOUD_API=https://api.example.test` のような **API origin** を設定して service を再起動する。`/v1`、末尾 slash、query、fragment、userinfo を付けない。省略時は従来のローカル復旧機能だけを提供する。URL は管理者設定だけから取得し、UI 入力や remote `events_path` を接続先として使用しない。TLS の通常の証明書・hostname 検証を維持し、環境 proxy、redirect、cookie 認証を使わない。

fixture 専用の `DEVICE_CLOUD_MODE=local-test` は literal `127.0.0.1` / `::1` だけを許し、HTTP/WS を許可する。通常 mode は HTTPS/WSS 必須。実運用の検証無効化・外部 HTTP の例外ではない。Firebase user token を Pi に設定する項目はない。

HTTP/SSE は専用 device bearer を Authorization header に付与する。WSS は同じ header と `brainoid-agent.device.v1` subprotocol を使用し、hello の能力は空集合。現在の transport は heartbeat を送受信するが、command/session/run/notification を自動実行・表示・ACK しない。追加 consumer は snapshot の現在認可と出力先を検証した上で DEV-04/07 の境界に接続する必要がある。

## pairing と保護保存

既存のローカル session と CSRF token を取得し、下の pairing 操作を呼ぶ。サービスが installation nonce、作成/交換キー、poll secret、access/refresh token を [CredentialStore](../services/device/src/brainoid_device/storage.py) の0700 directory 内の0600 file `cloud-connection-v1` に保持する。ブラウザーに返す pairing 値は `user_code` と `expires_at` だけ。スマホ側の既存 owner/admin 確認 API で承認すると、サービスが5秒間隔で exchange する。5分の期限後は自動で新しい登録を発行しない。

secret journal v1 は atomic replace と directory fsync を使い、API origin、credentials、rotation version、資格情報の上限時刻、未確定要求、SSE cursor を保存する。SQLite は登録 ID/epoch、wire generation、秘密 file の参照だけを保持する。mutation の本文は request/task の lifetime 内だけに保持する。資格情報 file を先に commit し、次に登録/参照を保存する。中断後は同じ tenant/device の `register()` で照合する。秘密 file を SQLite や browser storage へ複製しない。

access の有効期間は15分、family は最初の exchange から絶対30日。クライアントは要求開始時刻から保守的な上限を計算し、通常は残り60秒で更新する。JWT を decode して権限・有効性を推測しない。配置した HTTPS origin の strict response と各 cloud API の現在認可が authority であり、保存時刻の逆行も停止条件にする。

更新は一つの async lock に直列化する。更新ごとに新しい refresh request UUID/key を作り、**remote 呼出し前に**本文・key・累積試行数・開始時刻を保存する。HTTP 応答喪失、remote commit 後切断、local commit 前 crash でも同じ要求だけを再開する。60秒 receipt grace の内側の55秒、最大3試行（間隔1/2秒）に制限する。作成だけは5分 receipt 内の295秒を上限とする。上限後は旧 refresh を再送せず停止する。同じ秒の更新で access JWT の bytes が同じでも、保存した rotation version で重複更新を防ぐ。

401 は一度更新して同じ要求を再送する。既存 cloud policy は失効 device を404に隠す場合があるため、403/404 も一度だけ資格情報を再確認する。refresh が有効なら resource/session の拒否として返し、pairing を捨てない。refresh 拒否、family 期限、二回目の401では資格情報・私的メモリを消し、所有接続/task を停止する。再開には管理者による remote 状態確認、必要な失効、local reset と service 再起動、再登録が必要。reset 済みの同じ process から adapter を再開しない。

## ローカル HTTP 契約

すべて既存の loopback peer、固定 Host、同一 Origin、local session を必要とする。POST は `X-CSRF-Token` も必須。JSON POST を受けるのは pairing と operations の2 route だけで、`Content-Type: application/json`、一つの Content-Length、32 KiB/3秒上限を検査する。重複 header/JSON key、transfer encoding、未知 field、任意 URL/method/header、cloud Authorization header、query string は拒否する。他の route の body 禁止は継続する。

| route | 入力と出力 |
| --- | --- |
| `GET /local/cloud/status` | `status`: unregistered / pairing / connecting / connected / stopped。token/tenant/private本文を含めない |
| `POST /local/cloud/pairing` | `{"device_name":"Pi"}`。公開 code/期限だけを返す |
| `GET /local/cloud/pairing` | 現行の code/期限だけ。poll secret を返さない |
| `POST /local/cloud/operations` | 下表の discriminated operation。tenant は保存済み credential から決定 |
| `GET /local/cloud/runs/{run_id}/events` | fetch streaming 用の local SSE。必要なら UUID の `X-Private-Session`。bearer は不要 |

operations の共通省略項目は UUID の `private_session_id`。private session の作成・割当はこの API では行わず、cloud が既存 session と操作・表示先を毎回認可する。

| operation | 必須 field | 応答 |
| --- | --- | --- |
| `list_conversations` | なし。`cursor` は省略可 | ConversationPage |
| `get_conversation` | `conversation_id` | Conversation |
| `list_messages` | `conversation_id`。`cursor` は省略可 | MessagePage |
| `create_conversation` | `operation_id`、`conversation`（既存 ConversationCreate） | Conversation |
| `create_turn` | `operation_id`、`conversation_id`、`turn`（text/output_mode/output_device_id） | run_id/status。remote path は返さない |
| `get_run` | `run_id` | Run |
| `cancel_run` | `operation_id`、`run_id` | Run |

すべての ID は UUID。mutations の `operation_id` は UI が最初の操作時に生成して保持し、応答不明なら同じ値と本文を再利用する。サービスは一つの未確定 mutation の key/path/session、canonical body の SHA-256、試行数と期限だけを保護保存する。会話本文や応答本文は disk に保存しない。再起動後は呼出元が同じ typed body を再提示し、hash が一致した場合だけ55秒/最大3回まで同じ key/body/session で照合する。本文を失った場合も新しい key へ自動交換しない。結果不明が残る間は新しい mutation を拒否し、read と診断は利用できる。送信直前に未確定印を保存する。初回の確定拒否だけは新操作を許すが、応答消失・crash・5xx の後の403/404/409では未確定印を解除しない。解除できるのは同じ key の検証済み成功結果または管理者による明示的な復旧だけ。完了 marker に private response は保存せず、再取得も cloud の現在認可を通す。上限を超えた不明結果は自動再実行せず、管理者が cloud の conversation/run を照合してから復旧する。

## SSE と WSS の回復

SSE は chunk 分割と独立な strict UTF-8、複数 data 行の改行結合、64 KiB の各 frame 上限を検査し、`id == seq`、`event == type`、run UUID、連続順序、canonical schema を確認する。heartbeat comment は cursor を進めない。`(run_id, private_session_id)` ごとの seq を secret journal へ commit してから local queue に渡し、再接続では `Last-Event-ID` を送る。最大32個の cursor を保持し、新しい run には非接続の終端 cursor を古い順で退役させる。未完了 cursor が32個なら明示的に拒否する。

local SSE の `event: run` の data は検証済み run event。speech.ready は検証・cursor更新だけ行い、UI に再生要求を出さない。再読込み/再起動や410/409では、認証付き run GET と message page を取得して `event: history`（run/messages/replace=true/play_speech=false）を返す。message は常に1件/page（最大8000文字のUTF-8/JSON escape、20 source_refs、cursorを含め64 KiB以内）を現在認可付きで取得する。history は最大32 page・12秒で final_message_id を探し、1 pageだけを保持・返す。未到達なら `final_message_pending=true` と continuation cursor を返し、UI は最終本文を未確認として扱う。通常は `final_message_pending=false`。message page の `next_cursor` があれば上の `list_messages` で続きを取得できる。commit 後・UI 配送前の crash では cursor が先行し得るため、UI は history で表示を置換する。進行中の欠落差分を音声で補完せず、終端の final_message_id と履歴で確定する。`event: clear`、切断、session失効、reset は UI が暫定/private表示を消す契約である。将来の UI-03/DEV-04 でその表示処理を実装する。

WSS は hello_ack で受け取る **transport generation** を SQLite に保存し、既存 generation 以下を拒否する。credential/authorization generation を書き換えない。15秒ごとの heartbeat、最後のACKから45秒（三回欠落）で再接続する。再試行は jitter 付き指数1〜30秒。計画接続上限は54〜56分だが、15分 access の期限30秒前が先ならそれを優先する。SSE は45秒無受信で閉じ、cloud の access 期限による close/再認証も同じ資格情報 lock を使う。

local SSE は許可した local session を100msごとにも検査し、logout・期限切れ・無効化で idle upstream を cancel/close、queueを消去する。送信直前の検査も維持する。通常の操作拒否は HTTP に返し、daemon health の resource failure にはしない。予期しない task crash は従来どおり health を失敗にする。

全接続・request pump は Runtime task の所有下にあり、同時 task は16、local SSE queue は8件、HTTP response は64 KiB、通常HTTPは12秒、TLS/WSS handshakeは10秒、sendは5秒を上限とする。WSSのlibrary debug logger、HTTP library payload logger を抑止し、remote error本文や例外文字列を UI/log へ転送しない。redirect/proxy/cookie/compression を使わない。reset の generation と cloud の停止 generation を commit と local response の送出直前にも再確認する。shutdown は client/task を await してから保存層を閉じる。

## 依存・移行・検証

既存 lock と同版の httpx 0.28.1（BSD-3-Clause）、websockets 17.3（BSD-3-Clause）、jsonschema 4.26.0（MIT）、pydantic 2.14.1（MIT）と workspace `brainoid-runs` の公開 DTO を device の直接依存へ追加した。API package/server SDK を device の依存にしない。新しい cloud 権限や外部 provider は追加しない。httpx/websockets は既存 upstream が保守する transport を固定版で使い、今後の更新は lock とこの試験を一緒に検証する。依存の配布・ライセンス表示は device artifact に含める。

SQLite schema 変更はない。旧版への rollback は service を停止してから行い、secret journal・cursor・pending ID を削除しない。旧版は新しい cloud 接続を行わない。再 rollout は保存済み pending deadline を尊重し、古い token を新規要求として再送しない。reset 前 backup から資格情報を復活させない。

[局所試験](../tests/test_device_cloud.py)は actual API fixture、検証有効のローカル TLS/WSS、部分 UTF-8/SSE 切断・別API instance、fake clock、同時401・同秒rotation、exchange/refresh成功後crash、mutation応答喪失、masked失効/通常resource拒否、型付きHTTP負例、実 `DeviceApplication` lifespan/reset を検証する。[新しい Emulator 結合](../tests/emulator/test_device_cloud_integration.py)は real Auth/Firestore runtime の exchange、refresh応答喪失・reopen、現在認可を確認する。既存 pairing/gateway/turn/SSE Emulator suite も関連契約として実行する。

実 Pi、Cloud Run、公開 ingress log、実回線の遅延・実機電源断・実 owner スマホ画面は未実施。frontend source を変更せず、画面の完成・音声/家電実行・snapshot割当をこのチケットの検証結果としない。

一次資料（2026-10-28確認）: [HTTPX AsyncClient と streaming cleanup](https://www.python-httpx.org/async/)、[HTTPX trust_env](https://www.python-httpx.org/environment_variables/)、[websockets asyncio client の header/proxy/limits](https://websockets.readthedocs.io/en/stable/reference/asyncio/client.html)。インストール済み17.3のAPIとも照合した。

BA-DEV-04の[状態復旧](device-snapshot.md)はhello_ack後の全頁同期、session失効時の出力fenceと通知のdisplayed receiptを接続する。
