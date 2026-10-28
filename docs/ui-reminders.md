# リマインダー・通知の画面

BA-UI-07 / FR-009 / AC-009 の[画面](../packages/ui/src/reminders/view.tsx)は、認証済みテナントのリマインダーとアプリ内通知を表示する。[Reminder API](reminders.md)と[Notification API](notifications.md)を使用し、サーバーの配送・繰り返し予定・OSの厳密な表示時刻保証は追加しない。

## 予定と通知

作成時は題名・未来の時点・IANA timezone・配信先の種類とID・公開範囲・表示方針を指定する。時点は秒とUTCオフセットを含める。DSTの重複時刻を推測せず、存在しない日付、offsetなし、`-00:00`を拒否する。timezoneは別に保存し、表示は保存したtimezoneを使う。配信先は最初に本人UIDを表示し、利用者が確認して保存する。任意のtask_idはUUIDで指定する。現在の所属、Taskの可視性、user/device宛先の権限はAPIが再検査する。

編集は題名・日時・timezone・取消のみ。作成・編集・スヌーズは同じ要求を再確認するIdempotency-Keyを保持する。編集は取得済みETagをIf-Matchへ、スヌーズは元Reminderのversionをexpected_versionへ送る。保存前の入力と保存済み正本を別表示し、412は入力を保持して最新版の明示確認を求める。通信失敗では保存済みと表示せず、同一body/key/期待版で再確認する。receiptが後の編集を反映した現在正本を返しても元入力を上書きして復元しない。

通知のpending、sent（配信受付）、displayed（表示記録）、acknowledged（本人確認）を分ける。開く操作はackを送らず、「本人確認済みにする」を明示したときだけ送信する。ackはIdempotency-Keyのみを要求し、If-Matchを追加しない。戻り値のID・tenant・reminder・状態・ETagを検査する。通知を開く・ack・ack再確認で取得した現在Reminderを同じ認証contextの一覧にも反映する。resource versionとschedule_versionを巻き戻さず、一覧の条件を適用する。編集中の入力と期待版を勝手に置換せず、最新版との競合確認として保持する。

OSから渡る情報はtenant_idとnotification_idのUUIDだけを取り出し、任意のtitle/body/URLを使わない。現在認証した同tenantで通知を取得し、元Reminderを取得して、もう一度同じ通知IDを取得する。Notification DTOにはschedule_versionがないため、公開versionを予定版として比較しない。サーバーのsource/current_schedule検査が旧世代IDを拒否し、UIは選択したReminderの予定版を確認操作にも束縛する。スヌーズ・取消・編集の保存操作の開始時には表示中の通知を消し、保存後に現在一覧を再取得する。古い通知IDから新しい予定を完了扱いにしない。

pushから別tenantへ自動切替しない。対象tenantを選んで通知一覧を再取得する案内を出す。サインアウト中のhintは保持しない。本文・入力・未確定要求・通知hintはURL、history、browser storageへ書かず、UID/tenant変更・signout・pagehideで破棄する。並行して開いた古い通知の読取と遅い403は新contextへ作用しない。`expires_at`はdue_atから5分の自動音声再生期限であり、一覧や本文の削除期限ではない。音声期限を超えても、現在認可と予定版を満たす未確認通知は一覧・取得・明示ackを利用できる。実際のexpired状態、取消、旧世代、認可失効は別に拒否する。offlineやOS未登録でも、通信復旧後にアプリ内一覧を取得でき、古い音声を再生しない。

本人未確認の共有ディスプレイはsharedかつshared_displayのみ。表示だけでなくコントローラーも作成・編集・取消・スヌーズ・本人確認を拒否する。

## Native FCM接続と所有期間

[ネイティブadapter](../apps/mobile/platform/push.ts)は`@capacitor-firebase/messaging` **8.5.2**の公開APIを直接使用する。`checkPermissions`は状態取得だけ、`requestPermissions`と`getToken`は「OS通知を許可・登録」の利用者操作から呼ぶ。iOSでもFCM tokenを使い、APNsの`apnsTokenReceived`をAPIへ登録しない。foreground通知を独自に音声再生せず、内容を含まない再取得案内にする。iOSのforeground presentationOptionsは空配列。background/lockscreenへ送るserver payloadはAPP-05の固定汎用本文だけである。

