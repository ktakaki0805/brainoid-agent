# Entity登録・候補検索API

BA-MEM-02はFR-006/FR-011、T-001/T-009に対応する。人・部屋・家電・活動・話題はtenant内のUUIDで参照し、名前や別名が一致しても統合しない。登録済みPiは `devices`、家電は `entities` の `kind=appliance` であり、同じUUIDや表示名でも相互に解釈しない。personの登録は本人認証やmembership付与を行わない。

## 1. 公開HTTP

| 操作 | 動作 |
| --- | --- |
| `POST /v1/tenants/{tenant_id}/entities` | `EntityCreate` を保存し201とETagを返す。`Idempotency-Key` 必須 |
| `GET /v1/tenants/{tenant_id}/entities` | 現在閲覧可能な候補をUUID順に返す。`query`、`limit`、`cursor` を受ける |
| `GET /v1/tenants/{tenant_id}/entities/{entity_id}` | 現在の権限で取得しETagを返す |
| `PATCH /v1/tenants/{tenant_id}/entities/{entity_id}` | label/aliasesのみ更新。`Idempotency-Key` と引用符付き `If-Match` 必須 |

入力・出力の構造は既存[OpenAPI](../specs/openapi.yaml)を維持する。kind/visibility/display_policy/owner/external_refsはPATCHできない。公開POSTではexternal_refsを受け付けず、クライアントがprovider IDを申告して既存家電を取得・共有化する経路を作らない。内部保存のcreated_by、created_via_device_id、削除epoch等はHTTP応答に含めない。

queryは最大200文字、labelとaliasesのUnicode casefold後の部分一致とする。空文字/未指定は全可視候補。候補数が複数なら全候補をページとして返し、先頭を正解とは扱わない。呼出UI/toolは候補のUUIDと部屋などを確認して利用者へ選択を求める。名前一致はID統合、本人認証、操作許可の根拠にならない。

limitは1〜100、既定20。cursorはtenant・actor・現在の認可・query・limit・可視候補のID/version/epochに結び付ける。非公開候補を絞り込んでから名前・別名を検索し、ページ・cursorを作る。他人のprivateの存在・別名・件数・ID・変更が一覧やcursorに現れない。可視候補の変更や認可変更後は古いcursorを再利用できない。台帳読取障害は空の候補一覧に変換しない。

初期実装は同tenantのcanonical collectionをtransaction内で走査して認可する。レスポンスは最大100件だがDB読取数はtenant内entity数に比例する。大規模データへの検索index最適化は後続とし、負荷上限を実測済みとは扱わない。

## 2. 現在の権限・Pi

全操作で検証済みuser/device tokenを共通Readerから受け取り、同transactionでAccount、membership、独立台帳、device family/generation、private sessionを再検査する。Firebase UIDはopaque値として等価検索し、パスへ埋め込まない。現在のuserは自分のprivateのみ閲覧可能で、admin/ownerにも他人のprivateを返さない。共有情報の変更は作成本人またはadmin/ownerに限る。

