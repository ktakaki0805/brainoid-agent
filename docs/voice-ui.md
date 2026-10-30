# 音声の状態と停止・取消

BA-VOICE-05 / FR-005・FR-012。スマホの実 `main.tsx → SessionApp → Chat` とPiの実 `main.tsx → Recovery → DeviceSession` に音声状態と操作を接続する。共通UIは `VoiceFactory` / `VoiceLease` を受け取り、appのnative実装をimportしない。Piのraw音声は引き続きdaemonが所有する。

## 状態と入力

Idle / Listening / Transcribing / Thinking / Confirming / Speaking / Errorを文字で表示する。Speakingはadapterの実再生状態であり、`text.delta`だけではSpeakingにならない。offline、microphone muted、private sessionと発話先は別に常時表示する。Piのマイク設定が未取得なら「未確認」と表示する。

録音開始は明示tapだけ。録音終了は送信せず、送信と破棄を分ける。STT成功の `audio_input_id` で `output_mode=speech` のturnを作る。スマホは現在のcontext・選択会話・出力先・音声generationに結び付く最初のSSE接続だけから `speech.ready` をadapterへ渡す。run完了後も有効な文の再生は継続できる。失敗・取消・切断・selection変更・session終了・signout・pagehideでは旧意図を破棄する。再接続・履歴選択は文字復元のみ。

拒否・無音・provider失敗では文字入力を操作でき、設定確認・再録音の案内を表示する。Piでも「入力できる会話を確認」から現在のdevice actor/短命sessionで認可された会話を選び、文字を送信できる。private会話は現在session本人、conversation scope、authenticated_session表示方針を必要とする。新規会話の作成はスマホで行う。オフラインでは入力を編集できるが、新規クラウド操作は送らない。

## 停止と取消

- 録音の破棄は入力bufferと待機処理を消去する。既に始まったprovider処理や料金の取消は約束しない。
- 読み上げ停止は音声queueと現在の再生を消去する。run、家電、予定の取消成功とは表示しない。
- 生成停止は音声を止めてrun cancelを呼ぶ。canonical結果がcompletedの場合は完了を示し、取消と競合した結果を隠さない。
- commandカードの「未実行操作の取消・結果を確認」は専用のcommand GET / cancel / GETを使う。claimと取消が競合しても現在statusを表示する。APIの元actor制限を維持し、スマホ起点の操作をPiが本人になり代わって取り消さない。
- Calendar確認候補の確認・取消は既存の本人スマホの確認カードを使う。Piではその導線を明示する。実行済み操作を戻す場合は新たな操作・許可が必要。

## Piの明示的な読み上げ

`GET /local/audio` は既存stateに `muted` / `waiting` を追加する。機器選択・録音・停止は既存のlocal cookie/session/CSRF経路を使い、cloud credentialsや音声バイトをブラウザーへ渡さない。画面初期化、pagehide、session消去時にstopを送る。reloadは録音や待機を自動再開しない。停止が確認できなければ未確認と表示する。

`POST /local/cloud/runs/{run_id}/events` はCSRF保護付きの明示attach。bodyは `mode: future | waiting` と任意の `private_session_id` だけ。session headerとの一致、現在の完全snapshotのspeech割当、canonical run/conversation、現在の出力先と個人表示方針、ownerと期限を再検査する。任意のURL/headerを転送するproxyではない。`speech.ready`のID/seqは文字UIへも渡して連続cursorを保つが、音声取得・再生はdaemonだけが行う。

`future` は「この会話のこれ以降の新しい音声を再生」。canonical `last_event_seq` 以前を除外し、その後の最初のsentence indexから順序queueを開始する。完了済みrunは拒否する。UIの旧stream終了待ち・認可読取・local session準備からPOSTまでを開始時の音声generationと取消signalに結び付ける。stop・新録音・新待機は遅い成功/失敗を失効させ、停止済み要求からPOSTしない。文字の再同期は継続できる。SSE接続中の再接続/EOF/認可失敗ではその意図を消去し、backlogは再生しない。

