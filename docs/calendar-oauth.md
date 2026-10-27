# Calendar OAuth（BA-CAL-01）

FR-010の本人単位の認可開始、callback、資格情報保存を実装する。既存OpenAPIのPOST開始、匿名GET callback、連携metadata一覧を提供する。予定参照は[BA-CAL-02](calendar-events.md)、書込は[BA-CAL-04](calendar-writes.md)、連携解除・再認可状態は[BA-CAL-05](calendar-disconnect.md)を参照する。

## HTTPと所有者

- `POST /v1/tenants/{tenant_id}/integrations/google-calendar/authorizations` はFirebase user bearerと`Idempotency-Key`を必要とする。bodyは`scope_profile: read | read_write`と`return_target: mobile | web`だけ。
- 201の`authorization_url`にはOAuthに必要なstateとS256 challengeを含む。`Cache-Control: no-store`、`Referrer-Policy: no-referrer`を付ける。PKCE verifier、code、token、secret referenceは返さない。
- `GET /v1/oauth/google-calendar/callback` はproviderからの匿名redirectを受ける。stateと、codeまたはerrorの一方だけを受理する。重複queryや所有者・任意URLの追加を拒否する。code/token/stateを含まない固定Locationで303を返す。成功/取消後は連携一覧を再取得する。
- `GET /v1/tenants/{tenant_id}/integrations` は現在の本人が所有するCalendar metadataだけを返す。limit/cursorは共通処理を使い、他人・別tenant・変更済み認可のcursorを拒否する。admin/ownerであっても他人の連携は取得できない。

mobileとwebの復帰先は、構成時に明示する2個の固定HTTPS URL（mobileは登録済みUniversal/App Link）である。providerに登録するAPI callback URLとは別の設定であり、入力URLから作らない。このリポジトリは実domain/client ID/secret/replication regionを決めない。

## 状態と秘密の境界

`tenants/{tenant}/calendar_authorizations/{id}`は、開始者、tenant、account/membershipの認可binding、要求scope/復帰先、期限、opaque secret slot、状態とcleanup pendingだけを保持する。state/nonceはSHA-256だけを保持する。state・nonce・PKCE verifier本文は短命のsecret slot内に置く。

期限は開始から10分と開始時bearer期限の早い方。期限ちょうどで無効になる。callbackは保存された開始者を復元し、現在のaccount、tenant、membership、独立control ledger、lifecycle fenceを再検査する。DBの所有者・要求・期限・slotが差し替わっても、secret内のimmutable bindingと一致しなければ交換しない。callbackの別bearerやqueryから所有者を選び直さない。

| 状態 | 意味・再送 |
| --- | --- |
| preparing | DB予約とsecret-less冪等receiptをcommit済み。secret保存の応答喪失は同じslotから回復できる |
| pending | 認可URLを発行可能。同じ本人・同じkey/bodyの再送は同じstate/PKCEに収束する |
| exchanging | DBで一回消費済み。外部交換の成功/失敗が不明でも同じcodeを再交換しない |
| connected | token保存と連携metadataのcommit済み。secret参照は明示versionだけ。短命slotの消去が残ればcleanup pendingを保持する |
| failed | 接続済みを作らず、短命/token両slotを失効・消去する。消去失敗はcleanup pendingを残す |

交換とsecret保存はアプリDB transactionの外で行う。交換前、交換後、metadata commitで現在の認可を再確認する。外部tokenは`SecretStr`を経てSecretStoreへだけ渡し、Firestore、冪等receipt、公開metadata、exportには含めない。read要求は`calendar.events.readonly`、read_write要求はそれと`calendar.events`である。保存する有効scopeはprovider応答のscopeと要求scopeの共通部分だけであり、要求したwrite権限を付与済みと推定しない。Calendar scopeやrefresh tokenが欠ける応答は接続済みにならない。

## Secret Manager adapterと構成

`GoogleOAuth`と`OAuthHTTP`は明示client設定でGoogle認可URLとtoken endpointを使う。`SecretManagerHTTP`は明示credentialとreplication policyを受け取り、secret作成、version追加、exact-version access、destroyを実装する。HTTPはtimeout 10秒、redirect禁止、環境proxyの暗黙利用なし、POST/認証refreshの自動再送なし。外部例外文字列を公開しない。client/transportは構成者がcloseする。

