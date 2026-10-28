# Firebase本人認証の利用契約

BA-IAM-01は、APIの本人認証、`GET /v1/users/me`、直近再認証のhelperを提供する。membership認可はBA-IAM-02、端末専用tokenはBA-IAM-07で実装する。HTTPの正本は[OpenAPI](../specs/openapi.yaml)と[共通規約](04-interfaces.md#21-http認証スコープ)。

## 認証と後続認可の境界

[FirebaseIdentityVerifier](../services/api/src/brainoid_api/auth/firebase_identity.py)は公式Firebase Admin SDKの `verify_id_token(check_revoked=True, clock_skew_seconds=0)` に署名・algorithm・audience・issuer・UID・期限・失効・無効化の検証を委ねる。API設定のprojectをSDKへ明示し、署名を独自実装しない。未認証、不正token、期限切れ、別project、失効、無効化・削除済みFirebaseユーザーは共通401になる。公開証明書やAuth backendの取得失敗は内容を公開せず503にする。

固定SDK 7.7.0はemulatorモードで署名と時刻検証を省略する。このためSDK通過後も `auth_time`・`iat`・`exp` を整数秒として検査し、`0 <= auth_time <= iat <= now < exp` を全環境で必須にする。また同版の失効確認は `iat` を基準にするため、追加の `get_user` で `auth_time` と `tokens_valid_after_timestamp` も比較する。1回の認証でAuthユーザー取得を2回行い、失効前に認証したsessionのrefreshを許可しない。requestを跨ぐ失効状態のcacheは置かない。SDK更新時はこの差異と必要な取得回数を再確認する。

[AuthenticatedIdentity](../services/api/src/brainoid_api/auth/context.py)は検証済みUIDと認証・発行・期限時刻だけを持つ不変の値である。UIDはUUIDを要求せず、空白・大文字小文字・`/` を変換しない。本文、query、追加header、JWT custom claimの `user_id` / `role` / `allowed_scopes` は主体や権限として採用しない。Authorization headerは一つのBearer値だけを受理する。

[ADR-005](02-architecture.md#11-設計判断の記録)に従い、Firebase Authentication側のtenant分割は使わず、project直下のglobal UIDだけを受理する。root Admin SDKの署名検証は同projectのAuth tenant tokenにも成功するため、検証済みclaimの `firebase.tenant` が存在するtokenは値が空文字・nullでも401にする。`firebase` 自体がobject以外の場合も401とし、別の認証namespaceの同UIDでglobalプロフィールを参照させない。アプリのtenant選択・membershipとは別の境界である。[Identity Platformの本人確認](https://docs.cloud.google.com/identity-platform/docs/multi-tenancy-managing-tenants#identity_verification)

tenantを選ぶ経路では `select_tenant(identity, tenant_id)` の `SelectedTenantIdentity` を後続のmembership照合へ渡す。この値は選択値であり、[共通型](contracts.md)の `UserActorContext` / `TenantContext` ではない。認証成功から `membership_status=active`・許可scope・認可版を補完してはならない。BA-IAM-02は現在のmembership等を確認した後に認可済みContextを構成する。

破壊的操作は、認証済みIdentityに `require_recent_authentication` を適用する。`auth_time` から5分以内（300秒を含む）が必要で、token refreshによる新しい `iat` は再認証を代替しない。有効tokenの古い再認証は403 `reauth_required`、期限切れや未来の認証時刻は401になる。helperの成功自体はtenant管理権限を証明しない。

## 本人プロフィール

`GET /v1/users/me` は[FirestoreProfiles](../services/api/src/brainoid_api/auth/profiles.py)でglobal `users` の `user_id` を検証済みUIDと等値照合し、公開Userの5項目（user_id、display_name、timezone、status、version）だけを返す。内部metadata、他人のprofile、Firebase custom claim、email等を追加返却しない。成功には保存済みversionのETag、全応答には `Cache-Control: no-store` とサーバー生成request IDを付ける。

UIDはFirestoreのパスに連結しない。既存 `users/{user_id}` のfixtureと任意の安全なdocument IDを読める等値queryを使用する。上限2件で重複を検出し、0件は404、重複・不正な保存値は500、削除中は403とする。単一fieldの等値queryなので追加の複合索引は不要。削除中アカウントのstatus専用APIはBA-IAM-10で実装する。

このGETはプロフィールを作成・更新しない。未作成ユーザーにactive状態・timezone・versionを推測して返さない。実利用者の初回プロフィール作成は後続のonboarding実装で、本人確認、保存項目、UIDの一意性、安全なdocument ID、削除状態を定めて提供する。今回のemulator正常系はBA-FND-05の保存済みfixtureを使う。スキーマ移行やデータ書換えはない。

## 起動と運用

[API lifespan](../services/api/src/brainoid_api/auth/runtime.py)は設定検証後に固有名のFirebase appと指定databaseのFirestore clientを作り、終了時に両方をcloseする。同期SDK呼出しはASGIのworker threadへ渡す。staging/productionはADCを使い、projectをcredentialから推測しない。サービスアカウント鍵を新規作成・保存する処理はない。

SDKはprocessのemulator変数を読むため、明示Settingsを注入した場合も起動時とtoken検証の前後に実環境との一致を確認する。cloud設定にemulator変数がある場合は空文字も拒否し、起動失敗または503にする。local-emulatorは明示したAuth/Firestore設定が必須で、Firestoreは匿名credentialを使い、Authは公式SDKのemulator専用経路を使う。`local-fake` はhealth等の開発起動に使えるが、本人認証は常に401となりdummy bearerを受理しない。

token、Authorization、要求本文、JWT claim、SDK例外の文字列・tracebackをログやレスポンスへ出さない。requestログは既存のevent/request ID/status/error codeの許可リストだけを使う。SDKやtransportのpayload debugログを運用で有効化しない。

配備にはFirebase Authのユーザー照会とFirestoreのプロフィール読取に必要な既存サービス主体権限を設定する。依存追加だけでIAM権限は付与されない。本番Firebase、実ADC、Cloud Run、索引のstaging確認は今回実施していない。切戻しは以前のrevisionへ戻す。ただし旧revisionにはこの認証routeがないため、同時に対応クライアントの利用を停止する。

## 検証

通常の `mise run test` に含む[署名・HTTPテスト](../tests/test_authentication.py)は一時RSA鍵と公式SDKの実署名検証を使い、公開鍵取得・ユーザー照会のネットワークだけを置換する。正しい鍵の成功、別鍵の偽署名、異なるaudience/issuer、Auth tenantや不正なfirebase metadataの拒否とプロフィール未参照、期限・時刻、不正なBearer、失効・無効化・削除、古い再認証、主体すり替え、T-027、ログ非漏えいを確認する。cloud通信や保存された秘密鍵は使わない。

[emulator結合テスト](../tests/emulator/test_authentication_integration.py)は公式SDKで取得したemulator ID tokenをAPIへ送り、6ユーザーの公開schema、実失効・無効化、未作成・重複・不正・削除中profile、opaque UID、emulatorが省略する時刻検証を確認する。テストごとに専用demo projectをreset/seedし、終了時もresetする。明示的な実行手順は[開発ガイド](05-development-and-testing.md#52-authfirestore-emulatorの起動と初期化)の `mise run test-emulator` を使う。

公式資料（2026-10-26確認）: [ID token検証](https://firebase.google.com/docs/auth/admin/verify-id-tokens)、[失効の確認](https://firebase.google.com/docs/auth/admin/manage-sessions)、[Python auth API](https://firebase.google.com/docs/reference/admin/python/firebase_admin.auth)、[Auth emulator](https://firebase.google.com/docs/emulator-suite/connect_auth)、[固定SDKの実装](https://github.com/firebase/firebase-admin-python/tree/v7.7.0/firebase_admin)。

BA-UI-02 の [mobile認証・tenant選択](ui-authentication.md) は公式Web/native SDKと生成clientを接続し、UID/tenant変更・サインアウト・bfcache復元で前contextを破棄する。
