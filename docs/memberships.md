# 会員管理と owner 移譲

BA-IAM-06 は FR-002 の会員一覧、role 更新、所属失効、owner 移譲を実装する。[repository](../services/api/src/brainoid_api/repositories/memberships.py)、[route](../services/api/src/brainoid_api/routes/memberships.py)、[OpenAPI](../specs/openapi.yaml) が正本。本人の退出・アカウント削除・private データの purge は IAM-10、画面は UI-11 の範囲とする。

## API

| 操作 | 契約 |
| --- | --- |
| `GET /v1/tenants/{tenant_id}/memberships` | 現在 owner/admin のみ。ID・role・status・整数認可版を返す。limit は1〜100、既定20。cursor は現在の actor・tenant・一覧状態・limit に束縛 |
| `PATCH /v1/tenants/{tenant_id}/memberships?user_id={uid}` | `{role}` のみ。owner/admin が既存 active 会員を変更。owner 付与・剥奪は owner のみ。admin の自己操作は拒否 |
| `DELETE /v1/tenants/{tenant_id}/memberships?user_id={uid}` | owner/admin による他会員の当該 tenant 所属失効。owner 対象は owner のみ。直近5分の再認証が必要。本人退出は受け付けない |
| `POST /v1/tenants/{tenant_id}/memberships:transfer-owner` | owner 本人と直近5分の再認証が必要。`{target_user_id, target_version}` を送り、既存 active な非 owner へ移譲。本人は member になる。対象の Membership と ETag を返す |

変更には16〜128文字の `Idempotency-Key` と引用符付き整数の `If-Match` が必要。PATCH/DELETE は対象の認可版、transfer は本人の認可版を `If-Match` に使い、移譲先の版を本文に含める。header の重複は400、欠けた `If-Match` は428、版違いは412、別 payload へのキー再利用は409。Firebase の opaque UID は必須 query `user_id` で受け取る。生成 SDK の `updateMembershipByUid` / `revokeMembershipByUid` には未変換の UID を `query.user_id` として渡し、SDK が query encoding を行う。重複した `user_id` は400、欠落は422。DB path に UID を連結せず、既存 canonical collection の UID 検索を使う。transfer は UID を本文で受け取る。

既存の `/memberships/{user_id}` と SDK の `updateMembership` / `revokeMembership` は互換性のため残す。URL の dot segment 正規化により `.` / `..` 等は percent encode だけでは path に保持できないので、任意の opaque UID を扱うクライアントは新しい query 経路を使う。UID 自体は禁止・変換しない。両経路は同じ認可・版照合・冪等 receipt を共有し、経路だけを変えた同一要求も二重適用しない。

| UID の例 | query 経路での扱い |
| --- | --- |
| `.`、`..` | URL path に含めず、そのまま対象 UID として照合 |
| `opaque / uid` | slash と空白を query encode し、一度 decode して照合 |
| `%2E` | literal な percent を encode し、`.` とは別の UID として照合 |
| `u?/#+` | query 区切り・fragment・plus をそれぞれ encode して保持 |

owner は別の有効 owner がいる場合に限り本人を admin/member へ降格できる。最後の有効 owner の降格・失効は409。owner 付与だけなら PATCH を使える。変更しない role の PATCH と失効済み会員への新規変更は403。同一要求の再送は固定された操作の完了を確認して返す。後続変更後の古い操作は権限を戻さず拒否する。

一覧と変更結果にはプロフィール・会話・private 本文・資格情報を含めない。所属失効は会員の Firebase account、他 tenant の所属、本文を変更しない。本人が同じ古い Firebase token を持っていても、tenant/招待/会員 API は現在 DB と独立台帳を評価して失効した tenant を拒否する。今後の worker/output 経路も同じ guard を使う必要がある。

## 先行 deny より前の予約

DB の最後の owner 検査だけでは、別の owner を対象とした二つの先行 deny が同時に台帳へ入ることを防げない。そこで `tenants/{tenant_id}.membership_mutation` に一つの operation UUID を保存し、同じ transaction で `membership_operations/{hash(actor, key)}` を作る。予約時に現在の actor、tenant、対象の版、全会員の有効状態を読む。予約取得前には台帳へ一切書き込まない。

receipt は actor UID、グローバル user 状態の fingerprint、tenant policy/deletion epoch、要求 fingerprint、変更前状態、固定した一つまたは二つの `Change` を保持する。秘密・本文を含めない。別の会員変更は予約がある間409となる。予約はプロセス終了・タイムアウト・台帳障害でも残し、自動期限切れや TTL では解放しない。

有効 owner は、canonical membership の tenant/UID が一致し、重複がなく、[通常の台帳 guard](control-ledger.md)を通った role=owner、status=active の会員である。DB の active だけでは数えず、pending intent、台帳と版・role・status の不一致、追跡済み主体の台帳欠落は suspended として除く。未追跡初期 owner の許可は通常稼働の bootstrap モードだけであり、隔離復元では使わない。

