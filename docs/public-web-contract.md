# 公開Webからの本人アカウント削除

BA-UI-13 / FR-013 / AC-013 / T-022・T-034。[独立entry](../apps/web-data-controls/src/main.tsx)はアプリ未導入の通常ブラウザーで、既存の[認証UI](ui-authentication.md)、[削除フォーム・進捗表示](ui-data-controls.md)、[global account削除API](account-deletion.md)を利用する。メール代行、管理者による本人確認の迂回、別の削除システムは設けない。

## 本人確認と申請

1. 登録済みメール・パスワードでFirebaseへサインインする。登録・パスワード復旧はこの入口の対象外。
2. 「本人アカウントの削除を確認」から、同じUIDに束縛した `reauthenticateWithCredential` で再認証する。ログイン直後でもこの手順を必須とする。token refreshを再認証とみなさない。
3. 現在の本人UIDと「すべての所属・本人だけに公開したデータ・個人連携・通知経路、共有寄稿は自動的な全消去の対象外」の範囲を確認し、明示的に要求する。再認証だけでは送信しない。
4. 生成clientの `POST /v1/operations` に `{"kind":"delete_account","confirm":true}` と同じ要求用の `Idempotency-Key` を送る。URL query、history、storage、フォームからUID/tenant/kind/API接続先を採用しない。公開entryはtenant一覧や通常のprivateデータを取得せず、tenant作成・選択・削除を構成しない。

サーバーは現在のtokenの `auth_time` が300秒以内か再検査する。拒否後は同じ本人の再認証から同じbody/keyで再送する。最後のownerは409で拒否され、所有権の移譲またはtenant削除が必要と案内する。このページが管理権限を代行することはない。応答喪失は受付不明として同keyで再送し、成功扱いにしない。最初の明確な拒否だけ選び直せる。

202は台帳とtombstoneを記録した受付であり、物理消去完了ではない。pending / running / failed / outcome_unknown / succeededを既存表示で区別する。accountのprovider最終処理後は本人GETが404、認証構成により401になるため、認証終了や404を完了証明として扱わない。実APIが成功を返していないとき、画面が成功を推測することはない。

## 再読込みと私的状態の寿命