スマホ→Piは次の順序で使用する。

1. Piで機器を選び、必要な個人sessionをスマホで開始する。
2. Piで「次の新しい割当の読み上げを待つ」を選ぶ。`POST /local/cloud/audio-wait` は完全snapshotを取得し、その成功応答のserver時刻を境界に60秒・次の1件だけの意図を作る。
3. 「待機中」になってから、スマホでこのPiを出力先にして録音を送信する。新しい割当を検出したPiは `mode=waiting` で初回streamへ接続する。

待機はbaseline内にないrun、同じcurrent session/output binding、server境界より厳密に後のcanonical `created_at`、未使用のstream cursorを必要とする。同時刻・古い遅延割当・既存履歴は拒否する。明示待機後に作成されたrunは、初回接続までに完成しても、その意図に属する文を一度だけ再生できる。待機開始前の送信は対象外。停止・session/credential変更・cloud切断・期限・ページ再読込で待機は消える。待機開始の失敗・応答喪失はdaemon意図も停止し、停止未確認時もGETだけでは再待機しない。新しい明示waitの成功応答だけが待機を有効にする。同じsnapshotの新規候補は未試行queueで保持し、古い候補が拒否されても後続を確認する。1件ごとにdaemonの未消費待機を再確認し、単発消費後は他候補を試さない。POST前の一時失敗は候補ごと最大2回、POST後の不確定な要求は自動再送せず文字確認と再待機を案内する。候補queueもstop・session・selection・切断・期限・新waitで失効する。待機案内は消費・失効時に消去し、停止未確認の案内は保持する。

音声statusを取得できなくなった場合、待機の消費・期限到達とは断定せず、確認済みの待機と候補queue全体を明示失効してdaemonへstopを送る。現在のstatusには同じ待機を識別するIDがないため、不明な待機の再開よりも、既存の切断時と同じく明示再待機を求める方針を採用する。後のGET成功だけでは待機を復活させず、文字入力・接続確認・再待機を案内する。stopが確認できなければ機器の停止は未確認と表示し、文字の再同期でもその案内を消さない。新しい待機は旧stopの終了を待ってから開始し、旧pollや旧候補の遅い結果は新しい待機を操作しない。

`POST /devices/{device_id}/snapshot:validate` 成功応答に `X-Brainoid-Observed-At`（runと同じserver clockのdate-time）を追加した。完全validationの応答そのものに結び付き、Piのwall clockとrun作成時刻を比較しない。未対応server・欠落/不正headerでは新規待機を拒否する。通常文字表示とfuture attachは既存serverとの互換を維持する。headerは認可grantではなく、毎回のsnapshot・run・TTS認可を省略しない。

## 設定と検証範囲

スマホは `VITE_VOICE_ENABLED=true` の明示build設定と登録済みnative pluginの両方が必要。既定は無効、browser fallback録音はない。Piは既存 `DEVICE_AUDIO_BACKEND=disabled` が既定。STT/TTSのprovider設定も既存gateのまま。[スマホ音声](mobile-audio.md)、[Pi音声](device-audio.md)、[入力](audio-inputs.md)、[出力](speech-outputs.md)の実機・実provider公開前条件を緩和しない。

ローカルのNode/fake adapter・Python local route/snapshotとChromium smokeはT-006/007/021の境界を確認する。実マイク/スピーカー、iPhone/Pi停止時間、iOS/Android SDK build、実provider課金・日本語品質は別の公開前検証。Linux環境にMac/Xcode・Android SDK・対応Piはなく、利用者のiPhoneだけではiOS native buildは実施できない。fake検証を実機合格とは扱わない。切戻しはmobile gateを無効化、Pi backendをdisabledにして文字会話へ戻す。永続音声データの移行はない。