Piのperson/activity/topicはscope・所有者・表示方針で制限する。家電はdeviceのallowed_entity_ids、部屋はallowed_room_idsも必要とする。この分類は[権限表](03-data-and-authorization.md#51-actorcontext)と家電操作境界に合わせたもの。MEM-01では全Entityへ無条件にentity_idを付けていたためpersonまで家電allowlistに制限していたが、今回そのmetadata分類と既存のPERSON拒否テストを修正した。kindの変更やpersonをprovider家電に変換する経路は提供しない。

Piの書込は本人private sessionとmemory.writeが必須。person/activity/topicはその条件で新規作成でき、created_byは本人UID、created_via_device_idはPi IDとなる。家電/部屋の新規UUIDには既存allowlistがないためPiからの新規作成は拒否する。スマホで作成し、別のdevice管理フローで許可済みとなった家電/部屋のみPiから閲覧・編集する。自動でallowlistを拡張しない。すべてのEntity操作でsessionのidle期限を延長しない。

検証済みtoken/family期限と正本session絶対/idle期限は、最後のDB読取後およびtransaction応答後にI/Oなしで再確認する。申告contextの期限で代用せず、欠けた期限を推測して延長しない。commit応答の待機中に期限が来た場合、本文を返さなくても保存済みの可能性がある。現在の認証を取り直し、同じIdempotency-Key/内容で結果を再照合する。拒否をrollback完了の証拠にしない。

## 3. 内部provider登録

`Entities.register(identity, tenant, ExternalRef, key)` はサーバー内部のみの入口で、HTTP routeはない。現在のtenant owner/adminに限り、canonical integrationのID/tenant/provider/statusと、所有UIDがある場合はその本人を検証する。さらにサーバー構成時に注入した `Inventory.resolve(tx, context, reference)` が必要で、未構成の本番runtimeでは登録を拒否する。

Inventoryは信頼済みprovider取得結果のcanonical DB snapshotから、現在のintegration/inventory権限、所有者、scopeと完全一致のreferenceを検証してEntityInputを返すportである。HTTP/model入力のコピーやネットワーク呼出をこのportに使わない。実Home Assistant取得・snapshot保存・更新・失効のadapterはBA-DEV-06で実装し、今回のfake/emulator adapterは合成データのみを扱う。provider同期を実装済みとは扱わない。

登録対象は `kind=appliance`、external_refsは検証された1件との完全一致を必須とする。external IDはopaque値であり、slash・Unicodeを含んでも加工せず照合する。`entity_external_ids` のkeyはtenant/integration/provider/external IDの配列をhashしたものとし、外部文字列をDBパスへ埋め込まない。

Entity、外部ID index、共通冪等性receiptを同一transactionで作成する。並行する同一外部参照の登録は1つのEntityへ収束し、tenantやintegrationが異なれば別Entityになる。既存indexは許可証として扱わず、参照先の現在WRITE権限・canonical external_refs・kind・scope・epoch・削除/再利用抑止を確認する。再送でもintegration/inventoryとindexを再検査する。同名の手動作成Entityは統合しない。

再登録でlabel/aliases・所有者・scopeを書き換えない。別名の変更は公開PATCHを使う。private/deleted/suppress_reuse/epoch不整合の既存Entityへ衝突した場合は内容を返さず、共有化・上書き・別IDへの再作成もしない。外部ID indexにversionを固定しないためlabel/aliases編集後も同じEntityを照合できるが、epochは固定して再生を防ぐ。

MEM-01で保存したexternal_refsにindexがない場合はcanonical collectionを同じtransactionで検索し、権限と完全一致を検査して既存IDのindexを作る。複数の既存一致や不正recordは自動統合せず拒否する。物理削除後もindexを削除しないこと、削除fenceを維持することは後続forget処理の責務とする。

## 4. 合成・検証・切戻し

`MemoryRepository.entities(tx, context)` のEntityTransactionはframeworkに依存しないDB合成APIで、内部で別のtransactionを開始しない。呼出元は最後の読取後・追加write前とcommit応答後に `finish()` を呼ぶ。通常のMemoryRepository create/get/updateも同じ実装へ委譲する。共通idempotencyは本文を保存せずresource IDと版のreceiptだけを保存し、結果の本文は最新権限で取り直す。

unit/fakeとFirestore emulatorで同名候補・別tenant/private、alias検索、実Pi token/session、新規personと家電/部屋制限、競合dedupe、ETag CAS、現在の権限変更、lost commit、期限到来と保存済みreceiptを検証する。実provider・実Pi・負荷試験は実施対象外。

既存HTTP構造に破壊的変更はなく、保存Entity schema_version 1を維持する。必要保存メタデータのないbootstrap/旧recordはMEM-01同様に拒否し、一覧では候補にしない。indexがない正規recordの採用以外に自動DB移行はない。rollbackは新route/内部登録の接続を戻し、Entity・index・receiptを保持する。古い実装へ戻すとPiの非操作Entity読取が再びallowlistで拒否されるため、その差を運用上確認する。
