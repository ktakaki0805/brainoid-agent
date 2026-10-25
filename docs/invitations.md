# 期限付き招待と本人の参加

BA-IAM-04 は FR-002 のコード招待を実装する。[repository](../services/api/src/brainoid_api/repositories/invitations.py)、[HTTP route](../services/api/src/brainoid_api/routes/invitations.py)、[OpenAPI](../specs/openapi.yaml) が対象。メール送信は行わず、コード共有 UI は BA-UI-11、role 変更・owner 移譲は BA-IAM-06 の範囲とする。

## API とコード

| 操作 | 契約 |
| --- | --- |
| `POST /v1/tenants/{tenant_id}/invitations` | 現在の owner/admin が `role: admin/member` を指定。201で id・code・expires_at・role を返す |
| `POST /v1/invitations:accept` | Firebase で認証した本人が `code` のみを送る。201で本人の membership を返す |

両方に16〜128文字の `Idempotency-Key` が必要。重複 header は400。owner、任意の user_id、受諾側の role/tenant_id は422で拒否する。member は発行不可。端末資格情報は利用不可。既存の active membership がある利用者には新しい招待で role を上書きせず403を返す。profile 未作成でも参加できるが、既存 profile が削除中なら拒否し、profile を推測して自動作成しない。

コードの有効期間は24時間。サーバー生成 UUID に `CURSOR_SIGNING_KEY` を使った HMAC-SHA256 を適用し、64文字のコードを返す。入力 prefix `brainoid/invitation/v1` と NUL 区切りで cursor MAC と用途を分離する。Firestore、共通冪等 receipt、独立台帳には生コードを保存しない。コードの SHA-256 とランダムな invitation ID を保存し、発行の同じ key/body の再送は現在の管理権限・未使用・期限・招待 epoch を再確認して同じコードを再導出する。応答は `Cache-Control: no-store`。コードを URL、ログ、通知本文へ置かない。

キーの rotation は古い cursor を無効にする。受け取った既存コードの受諾は保存済み hash との照合なので、有効期限内は rotation 後も可能。同じ発行要求の再送では古いキーがなければ再導出できず410を返す。別 key で新しい招待を発行する。既発行コードを即時無効にする手段としてキー rotation を扱わない。全 replica/revision に同じキーを注入し、24時間の発行再送を維持する必要があればその間はキーを保持する。秘密値は Secret Manager 等で管理し、公開 emulator 用キーを本番へ転用しない。

## 原子的な claim と grant

受諾は検証済み UID の hash を用いて、テナント横断で15分間に5回まで照合する。異なるコード・key・テナントでも同じ UID の上限を共有し、成功・再送も回数に含む。上限は429、有効なコードの最初の claim が `now >= expires_at` なら410、存在しない/使用済みコードは404。同じ成功 key に別コードを送ると409。window の境界で回数をリセットする。失敗の結果は transaction から返し、カウンタの commit 後に HTTP error へ変換する。失敗例外による rollback で総当たり回数を消さない。

1. Firestore transaction で現在の本人 account、招待発行者の owner/admin、tenant、招待 epoch、コード hash、期限、一回使用、現在の本人 membership と最新台帳を確認する。
2. 招待を一つの operation に使用済みとして束縛し、UID/key ごとの `invitation_acceptances` receipt を同じ transaction で保存する。receipt は code hash、招待 path、固定された IAM-05 `Change`、claim 時刻だけを持つ。UID を document path へ連結しない。
3. [独立認可台帳](control-ledger.md)へ transaction 外で intent を記録する。DB は同じ operation・整数認可版・目標 role と `suspended` を保存する。
4. DB の適用を確認した committed grant のみを有効にする。validate/apply/confirm/activate と応答の取得で現在の本人 account、発行者権限、招待 epoch、固定 claim と最新台帳を再確認する。

最初の claim が期限内に成立した後は、期限を越えても同じ operation の再試行で処理を続けられる。receipt は24時間で新操作へ置き換えず、古い claim を別の grant に変換しない。最新の失効・別 operation が存在すれば旧受諾を拒否する。権限喪失や別の減権限で途中の grant が停止した場合は pending を自動確定せず、隔離した運用手順で判断する。

