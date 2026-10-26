# ノートの API と会話 tool

BA-APP-01 / FR-007 / AC-007 は [共通 Notes サービス](../packages/memory/src/brainoid_memory/notes.py) を HTTP と実行中の会話 tool から利用する。HTTP/Firebase SDK を共通サービスへ持ち込まず、既存の transaction・現在認可・独立削除台帳を使う。local fake と Auth/Firestore emulator で検証し、UI・外部文書 import・高度な全文検索は含めない。

## HTTP と保存内容

`GET/POST /v1/tenants/{tenant_id}/notes`、`GET/PATCH /v1/tenants/{tenant_id}/notes/{note_id}` を提供する。作成・編集は Idempotency-Key、編集は If-Match が必要。成功応答は現在の Note と ETag、古い期待版は412。冪等記録は24時間の参照だけを保持し、再送時にも現在の権限・正本を読む。後続更新後は現在の正本を返す。削除された参照を冪等再送で作り直さない。

一覧は query（HTTP最大200文字、tool最大2000文字）による title/body の大文字小文字を区別しない部分一致。ID順、HTTPは既定20・最大100件、署名cursorをquery・limit・現在認可と可視な版集合へ束縛する。ページは常に items と next_cursor を持つ。末尾の next_cursor は null。既存 NotePage 契約どおり各itemにbodyを含め、toolは既定10・最大20件に加えて結果JSONを48KiB以内に制限する。全件数や不可視privateの存在は返さない。

現実装の読取はtenantのnote集合を走査し、各正本を現在認可する。上記は応答の件数・byte制限であり、DB読取量の上限ではない。認可条件付きの有界queryとcursorの設計は将来の検索最適化として残す。

保存先は `tenants/{t}/notes/{id}`。title/body/visibility/display_policyに、tenant/id/version、schema_version、deletion_epoch、deleted/suppress_reuse、作成・更新時刻、created_by/updated_byと検証済み端末IDを記録する。privateだけowner_user_idを持つ。owner/creator/visibilityはPATCHで変更できない。privateは本人だけ、sharedの編集・削除は作成者または現在owner/adminだけに許可する。共有Piの書込には本人の有効なnotes scope付きprivate sessionが必要。独立認可台帳の障害は503、確定したprivate拒否は404に分ける。

ノートは明示された利用者本文を保存するsource。source_refsは空で、モデルが推測した出典や任意の出典IDを受け取らない。APIとtoolで本文・所有者・actor・出典metadataの形式を共通にする。Factから参照する際はCanonicalSourcesが現在のnote版、scope、削除・再利用抑止を再検査する。note更新は版を進めるため、旧版に基づくFactやtool結果を新しいmodel入力へ流用できない。exportは既存の公開note投影とmanifestへ含め、actor/effectの内部記録は出力しない。

## 会話の明示操作

既定RunTurnは notes.create/list/update/delete を型付きExecutorへ登録する。明示注入した既存business adapterが同名toolを登録済みなら、そのadapterとauthorizerが担当する。既定ノートtoolは現在run/lease/grant/agent allowlist、本人・出力先、正本対象と期待版を検査する。作成・編集・削除では正本のdirect user messageが次の書式と全文一致する必要がある。

- 作成: `Save shared note 買い物: 牛乳` または `sharedノートを保存 買い物: 牛乳`。privateの場合はsharedをprivateへ置換する。title/bodyはこの本文と一致させ、visibility/display_policyは会話scopeと一致させる。
- 編集: `Update note UUID: {"body": "牛乳とパン"}` または `ノート UUID を更新: {"body": "牛乳とパン"}`。changesはキーを辞書順に並べ、Unicodeをescapeせず、通常のJSON区切り（colon/comma後に空白）で表す。
- 削除: `Delete note UUID` または `ノート UUID を削除`。
- 一覧・検索: notes.listのquery/limit。会話の読者・表示先へ公開できる現在noteだけを返す。

不一致、否定、別対象、取得文書内の命令、派生したuser messageは権限拒否となり、保存・削除しない。自然文から自由に要約して保存する経路は未提供。保存内容と個人/共有を上の明示本文で確認できる。モデルが検索memoryから本文を転記する場合も、その転記が正本利用者本文そのものとして明示されていなければ拒否する。

note_effectsには固定effect ID・要求・確定後の正本を同じtransactionで結ぶ。完了・再送・reconcileは現在の正本と証拠が一致する場合だけ結果を利用できる。notes.listも結果保存後に全noteを再検査する。別runの履歴へ結果を再利用する際は現在の読者・会話全体のaudience、現在版、削除・suppress_reuseを検査する。started/unknownを消して再実行しない。reconcileは確定証拠の読取だけを行う。

