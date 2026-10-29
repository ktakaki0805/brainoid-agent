# スマホのメモリ内音声 bridge

BA-VOICE-03 / FR-005。利用方法と公開前検証は [mobile audio](../../../docs/mobile-audio.md)。このディレクトリは app-local Capacitor 8 plugin の正本であり、生成 Android/iOS project はまだ repository に存在しない。現在の mobile entry は音声機能を公開していない。生成projectには既存 `capacitor.config.json` の `loggingBehavior=none` を同期し、debug版でもraw base64のbridge loggingを有効にしない。

## Android の組込み

BA-OPS-09 の生成 project で、最小 API を **26 以上**に設定する（AudioFocusRequest / USAGE_ASSISTANT を利用）。[Java 2ファイル](android/BrainoidAudioPlugin.java)と [PCM helper](android/PcmWav.java)を `app/src/main/java/dev/brainoid/agent/audio/` に同じ内容で配置する。[manifest fragment](android/AndroidManifest.xml)を app の manifest に統合する。storage permission、foreground service、background microphone は追加しない。

既存 `MainActivity extends BridgeActivity` の `onCreate` で `super.onCreate(savedInstanceState)` より前に次を追加する。

```java
registerPlugin(dev.brainoid.agent.audio.BrainoidAudioPlugin.class);
```

Java 21 と生成 project の固定 Gradle / Android SDK で `./gradlew :app:assembleDebug :app:lintDebug` を実行する。API 26 と公開対象 OS のマイク拒否・許可・背景・focus loss・Bluetooth 切断を確認する。API 26 未満への暗黙の配布はしない。OS 音声入力経路が 16kHz mono に対応しない場合は録音を失敗させ、文字入力を維持する。

## iOS の組込み

BA-OPS-10 の app target に [bridge](ios/BrainoidAudioPlugin.swift)、[PCM converter](ios/PcmCapture.swift)、[構成変更observer](ios/CaptureConfigurationObserver.swift)を追加する。Capacitor 8 の対応 deployment target を使用し、[plist fragment](ios/Info-audio.plist)の `NSMicrophoneUsageDescription` を app の Info.plist に統合する。`UIBackgroundModes=audio` は追加しない。

app の `CAPBridgeViewController` subclass の `capacitorDidLoad()` 内で次を登録し、storyboard の bridge controller をその subclass にする。

```swift
bridge?.registerPluginInstance(BrainoidAudioPlugin())
```

Mac / Xcode / iOS SDK がある環境で、選定した simulator destination に対して `xcodebuild -scheme App -destination 'platform=iOS Simulator,name=<available simulator>' build CODE_SIGNING_ALLOWED=NO`、続いて署名済み実機 T-006/T-007 を実施する。scheme / simulator 名は生成 project から記録する。Swift の PCM / Foundation通知fixtureだけなら次で検証できる（Apple SDKの代わりに通知を注入し、実機の通知発火・engine停止は別途検証する）。

```sh
swiftc apps/mobile/native-audio-config/ios/PcmCapture.swift apps/mobile/native-audio-config/ios/CaptureConfigurationObserver.swift tests/mobile/audio-fixtures/PcmCaptureFixture.swift -o /tmp/brainoid-pcm-fixture
/tmp/brainoid-pcm-fixture
```

上記の native project 生成・登録・署名・framework build はこの環境で実行済みではない。生成 project と source の digest、plugin availability、権限説明、実機受入を照合するまで配布を有効にしない。
