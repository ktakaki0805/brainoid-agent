# テナントの作成・選択・設定

BA-IAM-03 は FR-001・FR-004 の初期状態を提供する。API は `/v1/tenants` の GET/POST と `/v1/tenants/{tenant_id}` の GET/PATCH。実装は [tenant repository](../services/api/src/brainoid_api/repositories/tenants.py)、通信契約は [OpenAPI](../specs/openapi.yaml) を正本とする。人格の会話への反映・agent API は BA-RUN-03、招待は BA-IAM-04、role 変更は BA-IAM-06 の対象。

## 作成の境界

検証済み Firebase user identity の有効期限を確認し、既存の本人 `users` document があれば現在の active 状態を同じ transaction で確認する。profile が未作成でも tenant 作成・一覧を利用できる。表示名を推測して global profile を作ることはなく、`GET /v1/users/me` は保存済み profile がなければ引き続き404。既存 profile が削除中なら403、破損・重複なら500で拒否する。端末資格情報はこの user API の認証にならない。

POST の name と timezone（省略時 `Asia/Tokyo`）から tenant、本人の active owner membership、初期 agent、冪等記録を一つの Firestore transaction で作る。tenant・agent ID はサーバー生成 UUID、membership document ID は `["user", Firebase UID]` の hash とする。UID は不透明な値としてフィールドへ保存し、path へ連結しない。作成途中の失敗は全書込みを rollback する。callback は再実行され得るが、外部 API・通知等の副作用を行わない。

初期 agent の設定値は repository の bootstrap が正本で、`name=Brainoid`、`persona=""`、`allowed_tools=[]`、`prompt_version=1`、`version=1`。人格本文・tool 権限を自動生成しない。tenant の `policy_version` と owner の `authorization_version` は1、削除 epoch は0で始める。設定 PATCH は name・timezone のみを更新するため policy version は増やさず、resource version を1増やす。

tenant が存在しない作成要求には [共通冪等処理](common-mutations.md) の明示的な `execute_user` を使う。`user_operations/{user_hash}/idempotency_records/{request_hash}` は user・operation・key で安定して決まり、要求ごとに生成する tenant UUID を台帳キーへ含めない。tenant scope を偽装しない `AuthorizedUserScope` が現在の global account 状態へ束縛する。台帳の24時間・正規化 body hash・成功 receipt・原子的 commit は通常の共通処理と同じ。生 key・本文・token は保存しない。

作成の再送は現在の identity/account と receipt が指す tenant の現在の active owner を確認する。owner を失った利用者には古い作成結果を返さない。別 body の同じ key は409。期限後の key は新操作であり、新しい tenant を作れる。初回と再送は201、返却する Tenant と ETag は現在の認可済み document から再取得する。過去の本文 snapshot を返す保証ではない。commit 後に応答だけ失われた場合の503は、同じ key/body で再送して照合する。

## 選択・設定・ページング

tenant ID は毎回未信頼の選択入力として扱い、同じ read/write transaction の現在 tenant・membership を共通 policy で照合する。未所属・失効・削除中 tenant は404。個別取得の `my_role` は現在の membership から投影する。管理設定は user の owner/admin のみ、member は403。PATCH は Idempotency-Key と強い If-Match が必須で、If-Match 欠落428、古い version は412。正規化 patch と If-Match の version を冪等 body に束縛し、再送で二重更新しない。取得・作成・更新の ETag は返却本文の version に対応する。

一覧は UID 等価条件の `memberships` collection-group query を同じ transaction で実行する。正規の `tenants/{uuid}/memberships/{key}` path の候補だけを採用し、各 tenant の現在 policy を再検査してから本文を取得する。未所属 tenant を全走査して本文をフィルタする実装ではない。破損・曖昧・inactive membership は公開しない。tenant ID 昇順、limit は既定20・最大100。

cursor は user の現在状態と現在有効な membership 全件の policy stamp、operation・order・limit へ束縛する。過去ページの所属失効・role 変更・tenant policy 変更でも400となり、先頭から取得し直す。cursor は1時間で失効する。現在の所属候補は全件読み、本文はページ内だけ読む。非常に多数の tenant に所属する利用者のコスト最適化は、権限集合の版を原子的に更新する設計を導入する際に見直す。

`CURSOR_SIGNING_KEY` は Firebase 利用環境で必須の32〜512文字の秘密値。十分な乱数を Secret Manager 等で管理し、全 replica・revision に同じ値を明示注入する。値は SecretStr で隠し、設定エラーには出さない。自動生成・production の固定 fallback はない。変更時は古い cursor が失効する。`.env.emulator.example` の公開固定値はテスト専用で、staging/production には使わない。local-fake の health 起動には不要。

## 検証・配備・復旧

`mise run test` は [HTTP/boundary テスト](../tests/test_tenants.py)、`mise run test-emulator` は [実 transaction テスト](../tests/emulator/test_tenants_integration.py) を実行する。2 tenant fixture の T-001、並行作成の収束、同時 body conflict、owner/admin の更新競合、全 write rollback、実 commit 後の応答喪失、失効後の再送、opaque UID、profile 未作成、cursor の現行権限を検証する。emulator は固定 loopback の `demo-brainoid` のみ使用する。

本番の collection-group 等価 query には [index 定義](../firestore.indexes.json) の `memberships.user_id` が必要。emulator は本番の index 要求を再現しないため、BA-OPS-02 で index の配備と staging 確認を行う。本 ticket はクラウドへ配備しない。既存 tenant データを移行する場合、公開契約の `id=tenant_id` と明示 timezone を確認し、推測値を読取り時に補完しない。合成 fixture にはこれらを追加済み。

切戻し時も tenant・owner・agent・冪等記録を削除しない。作成台帳は user 名前空間なので、後続の account/tenant 削除・purge と backup 復元では receipt の tenant 参照も対象にする。24時間の保持中に台帳形式や namespace を変える場合は旧記録を読める期間を確保する。独立認可台帳による復元・権限変更 fencing は BA-IAM-05 で接続する。
