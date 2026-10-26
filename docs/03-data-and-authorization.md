# データ・記憶・権限設計

| 項目 | 値 |
| --- | --- |
| 状態 | 開発着手用の設計ベースライン案。実装・運用は未実施 |
| 文書版 | 0.2.0 |
| 更新日 | 2026-10-25（日本時間） |
| 責任者ロール | バックエンド担当。権限変更はセキュリティ責任者と共同レビュー |
| 関連 | [要件定義](01-requirements.md)、[基本設計](02-architecture.md)、[インターフェース](04-interfaces.md)、[運用](06-deployment-and-operations.md) |

## 1. 設計方針と守る条件

Firestore Standard を業務データの正本とし、記憶は `entities` と `facts` による軽量なグラフとして保存する。会話・ノートは出典、グラフは関係と条件、ベクトルは候補検索を担う。LLMが生成した文章やグラフ上の関係から、会員資格・アクセス権・家電の現在状態を確定しない。

以下は実装全体に適用する条件である。

1. すべてのテナントデータ操作に、サーバーが確定した `TenantContext` と `ActorContext` を渡す。
2. 同じテナント内でも、個人情報は所有者に限定する。owner/admin は他人の個人情報を自動取得しない。
3. 検索候補・グラフ探索・要約・埋め込み・キャッシュにも元情報のアクセス範囲を引き継ぐ。
4. 取得権限と表示先の許可を別々に確認する。スマホで読める情報が共有ディスプレイに出せるとは限らない。
5. 「忘れる」は検索対象からの除外だけで終わらせず、派生データ・ジョブ・端末キャッシュ・復元時の再出現まで扱う。
6. APIの入力に含まれる `tenant_id`、エンティティID、LLMのtool引数は認可の根拠にしない。

