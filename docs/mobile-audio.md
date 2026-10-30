# スマホ録音・PCM 変換・文単位再生

BA-VOICE-03 / FR-005 は [共通 adapter](../apps/mobile/platform/audio.ts)、[認証付き transport](../apps/mobile/platform/audio-transport.ts)、[app-local Capacitor plugin](../apps/mobile/native-audio-config/README.md)を提供する。画面全体の録音・思考・発話表示とボタン接続は BA-VOICE-05、生成 native project / 署名 / 配布は BA-OPS-09/10。BA-VOICE-05の[音声UI](voice-ui.md)が実entryへ接続する。既定無効の `VITE_VOICE_ENABLED` とnative登録の両方を必要とし、未検証の実機機能を公開しない。

`createNativeAudio()` は native platform と `BrainoidAudio` の登録を要求する。browser ではマイク取得へ fallback しない。1 app の adapter で1 active lease を持ち、新しい `open` は古い lease を閉じる。UI は現在の tenant / actor / session / run / 出力先を一意に表す `key`、`SessionContext.signal` と run/session取消 signal の合成、実期限 `expiresAt`、現在の出力 binding を検査する `isCurrent` を渡す。`SessionContext.dispose()` の abort は即座に native stop と fetch abort へ伝わる。既存 pagehide→suspend も同じ所有境界を使う。出力 binding の変更、run取消、session終了では `close()` する。run完了後の未再生文は現在の session と文参照の期限内だけ継続可能。

録音タップだけが `start()` を呼ぶ。許可済みでも毎回 native 側の現在世代・foreground・permission を確認する。拒否では `issue=permission` と `microphoneDeniedMessage`（端末設定で再許可し、再度タップする案内）を返し、全 snapshot は `textInputAvailable=true`。文字入力の有効性をマイク権限に依存させない。拒否後の再試行は新しい lease にする。`finish()` は停止とメモリ内 WAV の準備だけを行い、`upload()` が明示的な送信になる。破棄は `close()`。送信前の破棄は upload 0件。送信開始後の取消は fetch を abort し、遅い transcript を捨てるが、既に受け付けた provider の課金を取り消したとは表示しない。1 lease は1録音・最大1 upload とする。

Android は AudioRecord の 16kHz/16bit/mono 入力を固定長 short buffer に受け、little-endian PCM WAV にする。iOS は AVAudioEngine の float32 非 interleaved 入力を mono 平均し、入力 frame の面積を出力 frame に分配する streaming resampler で16kHzへ変換、[-1,1] に clamp して PCM16 に量子化する。8〜192kHz / 最大8chを受け、未知形式・途中のsample rate / channel count変更は fail closed。変換の継続・正常上限・不正入力を区別し、不正入力は蓄積PCMを即座に消去、以後のfinishも拒否する。chunk 間の fractional frame を保持し、末尾の不完全 frame は捨てる。音声用途の簡易平均フィルターであり、音楽用の高品質 resampler とは扱わない。60秒/960,000 framesを native で制限し、別の60秒 timerでもマイクを止める。44byte headerを含む最大 WAV は1,920,044bytesで [入力契約](audio-inputs.md)の5MiB以内。上限停止時は ready へ進めるが自動送信しない。無音・不正WAVは文字入力へ戻せるエラーになる。

文は0始まり `index` 順に最大64件まで保持し、欠番は後続を先に再生せず待つ。重複イベントは再取得せず、同indexの異なるIDは失敗として停止する。文ごとに現在 binding、期限、WAV実データを取得前後で確認し、1件ずつ取得・再生する。[出力契約](speech-outputs.md)の optional RIFF metadata は検査後メモリ内で除去し、native へ canonical WAV を渡す。AudioTrack / AVAudioPlayer(Data) の終了後に次へ進み、停止時は再生中・未再生の両方を破棄する。文期限とsession期限は native watchdog にも渡し、JS停止中も期限を延長しない。native audio停止と、実行中toolの取消可否は別の状態として UI へ接続する。

bridge は単調増加世代を受け、stop済み世代を tombstone として拒否する。遅い permission/start/play/activate、以前の session の stop は新世代の録音・再生を復活・停止させない。Android lifecycle pause/destroy・audio focus loss・入力機器除去、iOS background・音声割込み・機器切断・media service reset・AVAudioEngine構成変更で録音と再生を止め、復帰後も自動再開しない。permission dialog が activity を pause させる端末でも自動録音せず、再タップを必要とする。iOS構成変更は録音ごとにengineを指定して監視する。OS通知callbackではその録音のPCMだけを無効化し、engine停止・破棄は非同期main queueで世代とengine identityを検査した後に行う。これにより通知とfinishが競合しても部分WAVを成功として返さず、旧engineの遅い通知は新leaseを壊さない。

[Capacitor設定](../apps/mobile/capacitor.config.json)は `loggingBehavior=none` を固定し、debug buildでもbridge引数/応答のbase64を通常logへ出さない。生成projectでOS別logging overrideを追加しない。

raw 音声は native 配列 / Data / JS typed array / bridge base64 / HTTP body のメモリだけに置く。DB、Preferences、localStorage、Filesystem、temporary file、object URL、通常ログへ保存しない。mutable buffer は停止・失敗・使用後に上書きし参照を解放する。bridge stringやOS管理bufferは解放対象であり、immutable string / GC / OSメモリの物理ゼロ化までは保証しない。録音中の無断再取得やbackground継続は行わない。HTTPは既存 SessionContext の bearer / tenant / abort / `cache=no-store` / redirect拒否を使用。音声adapter独自の retry はなく、既存sessionの401に限るtoken更新1回は引き継ぐ。TTS は最大5MiB、60秒受信待機、`audio/wav` / `no-store`を検査する。サーバーの参照5分・最大2attempt・現在認可を緩めない。upload結果は15分期限内の `audio_input_id` のみを返す。

