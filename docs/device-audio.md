# Pi の録音と再生

BA-VOICE-04 / FR-005。Pi daemon が ALSA の入力・出力を所有する。既定は `DEVICE_AUDIO_BACKEND=disabled` で、音声 local route も登録しない。明示的な `alsa` と cloud 設定がそろうと adapter を構成する。画面全体の音声状態機械は BA-VOICE-05、対応機器 matrix は BA-GOV-04。今回の実装は実機公開の承認を意味しない。

## Local control

既存の loopback、Host、Origin、local session、CSRF、32KiB JSON 上限を使う。GET も local session 必須。資格情報と raw audio は UI へ渡さず、任意 URL・header・ファイル名・音声本文を受け取る proxy は作らない。

| Route / operation | 挙動 |
| --- | --- |
| `GET /local/audio/devices` | 入力・出力の `id` / `direction` を最大64件返す |
| `POST /local/audio` / `select` | `input_device` と `output_device` を列挙済み ID から明示選択。両方必須 |
| `POST /local/audio` / `start` | 必要なら `private_session_id` を付けて録音。保存済み microphone mute を尊重 |
| `POST /local/audio` / `finish` | 機器を止めて送信待ちにする。HTTP upload はしない |
| `POST /local/audio` / `send` | 停止済み録音を一度だけ STT に送り、既存の transcript DTO を返す |
| `POST /local/audio` / `discard`、`stop` | 録音・再生・未再生 queue を破棄。`run_cancelled: false` を返す |
| `GET /local/audio` | capture / playback 状態、notice、選択機器、待機文数。他の local session には busy のみ |

個人録音の finish/send は開始時と同じ `private_session_id` が必要。send の `audio_input_id` は通常の `create_turn` に渡せる。`output_mode=speech` の成功した local create_turn が、その run/session に再生を束縛する。UIBridge の認可済み live `speech.ready` だけを daemon queue へ渡す。run cursor は渡す前に既存の metadata 台帳へ保存する。重複文は再取得せず、history、snapshot、daemon 再起動は音声を再生しない。snapshot 内の speech assignment は文字履歴の復元対象として受け付ける。

停止は run/tool 取消と別操作。既存 `cancel_run` はまず local 発話を止め、その後 server の取消結果を取得する。操作済み tool を取消済みと推測しない。UI reload では daemon 所有の録音は残り、同じ local session の GET で制御を再開できる。run SSE の切断・EOF・UI接続終了では現在の再生と待機queueを消去し、そのrunは文字だけを復元する。同runの再接続やreloadで読み上げを復活させず、未受信backlogを再生しない。新しいturnで再び音声を選んだ場合だけ新たな再生意図になる。turn送信時に音声世代を固定し、待機中のstop/discard、機器変更、新録音/turnで失効した遅延成功応答は音声をbindしない。serverで受理済みのrun receipt自体は返し、取消済みとは表示しない。

## 上限と失効

録音は16kHz・16bit signed little-endian・mono、PCM 最大1,920,000 bytes / 960,000 frames / 60秒。WAV は44-byte headerを付ける。停止は socket を閉じて所有 helper へ TERM、guardian が期限後に KILL へ進む。停止後の read/open は世代と状態を再検査して破棄する。全0/空録音は `audio_silent`、拒否・抜去・無入力 timeout は安全な notice と idle に戻す。微小音や雑音を無音と推測しない。

送信待ちを含む録音保持は開始から120秒。STT upload / TTS fetch は60秒、open / enumerate は3秒、無入力は2秒、再生は65秒を上限とする。HTTP は既存 bearer/private-session を用い、自動 retry しない。応答は JSON 64KiB / WAV 5MiB と PCM 60秒を検証する。音声失敗後もテキストの local operation は残る。

queue は1 run、0始まり64文まで。run、private/local session、runtime/cloud/output generation、文期限を使用する。順序が抜けた文は期限まで待ち、重複は無視、矛盾は全消去。HTTP 完了後・機器 open 後・各 write 直前も検査する。現在の音声だけを取得し、後続はID/期限のみ保持する。5分の文期限を超えて再生しない。wall clock と monotonic deadline を使用し、時刻の巻戻りも拒否する。

local logout/期限、個人 session 終了/期限/置換、snapshot の出力世代更新、cloud/WSS接続失効、run failed/cancelled、reset、shutdown/update の Runtime close で消去する。自然期限は最大50msの監視間隔。cleanup はPCM bytearrayを消去し、socketを閉じ、owned taskを取消す。遅いHTTP応答やnative callbackが新しいrun/sessionへ移ることはない。送信開始後のprovider処理を未送信に戻す保証はなく、既存APIの利用量規則に従う。

録音・TTS・transcript をSQLite、credential journal、通常log、一時ファイルへ保存しない。Python/HTTP/native の短命な immutable copy を物理的に完全消去できるとの保証はしない。管理bufferと参照を消去し、終了時は子プロセスのアドレス空間も解放する。core dump は既存 unit の `LimitCORE=0` を維持する。

## Backend と依存

