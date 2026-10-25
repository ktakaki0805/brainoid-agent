# 端末ペアリングと専用資格情報

BA-IAM-07 は [FR-003](01-requirements.md) の未登録 Pi 開始、owner/admin 確認、Pi への資格情報交換と更新を提供する。[HTTP route](../services/api/src/brainoid_api/routes/device_pairings.py)、[transaction repository](../services/api/src/brainoid_api/repositories/device_credentials.py)、[専用 token codec](../services/api/src/brainoid_api/auth/device_tokens.py) を使う。端末失効・設定・再登録は IAM-08 の[端末管理契約](devices.md)、Pi の秘密ファイル保存は DEV-02、画面は UI-09、実署名鍵・実行時 IAM は OPS-04 の範囲である。

## API と確認コード

| API（`/v1` 配下） | 本人・秘密と動作 |
| --- | --- |
| `POST /device-pairings` | Pi サービスが installation_nonce、表示名、Idempotency-Key を送る。5分有効の pairing_id、8文字 user_code、256 bit poll_secret を返す |
| `POST /tenants/{tenant_id}/device-pairings:confirm` | Firebase で検証した owner/admin が code、端末名、entity allowlist を承認。現在の user・tenant・membership・独立台帳を同じ transaction で確認する |
| `POST /device-pairings/{pairing_id}:exchange` | Pi が poll_secret と固定した Idempotency-Key を提示し、一組の access/refresh を受け取る |
| `POST /device-tokens:refresh` | Pi が device_id、refresh_token、refresh_request_id、Idempotency-Key を提示し、refresh を一世代進める |

8文字の code は36種類の大文字英数字から暗号学的乱数で生成する。前半4文字を selector、後半4文字を secret とする。selector の HMAC index を transaction で予約し、有効な selector の衝突時は新しく生成する。code 全体の HMAC のみを保存する。selector を知る場合の秘密は `36^4 = 1,679,616` 通り、約20.7 bit である。これはコード照合を対象ごとに5回までに制限する前提の短命コードであり、高エントロピーの poll_secret の代用にはしない。

同じ selector への誤 secret は対象 pairing の失敗数を原子的に増やし、5回目で失効する。未知 selector は他の pairing を失効させない。owner/admin の権限確認を先に行い、無権限者は対象の失敗数を変更できない。selector を知る有権限者による5回の誤入力は当該 pairing のサービス妨害になり得る。利用者は新しい pairing を始め直す。未知 selector の探索も IP と actor の制限を受ける。

固定5分窓の上限は作成20/IP・5/installation、確認60/IP・30/actor、exchange120/IP・60/pairing、refresh240/IP・120/device。拒否・再送も回数に含む。counter は Firestore transaction で更新し、IP/installation/actor の index は専用鍵で HMAC 化する。HTTP 429 は既存共通エラーを返す。Pi は5分窓を考慮して backoff する。API は `Forwarded` / `X-Forwarded-For` を読まず、標準起動では Uvicorn の proxy header 処理も無効にする。cloud 配備では API の直接アクセスを閉じ、信頼する ingress 境界を OPS-04 で設定する。未設定の proxy header を信用してIP制限を外さない。proxy 下では同じ接続元IPへ集約され得る。

installation_nonce は Pi が安定して保持する32文字以上の識別乱数であり、物理端末の証明ではない。HMAC index を初回確認で tenant と初回deviceを記録する。tenant束縛は恒久的で、別tenantへの移動は409。既登録installationは通常の別pairingで追加発行できない。元の確認要求の同一再送は、認可版/generationが変わらない間だけ同deviceを返す。IAM-08の明示再登録では、現在のowner/adminが最新台帳で失効済みの旧device IDを指定し、同tenantの後継device UUIDへindexを原子的に更新する。旧deviceと初回IDは保持し、旧credential/commandを後継へ移さない。nonce を変更した装置を同じ物理端末と推定せず、別の未登録 installation として管理者確認を要求する。

## 秘密の受渡しと再送

開始と資格情報の応答は Pi サービスだけが受け取り、画面へ渡す値は user_code と expires_at に限る。確認応答は公開 Device metadata のみであり poll_secret、refresh、access token を含めない。CORS を有効にせず、HTTP 成功応答は `Cache-Control: no-store`。秘密を URL、ブラウザ bundle/storage、request log、例外、監査へ入れない。既存共通ログは request ID・status・error code の allowlist のみを記録する。