## 選定と依存

決定日2026-10-29。候補 [capacitor-voice-recorder の実装](https://github.com/tchvu3/capacitor-voice-recorder/blob/master/android/src/main/java/com/tchvu3/capacitorvoicerecorder/VoiceRecorder.java)は録音ファイルを作成後にbase64を読むため、削除後でも原音声の一時永続保存禁止に合わない。不採用とし、OS のメモリAPIを使う小さい app-local Capacitor plugin を採用した。第三者の実装をコピーせず、この repository の権利・配布規則に従う。新しい package / SDK distribution / license / lock 更新はない。既存 Capacitor core 8.5.3（MIT）と OS framework を使い、追加権限はマイクのみ。Android API 26以上の必要性、iOS権限説明、登録手順は [native組込み](../apps/mobile/native-audio-config/README.md)に固定する。既存 [依存台帳](toolchain.md)のCapacitor表示を維持する。保守はこの repository のMobile担当、OS/API変更または実機音質が受入を満たさない場合に見直す。

一次資料（2026-10-29確認）: [Capacitor Android plugin](https://capacitorjs.com/docs/plugins/android)、[iOS plugin](https://capacitorjs.com/docs/plugins/ios)、[AudioRecord](https://developer.android.com/reference/android/media/AudioRecord)、[AudioTrack](https://developer.android.com/reference/android/media/AudioTrack)、[AVAudioEngine](https://developer.apple.com/documentation/avfaudio/avaudioengine)、[音声割込み](https://developer.apple.com/documentation/avfaudio/handling-audio-interruptions)、[engine構成変更と同期破棄の禁止](https://developer.apple.com/documentation/avfaudio/avaudioengineconfigurationchangenotification)、[Swift通知名](https://developer.apple.com/documentation/foundation/nsnotification/name-swift.struct/avaudioengineconfigurationchange)。

## ローカル検証と公開前の実機条件

[Node fake bridge / HTTP / WAV tests](../tests/test_mobile_audio.mjs) は T-006/T-007 の拒否、文字継続状態、破棄upload0、停止後の遅い音声、世代交換、背景・割込み、期限、順序、重複、session abort、無音・API失敗とサイズ境界を検査する。Java 21 の実compilerで [Android PCM fixture](../tests/mobile/audio-fixtures/PcmWavFixture.java)を実行し、ヘッダー・signed LE sample・最大長を検査する。Android bridge全体は [javac構文parser](../tests/mobile/audio-fixtures/ParseJava.java)でparseするが、Android SDKのsymbol/type確認ではない。[iOS変換fixture](../tests/mobile/audio-fixtures/PcmCaptureFixture.swift)は48k stereo / 44.1k / 16k / 8k、137frame分割、60秒境界、正常PCM蓄積後の不正rate/channel・途中形式変更・finish拒否を用意した。同じfixtureで製品のFoundation observerに通知と非同期配送を注入し、active通知でのPCM無効化・遅延停止、無関係engine無視、旧engine/旧世代の遅延通知無視、observer解除を検査する。Nodeのfake bridgeは中断/エラーとfinish応答の競合でready/uploadへ進まないことを実行確認する。これは実Swift/AVAudioEngineの実行証明ではない。

このLinux環境には Android SDK / Gradle native project / Swift compiler / Apple SDK / Xcode がなく、iOS fixture実行、両OS framework build、実マイク・スピーカーの可聴試験は未実施。利用者のiPhoneはあるがMacはなく、この作業で実機確認済みとは扱わない。docs08 §1の共通条件に従い、fakeで独立確認できる境界と公開前の結合を分ける。実機検証結果がそろうまで配布対象の音声gateを有効にしない。

公開担当は両OSについて次を同じ候補artifactで記録する。音声・会話本文・bearerを証跡へ残さず、合成信号または個人情報を含まない短音声だけを使用する。

1. native projectへの登録・権限説明・対応OS・compiler/SDK版とbuild成功、source/artifact digestを記録する。Swift fixtureを実行する。実providerはdisabledのまま、ローカルfake endpointへの回数を数える。
2. T-006: 初回拒否→文字入力継続、設定で再許可→再タップ、0秒・無音・API503、マイク使用中の他アプリ、60秒停止を確認。48k/44.1k入力機器でも出力PCMヘッダーと最大フレームをメモリ内検査し、サイズ/合否のみ記録する。
3. 録音中・ready中の破棄でHTTP upload0、permission表示中の終了、古いfinish待ち→新session、背景/ロック/電話割込み/Bluetooth切断・sample rate/channel countの変更通知でマイク停止と破棄、復帰後の無断再開なしを確認する。
4. T-007: 3文を2→0→1順に届け0→1→2順に1回ずつ可聴再生する。fetch待ち・再生開始bridge待ち・実発話中に停止し、遅い結果や次文が鳴らない。背景・session終了・run取消・文/実session期限で発話が止まる。stop→新session→遅い旧stop/permissionでも新sessionを壊さない。
5. 端末appのデータ/cache領域にraw録音/再生ファイルが生成されないこと、再起動時に音声が復帰しないことを確認する。OSの通常権限表示と録音停止を照合する。実STT/TTS・日本語音質・課金照合は [入力](audio-inputs.md)と [出力](speech-outputs.md)の別gateに従う。

切戻しは音声UI接続を無効化しactive leaseをclose、native登録を外した旧artifactへ戻す。永続raw audioの移行はない。text会話を継続し、既存のtranscript / speech参照はサーバー期限・消去規則に従う。
