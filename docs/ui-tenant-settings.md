# 会員・招待・利用量の画面

BA-UI-11 / FR-002・015 / T-002・033・034。スマホの「会員と利用量」は[既存会員API](memberships.md)、[招待API](invitations.md)、[利用量API](usage.md)を利用する。[controller](../packages/ui/src/tenant-settings/controller.ts)と[画面](../packages/ui/src/tenant-settings/view.tsx)は共有UIに置き、Piの共有画面にはスマホへの案内だけを表示する。新しい認可台帳、利用量計算、メール配信、他人のprivate会話・利用明細への導線は追加しない。

## 操作と現在の権限

owner/adminは会員ID・role・status・認可版の一覧、member/adminとしての招待発行を利用できる。adminの自己操作、ownerへの変更、owner対象の操作、owner移譲を表示しない。ownerは他のactive会員へownerを移譲でき、本人はmemberになる。最後の有効ownerについては移譲またはテナント削除が必要であることと、本人のアカウント削除との違いを案内する。本人退出・テナント削除・アカウント削除の実行はBA-UI-12の範囲で、この画面の所属失効は他会員の当該テナント所属だけを対象とする。

変更には対象テナント、会員ID、現在role/版、変更内容の確認を必要とする。確認直前にも所属と一覧を再取得する。UIDはopaque値のまま生成SDKのquery対応APIへ渡し、path化・trim・UUID化しない。PATCH/DELETEは対象のIf-Match、移譲は本人のIf-Matchと移譲先のtarget_versionを使う。412は最新一覧へ更新して選び直し、409は最終ownerや他の未完了操作による競合として案内する。減権限や所属失効によって既存招待すべてが無効になることを確認画面にも表示する。

会員管理を開いている間の60秒周期・window focus、明示更新、操作前に所属を再検証する。権限判定を含むAPI呼出しは常にサーバーの最新台帳で拒否できる。画面の検出時にはtenant contextを破棄し、他機能も含む旧世代のcache・draft・取得済み画面を消去する。リアルタイムのrole通知は追加しない。

## 結果不明と再認証

操作は一度に一件とし、結果不明ではIdempotency-Key、対象、本文、If-Matchを変えず手動で再確認する。後続403/429を先行要求の失敗と断定しない。自己降格や移譲により通常の管理権限が消えても、既存APIが許す本人のexact receipt回復を維持する。そのため要求だけをglobal user contextと選択tenantの世代へ束縛し、通常の会員一覧、利用量、招待コード、確認画面とは別に保持する。tenant切替（同じtenantへの戻りも含む）、本人切替、サインアウト、pagehideでこの回復要求も破棄する。別actorへの引継ぎ、予約や台帳の削除は行わない。

所属失効・owner移譲の403では現在のアカウントによる再認証を案内する。token forceRefreshを再認証として扱わない。ブラウザーはFirebaseの`reauthenticateWithCredential`、nativeは既存pluginに専用methodがないため現在のemailを事前照合した`signInWithEmailAndPassword`と前後の同一UID・認証世代確認を使用する。認証SDK操作はSessionのqueueで直列化し、再認証中のサインアウトは先に画面を失効させ、遅いSDK完了の後で資格情報を消す。パスワードは送信時に入力欄から消し、controllerの状態・storage・ログへ保存しない。未対応adapterは再認証不可を表示する。

## 招待と利用量

受諾フォームはログイン済みの未所属・tenant未選択の利用者にも表示する。送信する値はコードだけで、参加先・role・本人UIDの指定を受け付けない。成功後は所属一覧を更新し、利用者が選択する。招待コードは発行表示と未確定受諾要求のメモリにだけ保持し、URL、storage、通知、ログへ置かない。発行コードは隠す操作、所属の再確認、tenant/本人の切替で消去する。受諾の5回/15分制限には成功の再送も含まれるため、応答喪失後の429では同じ要求を維持して待機を案内する。

memberは本人集計、owner/adminはtenant総量として、UTC月・会話回数・音声秒・残り会話回数を表示する。音声は未確定予約を含む。memberの残り回数は本人とtenant枠の小さい方で、状態は両方の枠を評価する。owner/adminの残り回数・状態はtenant枠だけを評価し、本人の利用可否を示さない。tenantがavailableでも本人の月次枠・同時実行枠に達していれば要求は拒否される。available/limited/exhaustedの案内はこの集計範囲に合わせ、月次上限と同時実行上限を分ける。60秒後の再確認が容量回復の保証ではないことを示す。料金、購入、上限変更、任意userの明細機能は追加しない。

## 検証と展開

[Node境界試験](../tests/test_ui_tenant_settings.mjs)はrole、opaque UID、確認、競合、減権限後のexact再送、tenant/user/認証世代の競合、コード消去、制限表示、native bridgeの再認証を検証する。[利用量の描画試験](../tests/test_ui_tenant_usage.mjs)は実APIの本人月次枠・同時枠到達とtenant余裕の6ケースをReactへ入力し、role別のquota3状態も確認する。[ブラウザー試験](../tests/ui/tenant_settings_browser.mjs)は明示したChromiumとfake bundle、[実API結合](../tests/emulator/test_ui_tenant_settings_integration.py)はAuth/Firestore emulator・本番と同じmobile entry・生成SDKを使用する。実Firebase tokenによる招待受諾、UID5種のrole変更/所属失効、同一本人の再認証、移譲応答喪失後の回復を検証する。

通常の`mise run check`・`mise run test`に加え、専用のlocal emulator環境で上記結合試験を実行する。ブラウザーは`UI_CHROMIUM`と`UI_ARTIFACTS`を明示する。スクリーンショットに招待コードやパスワードを含めない。実cloud・実iOS/Android端末・本番負荷の検証ではなく、UI-11のfake/emulator受入範囲である。

追加dependency・API/schema・永続データ移行はない。切戻し前に未確定の会員操作を同じ本人・同じ要求で解決する。ブラウザー終了後はメモリ内要求を復元できないので、サーバーの予約を削除せず[会員APIの復旧手順](memberships.md#監査再試行停止と復旧)で確認する。