同じコードの同時受諾は一人だけが claim できる。同じ本人/key の同時再送は同一 operation に収束する。同じ本人が別コードを並行受諾しても、台帳の主体・整数版の単一 slot が一つの grant だけを許可する。失敗した側のコードは使用済みになり得るが、membership を複数作ったり role を上書きしたりしない。

## 失効と再招待

membership の deny（失効・減権限）の最初の DB 適用時、tenant の `invitation_epoch` を同じ transaction で1増やす。欠落時の初期値は0。招待は発行時の epoch を保存し、受諾・再送ごとに現在値と照合する。これにより、失効前に保持していた未使用の招待も利用できなくなる。受領者を指定しないコード方式のため、**一人の失効でも同じ tenant の既存招待すべてが無効になる**。管理者は必要な利用者へ新しいコードを発行する。

同じ deny の operation/version/payload が DB に適用済みなら epoch を再増分しない。失効後の参加には、現在の DB と一致する最新の committed revoke、新 epoch で発行された未使用招待、新しい acceptance key/operation、DB 適用確認済み grant が必要。pending deny/grant、古い active DB、古い受諾 receipt では再参加できない。

隔離復元の `reapply` では、最新 operation/version が復元 DB と異なる membership を適用する際にも epoch を増やす。最新が grant でも、backup から失われた途中の deny を飛ばして古い招待を有効にしないためである。同じ操作の再適用では増分しない。これは通常の受諾 retry と別の保守経路で、復元中は全 writer を停止する。

## runtime・保持・切戻し

Firebase 利用環境の tenant 作成再送・一覧・個別取得・設定更新と招待 API は同じ最新 ledger guard を利用する。初回 owner 等、台帳がまだなく DB にも authorization_operation_id/authorization_change がない主体にだけ `allow_untracked_bootstrap=True` を使う。台帳障害・pending・DB と異なる版は停止扱いであり、この互換設定で回避しない。隔離復元では互換設定を利用しない。

| 環境 | 必須の台帳設定 |
| --- | --- |
| local-emulator | `AUTHZ_LEDGER_DIRECTORY` に絶対 path。DB と別の create-only ファイル台帳を使用 |
| staging / production | `AUTHZ_LEDGER_BUCKET` に独立した GCS bucket 名。明示 project/credential の Storage client を使用 |
| local-fake | 台帳設定なし。認証済み業務 API の dummy access を提供しない |

local directory と cloud bucket の同時指定・別環境への流用は起動時に拒否し、暗黙のメモリ台帳・別 bucket・ADC/project 推測へ切り替えない。local adapter は一時ファイルを fsync してから atomic hard link で公開し、既存オブジェクトを上書きしない。process 再起動・emulator DB reset でも台帳は保持される。これはローカル検証用であり、本番 GCS の IAM/retention の代替ではない。

本番 bucket の作成・IAM/retention は BA-OPS-02、実 backup の隔離復元は BA-OPS-07 が担当する。台帳と epoch を扱えない旧版へ戻さず、該当 API を停止して対応版へ roll-forward する。招待/lookup/claim の自動 TTL 削除は実装していない。purge 時は使い切り情報・claim・台帳を全復元可能 backup より長く保持し、古い backup からの再利用を防ぐ設計を先に確認する。emulator の独立台帳を消して DB reset を安全な権限復元とみなさない。

## 検証

[HTTP・障害行列](../tests/test_invitations.py)は権限、owner 注入、秘密非保存、期限と window 境界、再送・別コード、失効前の未使用コード、再招待、台帳/DB の各段階の失敗、旧受諾、最新 grant の復元、キー rotation、ローカル台帳の再起動を検証する。[emulator 結合](../tests/emulator/test_invitations_integration.py)は実 Firebase HTTP と Firestore の同時受諾、異なる本人/コード、残り1回の並行照合、失効後の pending grant、claim commit 応答喪失を確認する。

実行は `mise run setup`、`mise run check`、`mise run test`、`.env.emulator.example` を明示的に load した `mise run test-emulator`。外部 API・実 credential・本番データは不要。実 GCS 配備、staging index/負荷、メール、コード共有画面は本チケットの検証対象に含めない。