既存Webの `inMemoryPersistence` を維持する。token、refresh credential、Firebase User、パスワードをURL、history、log、Web storageへ保存しない。[公式Firebaseのmemory-only persistence](https://firebase.google.com/docs/auth/web/auth-state-persistence)は再読込みで認証を消すため、同じ本人として再ログインする必要がある。画面にもこの手順を示す。[公式再認証](https://firebase.google.com/docs/auth/web/manage-users#re-authenticate_a_user)のcredential確認を用いる（2026-10-29確認）。

再読込みの手掛かりは同一origin・同一タブのsessionStorageの `brainoid-account-operation-v1` だけで、厳密に `version: 1`、UIDのSHA-256 `owner`、operation UUIDの `operation` の3項目とする。認証・本文・プロフィール・削除要求body/key・進捗は保存しない。UID hashはアカウント取り違え防止の照合値であり、匿名化や認可ではない。タブを閉じた後の回復や端末間同期を保証しない。storageを使用できない場合も申請できるが、自動回復できないと表示する。

再ログイン後、UID照合と構造検査を通ったlocatorだけを既存 `GET /v1/operations/{operation_id}` で読み直す。取得中は新規申請を禁止する。locatorは未信頼入力であり、GETは現在のbearerによる本人・operation・global endpointのサーバー認可を毎回必要とする。返るkind/idも照合し、以前の進捗をstorageから描画しない。hashを改竄しても他人のoperationを読めず、locatorをPOST権限や再送keyへ変換しない。削除受付後のtenant参照失効は、既存の狭い本人operation GETを妨げない。

pagehideでは同期的にフォーム・受付表示・認証を消し、locatorだけを残す。bfcacheからのBackでも再ログインが必要。サインアウト、認証失効、別UIDへの切替はlocatorも消す。token取得自体の失敗とHTTP401もsessionを終了する。古いcontextから遅れて失敗が返っても、新しいcontextを終了せず、A→B→Aの切替後に旧進捗を書き戻さない。未ログインの初回描画だけではlocatorを消さず、本人確認前に表示もしない。

応答喪失のまま再読込みした場合、未取得のoperation IDを推測しない。同key再送はそのdocumentのメモリー内だけで保持する。locator保存済みの受付については、同じ本人の有効な認証を再取得して既存進捗を回復する。取得不能の場合は未確認のまま表示する。

## ビルドと公開設定

workspaceの `apps/*` に独立member `@brainoid-internal/web-data-controls` を追加し、`pnpm run typecheck` と `mise run check:ui` / `mise run check` の対象とする。`pnpm --filter @brainoid-internal/web-data-controls build` の出力は `apps/web-data-controls/dist/`。新しい依存版は追加せず、既存のFirebase 12.19.0・React 19.3.1を同じlockで利用する。ライセンス・配布条件は[toolchain](toolchain.md#ui認証とブラウザー検証の依存)に従う。

[既存設定validator](../apps/mobile/src/auth-config.ts)を再利用し、build時に次を明示する。未設定時は準備画面に留まり、fakeや本番projectへfallbackしない。環境値は公開bundleに入るため秘密を入れない。

| 設定・配置 | 契約 |
| --- | --- |
| 公開削除URL | **未確定**。運営がHTTPSの専用URLを選び、GOV-03の保持・削除説明ページから直接リンクする。queryによる削除targetやredirect URLを加えない |
| `VITE_API_URL` | 明示したHTTPS API origin。path/query/credentialを禁止。公開entryのURLや外部から注入された値を採用しない |
| Firebase client設定 | `VITE_FIREBASE_API_KEY` / `VITE_FIREBASE_AUTH_DOMAIN` / `VITE_FIREBASE_PROJECT_ID` / `VITE_FIREBASE_APP_ID`。対象projectとメール認証設定を運営が確定する |
| Firebase許可domain | **未確定**。実際の公開originのhostを対象projectで許可し、公式SDKで実接続を確認する |
| API `CLIENT_ORIGINS` | 確定したWeb originをexact matchで許可する。wildcardや資格情報をURLに含めない |
| GOV-03の配置・説明 | **未確定**。説明ページから削除URLへ接続し、保持条件・運営窓口・削除範囲と確認方法を整合させる |
| Hosting | HTTPS、HTML/APIの `Cache-Control: no-store`、`Referrer-Policy: no-referrer`、適切なCSPとframe制限を設定し、CDN・アクセスログへcredentialや個人内容を記録しない。service workerやprivate応答cacheを追加しない |

ローカル検証だけは `--mode emulator` で、API `http://127.0.0.1:8000`、`demo-brainoid`、`VITE_AUTH_EMULATOR=http://127.0.0.1:9099` に固定する。通常modeのemulator設定を拒否する。

**公開結合条件は未充足であり、この変更ではdeployしない。** URL、許可domain、GOV-03の配置に加え、既存IAM-10のproduction受付・provider最終処理・worker/cache/export接続のgateを満たしてから公開する。ローカル実装・fake/emulatorでの受入検証を、実project・公開HTTPS・本番消去の完了と同一視しない。

切戻し時は公開入口からの新規受付を閉じ、受理済みの台帳・tombstone・operation・workerは[削除契約](account-deletion.md)に従って継続する。locatorやブラウザーの認証を消してもサーバーの削除要求は取消されない。旧版の公開bundleを戻すために台帳を巻き戻さない。

## 検証

[境界テスト](../tests/test_web_account_delete.mjs)は必須再認証・対象確認、固定body/endpoint、禁止されたtenant操作、再ログイン回復、別UID・改竄locator/応答・A→B→A・古いtoken失敗、storage拒否を検証し、通常 `mise run test` に含める。

[実ブラウザー試験](../tests/emulator/test_web_account_delete_integration.py)は専用Auth/Firestore Emulator・実API/CORS・公式Firebase SDKと[Chromiumシナリオ](../tests/ui/web_account_delete_browser.mjs)を結合する。未ログイン、別人credentialでの再認証拒否、対象確認、実202、reloadでの認証消去と同本人再ログインによる実GET回復、別UIDと偽造hashの実GET拒否、最後ownerの実409、Auth emulatorで本人を無効化した後の消去、Back復帰をスマホ/desktop幅で確認する。進行・失敗・unknown・成功の表示と404の表示はGET応答の明示的なfault injectionも使い、実providerの消去成功とは主張しない。既存mobile/displayのデータ管理ブラウザー試験も共有フォームの回帰対象とする。

再現には `.env.emulator.example` の明示的なlocal設定、`UI_CHROMIUM` の既存Chromium実行path、`UI_ARTIFACTS` の新しい証跡directoryを指定し、`firebase emulators:exec --only auth,firestore --project demo-brainoid --config firebase.json` の中で `PYTHONPATH=tests uv run --all-packages --locked --offline python -B -m unittest discover -s tests/emulator -p test_web_account_delete_integration.py` を実行する。fixtureは専用demoデータをreset/seedし、API・preview・browserとemulatorを回収する。