FirestoreのサーバーSDKはSecurity Rulesを経由しない。そのため、MVPではクライアントからFirestoreへの直接アクセスを拒否し、バックエンドの認可を必須にする。IAMはサービス主体の権限を絞るために使い、アプリのテナント判定を置き換えない。[公式資料](https://firebase.google.com/docs/firestore/security/rules-conditions)

## 2. 識別子と共通フィールド

| フィールド | 定義 |
| --- | --- |
| `tenant_id` | household/workspaceの境界。サーバー発行UUID |
| `user_id` | Firebase AuthenticationのUID。UUIDであることを要求しない |
| `device_id` | 登録済みPiのID。家電エンティティのIDと区別する |
| `agent_id` | テナントで共有する人格・設定のID |
| `id` | リソースごとのサーバー発行UUID。表示名を主キーにしない |
| `version` | 1から始まる整数。更新のたびに増加。HTTP `ETag` と対応 |
| `created_at` / `updated_at` | サーバー確定のUTC時刻。APIではRFC 3339表現 |
| `created_by` | 作成したuser/device/systemの主体ID。監査用で閲覧許可を意味しない |
| `visibility` | `private` または `shared` |
| `owner_user_id` | privateの場合必須。sharedの場合も管理上の作成責任者を保持可能 |
| `display_policy` | `never`、`authenticated_session`、`shared_display` |
| `deletion_epoch` | 削除・再生成防止を検査する単調増加値 |
| `schema_version` | 保存スキーマの版。APIの版とは独立 |

`visibility=private` と `display_policy=shared_display` の組合せを拒否する。`never` はスマホ上の所有者による利用を妨げず、Piへの転送・表示・読み上げを禁止する。`authenticated_session` は本人がスマホで開始したPiの短命セッションでのみ許可する。`shared_display` でも登録端末のスコープ・部屋・操作許可を検査する。

## 3. コレクション構成

以下のパスは設計上の正本であり、存在するDBを記述したものではない。`tenants/{tenant_id}` の配下を基本とする。本文・索引・リスト結果には同じtenant文脈を適用する。

| パス／コレクション | 主な内容 | 寿命・注意点 |
| --- | --- | --- |
| `users/{user_id}` | 表示名、言語、タイムゾーン、アカウント状態 | グローバル最小プロフィール。テナント会話は入れない |
| `tenants/{tenant_id}` | 名称、状態、ポリシー版、利用上限 | 削除中は新規処理を拒否 |
| `memberships/{user_id}` | role、active/revoked/suspended、認可版 | owner/admin/member。招待状態とは区別 |
| `invitations/{id}` | 招待コードのハッシュ、期限、作成者、invitation_epoch、使用operation | 生コードは保存せず、一回利用・期限検査。[招待契約](invitations.md) |
| `agents/{id}` | 人格、プロンプト版、利用可能tool設定 | 外部資格情報を格納しない |
| `devices/{id}` | 名前、能力、許可scope、状態、session generation | 端末資格情報は別管理。無期限の接続状態を信用しない |
| `private_sessions/{id}` | user/device、許可scope、期限、最終活動、認可binding | 最長5分・無操作60秒。[個人セッション契約](private-sessions.md) |
| `conversations/{id}` | agent、visibility、display_policy、タイトル | privateは所有者のみ。sharedはactive memberが参加可能 |
| `conversations/{id}/messages/{id}` | role、text、source_refs、run_id | transcriptのみ。生音声は通常保存しない |
| `runs/{id}` / `runs/{id}/events/{seq}` | 実行状態、checkpoint、出力端末とassignment generation、最終結果、再接続用イベント | events初期保持24時間。本文は会話権限を継承 |
| `audio_inputs/{id}` | STT transcript、言語、所有文脈、expires_at | 初期15分。音声バイトを保存しない |
| `speech_outputs/{id}` | 合成対象の確定文参照、run_id、期限 | 初期5分。音声は認可付き取得時に合成 |
| `entities/{id}` | 人・部屋・家電・活動などの型、ラベル、外部参照 | 同名だけで自動マージしない |
| `facts/{id}` | 主語・関係・目的語／値、出典、時間、状態 | グラフ記憶の正本 |
| `memory_vectors_<configuration_hash>/{id}` | 対象ID/版、scope、embedding、モデル/次元 | 派生索引。正本の再検査後に利用 |
| `notes/{id}` | title、body、scope | 一覧は本文の過剰返却を避ける |
| `tasks/{id}` | title、status、due_at、assignee_user_id | 状態はopen/done/cancelled |
| `reminders/{id}` | due_at、timezone、schedule_version、内容参照、通知先、状態 | 再予定で版を増加し旧通知を無効化。配信と本人確認を区別 |
| `notifications/{id}` | 元reminder、対象主体、delivery状態 | 私的内容をpush payloadに含めない |
| `integrations/{id}` | provider、所有者、許可calendar/家電、secret_ref | OAuth token本文はSecret Manager |
| `calendar_proposals/{id}` | 正規化操作、引数hash、本人、期限、状態、必要ならrun/tool_call/checkpoint | 一回確認。初期期限5分。confirm/deny後のresumeを永続化 |
| `commands/{id}` | 操作、対象Pi/家電、期限、状態、観測結果 | 詳細はインターフェース仕様 |
| `jobs/{id}` | outbox/worker状態、source版、リトライ回数 | 外部副作用前後の境界を保存 |
| `operations/{id}` | export/delete/forgetなどの非同期処理 | 進捗・失敗理由。所有者または適切な管理者のみ |
| `deletion_tombstones/{id}` | 削除対象ID、epoch、削除範囲、時刻 | 本文なし。復活防止のため保持 |
| `audit_events/{id}` | actor、操作、対象ID、結果、request_id | 本文・認証token・音声を含めない |
| `users/{user_id}/push_installations/{id}` | app installation、FCM token、端末OS | 利用者専用。退出/失効時に通知経路を再評価 |

[tenant 作成](tenants.md) 前の冪等記録は `user_operations/{user_hash}/idempotency_records/{request_hash}` に保存する。本人の現在状態と再送対象の現在 owner を確認し、tenant・owner・初期 agent と同じ transaction で commit する。削除・復元ではこの user 名前空間も対象にする。

ペアリングの一時情報は `device_pairings/{id}` に保存し、未所属の端末をテナントデータに直接アクセスさせない。テナント・actor・operation・idempotency keyの組をハッシュ化した `idempotency_records` を保存し、初期保持24時間とする。上表の追加コレクションも同じ認可・削除ルールの対象である。

## 4. 軽量グラフのモデル

### 4.1 EntityとFact

エンティティは `kind=person|room|appliance|activity|topic`、`label`、`aliases`、`external_refs` を持つ。Home Assistantの `entity_id` は `external_refs` に保存し、表示名から対象家電を決めない。同じ家電名が複数ある場合は部屋や端末の情報で解決し、解決不能なら利用者に選択を求める。登録Piはdevices、家電はentitiesの別namespaceとし、person登録で本人認証やmembership付与を行わない。公開CRUD・候補検索と内部provider登録の境界は[Entity API](entities-api.md)を参照。

factは以下の構造を持つ。例は架空の照明設定である。

```json
{
  "subject_entity_id": "5b817fe9-a7e4-4df9-bf78-0b5a4d0f8759",
  "predicate": "prefers_setting",
  "object_entity_id": "c22fa08a-36ab-4b35-a14d-c891d2c0112f",
  "qualifiers": {"context": "bedtime", "brightness_pct": 20},
  "visibility": "private",
  "display_policy": "authenticated_session",
  "owner_user_id": "firebase-user-example",
  "origin": "user_explicit",
  "status": "confirmed",
  "source_refs": [{"kind": "message", "resource_id": "fcae923b-e348-4366-943c-f2634ad6c09b", "version": 1}],
  "valid_from": "2026-10-25T10:00:00Z",
  "valid_to": null,
  "recorded_at": "2026-10-25T10:00:00Z",
  "version": 1,
  "deletion_epoch": 0
}
```

目的語は `object_entity_id` と `value` のいずれか一方を必須にする。数値や文章は `value`、別エンティティとの関係は `object_entity_id` を使う。`qualifiers` のキーはpredicateごとの許可リストで検証し、自由な命令文として解釈しない。

| predicate | 保存可能なqualifier |
| --- | --- |
| `prefers_setting` | `context`（200文字以下）、`brightness_pct`（整数0〜100）、`temperature_c`（数値16〜30） |
| `located_in` / `prefers` / `related_to` / `has_attribute` | `context`（200文字以下）のみ |

HTTP/toolの構造schemaは型・範囲を検証し、保存モデルが上表の組合せも検証する。構造schemaの成功は業務上の保存可否を意味しない。BA-MEM-03のHTTP/tool接続でも保存モデルの検証を必ず通す。BA-MEM-01の保存・出典解決・条件付き更新の実装契約は[記憶repository](memory-repository.md)を参照する。

| 項目 | ルール |
| --- | --- |
| `predicate` | 初期は `located_in`、`prefers_setting`、`prefers`、`related_to`、`has_attribute`。追加はschema変更としてレビュー |
| `origin` | `user_explicit` / `imported` / `inferred` |
| `status` | `candidate` / `confirmed` / `superseded` / `retracted` |
| `source_refs` | 根拠リソースIDと版。実際のパスはサーバー側で解決し、存在・権限・削除状態を検査 |
| `valid_from` / `valid_to` | 現実の有効期間。記録時刻とは別。`valid_to` は排他的終端 |
| `recorded_at` | システムがその主張を記録した時刻 |
| `confidence` | 将来追加する場合も順位付け専用。認可や自動確定に使わない |

MVPは利用者が明示した記憶の保存を優先する。推測を保存しても `candidate` に留め、通常の回答や家電操作の確定条件には使わない。「覚えました」は正本への保存完了後に返し、埋め込み生成の完了は待たない。

### 4.2 記憶の更新と整合

記憶の訂正は旧factを `superseded` にし、新factを作成して `supersedes_fact_id` で関連付ける。取り消しは `retracted`、忘却は削除操作で扱う。矛盾するconfirmed factが残った場合は、出典・有効時刻・確定状態を利用者に示して選択させる。最新時刻だけで異なる状況の好みを上書きしない。

共有factは個人エンティティを参照できない。共有エンティティに個人factを関連付けることはできるが、共有探索からそのedgeや件数が見えてはならない。私的な会話を材料に共有記憶を自動生成しない。共有保存が必要なら、利用者が共有する内容を明示し、共有入力として独立した出典を作る。

### 4.3 検索手順と予算

1. active membership/device/sessionを検査し、許可されるscopeを決定する。
2. sharedと本人privateを別クエリで検索する。Piへの出力ではdisplay_policyをさらに絞る。
3. エンティティを外部ID・alias・意味検索で解決する。曖昧な対象を勝手に結合しない。
4. ベクトル候補を取り、正本の版・状態・削除epoch・権限を再検査する。
5. 同じscope内で最大2hop、40fact、20entityまで展開する。この値は初期設計の予算でありFirestoreの制限ではない。
6. 有効なconfirmed fact、必要な出典、条件だけをモデルへ渡す。権限検査で落とした候補をプロンプトやtraceに残さない。
7. ツール実行時には対象・権限を再検査する。検索時の許可を使い回さない。

Firestoreのベクトル検索は最大2048次元で、ベクトルクエリのリアルタイムsnapshot listenerはサポートしない。本設計では埋め込み次元をモデル設定と索引の両方に固定し、API経由の検索結果として返す。通常のコマンド監視とベクトル検索は別の経路とする。[公式資料](https://firebase.google.com/docs/firestore/vector-search)

BA-MEM-05の配置・世代・正本fenceは[vector契約](memory-vectors.md)を参照する。

BA-MEM-06の直接ユーザー発言の要約、prefix出典・scope・版の継承と抑止は[会話要約契約](conversation-summaries.md)を参照する。

### 4.4 索引計画

| 用途 | 初期クエリキー |
| --- | --- |
| 共有記憶 | `visibility, status, subject_entity_id, updated_at` |
| 個人記憶 | `visibility, owner_user_id, status, subject_entity_id, updated_at` |
| 逆向き関係 | `visibility, owner_user_id（privateのみ）, object_entity_id, status` |
| ベクトル候補 | `scope_key, status, embedding`。scope_keyはサーバー生成 |
| 待機コマンド | `device_id, status, expires_at` |
| リマインダー | `status, due_at` |
| pending outbox | `status, available_at` |

必要な複合索引はクエリ実装と一緒にIaCへ追加し、stagingで実クエリを確認する。テナント横断collection group queryは管理用ジョブに限定し、テナント/主体の検査を省略しない。embedding、長文、token類を不要な単一フィールド索引に入れない。専用グラフDBの評価は、探索数・read数・p95・実装複雑度が予算を超えた時点で行う。

## 5. 認可モデル

### 5.1 ActorContext

認証結果から `actor_type=user|device|worker`、`actor_id`、`tenant_id`、membership/device status、許可scope、認可版、必要ならprivate_session_idを構成する。userが複数テナントに所属する場合も、リクエストごとに一つのtenantを選択しmembershipを確認する。workerはIAMで認証した上で、ジョブ対象とsourceの権限・状態を検査する。

| 操作／対象 | 本人member | owner/admin | 未認証話者の登録Pi | 本人のPi個人セッション |
| --- | --- | --- | --- | --- |
| 本人privateの読書き | 可 | 本人分のみ | 不可 | 許可scope・表示方針内で可 |
| 他人privateの閲覧 | 不可 | 不可 | 不可 | 不可 |
| sharedの閲覧・会話参加 | 可 | 可 | `shared_display` の許可範囲のみ | 同左＋本人private |
| sharedの内容編集・削除 | 作成者のみ | 共有コンテンツのみ可 | 会話投稿のみ。記憶管理は本人確認が必要 | 本人権限の範囲 |
| 会員招待・role変更 | 不可 | owner付与・移譲はownerのみ。adminはowner変更・自己昇格不可。最後のowner削除不可 | 不可 | 不可。スマホで行う |
| Pi登録・失効・scope変更 | 不可 | 可 | 自分の接続・状態報告のみ | 不可。スマホで行う |
| テナント削除 | 不可 | ownerのみ・再認証 | 不可 | 不可 |
| 個人Calendar | 自分の連携のみ | 自分の連携のみ | 不可 | 本人範囲で参照。書込み確認はスマホ |
| 許可された照明・空調 | 権限内で可 | 権限内で可 | 端末allowlist内で可 | userとdevice双方の許可の積 |
| export | 自分のデータと閲覧可能な共有範囲 | 他人privateを含めない | 不可 | 不可。スマホで行う |

ownerによる所属整理は当該テナントからの所属解除と、そのテナント内データの破棄に限定する。私的内容を読む権限と切り離し、監査と再認証を要求する。他テナントへの影響やグローバルFirebaseアカウントの削除は認めない。アカウント削除は本人の再認証を伴う要求のみで行う。破棄処理にも本文を管理者へ返さない。

membership/deviceの減権限・失効・owner移譲はDB外の制御bucketに認可変更台帳を先行記録し、DBに認可版を適用する。減権限は未完了の意図もdenyとして扱い、grantはDB適用確認済みのcommitted recordだけを有効にする。競合・未確定の主体はsuspendedとし、roll-forwardで整合させる。旧backupを復元した後に再ログインさせるだけでは旧権限が復活し得るため、最新台帳の再適用が終わるまでAPIを開かない。台帳は全復元可能backupより長く保持する初期30日案とする。

BA-IAM-05の[独立認可台帳](control-ledger.md)は operation binding・主体ごとの連続版 slot・DB 適用確認後の committed marker を追記する。effect と確定後の status を分け、部分減権限後の active を表現する。未確定の grant は隔離復元で確定せず suspended に留める。台帳なしの初期 owner を許す通常導入時の互換モードは、復元時には使わない。tenant/招待 API は IAM-04、[会員管理 API](memberships.md) は IAM-06 で接続済み。会員変更は先行 deny 前に tenant 予約を取得し、有効 owner を保持する。[端末設定・失効と保存command/worker/outputの検査境界](devices.md)は IAM-08 で接続済み。実dispatch/socketへの接続は後続チケット、実復元 gate は OPS-07 で行う。

EntityのPi読書きではapplianceにallowed_entity_ids、roomにallowed_room_idsを適用する。person/activity/topicは操作対象ではないためscope・本人所有・表示方針で検査する。本人sessionで非操作Entityを新規作成できるが、家電/部屋の新規作成に伴うallowlistの自動付与は行わない。kindのPATCH変更と公開external_refs申告は禁止する。

### 5.2 端末ペアリングと個人セッション

未登録Piは一時pairing ID、短いuser code、十分な乱数のpoll secretを取得する。codeは画面に表示し、poll secretはPiサービスのみが保持する。初期期限5分、コード照合失敗5回で失効、作成・照合にrate limitを設ける。スマホのowner/adminがテナントと端末表示名・許可範囲を確認して承認し、Piはpoll secretを提示して一度だけ専用refresh credentialを取得する。

BA-IAM-07 の[端末資格情報契約](device-pairing.md)でコードの selector/secret 構造、5回失効、IP/installation 制限、60秒の暗号化再送 receipt と再利用検知を実装する。installation はtenantへ固定し、初回device IDを保持する。IAM-08の明示再登録は旧deviceの台帳失効を条件に同tenantの新device IDを作り、旧credential/commandを移さない。通常再起動で保持する外部 `DEVICE_CREDENTIAL_EPOCH` は隔離復元時に必ず更新し、旧資格情報を復活させない。

端末access tokenは初期15分とし、refresh credentialは暗号学的ハッシュで保存・ローテーションする。ログ、URL query、Chromiumの永続storageに入れない。OSの秘密ファイルは専用ユーザーと最小権限で管理する。再ペアリングでは古いcredential・セッションを失効する。失効は新しい接続だけでなく既存接続・待機コマンドにも適用し、オンライン反映目標は60秒以内とする。

本人のスマホからPi個人セッションを開始する際、対象Pi、共有空間での表示・読み上げ、許可範囲を示す。セッションは最長5分・無操作60秒で失効する初期案とし、device・user・tenantに束縛する。セッションID単独は資格情報にしない。終了時は表示、再生キュー、ローカル一時本文を消去し、遅延した応答もsession generationで拒否する。

BA-IAM-09の[個人セッション契約](private-sessions.md)は本人の開始・状態取得・終了、Piごとの単一active slot、再送で復活しないreceipt、認可済み活動だけのidle更新を提供する。開始時のmembership/device認可版とaccount状態を現在値に照合し、変更後の再同意を必要とする。snapshot用内部境界は正規状態を返すが、配送と画面・音声消去は後続チケットで接続する。

許可scopeはconversation、memory.read/write、notes、tasks、reminders、calendar.read/propose、home.controlから本人が選ぶ。Calendar実行確認、membership/端末管理、export、アカウント/tenant削除はスマホで行う。Piはsession_startedと再接続snapshotで現在のsessionを取得し、cloud側と異なるローカル推測で本人を決めない。

## 6. 外部連携と副作用

Google CalendarのOAuth連携はuser単位で所有する。開始状態にはuser/tenant/nonce/redirect target/要求scope/期限を結び付け、callbackで `state` の一回利用と有効期限を確認する。redirect先はサーバーのallowlistから選び、任意URLを入力で受け取らない。Googleの認可コードフローと付与済みscopeを検証し、refresh tokenはSecret Managerへ保存する。[公式資料](https://developers.google.com/identity/protocols/oauth2/web-server)

Calendarの作成・変更・削除は、正規化済み内容をproposalとして表示し、同じuserの確認を受けて実行する。確認はproposal ID・引数hash・対象calendar・期限・provider ETagに束縛する。確認後の引数変更は新proposalを要求する。MVPでは自分の選択calendar上の単発予定を対象とし、参加者への招待や繰り返し系列の一括変更は別スコープとする。

家電操作ではHome Assistantの現在状態を正本とする。記憶は好みを解決するために使い、成功判定や権限を代替しない。LLMから渡されたentity IDや温度などは、型・範囲・device allowlist・provider状態で検査する。

## 7. 更新、非同期処理、整合性

クライアント更新は `If-Match` による版チェックを行い、競合は412で返す。内容の同時編集を無条件のlast-write-winsにしない。更新本体とpending jobを同一Firestore transactionで保存し、dispatcherがCloud Tasksへ投入する。投入後の通信断では同じjob IDで照合する。

workerは開始時とコミット直前にsource version、認可版、deletion_epoch、operation状態を確認する。古いジョブは結果を書き込まず `obsolete` として終了する。モデル呼出しや家電操作をFirestore transaction内で行わない。transactionの関数は競合で再実行される可能性があるためである。[公式資料](https://firebase.google.com/docs/firestore/manage-data/transactions)

エクスポートは本人のrequestで非同期operationを作成し、実行時点の権限で対象を確定する。暗号化された一時オブジェクトを作り、認証付きAPIでストリーム取得する。各downloadで現在のmembership・owner・対象deletion_epoch・期限を再検査する。退出・forget・削除・scope変更で関連する生成済みexportを失効し、GCSオブジェクトを消去する。初期ダウンロード期限24時間とし、生の公開URLをpushやログへ出さない。他人のprivateデータ、token、内部推論、管理上の秘密を含めない。

## 8. 削除・忘却・保持

| 操作 | 主な扱い |
| --- | --- |
| 会話削除 | 会話・messages・run events・speech参照を削除。派生記憶も対象にするかUIで明示し、既定は同時に削除 |
| 記憶のforget | fact、対応vector、出典から生成した要約・cacheを削除。元出典は既定でモデル再利用を抑止し、利用者の選択で出典ごと削除 |
| テナント退出 | 即時membership失効。本人のprivateデータ・連携・端末sessionを削除。共有寄稿は内容と責任者表示をポリシーに従い処理 |
| アカウント削除 | 全membership/privateデータ/個人OAuth/push経路を削除。最後のownerの場合は所有権移譲かtenant削除を先に選択 |
| テナント削除 | owner再認証後、全新規処理を拒否し、全配下・資格情報・端末sessionを削除 |

削除は認可と対象版を確認し、DB外の削除台帳へ一意なoperation IDを伴う削除意図を先行記録した後、tombstoneとoperationをFirestore transactionで保存して202を返す。tombstoneのコミット直後から検索・取得・派生書込みを拒否し、キューに残るjobも抑止する。物理削除はprimary/derivedを24時間以内とする初期目標で、成功までoperationを完了扱いにしない。

`source_policy=suppress_reuse` は、該当出典のspanを記憶検索・要約・会話履歴からのモデルcontext構築・再抽出のすべてで抑止する。spanを安全に特定できなければsource message全体を抑止する。UIの履歴として利用者が読むことと、モデルへの再提示は別の権限として扱う。元出典を残す限り抑止を保持し、期限付きtombstoneの掃除で解除しない。`delete_source` は本人が削除可能な出典も削除する選択である。他人の出典を削除する権限は与えない。独立した別出典や将来の新しい発言から同じ内容を知る可能性は残るため、「事実を完全に知り得なくなった」とは表示しない。

Firestoreでは親documentの削除だけでsubcollectionは削除されない。削除workerは対象リソースの子階層を明示的に列挙し、cursorと再試行状態を保存して走査する。[公式資料](https://firebase.google.com/docs/firestore/manage-data/delete-data)

バックアップからの復活を防ぐため、削除対象のopaque ID・版・epoch・source方針・要求hash・時刻だけをDB外のCloud Storage制御bucketの削除台帳へ先行追記する。本文は保存しない。追記はDB transactionの外で行う。台帳記録失敗時は503を返し、受付完了と表示しない。台帳成功後にDB処理が失敗した場合は同じoperation IDで再構築・再適用し、削除意図を失わない。復元では最新台帳の適用が終わるまでAPI・worker・通知を停止する。初期backup保持7日に対し台帳30日を提案し、すべての復元可能なbackupより長く保持する。詳細は運用手順で管理する。

BA-MEM-07の提供済み受付は[忘却受付と削除台帳](memory-forgetting.md)を参照する。独立tombstoneは正本の古い再配置にも適用し、再作成・model再利用・要約・履歴・export判定を遮断する。物理purgeは専用のwaiting envelopeをBA-MEM-08 workerがactivationし、受理済み削除意図の範囲に限定して処理する。全世代vector、要約、run/job payloadと子階層の消去・再開・完了確認は[忘却の物理消去と再開](memory-purge.md)を参照する。

TTLは期限付き補助データの掃除に使うが、認可・セッション失効・コマンド期限・忘却の即時判定には使わない。FirestoreのTTL削除は即時ではなく、通常は期限から24時間以内とされるため、すべての利用時にexpires_atを検査する。[公式資料](https://firebase.google.com/docs/firestore/ttl)

## 9. 監査と検証条件

監査対象はmembership変更、端末pair/revoke、個人セッション、外部連携、家電指示、Calendar確認、forget/export/delete、管理者操作とする。actor/対象ID/結果/request ID/時刻だけを既定とし、個人会話本文や秘密情報をログへ流さない。

実装完了には、別テナントIDの差替え、同一テナント内の他人private、共有Piからのprivate検索、権限変更直後の既存接続、削除後に戻ってきたembedding job、backup復元後の忘却再適用を検証する。具体的な実行方法は[開発・テストガイド](05-development-and-testing.md)で管理する。

## 10. 初期検証で確定する事項

| 項目 | 担当 | 確定時期 |
| --- | --- | --- |
| Firestore location、実際のモデル/embedding次元、索引 | バックエンド・インフラ | DB作成前（OPEN-005） |
| OAuth scope、Calendar対象範囲、公開審査 | 連携担当 | 公開beta前（OPEN-006） |
| 保持期間、backup/削除台帳の運用値 | プロダクト・運用 | 利用規約/プライバシー説明の公開前 |
| graph探索予算と性能、scope別索引のread数 | バックエンド | 実データに近い性能試験時（OPEN-007） |

数値はこの版の初期設計案であり、達成済みの性能や運用保証ではない。変更時は文書・スキーマ・検証項目を同じPRで更新する。
