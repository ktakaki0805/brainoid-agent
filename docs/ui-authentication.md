# ログイン・テナント選択と画面の消去

BA-UI-02 / FR-001 はメール・パスワードの Firebase サインイン、サインアウト、所属一覧・作成・選択を提供する。ユーザー登録・招待の受付、会員 role 管理、会話画面はそれぞれの後続 UI の範囲。[サーバー本人認証](authentication.md)と[tenant API](tenants.md)の既存契約を使用し、プロフィール未作成の404をサインアウト理由にしない。

## 認証と保存先

[AuthenticationAdapter](../packages/ui/platform/auth.ts) は global UID の観測、サインイン、UID に束縛した ID token 取得、サインアウトを提供する。tenant membership や role を認証結果から補完しない。生成 SDK は要求ごとに adapter から bearer を取得する。token・Firebase User を React state に保存しない。password は入力中だけフォームの一時的な React state に保持し、送信時・pagehide時に入力を消去する。token・password・Firebase User を URL、history state、log、UI cache、Web storage に保存しない。

- Web は[Firebase browser adapter](../apps/mobile/src/firebase-browser.ts)から公式 SDK の `initializeAuth` と `inMemoryPersistence` を使う。ページ再読込と別documentからのBack復元では再ログインが必要。localStorage / IndexedDB に認証 session を永続化しない。この方針を native の安全な保存と同一視しない。
- Capacitor は[実 native adapter](../apps/mobile/src/firebase-native.ts)から `@capacitor-firebase/authentication` の native Firebase SDK を利用する。`skipNativeAuth=false`、native の `getCurrentUser` / `getIdToken` / `authStateChange` / `signOut` を接続し、refresh credential は JS 側へコピーしない。保存・復元・消去の責任は native Firebase SDK に置く。Android の app-private 保存を独自の暗号化 secure-store と呼ばず、端末 OS と SDK の保護を前提とする。iOS/Android の実機での保存・バックアップ・ロック時保護・signout と再起動の検証は BA-OPS-09/10 の配布 gate として残る。
- native plugin が失敗したとき browser persistence へ fallback しない。実 native project、Firebase 設定ファイル、署名・ストア配布は未作成・未検証であり、この変更で実機対応済みとはしない。native API への具体的な呼出経路と UID 競合は注入 plugin で検証する。
- Pi の通常 entry は既存の復旧画面を維持する。所有者の Firebase credential を Pi に置かない。display の `fake` モードだけが合成 user/tenant で共有画面を表示する。

