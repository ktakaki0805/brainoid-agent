# 退出・本人アカウント・テナント削除

BA-IAM-10 / FR-013 / AC-013 / T-022・T-034。`POST /v1/tenants/{tenant_id}/operations` の `leave_tenant` と `delete_tenant`、`POST /v1/operations` の `delete_account` を提供する。忘却は[既存受付](memory-forgetting.md)を維持する。画面はBA-UI-12、利用者向け導線はBA-GOV-03で提供する。

全要求に `Idempotency-Key` と直近300秒以内の `auth_time` が必要で、token更新時刻では代用しない。account要求の本文は `{"kind":"delete_account","confirm":true}` のみで、UID指定を受けない。tenant削除は現在のownerと現在の名前に一致する `confirmation_name` を必要とする。tenant管理権限は他tenantや他人のglobal accountへの権限を与えない。最後の有効ownerの退出・account削除は409 `ownership_transfer_required`。先に所有権を移譲するかtenant削除を受け付ける必要がある。

## 受付と再送

本人全所属resolverはcanonical collection-groupからactive/revoked所属を調べ、各tenantの最後ownerを検査する。単一tenant resolverと分離し、共有内容や他人privateを本人データへ拡大しない。Firebase UIDはopaqueとして等価検索し、pathへ連結しない。profile未作成でも本人の全所属削除を受け付ける。workerはglobal usersもbounded show_missingで列挙し、canonical user_idが本人と一致する親だけを選択する。親がなく子が存在する枝やUIDのない親は推測せず、正規所属の消去を進めつつ `profile_parent_unknown` のrunningで保持し、identity provider最終処理を待つ。親も子もない通常の未provision本人は完了できる。重複profileは受付時に拒否する。

1. actor・keyからoperation IDを導出し、endpointと本文hashへ固定する。全対象tenantの `membership_mutation` を同transactionで予約する。tenant設定PATCHも各認可phaseで予約を検査して409とし、独立台帳I/O中と障害後の名前・版変更を防ぐ。account要求はglobal account fenceも予約し、新join・tenant作成・新owner移譲の対象化を拒否する。進行中の削除tenantは既存の永久tenant fenceを使う。
2. DB外の `DeletionLedger` へopaque subject UUID、tenant UUID、版・epoch、要求hash・時刻と固定scopeを先行記録する。UID、プロフィール、本文、credentialを含めない。既存forget JSONは新default fieldを出力せず互換性を保つ。
3. 所属失効は既存 `AuthzChanges` の固定denyを利用し、independent ControlLedgerのcommitted revokeとDB認可版を揃える。同処理でinvitation epochを一度だけ進め、既存招待を失効させる。途中失敗でもtenant予約を解除せず同要求でroll-forwardする。
4. 現在の予約・固定snapshot・所有権を再検査し、tombstone、operation、専用 `lifecycle_purge` waiting envelopeを同transactionで保存する。このcommit後だけ202を返す。300秒境界は全writeとtransaction応答releaseまで検査する。

台帳障害は503、別scope・別本文で同key再利用は409。同じ本人/key/bodyなら失効した所属やdeleting accountでも同じoperationを返す。新しいkeyで通常操作を再開する例外はない。本人statusはtenantまたはglobalの対応するoperation GETに限る。認証provider最終処理後のaccount statusは404（providerが認証を拒否する構成では401）になる。202は受付であり、物理消去や認証providerの削除完了を表さない。

## Fenceと消去範囲

global account fenceはプロフィール不存在のbootstrap判定より先に評価し、profile物理消去後も再利用を拒否する。tenant fenceは全principalを拒否する。member fenceは本人の所属、private参照、model、通常job、export判定へ適用する。export生成・取得は `exportable` と現在のscope認可を両方通す必要がある。

退出/accountの共有寄稿は本文を保持し、管理上のauthor IDも内容なしの出典識別として保持する。削除プロフィールから表示名を復元しない。sharedへの保存は独立した共有原本として扱い、本人のshared寄稿や他人privateを一括消去しない。本人privateの会話・記憶・entity・note/task/reminder、対応vector・run/job/tool receipt、private sessionと参照slot/receipt、本人通知・cache/exportコピー、private integrationの参照を選択する。accountはglobal profileとpush installation等の全子階層も消去する。

`entity_external_ids` はcanonical entityまたは同operationの永続選択証拠へ結合し、entityが先に消えても処理する。raw external ID・provider・integration参照と全子を消去し、hashを文書IDとする `entity_id`・`deletion_epoch`・`status: deleted` だけを再登録拒否のため保持する。他人private・shared・他tenantのindexは保持する。