[登録controller](../apps/mobile/notifications/controller.ts)はtenantから独立したglobal user contextを使う。POST201、refresh PATCH200のDTO・ETagを検査し、rotationは同じinstallationを更新する。tokenと未確定要求はメモリー内だけに保持し、登録応答喪失では同key/bodyを再送する。古いtokenの受付が未確定の間に到着した最新tokenも保持し、照合後にrefreshする。403/404/412などの確定拒否では同じ期待版の無限再送をせず、登録ボタンから現在tokenを登録し直す案内にする。providerエラー・初期化失敗・権限拒否は認証とアプリ内通知を止めない。

通常signoutは最初に通常contextを無効化して本文を消し、元UIDだけに束縛された非公開の終了用contextで登録照合とDELETEを最大6秒待つ。そのcontextは新しいログインには使えない。外部認証失効・UID交代・pagehideは旧credentialでのDELETEを保証できないため、旧callbackを即無効化し、native deleteTokenを試みる。native getToken/deleteTokenは実完了まで直列化する。遅い旧deleteTokenの後に新UIDのgetTokenを行い、旧cleanupが新tokenを消さない。終了や解除が失敗した場合、serverの残存登録消去とprovider到達停止は保証せず、OS設定から停止できる。手動解除の失敗は「解除未確認」と表示し、破棄済み要求の再送ボタンを残さない。「配信先を再確認・登録」で新しいnative leaseと現在tokenを取得し直してから解除を再試行できる。解除済みやnative停止成功とは表示しない。サインアウト後は汎用pushでも本文を取得しない。

ブラウザーのWeb Pushやservice workerは追加しない。ブラウザーはアプリ内通知を利用する。Nativeアプリ起動後やpagehideから復帰した後は、利用者が登録ボタンでOS配信先を再確認する。

## 依存と実機準備