公式資料（2026-10-28確認）: [Web サインイン](https://firebase.google.com/docs/auth/web/start)、[Web persistence](https://firebase.google.com/docs/auth/web/auth-state-persistence)、[Capawesome native Firebase adapter](https://capawesome.io/docs/sdks/capacitor/firebase/authentication/)。依存と配布条件は[toolchain](toolchain.md#ui認証とブラウザー検証の依存)を参照。

## 設定と実接続

mobile の Vite build/dev に以下を明示する。`VITE_*` は公開 bundle に入るため、service account、password、ID/refresh token を入れない。設定不足・不正時は準備画面に留まり、実 project や fake を推測しない。接続先を URL query から採用しない。

| 設定 | 内容 |
| --- | --- |
| `VITE_API_URL` | API の HTTPS origin（末尾 `/` なし）。path、query、credential は不可 |
| `VITE_FIREBASE_API_KEY` / `VITE_FIREBASE_AUTH_DOMAIN` / `VITE_FIREBASE_PROJECT_ID` / `VITE_FIREBASE_APP_ID` | 明示した Firebase client 設定。native は同じ project の native 設定も別途必要 |
| `VITE_AUTH_EMULATOR` | `--mode emulator` のときのみ `http://127.0.0.1:9099`。project は `demo-brainoid`、API は `http://127.0.0.1:8000` に限定。通常 mode での存在は拒否 |
| API の `CLIENT_ORIGINS` | カンマ区切りの exact origin、最大16件、既定空。Web の公開 origin と必要な Capacitor origin を明示。wildcard、`null`、credential、path、query は拒否 |

[Capacitor 設定](../apps/mobile/capacitor.config.json)は server の scheme/host を上書きしていないため、[Capacitor 8 の既定](https://capacitorjs.com/docs/config)は iOS `capacitor://localhost`、Android `https://localhost`。API 側に利用する origin だけを列挙する。HTTP は local 環境の numeric loopback に限定し、staging/production は拒否する。native の plugin 設定を実 project に sync し、メール/password provider の有効化は実配布前に別途行う。この作業では実アカウントや cloud を使用しない。

API は既存 Starlette CORS middleware により Authorization、Content-Type、Idempotency-Key、If-Match、Last-Event-ID、X-Private-Session を登録 origin に許可し、ETag / X-Request-ID を公開する。cookie credentials は許可しない。CORS は browser の response 読取制御であり、認証・membership 認可や native caller の身元証明ではない。未登録 origin の preflight は拒否するが、Origin なし・単純要求を CORS だけで拒否する仕組みではない。既存 bearer/現在権限の検証は全要求に適用する。

## Context の寿命

[SessionController](../packages/ui/src/session.ts) は UID と選択世代ごとに独立 SDK client と AbortSignal を持つ。URL の tenant は未信頼の選択値であり、`GET /tenants/{id}` が現在の所属を確認するまで名前や選択済み内容を描画しない。一覧の行を選んだ場合も個別 GET を再実行する。

切替・UID変更・サインアウト・pagehide では旧 context を同期的に無効化し、要求中断、cache/draft 消去、登録済み resource（SSE reader等）の dispose を行う。`context.run((client, signal, tenantId) => ...)` で生成 SDK を呼び、完了時も世代を照合する。transport が abort を無視しても古い応答・遅延した body parse を採用しない。後続画面は `context.generation` で subtree を key 化し、stream の signal と callback の `assertCurrent()`、追加 cleanup の `own()` を使う。生 client を global に保持しない。

React は `useSyncExternalStore` で同期して context を反映する。pagehide は `flushSync` で private subtree を DOM から外してから bfcache に入れ、Web/fake は pagehide で認証の再利用も同期的に禁止し、SDK signout を待たずに閉じる。別documentでサインアウトしてBackした場合も、旧documentの in-memory identity を再採用せず明示サインインを要求する。SDK cleanup が freeze により遅延・失敗しても古い callback で解除しない。native は pageshow の復元時に共有 native SDK の identity と membership を再確認する。history に private state を保存しない。サインアウトは native/SDK cleanup を待つ前に画面を消去し、古い sign-in/refresh が完了しても復活させない。サインアウトに失敗した端末では再試行を表示し、認証済み操作を再開しない。

401 は1回だけ token refresh し、元の本文と冪等キーで再送する。refresh失敗・再度401は再ログインへ移る。選択中tenantへの403/404は選択内容を消去して理由を表示し、別tenantを自動選択しない。一覧・作成の拒否表示は操作時の選択世代に束縛し、古い作成の403/404で後から選択したtenantを消去しない。所属一覧のcursorが400なら一覧を破棄して明示更新を案内する。作成は同じ本文の再試行に同じ冪等キーを再利用する。作成中に別tenantを明示選択した場合、遅れた作成結果はその画面を上書きしない。サーバーで完了した作成を取り消したとは扱わず、一覧更新で照合できる。

## 検証と切戻し

通常の `mise run test` に[Node競合テスト](../tests/test_ui_session.mjs)と[CORSテスト](../tests/test_client_origins.py)を含める。`mise run test-emulator` には[公式 SDK + API + Firestore 結合](../tests/emulator/test_ui_session_integration.py)が含まれる。認証なし・未所属URL・401/403/404・遅延応答・古いrefresh・UID切替・応答喪失後の同key再送・プロフィール未作成を確認する。合成 account だけを一時作成し、終了時に reset する。

実ブラウザーは Playwright Core 1.63.0 と対応する Chromium executable を別途用意し、共有cacheを変更せず指定する。通常 test に browser download は含めない。

```sh
export UI_CHROMIUM=/absolute/path/to/chromium
export UI_ARTIFACTS="$(mktemp -d /tmp/brainoid-ui-evidence.XXXXXX)"
mise exec -- node tests/ui/session_browser.mjs
```

この確認は専用 tmp build/profile と loopback preview を所有し、390×844・1280×800で遅延 A→B、入力消去、サインアウト→Back、401/403/404、未所属URL、別documentでサインイン・サインアウトした後のBackと実 bfcache（`pageshow.persisted=true` と pagehide の private DOM 0件）を確認してスクリーンショットを残す。browser・previewは終了する。

公式 Emulator と実 mobile entry の browser 結合は、[Emulator設定](05-development-and-testing.md#52-authfirestore-emulatorの起動と初期化)を export し、上記 `UI_CHROMIUM` / `UI_ARTIFACTS` に加えて `CLIENT_ORIGINS=http://127.0.0.1:5175` を設定して次を実行する。8000/5175も未使用である必要がある。

```sh
mise exec -- pnpm exec firebase emulators:exec --only auth,firestore \
  --project demo-brainoid --config firebase.json --non-interactive \
  'uv run --all-packages --locked --offline python -B -m unittest discover -s tests/emulator -p test_ui_session_integration.py'
```

データ/schema移行はない。切戻しは以前の mobile bundle に戻し、新画面の利用を止める。CORS origin を外す場合は該当 client の接続も止まる。既に作成した tenant と冪等台帳は消さない。実 native build・OS安全保存・本番 provider・Pi実機・配布の検証はこのローカル結果に含めない。
