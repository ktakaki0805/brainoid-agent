# 冪等性・版・cursorの共通処理

BA-FND-08（FR-004/007/008）のAPI内部共通処理を `services/api/src/brainoid_api/common` に置く。業務route、認証・IAM実装、token発行、外部providerへの操作は後続チケットで接続する。[通信契約](04-interfaces.md#22-版ページング冪等性)と[データ認可設計](03-data-and-authorization.md#7-更新非同期処理整合性)を前提にする。

## 呼出し境界

`AuthorizedScope` は `TenantContext`、対象の `Scope`、所有userを必須で受け取る。privateのownerは省略できない。この型の生成自体は認可ではない。`Idempotency.execute` の `authorize(transaction, replay)` callbackは毎回、現在のmembership、grant、操作権限、対象のscope・owner・削除状態を確認してこの型を返す。古いContextを返すだけのcallbackは契約違反である。

最初は `replay=None` で要求を認可し、保存済記録がある場合は、その `MutationResult` を渡して対象resource/operationを再認可する。これにより、初回に生成されたIDも再送時に現在の権限・削除状態で確認できる。削除の再送を許可する業務は、認可可能なoperationまたはtombstoneを別途保持する。拒否は `APIError` で伝え、記録の存在や内容を返さない。callbackは同じtransactionを使って認可データを読み、同時の権限変更もtransactionの競合対象とする。独立認可台帳を使う将来のIAMは、その台帳のfencing/deny手順も満たす必要がある。

束縛対象はtenant、actorの種別・ID、許可scope、認可版、端末のprivate session IDと有効user、resource scopeとownerである。private sessionの現在の絶対期限・idle期限は毎回検査する。期限の値自体はhashへ含めない。同じsessionの活動でidle期限が延長されても再送とcursorを利用できるようにし、session交代・user交代・認可版変更では古い結果を拒否する。

## 保存と再送

`Idempotency.execute` はサーバー定義のoperation（更新対象IDも含める）、16〜128文字のkey、schema検証・既定値・時刻等の正規化後のJSON bodyを受け取る。bodyのobject keyは再帰的にソートして空白を除いたJSONをSHA-256でhash化する。配列順序とscalar型は維持し、NaN/Infinityは扱わない。この形式はRFC 8785ではなくPython API内部の形式であり、他言語から記録を生成しない。

`tenants/{tenant}/idempotency_records/{hash}` のIDはtenant/actor種別/actor ID/operation/keyの組をhash化する。本文や生keyは保存しない。保存フィールドはbody hash、認可scope binding、作成時刻、24時間後の `expires_at`、成功receiptのみ。同じkey・正規化bodyは同じreceiptを返し、異なるbodyは409 `idempotency_conflict`。認可・scopeが変わった場合は結果を返す前に拒否する。24時間の境界以降は新操作として扱い、残っている古い記録を同じtransactionで置き換える。

成功receiptの `MutationResult` はstatus code、resource/operation UUID、versionだけを許可する。任意のresponse bodyやheaderを受け取らないため、token発行の秘密は保存できない。業務本文の取得は現在の認可を通して別途行い、この共通receiptから当時の本文を再構成できると仮定しない。token発行は専用の再送プロトコルを持つこと。

Firestoreの公式SDK adapterが、業務document、必要なoutbox job、冪等記録を同じtransactionでcommitする。callbackは競合時に再実行される。`mutate` の副作用は渡されたtransactionへのDB writeだけに限定し、LLM・Calendar・家電・通知などを直接呼ばない。外部操作はoutbox/jobを保存して後続workerが処理する。[公式transaction仕様](https://firebase.google.com/docs/firestore/manage-data/transactions)、[Python transaction API](https://docs.cloud.google.com/python/docs/reference/firestore/latest/google.cloud.firestore_v1.transaction)

commitの応答喪失は503となり得る。その場合も同じkeyで再送し、永続記録でcommit済みかを判断する。記録と業務writeのどちらか片方だけを成功としない。SDKの競合retryは5回までで、枯渇や通信障害は本文を含まない503へ変換する。SDK clientはcomposition rootから明示的に注入する。module importやhealth起動時にADCや本番接続を開始しない。同期SDK呼出しはASGIのevent loop外で実行する。

`expires_at` はFirestore timestampであり、TTL削除が遅れてもread時の期限検査で再送保証を終える。TTL policyの配備と認可・削除時の物理purgeはBA-OPS-02・後続IAM/削除workerの対象。このticketは本番データやTTL設定を変更しない。

## 版とページング

`etag(version)` は `"3"` の形式を返す。`require_if_match` は欠落428、契約外のweak ETag・wildcard・複数値等を400として拒否する。`compare_and_write` は同じtransaction内で既存versionを読み、古い版は412、成功時はversionを1増やして完全置換、`replacement=None` なら削除する。現在のtenant配下以外を拒否する。個別のフィールド権限・状態遷移・対象ownerの判定は呼出元の認可に委ねる。`Idempotency.execute` のmutation callback内に組み込めば、再送時に版更新を重ねない。

cursorのissue/readは毎回現在の認可callbackを必須にし、上記scope bindingと正規化queryのhashへ束縛する。queryにはoperation、対象親ID、filter、sort、limitを含める。HMAC-SHA256で改ざんを防止し、署名検査・束縛不一致・期限切れは400、現在の認可拒否は認可エラーを返す。既定寿命1時間、上限24時間、wire上限2048文字。limitは既定20・1〜100である。

cursor用の32バイト以上のランダム秘密鍵は呼出元が安全に注入し、API replica間で共有する。既定鍵や環境変数からの暗黙取得はない。鍵の変更は古いcursorを失効し、最初のページから再取得する。署名は暗号化ではないため、positionに使うのはopaque resource IDや本文を含まない順序keyに限る。private本文、token、表示名を入れない。

## 検証と導入

`mise run test` はキーの境界・24時間・body正規化・retry/rollback・ETag・scope/owner/actor/tenant/認可版・session交代/期限延長・cursor改ざん/失効を検証する。`mise run test-emulator` は[既存のdemo設定](05-development-and-testing.md#52-authfirestore-emulatorの起動と初期化)を明示的に読み込んで実行する。新しい試験も同じdiscoveryへ統合済みで、対象は検証済み `demo-brainoid` の固定loopbackのみ、SDKには匿名credentialを明示してADCを使わない。

実emulatorでは、同時createの同結果への収束、同時のbody差替えの409、実commit後の応答だけを失う再送、現在DB認可の失効、T-012のcreator/admin版競合とmember拒否、失敗時の全write rollback、cursorの現在認可を試す。IAM callbackは合成fixture用の最小policyであり、後続IAMの実装完了を意味しない。

今回はroute/永続business schemaを変更しない追加処理である。将来接続するrouteはreceiptとbindingの形式を互換に保つ。運用開始後にhash形式・receiptを変更する際は、24時間保持中の記録を消して再実行を起こさず、旧形式を読める期間を確保する。切戻し時もcommit済み記録を削除しない。