認証は各Secret Manager HTTP requestに専用の送信予算を持たせ、必要な同期refreshで実HTTP送信を最大1回に制限する。認証HTTP自身にもredirect禁止とtimeout 10秒を設定し、非200応答はSDKへ本文を返さず固定例外で終了する。SDKが同じrefresh中に再度requestを呼んでも2回目は送信前に拒否する。予算は並行操作と共有せず、次の操作は新しい予算で必要なrefreshを行える。認証成功後のSecret Manager応答が401でもrefreshや操作の再送はしない。

credentialは明示注入された同期`refresh(request)`/`apply(headers)`契約を使う。標準のGoogle `service_account.Credentials`による単一token endpoint交換を検証済みで、SDKのbackground refreshや暗黙credential discoveryは使わない。複数回HTTPを必要とするmetadata discovery・external account federation・impersonation等の複数段refreshはこのadapterの対象外であり、2回目で失敗する。credentialがこのrequest契約を迂回して独自通信する構成はサポートしない。構成者はこの制約を満たすcredentialを明示選択する。fakeでは`AnonymousCredentials`を明示注入できる。認証本文を記録するSDK transport loggerを経由せず、署名assertion・token・providerのerror本文を公開例外やlogへ出さない。

`GoogleCalendarSecrets`は、UUIDで固定したsecret名ごとにnumeric version 1を使い、`latest`を使わない。追加前に独立したdurable registryへ`adding`をcommitする。追加の応答を失ったらversion 1だけを照合し、2回目のaddVersionを発行しない。secret payloadにはimmutable bindingと秘密本文を置く。registryには所有scope、generation、binding、effect状態だけを置き、token本文を保存しない。Googleが返すcanonical project表記と一致するproject numberを明示設定できる。

registryは`LocalCalendarSecrets`のSQLite transaction/fenceを共有する明示compositionで、プロセス再起動をまたぐ。SQLiteは同一永続volumeを共有するプロセス群の検証用構成であり、各Cloud Run instanceの一時diskへ個別配置して公開してはならない。分散配備時は全writerが同一の直列化・失効境界を使うregistryへ置き換え、その結合試験をOPS-04で行う。`LocalCalendarSecrets`自身はAES-GCM暗号化されたfake/emulator用secret adapterで、実Secret Managerの代替として自動選択しない。

デフォルトの`AuthServices.calendar`は未構成である。APIは503で閉じ、fake/provider credentialを自動発見しない。有効化にはAPI service、SecretStore registry、recovery処理、lifecycleのcalendar hookを一体で明示compositionする。実providerへの公開はこの配線とOPEN-006の結合確認後に行う。

## 削除、遅延結果、export

secret slotとaccount/tenant/member scopeにdurable fenceを設ける。削除より前に開始した遅延putも失効後に読み出しを復活させられない。membership削除は受け付けたauthorization generationまでを失効し、完了後の新generationによる再参加・再連携を妨げない。古い削除の再送で新generationのsecretを消さない。Secret Managerの消去再試行は今回受理したscope/generationの予約だけを対象とし、別tenant・別userの未確定追加が今回の秘密消去を止めない。

lifecycle producer inventoryにprivate `calendar_authorizations`を加え、private `integrations`とともに本人/tenantの受理済みpurgeで消去する。外部secretの`calendar` hookがない場合、対象producerを消して完了証明を出さない。hookが必要だった事実はpurge進捗に残り、canonical削除後のworker再起動でもhookを省略できない。Secret Managerのaddが不明でexact versionがまだ存在しないときは、消去成功と扱わずpurgeを再試行状態に保つ。

exportの閉集合には`integrations`の公開metadata projectionだけを追加する。本人と現在のbindingを再確認し、token、付与scope、secret reference、OAuth state/attemptは出力しない。manifestは公開metadataと秘密本文を含まない認可/secret referenceのhashに束縛し、生成後の差し替え・削除・認可失効では既存downloadを拒否する。

## 復旧と切戻し

`CalendarAuthorization.recover(tenant, authorization_id)`は内部worker/運用のentryでありHTTPには公開しない。terminal attemptのcleanupを再試行し、期限を過ぎたpreparing/pending/exchangingをfailedにして両slotを失効する。運用構成は予約を期限順に走査し、この処理を継続する。接続commit後に応答だけを失った場合はconnectedを読み直し、保存済みtokenを消さず短命slotだけを消す。

