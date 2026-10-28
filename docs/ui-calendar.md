# Calendar連携と確認画面（BA-UI-08）

FR-010 / AC-010 / T-015・T-016・T-037。本人スマホのSessionContextに連携、選択calendar、予定、入力、固定proposal、operationを束縛する。API契約は[OAuth](calendar-oauth.md)、[予定参照](calendar-events.md)、[proposal](calendar-proposals.md)、[書込](calendar-writes.md)、[解除](calendar-disconnect.md)を正本とする。Pi画面は本人スマホへの案内のみで、controllerからも予定・proposalの取得と確認・拒否・設定変更を送信しない。

## 本人の連携と予定

本人のCalendar metadataだけを表示する。Home Assistantは別の連携として扱う。選択した連携はGETで現在のowner/tenant/strong ETagを検証する。Calendar IDはGoogle Calendarの設定から利用者が入力する。calendarList scopeや自動探索は追加しない。最大10個の重複しないopaque ID、空欄は全選択解除である。PATCH/DELETEには同じ要求に固定したIdempotency-KeyとIf-Matchを渡す。412では入力を保ち、取得時と現在の選択を比較して利用者が基準の更新を選ぶ。応答喪失では内容・key・版を変えずに再確認する。

予定の期間は明示offsetの正の区間、最大31日。cursorはcalendar/期間/limit/認証contextを固定し、空の途中ページも続け、最大10ページで打ち切る。不正なページ、重複ID、cursorループ、異なるcalendar、壊れた日時では途中結果を公開しない。終日は日付と排他的終了日、時刻付きはUTC instantとIANA timezoneを表示する。providerが返さない説明や題名を補わない。無題は「予定」と表示する。opaque ID、provider ETag、fetched_atを保持する。

本人の同じSessionContextで観測した連携の解除・再認可要求・選択や版の変更は、予定画面と会話の確認カードへ同期的に伝える。影響する連携の予定・入力・固定proposal・operation・再送内容を消去し、処理中の取得を中止する。遅い成功応答や403も古い内容や認証状態を復元しない。一覧の欠落を削除扱いにするのは、全ページ取得中に新しい連携観測がなかった場合だけである。別の連携や現在のログインは保持し、結果が未確定の送信は権限失効を観測するまで同じkey/bodyで照合できる。これは現在権限の変更への処理であり、確認期限を本文の保存期限として扱わない。

会話のproposalを連携metadataより先に取得した場合も、その連携IDへの依存と認証済み応答の観測順を記録する。新しい完全一覧からの欠落は、そのカード・受付済みoperation・未確定再送にも適用する。連携IDがまだ分からない取得中のproposalは完全一覧の更新で中止し、明示的に取得し直す。逆に、一覧取得開始後に新しいproposalや受付を観測した場合は古い一覧を採用しない。proposalからIntegration DTOやwrite権限を合成しない。

## 固定内容と結果

作成・変更・削除の入力からPOSTで新しいproposalを作る。変更は題名・開始・終了の全内容、変更・削除の対象は取得済みevent ID/provider ETagに固定する。日付と時刻の混在や終了が開始以前の入力は送らない。timezoneとDSTの最終正規化・権限・原出典・現在会員状態はサーバーが判定する。表示する固定内容/hashをクライアント側で再計算しない。入力の変更は新key・新proposalで、旧確認を流用しない。入力中は旧カードの確認を停止する。

削除カードはcalendar/event/ETagが一致する取得済み予定だけ題名・日時を補足する。一致する取得情報がない会話由来カードではIDを示し、Google Calendarまたは一覧で対象を確認するよう案内する。サーバーの削除DTOにない内容を固定済みの内容と偽らない。

会話のwaiting_proposal_idから本人の現在認証でGETしてカードを開く。confirmは`{args_hash, confirmed:true}`だけ、rejectはbodyなし。二重tapを抑止し、通信断は同じkey/bodyを再送する。202はoperation受付として表示し、Google書込成功とは扱わない。pending/confirmed/executing/succeeded/failed/outcome_unknown/expired/declined/cancelledを分ける。5分の確認期限を過ぎた最初のconfirmは送信せず現在状態を取得する。受付済みoperationは確認期限後も取得できる。期限切れでも取得を許可された固定内容は表示できる。

状態は明示的な再取得ボタンで確認する。自動ポーリングや自動再実行はしない。結果不明や実行中のカードから新しい入力へ進まず、Googleの予定とoperationを照合する。連携解除・run取消は実行済み外部変更のundoではない。解除の202/revoked/cleanup pendingを消去完了と表示せず、GETでcompleteを確認する。Google user/project全体の同意取消は行わない。再接続は新しいサーバー連携となり、古い解除を新しいIDに付け替えない。

## ネイティブOAuthと公開前の設定

