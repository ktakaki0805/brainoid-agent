# 会話画面と run の表示

BA-UI-03 は [共通会話画面](../packages/ui/src/chat/view.tsx)を認証済みのテナントに接続する。スマホの Firebase/native 認証は[既存のセッション境界](ui-authentication.md)を使う。Pi は `fake` build で共有・`shared_display` の会話だけを表示する。通常の Pi recovery entry と device credential の取扱いは変更しない。実 Pi への HTTP/SSE 接続は BA-DEV-10。

会話一覧から agent と個人／共有を選んで作成し、文字を送信できる。個人会話は `display_policy=never`、共有会話は `shared_display` として作る。会話の公開範囲・agent・表示ポリシーは認可済み GET を使う。今回の新規 turn は `output_mode=text` で、他端末への出力指定を送らない。「この画面で文字表示」は閲覧中の UI の表示先であり、サーバーによる端末単位の独占配信を保証しない。既存 Run DTO には出力先がないため、履歴では「応答先は不明」と表示する。

[ChatController](../packages/ui/src/chat/controller.ts)は SessionContext に所有され、会話を切り替えるたびに独立した abort/世代を更新する。tenant/UID 切替、サインアウト、pagehide、認可拒否は draft・本文・カード・購読を破棄する。abort を無視した旧会話の HTTP 応答、body 読取、SSE callback も現在画面へ再公開しない。旧会話から遅れて返る 401/403/404 は現在会話の認証を解除しない。

## 差分と正本

[run-events.schema.json](../specs/run-events.schema.json)の全公開イベントについて、envelope・必須／追加 field・型・長さ・UUID・日時を[表示境界](../packages/ui/src/chat/events.ts)で検査する。SSE の `id=seq`、`event=type` を検査した後、同じ run の seq と描画状態を一緒に更新する。既読 seq は重複表示せず、heartbeat コメントは消費しない。認証は SessionContext の isolated SDK client の Authorization header を使い、イベント URL を本文から取得しない。

EOF、欠番、不正 event、HTTP 410、completed event では、同じ会話・run の GET と message 一覧を取得する。completed の `final_message_id` と同じ ID/run/assistant role の message だけを最終回答に採用し、completed event に ID があればそれとも照合する。message は cursor を追い、同じページの最新本文で代用しない。非終端 run の状態／message 取得が一時的に失敗したときは、適用済みの本文 prefix と seq を一体で保持する。既知の終端や終端 event の後は暫定本文を表示せず、最終 message の照合が済むまで保存済み最終回答とは表示しない。ページ取得は最大100ページ、各100件までで、循環 cursor や上限到達はエラーにする。

worker が最初の GET より先に完了・失敗した場合も、正本の終端表示と未取得のイベント履歴を別に扱う。保持中の SSE 履歴から操作カードを回収し、既知終端の古い本文や思考／発話状態は復活させない。切断時の GET で新たな終端が分かった場合は、残る履歴を取得する接続を一度だけ追加する。取得失敗後は「操作履歴を確認」で明示的に再取得できる。410 や取消後の配信抑止では未取得結果を確認できない旨を表示する。

終端でない EOF は「接続が終了しました」と表示し、利用者が再接続または状態確認を選ぶ。再接続にその run の最後の seq を送る。無制限の自動 retry はしない。各 HTTP 読取／書込は15秒、SSE 接続は30秒で区切り、通信が止まっても再確認の操作へ戻る。run の失敗・取消・失効では暫定本文を破棄する。作成／turn の通信結果が失われた場合、同じ入力の明示再試行には同じ冪等 key を使う。

## キャラクターと操作結果

2D は CSS と文字による待機・思考・確認待ち・応答表示・エラーの5状態。応答表示はテキスト生成の表現であり、音声再生を行った表示ではない。`speech.ready` は検証するが音声の取得／再生は行わない。録音・停止操作は BA-VOICE-05。

操作カードは認可済み `tool.status` から受付・実行中・成功・失敗・結果不明を表示する。モデルが「成功」と書いたことや run completed を操作成功に変換しない。410 等でイベント全体を取得できなければ、カードが取得済み部分の状態であることを表示する。run failed と操作 outcome_unknown は別々に表示する。

確認待ちは `confirmation.required` または run GET の `waiting_confirmation` で表示する。実際の Calendar 確認操作は BA-UI-08。生成停止は取消 API と再取得を使い、実行済み操作の rollback を保証しない。

## 検証と切戻し

[固定イベント試験](../tests/test_ui_chat.mjs)は T-005/T-019/T-032 の両 surface、重複再開・410・欠番・一時的な正本取得失敗・worker先行終了・不正 payload／frame、保存済み最終回答、現在 context の破棄、応答喪失の再送を扱う。[Chromium 試験](../tests/ui/chat_browser.mjs)は 390×844 と1280×800で状態、結果不明、入力、切断再開、private 消去を確認し、`UI_ARTIFACTS` の専用 directory に画像を保存する。

```sh
mise run check
mise run test
UI_CHROMIUM=/path/to/chrome UI_ARTIFACTS=/tmp/ui-chat-evidence mise exec -- node tests/ui/chat_browser.mjs
```

[UI SDK 結合試験](../tests/emulator/test_ui_chat_integration.py)は公式 Firestore emulator と実ローカル HTTP/SSE を使い、生成クライアントの差分再開、最終 message、410、取消、UI の正本取得を確認する。読者は架空の固定 identity を注入し、実 Firebase token 検証の代替とはしない。Firebase 認証そのものは既存 UI-02 の結合試験が対象。関連する turn・SSE replay・checkpoint/取消の emulator 試験と一緒に実行する。

新しい外部依存や DB 移行はない。切戻しでは UI build を以前の版へ戻し、サーバーの会話／run／message／イベントは保持する。実 cloud/provider/家電、実 Pi bridge、iOS/Android native build、音声、業務確認 UI は今回の検証範囲に含まない。