各 `AuthzChanges.execute` の validate/apply/confirm/activate は同一 DB transaction 内で receipt と予約、現在の user、tenant、対象と最新台帳を再確認する。処理対象自身は一時的に停止するので、未完了の各 phase で対象以外に有効 owner がいることを要求する。外部台帳の書込みは transaction 外で行う。

owner 移譲は次の順で roll-forward する。

1. 移譲先に owner grant を先行記録し、DB 適用・台帳 commit・DB active 化まで完了する。この間は旧 owner が有効。
2. 移譲先の最新台帳と active owner を確認してから、旧 owner の member への deny を記録・適用する。この間は新 owner が有効。
3. 両主体の現在版・payload・確定状態を確認し、監査成功記録と receipt 完了・予約解放を一つの DB transaction で行う。

そのため移譲の途中では一時的に owner が二人になり得る。複数主体を同時 commit する分散 transaction とは扱わない。安全条件は全 phase で少なくとも一人の有効 owner を残すことである。

本人の降格が始まると通常の管理権限が消えるため、その再開に限り本人に束縛した receipt と exact `Change` の進捗を確認する。現在台帳が別 operation や新しい版なら拒否し、一般 PATCH や他の管理操作の権限として receipt を使わない。本人の global user 状態・tenant の状態・再認証は毎回確認する。

## 監査、再試行、停止と復旧

`tenants/{tenant_id}/audit_events/{operation_id}` は actor UID、対象 UID、結果、request ID、時刻だけを保存する。予約取得と同じ transaction で `pending`、全変更の完了・予約解放と同じ transaction で `succeeded` とする。途中失敗は pending のまま残り、成功扱いにしない。HTTP の拒否結果は共通 request log の status/error_code/request_id で確認する。認証 token、要求本文、表示名、個人内容は audit やログへ記録しない。

503や応答喪失の後は、同じ本人が同じ `Idempotency-Key`、本文、`If-Match` を再送する。再認証期限を超えたら本人が再認証し、token だけを更新して同じ要求を送る。予約、intent、DB 適用、commit、active 化、完了応答のどこで停止しても同じ操作に収束し、deny による invitation_epoch を重複増分しない。receipt は24時間で消さず、未完了操作は解決まで保持する。完了後も古い要求の再送を別操作として扱わない。

actor の削除・不可逆失効、tenant の停止、policy/deletion epoch の変更、台帳不整合などで本人が再送できない場合は予約を保持したまま fail closed とする。管理者が reservation や ledger を手で削除して次の操作を通してはいけない。独立台帳と receipt、DB の主体状態を確認し、書込元を停止した隔離環境で復旧を判断する。自動引継ぎ・別 actor による強制再開 API は提供しない。全件列挙と実復元 gate は OPS-07 の範囲である。

IAM-10 の本人退出・アカウント削除など、今後 active membership を失効・減権限する全経路も、この tenant 予約と最後の有効 owner 判定へ参加する必要がある。認可変更の内部 DTO は業務権限を与えない。現在の招待 accept は存在しない/失効済み本人への admin/member grant のみで、active owner を変更できず、owner 付与もできないため、owner を失わせる経路にならない。予約中の招待 accept/grant は各 phase で409を返し、失効の完了前に次版の再参加が割り込むことも防ぐ。まだ claim していないコードは使用済みにせず、既存 claim は予約解放後に最新台帳を確認して再送する。deny の DB 適用は invitation_epoch を増やすため、古い招待 accept は再確認で拒否する。隔離 restore は全 writer 停止を前提とする。

導入時は route と ledger guard を同時に有効化する。追加の設定・依存サービスはなく、[招待と同じ runtime ledger](invitations.md)を使う。切戻しでも台帳と予約を残し、台帳を読まない旧実装へ owner 変更を戻さず、対応版へ roll-forward する。

## 検証

[fake テスト](../tests/test_memberships.py)は T-033/T-034、自己降格後の再送、最終 owner、先行記録前の競合、opaque UID、一覧 cursor、監査の allowlist、transfer の両主体の各台帳書込み前後と各 DB transaction 前後の失敗を確認する。[生成 SDK の実 Request](../tests/fixtures/memberships/sdk_requests.mjs)の URL・method・headers・body を変更せず API に投入し、上表の UID の更新・失効と既存 path API との再送互換も確認する。[emulator テスト](../tests/emulator/test_memberships_integration.py)でも同じ実 Request を投入して対象の更新・失効を確認し、公式 Firestore transaction の同時降格・異なる owner の同時移譲、部分失敗、予約/完了 commit の応答喪失、実 Firebase token を再利用した tenant 限定失効を確認する。

`mise run setup`、`mise run check`、`mise run test`、`.env.emulator.example` を明示 load した `mise run test-emulator` を実行する。外部の実 credential/bucket や本番データは使わない。実 IAM/retention、実 backup 復元、worker/端末 session の失効反映は後続チケットで検証する。