production mobileは公式`@capacitor/browser`と`@capacitor/app`を注入する。BrowserはiOSのSFSafariViewController、AndroidのシステムCustom Tabsを利用し、アプリ内WebViewへ認可画面を埋め込まない。`VITE_CALENDAR_MOBILE_RETURN_URL`は配布者が設定する固定HTTPS Universal/App Linkである。query・fragment・userinfo・任意portを許可せず、callbackのURLと完全一致した場合だけ、現在のpending UID/tenant/generationで連携一覧を再取得する。ユーザーqueryやAPI応答から復帰先を設定しない。Google認可URLはGoogleの固定HTTPS origin/pathを検査してブラウザーへ渡す。

開始APIはread/read_writeとreturn_target=mobileだけを送る。Integration DTOには実付与scopeがないため、要求したwrite権限を付与済みと表示しない。起動URLには生きたpending contextがないため採用しない。再起動後は現在の本人がログインして明示的に連携一覧を取得する。リスナーは個別handleを所有し、dispose後に解決したhandleも外す。ブラウザーopen/closeを直列化し、古いownerのcloseを新しいownerに適用しない。

設定なし・plugin初期化失敗・起動失敗・ブラウザー取消ではCalendarだけが未完了となり、認証、会話、リマインダー、通知は利用できる。webビルド単体ではネイティブOAuth開始を提供せず、既存連携の利用と設定済みスマホへの案内を表示する。code/state/PKCE/token/認可URL/予定本文をログ、URL履歴、browser storageへ保存しない。テナント・UID変更、signout、pagehideで同期的にprivate DOM/入力/retryを消去し、遅い応答・復帰通知を捨てる。bfcache復帰は既存SessionControllerの再認証方針を使う。

公開前に以下を実環境で設定・検証する。実domain/client ID/secretをこの実装で仮定しない。

- APIのprovider callbackと、mobile/webの固定HTTPS復帰先を別々に設定する。provider callbackはAPIの匿名callbackであり、mobile復帰先にcode/state/tokenを持ち越さない。
- iOS Associated Domainsと対象hostのapple-app-site-association、Androidの検証済みApp Link intent filterとassetlinks.jsonを、確定済み配布識別子・署名に結び付ける。設定は公開HTTPS hostの管理者が行う。
- 固定依存をインストール後、確定したnative projectで`cap sync`してpluginを含める。Browser/App用の不要なCalendar端末読取権限は要求しない。API OAuth・SecretStore/registry・cleanup workerのcompositionはCAL/OPS側の条件を満たす必要がある。
- 実Google同意、read/write downgrade、取消、iOS/Android warm/cold return、別UID/tenantへの切替、app再起動、background復帰、署名配布で確認する。実OAuth client、domain association、native SDK/署名環境が未構成のため、今回の合成bridge検証を実機合格とはしない。UI07の実機延期とは独立したこの機能のrelease条件である。

## 依存と検証

2026-10-28に公式v8ドキュメント、npm metadata、導入済み型とMIT licenseを確認した。`@capacitor/browser` **8.0.5**、`@capacitor/app` **8.1.2**はともにpeer `@capacitor/core >=8.0.0`で、既存8.5.3と互換。公式Ionic/Capacitor plugin群の継続保守対象で、pnpm-lockに完全固定した。mobile配布にnative browserとapp URLイベントのpluginが加わる。追加のcloud IAM、端末Calendar読取、連絡先、通知権限は付与しない。MIT表示は[Browser](../apps/mobile/LICENSE.capacitor-browser)・[App](../apps/mobile/LICENSE.capacitor-app)に同梱する。実platform SDKは導入していない。

直接Nodeの[境界テスト](../tests/test_ui_calendar.mjs)、[Chromium](../tests/ui/calendar_browser.mjs)、[実HTTP・Firestore](../tests/emulator/test_ui_calendar_integration.py)を用意する。Chromiumはproduction componentsとproduction native adapterを合成pluginで操作する。Firestore suiteは公式Emulatorと実API/client/controllerを使い、Google providerとauth readerは明示fake。公式Firebase bearer検証は既存Calendar OAuth/Auth suiteで別に検証する。通常の`mise run test`にCalendarのNode境界テストを追加し、`mise run check`でmobile/display両方をbuildする。

API/schema変更やデータ移行はない。切戻しはUIのCalendar入口を外す。受理済みproposal/operation/cleanupはサーバー正本を維持し、既存workerで継続する。クライアントを戻しても新しいoperationを作り直さない。

一次資料: [Browser API](https://capacitorjs.com/docs/apis/browser)、[App API](https://capacitorjs.com/docs/apis/app)、[Universal/App Links](https://capacitorjs.com/docs/guides/deep-links)、[公式plugin repository](https://github.com/ionic-team/capacitor-plugins)。