秘密応答は汎用 `idempotency_records` / `MutationResult` へ保存しない。専用 AES-256-GCM receipt を使い、暗号文と96 bit の毎回新しい nonce、鍵IDだけを保存する。AAD は credential epoch、operation、対象 tenant/device/pairing/family、要求 fingerprint、credential 世代へ束縛する。作成時は tenant/device が未確定なので installation と key に束縛した要求IDを使う。code、poll_secret、access、refresh の平文はDBに存在しない。

- 作成の同じ installation_nonce・key・本文は5分間同じ開始応答を返す。期限切れ・失効・交換済みは再開始として扱わない。
- 確認は同じ actor・tenant・key・本文だけ同じ device を返す。他の tenant、actor、key、本文による code 再利用は拒否する。
- exchange は一度発行した組を、発行後60秒未満かつ pairing 期限内だけ同じ key・poll_secret に返す。refresh が進んだ後は旧 exchange 応答を返さない。猶予が過ぎても新しい key で増発しない。
- refresh は更新ごとに新しい request UUID と key を作り、Pi 内で直列化する。同じ旧 token・device・request UUID・key の再送だけ、60秒未満かつ次世代が未使用なら同じ組を返す。response loss、commit 後503、再起動後も同じキー設定と epoch で再送できる。
- 同 request UUID の別本文・key は409。新しい rotation へ同 request UUID または同 key を流用しても409。使用済み refresh を別要求で使う、60秒以後に使う、さらに先の世代へ進んだ後に使う場合は family 全体を停止し、発行済み access も認証で拒否する。並行した別要求も再利用として扱う。

再送の失敗や未知の応答結果を、新しい要求へ自動的に置換しない。猶予内に同じ値で回復できなければ更新を止め、IAM-08 の失効・再登録経路へ進む。期限・reuse 検知の拒否は transaction の commit 後に例外へ変換し、失敗数や family 停止を rollback しない。

専用秘密 receipt を持つ pairing/request 文書は5分、使用済み refresh 文書と request/key tombstone は family の30日期限を `expires_at` に保存する。期限を過ぎた receipt は読出し不可であり、DB TTL の実行時刻を認可に使わない。OPS-04 は該当コレクションに TTL cleanup を設定する。refresh receipt は更新時の60秒 deadline が過ぎても最長 family 期限まで暗号文として残り得るため、鍵のアクセス権を通常 DB 権限から分離する。コード/installation HMAC の lookup_key は別用途鍵と共用しない。

## token と現在状態

access token は固定 `RS256`、`typ=device+jwt`、`token_use=device_access`、専用 issuer `urn:brainoid:{project}:device` と audience `urn:brainoid:{project}:device-api` を持ち、15分で失効する。未知 kid、別 algorithm、別 type、別 audience/issuer、改ざん、未来の発行時刻、期限切れを拒否する。Firebase の identity verifier と `AuthenticatedIdentity` へ渡さず、独立した `authenticated_device` dependency と `DeviceIdentity` を使う。Firebase token を device token としても使用できない。

tenant/device UUID、authorization_version、connection_generation、credential_epoch、family ID を署名対象へ含める。refresh は family UUID と256 bit乱数で構成し、検索・照合用 SHA-256 のみを保存する。family は最初の exchange から絶対30日で失効し、rotation では期限を延ばさない。

exchange、refresh、access 認証は同じ transaction 内の現在 tenant/device/family と[独立台帳 guard](control-ledger.md)を確認する。初回 device は一つの transaction で作成した認可版1の未追跡 bootstrap とし、IAM-08 の最初の変更から台帳追跡される。未確定 deny/grant、現在台帳と版や権限が違うdevice、停止family、別tenant/device、変化した認可版/generationでは発行・認証・再送を拒否する。refresh rotation は接続 generation を増やさず、既存 access は15分または family/現在認可が無効になるまで有効。後続 device 操作は、この依存関係から得た identity でも出力・副作用直前に現在認可を再確認する。