tenant削除はglobalの `device_credential_families`、`device_pairings`、`device_installations`、`device_pairing_requests`、`device_pairing_selectors`、`device_refresh_tokens` も公式boundedページで走査する。直接のtenant bindingかcanonical pairing/familyへの結合で選択し、scope推測はしない。未confirmの再pairも既存installationが同tenantへ束縛済みなら対象とし、未割当の新規pairingは保持する。選択した全子、暗号化receipt、current/poll/code hash、期限・request本文hash等を除去する。familyにはtenant/device IDとrevoked、pairingにはtenant/device IDとexpired、refresh indexにはfamily IDとrevoked、create receiptにはpairing IDとexpired、installationにはtenant/device/original-device IDとrevokedだけを残す。selectorは消去する。hash文書IDは既存replay拒否・installationの他tenant再登録拒否に必要な照合キーとして保持し、秘密値の復元・再発行には利用しない。全対象のzero-change pass後だけ完了する。退出/accountはtenant共有deviceのglobal credentialを選択しない。

[MEM-08](memory-purge.md)の公式Firestoreページ取得・postorder walker・lease/dispatch guard・zero-change passを再利用する。tenant削除はghost親も含む全配下を対象とし、内容のないtenant shell、operation、purge job、復元fence、他の受理済みforgetのerasure provenanceを保持する。tenant rootの名前等も完了時に除去する。private scopeの所有者を証明できないghost枝はlive子を削除せず未完了にする。

pairing createは保存・同key receipt再送と同じtransactionで既存installationのtenant fenceを確認する。削除受付直後から、まだ消去前のreceipt再送と新keyでの秘密再生成を拒否する。別tenantのinstallationと未割当installationは利用できる。confirmは管理認可、exchange/refreshはdevice principalを同transactionで再確認する。IP/subject digestだけのrate-limit counterはtenant credentialではなく、不正試行抑制のため保持する。

privateの選択済みrootには永続 `resource_deletion_fences` と本文なしの選択証拠を保存する。古いdocumentを再配置しても同IDを再利用できない。退出後の再参加はpurge完了までは409。完了後は本文を持たないrevoked membershipをControlLedgerとの照合用に保持し、新epochの招待・新grantで参加できる。旧private IDのfenceは残る。旧purgeは後続membership認可版を検出すると新しい所属のデータを消去できない。account削除では永久account fenceを保持する。

## 構成と提供境界

APIのruntime提供は明示した `APP_ENV=local-emulator` と依存注入のfakeテストに限定する。staging/productionではこの受付repositoryを構成せず503を返す。実Firebaseユーザーの削除、実Cloud Storage、Secret Manager、実cache/export backendへの操作は実装・実行しない。

workerは `PurgeResource(PurgeJobs(...), pages, handlers=LifecycleHandlers(), hooks={"cache": ..., "export": ...}, account_finalizer=...)` と専用 `lifecycle_purge` queue/handlerで構成する。通常Jobsの認可は緩和しない。accountのjob/operationはopaque subject UUIDをanchorにした制御用tenant pathへ保存し、実tenant rootは作らない。回収対象anchorは受理済み `lifecycle_requests` の固定値から取得する。

cache/exportのadapterは明示必須。外部コピーが存在しない構成だけ `NoExternalCopies` を指定する。account finalizerは省略できず、全ローカル消去とcache/export receiptの後に同じ安定keyで呼び、失敗・応答喪失はrunningのまま再試行する。テストのfake finalizerは外部認証を失効させた証拠ではない。OAuth secretの実消去、外部通知・端末cache、backup復元のadapterは各後続チケットで実装し、そのbackendを有効にする前にscope fenceと完了receiptを接続する。

## 展開・復旧・検証

新collectionは追加のみで、既存forget intent/schemaを移行せず利用できる。schemaのない旧private子は安全な所有証拠がない限り未完了として調査する。予約、独立台帳、tombstone、resource fence、完了receiptをTTLやrollbackで除去しない。予約の手動解除で別keyへ進めず元要求をroll-forwardする。旧版へ戻す際は受付・API/private読取り・model/workerを閉じ、fence対応版へ戻すまで再開しない。復元は全writerを停止し最新台帳適用を終えてから開放する（BA-OPS-07）。

[単体・HTTP試験](../tests/test_deletion_requests.py)は300秒境界、別user/tenant、最後owner、複数所属、予約中join/create/移譲、台帳前後/DB応答喪失、同key、private/共有・全子階層、再招待、provider retry、旧forget JSONを検証する。[emulator試験](../tests/emulator/test_deletion_requests_integration.py)は実Firestoreのowner競合・opaque UID・受付commit応答喪失・公式paged子階層消去を検証する。実provider・実課金接続・デプロイは検証対象外である。

[producer回帰試験](../tests/test_deletion_inventory.py)は全3種の設定PATCH競合とledger/DB障害後再送、正規entity登録後のindex縮退・原本消去後の再開、正規pairing/refresh後の秘密消去・job再配送・全replay拒否・他tenant保持を検証する。emulatorでも設定予約の独立transactionとglobal credentialのshow_missing/postorderを検証する。