notes.updateは保存後、固定通知「ノートを更新しました。」でrunを完了する。一覧→編集の同一runでは、更新transactionまで他noteを含む一覧とnote出典のMemoryRecordsを通常どおり検査する。更新と同じtransactionで消費済み入力・continuationを退役し、消去用の内容なし依存証拠とrun/call/effectの結合を保持する。その後はモデルを呼ばず、当該effectと現在Note・権限・lease・期限から通知を再認可する。外部編集や他runの証拠を旧snapshotの再利用許可には使わない。応答を失っても同じrunの再配信は確定証拠を読み、再更新しない。応答時に権限や期限が失効した場合、書込が確定済みでも成功通知は返せない。

## 削除と復旧

直接DELETE endpointは追加しない。既存 `POST /v1/tenants/{t}/operations` の `kind=delete_resource, target_type=note, target_id, expected_version`、またはnotes.deleteを使う。対象権限と版を確認し、source予約、独立DeletionIntentの先行append、再認可後のtombstone/operation/purge jobを順に確定して202を返す。台帳/DB失敗は同じkeyで回復し、accepted operationは永久の重複防止証拠として保持する。復元後の再受付でもID・created_at・immutable intentは同一、DB再commit時のupdated_atは進み得る。旧forget/lifecycleのimmutable JSON表現は変更しない。

受理と同じtransactionでtenant purge_epochを進め、以降のlist/get/model再利用をfenceする。toolからの削除は古いMemoryRecords・tool_bindings・continuationを消去用の内容なし証拠へ変換し、modelの再呼出しをせず固定の受付通知で終了する。削除後に既存履歴の現在性を証明できないrunは失敗する。

既存memory_purgeの専用Jobsは受理済みexact note意図だけを権限とする。shared editorの権限を他人private Factの読取権へ拡大しない。note sourceを持つFactの消去選択は正本source_refsとaccepted target版に限定し、operation配下のpurge_factsへ内容なし証拠を残す。既存Fact忘却が所有するtombstoneを上書きしない。これによりFact本体消去後もvector・job・履歴・結果と子階層を処理できる。派生依存を失った古い記録や説明できない子階層は成功扱いせず、既存の再試行理由を保持する。利用者が別に発言した本文は会話sourceとして独立して保持する。直接user発言だけのsummaryも独立したsourceを参照する。

include_derived_memoriesの既定trueではnote由来Factとvectorも物理消去する。falseはこれらの正本・索引を残す選択であり、削除したnoteを参照するFact/索引のmodel再利用を許す意味ではない。どちらも派生した会話・tool結果の再利用停止と消去、export失効を行う。他のtarget_typeは今回未提供として拒否する。

既存forget_memoryも、受理したtargets内のnote出典を同じ派生コピー判定に渡す。delete_sourceではnote原本とnote_effectsも消去し、suppress_reuseでは原本とその確定記録を保持する。両policyでnote結果から生成されたrun・tool・assistant履歴は消去し、新規exportへ再収集させない。この処理で別Factの原本消去権限を追加せず、既存forgetのFact選択は先頭の受理対象に限定する。独立したdirect user発言や無関係noteは残す。

note削除も既存exportのpurge_epoch失効、生成待機、独立artifact registryを読む消去hook/reaperへ接続する。期限切れ・失効後の遅いputを既存object tombstoneが拒否する。tenant/account/member削除は新しいnote_effectsのprivateコピーも処理する。新規resourceの導入時には同じproducer/manifest/purge inventory確認が必要。

## 検証と戻し方

[HTTP/tool/fake試験](../tests/test_notes.py)、[producer回帰試験](../tests/test_note_regressions.py) と [Firestore試験](../tests/emulator/test_notes_integration.py) で、公開schema、opaque UID、private/shared、ETag/冪等性、明示意図、自己source削除、結果/履歴失効、Fact忘却との重複、失敗回復、新規export、2名の認可済み編集者による競合を検査する。既存forget/lifecycle/exportと通常Jobsの回帰、mise run setup/check/test/test-emulatorも実行する。

Firestoreの共通adapterは、transaction内のSDK読取が確定したAbortedを返した場合、rollback後に現在認可を含むcallbackを新transactionで再実行する。SDK自身のcommit競合再試行を含め総callback回数は従来の5回以内。commit応答喪失や一般の一時障害は自動再実行の対象にせず、従来どおり同じ論理IDの回復へ渡す。[境界試験](../tests/test_firestore_transactions.py) と [private session試験](../tests/emulator/test_private_sessions_integration.py) で、読取面・混合試行上限・応答不明・同activity再送時の期限不変を検査する。

新規noteは追加形式であり既存データの一括移行はない。旧実装へ戻す前に新しいnote admissionとnotes toolを止め、pending/unknownのoperation/jobをこの版の専用purge workerで完了させる。旧workerは新kindを処理できないため、accepted削除が残った状態で置き換えない。source予約、immutable台帳、tombstone、operation、effect/冪等記録を消さない。外部provider・課金・本番配備設定は変更しない。