Secret Managerの未確定追加はregistryに残し、exact versionの存在/DESTROYED状態を照合する。存在を確認できない間は、旧requestの終了やprovider状態を運用者が確かめるまで削除完了を宣言しない。registry/fenceを初期化して再addする復旧は行わない。provider側にtokenが生成された可能性があり本文を受領できなかった場合、アプリは未接続を維持し、利用者がGoogleアカウントで旧同意を取り消してから新しいkeyで開始する。

追加のみのschemaで既存データ移行は不要。切戻し時はCalendar admissionを未構成へ戻してから既存attemptの期限・cleanupと受理済みpurgeを完了し、secret registry/fenceを保持する。旧binaryは新producerの外部消去hookを知らないため、未消去データがある間は旧purge workerで完了させない。

今回のfake/Emulator/HTTP transport試験は外部API・有料providerを呼ばない。実有効化では1開始に短命slot、成功にtoken slotが加わり、Secret Managerの保存/access/destroyとregistry運用が費用対象になる。料金・region・上限は実設定前に確定する。

## 検証と未実施

`tests/fakes/calendar_scenarios.py`を通常の厳密transaction fakeと実Firestore Emulatorで共用する。期限ちょうど、state/nonce/owner/要求scope/期限binding、再送、同時callback、取消、scope downgrade、現在のaccount/tenant/member/ledger、交換・保存・commit応答喪失、遅延失効、cleanup再試行を検証する。HTTPでは不正入力・秘密非漏洩・303固定復帰・本人metadata一覧・default 503を検証する。Emulatorの追加HTTP試験だけは実Firebase Auth Emulator bearerを使い、provider/token/secret backendはfakeを使う。

Secret Manager adapterはfake transport、具体的HTTP transportはfake HTTP responseでendpoint、form/JSON、timeout、redirect/retry禁止、numeric version、永続fence、遅延add、destroy応答喪失、membership新generationを検証する。lifecycleは実OAuth producerをmembership/account/tenant各purgeに流し、exportは実archiveとmanifest再検査を確認する。

認証transportは実`service_account.Credentials`、合成Signer、fake HTTPAdapterで通常のrefresh成功・cached token再利用・307/503・通信例外・並行/次操作の独立予算を検証する。複数段credentialの2回目送信拒否と、例外を捕捉して再呼出しするcredentialの上限も確認し、実Google通信は行わない。

実Google同意画面、登録redirect、scope審査、実Secret Manager/IAM、mobile Universal/App Link復帰、staging疎通はOPEN-006/OPS-04の設定が未構成のため未実施。docs/08の§1に従いfake/emulatorの独立完了範囲と分ける。Google server-clientのPKCE対応を実client設定で確認するまでは公開しない。

標準起動はUvicorn access logを無効にし、API logは固定fieldだけ、validation errorも入力を含めない。独自Uvicorn起動・reverse proxy・cloud request logには別途query全体の除去が必要であり、その実配備設定は今回検証していない。

## 一次資料

2026-10-27に以下を確認した。SDK/HTTP adapterは既存lock内のGoogle auth/requests/cryptographyを使い、依存追加・upgradeはない。

- [Google Web Server OAuth](https://developers.google.com/identity/protocols/oauth2/web-server): state、登録redirect、実付与scope。
- [Google Desktop OAuthのPKCE](https://developers.google.com/identity/protocols/oauth2/native-app): S256の計算。client種別が異なるため実server clientの疎通確認とは区別する。
- [Secret作成](https://docs.cloud.google.com/secret-manager/docs/reference/rest/v1/projects.secrets/create)、[version追加](https://docs.cloud.google.com/secret-manager/docs/reference/rest/v1/projects.secrets/addVersion)、[version破棄](https://docs.cloud.google.com/secret-manager/docs/reference/rest/v1/projects.secrets.versions/destroy): 固定endpointとpayload契約。
- [exact versionのaccess](https://docs.cloud.google.com/secret-manager/docs/access-secret-version): numeric versionでの整合性。独立fence/未確定effect registryはこのアプリの設計判断であり、Secret Managerのparent削除だけで即時失効すると仮定しない。