[ALSA PCM API](https://www.alsa-project.org/alsa-doc/alsa-lib/group___p_c_m.html) の NONBLOCK、`snd_pcm_set_params`、`readi/writei`、`drain/drop/close` を標準 ctypes から使う。S16_LE、RW_INTERLEAVED、1 channel、16000Hz、100ms bufferを指定する。ALSA hints の NAME/IOIDから `hw:CARD=...,DEV=...` / `plughw:CARD=...,DEV=...` だけを列挙する。方向情報のないhintは両方向候補で、実際のopen成功は選択後に確認する。defaultや暗黙の機器選択は使わない。hwが16kHzを開けない場合は列挙済みplughwを明示選択できる。soft_resampleはhwでは0、明示plughwだけ1とし、選択機器へのALSA変換を許す。daemon側PCMはどちらも16kHz・16bit・monoのまま。失敗したhwからplughw/defaultへ自動切替しない。

native呼出しは既存Runtime/guardian管理のchild sessionに隔離する。PCM通信はLinux abstract Unix socket、永続ファイルなし。双方が `SO_PEERCRED` を検査し、daemonは自分のguardianの直接の子PID・同じUIDのみ、helperは起動元daemon PID・UIDのみ受け付ける。接続期限3秒、helper最大4個、受信block 3,200 bytes。stdin/stdout/stderrは既存所有機構のDEVNULLを継承する。OS callが止まってもdaemon loopを塞がず、所有identityだけを停止・回収する。

新しいPython/Node packageはなく、uv/pnpm lockの変更はない。任意設定は既存5キーの導入設定例へ追加必須にせず、未指定時disabledを維持する。明示disabled/alsaを追加した既存設定も再provision/uninstallで保持する。追加の任意OS依存はdistroの `libasound.so.2` とALSA config。開発Linux x86_64では `libasound2t64` / `libasound2-data` **1.2.15.3-1ubuntu1.1** の実library列挙、不存在card拒否、48kHz固定null slaveへの明示plughw変換（録音/再生）を確認した。nullは合成backendで、物理マイク/スピーカーを開かない。Pi用OS package版は未確定で、公開artifactのOS manifestへ採用版・hashを固定して再検証する。開発機の版をPi検証済みとは扱わない。

ALSA Project保守のalsa-libはLGPL-2.1-or-later（[提供元COPYING](https://github.com/alsa-project/alsa-lib/blob/master/COPYING)、導入済みdistro copyrightと照合、2026-10-29）。共有libraryを動的利用しwheelへ取り込まない。再配布OS imageにはlicense notice、対応ソース入手手段など採用distroの再配布条件を保持する。追加権限は選定した `/dev/snd` nodeの読書きと同一UIDのUnix socket / owned child。新しいcloud IAM、保存bucket、root実行権限は不要。

## Pi 導入と公開前 gate

基準Pi5/8GB・マイク・スピーカーはこの作業環境にないため、録音・可聴再生・抜去・実機停止時間は未実施。実STT/TTSや有料cloud callも行っていない。共通完了方針に従いfake/ローカル受入と外部前提を分離し、以下が完了するまで既定disabledとする。

1. PiのOS/カーネル/ALSA package版、device ID、USB接続、artifact digestを記録する。libasoundを明示導入し、サービス専用UIDから利用する。audio groupとnode所有権を確認する。
2. 既定unitは `PrivateDevices=yes` で機器が見えない。[限定drop-in template](../packaging/systemd/audio.conf.example)のC1/D0を実際の入力/出力cardへ置換し、対象control/PCM nodeだけをDeviceAllowへ列挙する。別cardなら各controlも必要。PrivateDevices=no、DevicePolicy=closed、限定nodeの組合せを実機で確認する。全機器のchar-alsa許可は行わない。再接続でcard番号が変われば再検証し、意図した機器が開けなければ停止する。
3. 導入担当が限定drop-inと `DEVICE_AUDIO_BACKEND=alsa` を設定し、列挙→明示選択する。選択はメモリ限りでdaemon再起動後に再選択する。UI全体導線はVOICE-05の公開gateも必要。[PrivateDevices](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html#PrivateDevices=) / [DeviceAllow](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html#DeviceAllow=) の制約を採用OSで照合する。
4. cloudはfakeのまま、5秒以内のテスト音声を録音・再生する。finish後の入力停止、discard時uploadゼロ、UI reload後の停止、発話停止とrun取消の区別、無音/mute/拒否/抜去/再生中stop、session失効、reset、updateを確認する。停止時間とchild/node解放を記録し、raw音声やtranscript本文を証跡へ保存しない。
5. 実provider結合は [入力gate](audio-inputs.md) / [出力gate](speech-outputs.md) の短命認証・料金上限・保持条件を満たす別作業。失敗時はdisabled、drop-inを外して既定PrivateDevicesへ戻す。hostのunit install、ユーザー/group変更、実機配備は今回実行していない。

ローカル受入は [device audio試験](../tests/test_device_audio.py)。T-006の無音/拒否/失敗、T-007の停止と取消分離、T-021の失効と遅い結果拒否、順序/重複/容量/期限、local boundary、real owned helper/不正peer/強制停止を確認する。native loader/IPC試験は実音響機器の検証を代替しない。

VOICE-04の再生開始はPiのlocal create_turnに限定する。スマホからPiへ割り当てたspeech runのsnapshotは文字履歴だけを復元し、再生を自動開始しない。VOICE-05で出力先UIとdaemon adapterの明示的なlive attachを結合する際は、停止済みrunの再接続・同一sessionのreloadから発話を復活させない契約と検証を追加する。この結合が済むまでスマホ→Piの可聴出力を公開済みとは扱わない。

ALSA設定の一次確認は [pcm.c の set_params](https://raw.githubusercontent.com/alsa-project/alsa-lib/v1.2.15.3/src/pcm/pcm.c) と [plug の rate変換](https://raw.githubusercontent.com/alsa-project/alsa-lib/v1.2.15.3/src/pcm/pcm_plug.c)（2026-10-29）。[native transfer試験](../tests/test_device_audio_native.py)は短いread/write、EAGAIN、分割sample、drain失敗、実libasoundの48kHz null変換を扱う。