2026-10-28に導入済み8.5.2のpackage metadata・公開型・READMEと[npm固定版](https://www.npmjs.com/package/@capacitor-firebase/messaging/v/8.5.2)、[保守元の公式案内](https://capawesome.io/docs/sdks/capacitor/firebase/cloud-messaging/)を確認した。ライセンスはApache-2.0、peerはCapacitor >=8.0.0とFirebase ^12.6.0で、既存core8.5.3/Firebase12.19.0と互換。Capawesomeが保守するFirebase SDK wrapperであり、Firebase公式製プラグインとは扱わない。mobileのmanifestとpnpm-lockで固定し、配布物にプラグインおよびnative Firebase推移依存のライセンスを保持する。依存追加自体はcloud資格情報や配信権限を付与しない。実行時は通知権限、Firebaseへのregistration token通信、OS通知受信を使用する。

このcheckoutにはandroid/ios native project、Capacitor native platform/CLI、端末SDK、FCM/APNs設定がない。`dev.brainoid.agent`は既存の開発用placeholderで、配布識別子を確定したものではない。OPS-09/10で対象app IDと署名設定に合うnative projectを準備し、固定したworkspace依存をinstallしてnative syncを行う。秘密鍵や開発用provider設定をこの文書やcommitへ含めない。

実機に接続する前に、8.5.2の[導入手順](https://github.com/capawesome-team/capacitor-firebase/tree/main/packages/messaging)と導入済みREADMEに従って次を確認する。

1. AndroidのFirebase appとgoogle-services構成、通知icon/channel、Android 13以降のPOST_NOTIFICATIONSを確認する。初回の自動token生成を止めるにはAndroidManifestの`firebase_messaging_auto_init_enabled=false`と`firebase_analytics_collection_enabled=false`が必要。JSだけでこの設定済みとは主張しない。
2. iOSのFirebase app、Push Notifications entitlement、APNs開発設定、署名、GoogleService-Info構成、AppDelegateの登録成功/失敗・remote notification callbackを設定する。既存FirebaseAuthenticationとのURL callback連携も保守元の手順に合わせる。Info.plistの`FirebaseMessagingAutoInitEnabled=NO`を設定する。getTokenはauto-initを再有効化するため実機で確認する。
3. Swift Package Managerを使う場合、保守元が指定するpackage identity collision対策とCapacitor CLIの互換条件を確認する。同時に別のCapacitor Push Notification pluginを入れない。
4. 開発用providerと端末だけを使い、起動失敗・拒否・許可・取消・token rotation・再起動・アカウント切替・background/foreground/lockscreenを確認する。tokenや本文をlog、画像、browser storageへ残さない。

## 検証と未完了の実機条件

[controller試験](../tests/test_ui_reminders.mjs)、[native境界試験](../tests/test_ui_push.mjs)、[Chromium操作](../tests/ui/reminders_browser.mjs)、[HTTP/Firestore結合](../tests/emulator/test_ui_reminders_integration.py)を提供する。Chromiumでも本番のPushSettings/controllerをsynthetic bridgeへ接続して拒否・初期化失敗・登録・解除を表示する。native試験は公式pluginの型に接続したsynthetic bridgeであり、OS/APIの実機成功を証明しない。HTTP結合は認証readerをsynthetic overrideし、Reminder/Notification/installationの実HTTPとFirestoreを検証する。既存[公式Firebase認証結合](../tests/emulator/test_ui_session_integration.py)は別に認証世代・signout回帰を確認する。

通常検証は`mise run test`と`mise run check`。ブラウザーは`UI_CHROMIUM`と専用`UI_ARTIFACTS`を指定して`mise exec -- node tests/ui/reminders_browser.mjs`を使う。実施結果・失敗工程・証跡は[作業ログ](implementation-log.md)を正本とする。

2026-10-28の利用者の明示指示により、**BA-UI-07のローカル完了条件は実装・ローカル検証・独立レビューの完了**とする。T-013/T-014/T-030/T-038のfake、synthetic native bridgeを使う実Chromium、HTTP/Firestore結合をこの条件で確認する。実機成功を示すものではなく、他チケットの受入・検証条件は自動的に変更しない。

**Android/iOSの実機OS通知許可と実FCM/APNs確認は、リリース前の必須gateとして未完了のまま保持する。** [BA-OPS-09](08-implementation-tickets.md#ba-ops-09-android署名buildと内部配布手順)・[BA-OPS-10](08-implementation-tickets.md#ba-ops-10-ios署名buildと内部配布手順)でnative build・署名・端末環境を準備し、[BA-QA-06](08-implementation-tickets.md#ba-qa-06-mvp全受入基準の証跡と公開判定)で受入証跡とrelease判定へ束ねる。各OSの端末/OS版、署名build・app ID・artifact digest、Firebase/APNs開発構成識別子、実行日時・結果を結び付ける。秘密鍵・token・private本文を証跡へ保存しない。

実機では許可/拒否/解除、拒否中の作成と未確認一覧、実FCM/APNs配信、foreground/background/lockscreenのprivate本文非露出、重複pushで自動ackしないこと、offline後5分超でも一覧を確認でき古い音声を一斉再生しないこと、snooze後の旧push/遅延ackが新予定を完了にしないこと、token rotation、認可失効後tap、UID切替、再起動を確認する。利用者からiPhoneはあるがMacはないと共有されている。macOS/Xcode、Android端末/SDK、native project、署名・FCM/APNs構成、実機証跡は未提供/未検証。cloud Macは未構成で、本チケットの承認は外部サービス調達・配備の許可ではない。実cloud送信、APNs、署名build、native実機成功は主張しない。

データ/schemaの移行はない。切戻し時は登録を解除してnative配信を停止し、既存API/workerのreceiptと予定正本を保持してUIを戻す。未知の登録・配送結果を消去して再送しない。FCM tokenを古い保存場所へ複製しない。