独立台帳は認可 metadata だけを持ち、秘密 credential を復元しない。`DEVICE_CREDENTIAL_EPOCH` はDBとは別の明示設定であり、通常再起動では保持する。OPS-07 の隔離復元では全 writer を停止し、**新しい epoch への更新と旧 credential/session 失効・再登録の準備を gate 開放の必須条件**にする。旧 backup と同じ epoch の再利用は禁止。epoch を変更すると旧 access、refresh、pairing、exchange/refresh receipt の再送をすべて拒否する。台帳の committed grant 再適用だけで旧秘密を有効に戻してはいけない。実復元 gate の実装・検証は OPS-07 へ引き継ぐ。

## 設定、鍵の導入と切戻し

| 設定 | 制約 |
| --- | --- |
| `DEVICE_TOKEN_PROVIDER` | 明示必須。local-fake は `disabled`、local-emulator は `local-test`、staging/production は `pem` |
| `DEVICE_TOKEN_KEYRING_FILE` | 有効な provider で絶対pathを明示。Secret Manager 等からservice用にmountし、cloudでは他user/groupに読めるpermissionを拒否する |
| `DEVICE_CREDENTIAL_EPOCH` | 有効な provider で UUID を明示。既定値・自動推定なし。local-fake ではkeyring/epochを設定しない |

keyring JSON は `test_only` boolean、`active_signing_kid`、PKCS8 PEM の `signing_private_key`、kid→public PEM の `verification_keys`、`active_receipt_kid`、kid→base64の256 bit鍵 `receipt_keys`、base64の256 bit `lookup_key` を持つ。RSAは2048 bit以上、active private/public は一致必須。署名鍵と receipt/lookup 鍵を共用しない。起動時に全鍵・provider・project・epochを検証し、不正なら安全なエラーで起動失敗する。test_only keyring をcloudで利用できず、鍵不足時にtest/fake/emulatorへfallbackしない。ローカルは明示した[合成鍵fixture](../tests/fixtures/device_tokens/local-keyring.json)だけを使う。

鍵rotationは新しい署名鍵の公開鍵と新しい receipt 鍵を全 replica に追加してから active kid/private を切り替える。旧署名公開鍵は旧 token の最大15分が過ぎるまで保持する。旧 receipt 鍵は旧作成receiptの最大5分とexchange/refreshの最大60秒が過ぎるまで保持する。古い cipher は鍵削除後に復号できないが、古い request を新規発行には戻さない。lookup_key は index の正本なので通常rotationで変更せず、変更する場合は全資格情報を停止する epoch 更新と index 移行・再登録を伴わせる。実キー配布・最小権限・retentionはOPS-04で実装する。

切戻しは現在 epoch と新旧公開鍵・有効な receipt 鍵を保持した互換版に限る。署名新鍵を使った後に旧鍵だけの構成へ戻すと利用中token/再送を拒否するため、旧版へ無条件に戻さない。漏洩時は対象鍵除去とepoch更新で資格情報を停止し、再登録へ進む。DB・独立台帳・消費済みtoken hashを巻き戻して回復しない。

暗号実装は [PyJWT の RS256・issuer/audience 検証](https://pyjwt.readthedocs.io/en/stable/usage.html)と [cryptography の AES-GCM](https://cryptography.io/en/latest/hazmat/primitives/aead/)を使用する。両者は既存transitiveと同版をAPIの直接依存へ追加した。PyJWTはMIT、cryptographyはApache-2.0/BSDで、追加の外部API権限は不要。RSA署名とAES-GCMはAPIプロセス内で動き、秘密の配布権限はOPS-04で分離する。

## 検証

[fake clock/HTTP テスト](../tests/test_device_pairing.py)はコード失敗5回、selector衝突、期限境界、owner/admin・越境拒否、rate limit、秘密の応答/DB/log非露出、再送、鍵・epoch変更、token改ざん・差替え、最新台帳を確認する。[emulator テスト](../tests/emulator/test_device_pairing_integration.py)は同時確認/二重交換/同一refresh、別refresh競合、Firestore commit応答喪失、同時誤入力、実Firebaseとdevice認証の相互拒否を確認する。

`mise run setup`、`mise run check`、`mise run test`、`.env.emulator.example`を明示loadした `mise run test-emulator`を使う。staging実鍵・IAM、実backup復元、Pi秘密保存、接続中失効とUI実機は本チケットで実行済みと扱わない。
