# 記憶の保存モデルとrepository

BA-MEM-01は `brainoid_memory` にEntity/Factの保存型と `MemoryRepository` を提供する。FR-006/T-009の保存境界であり、EntityのHTTP・候補検索・provider登録は[BA-MEM-02](entities-api.md)で接続した。FactのHTTP・訂正は[BA-MEM-03](memories-api.md)で接続した。toolルート、検索、embedding処理、forgetは後続チケットで接続する。既存のagent coreのMemory型はポート用の値として維持し、この変更で実storeへ接続しない。

## 1. 保存値と更新

`EntityInput` は種類・label・aliases・外部参照・scope、`FactInput` は主語・predicate・目的語またはscalar値・qualifiers・出典・有効期間・origin/statusを受け取る。型・文字数・数値範囲・目的語の厳密排他・privateとshared_displayの禁止・[predicate別許可リスト](03-data-and-authorization.md#41-entityとfact)を保存前に再検証する。boolを整数として扱わず、NaN/Infinity、nullの目的語、未知のqualifierも拒否する。

保存値は `Entity` / `Fact` で、UUID、tenant、所有UID、created_by、UTC時刻、version、deletion_epoch、schema_version、deleted、suppress_reuseを持つ。privateは所有者必須。初期保存はversion 1/epoch 0でサーバー時刻を付ける。出典は最大20件。BA-MEM-03ではuser_explicitのconfirmedとそのsuperseded/retracted履歴に限り空配列を許し、created_byとrecorded_atを直接入力の出典とする。imported/inferred/candidateには出典を必須とする。推測はcandidateから始め、confirmedにはできない。candidate/confirmedの取消はretractedへ遷移する。

保存形式の `owner_user_id` はsharedでnullでも必須キーとして保持し、Entity編集・Fact取消で欠落させない。Factの未指定のobject/valueやqualifierは省略し、明示null禁止の入力規則を維持する。HTTPで任意のownerを省略する処理と、保存recordの必須キー保持は別の規則とする。

`created_by` は現在の認可で確定した作成責任者のUIDとする。本人のPiセッション経由でも同じUIDを保存し、監査用の端末IDは `created_via_device_id` に分けて保持する。これにより共通policyの共有情報の編集条件が本人のスマホ・Piで一致し、無関係なmemberへ編集権限を与えない。両フィールドはserverが設定し、更新patchでは変更できない。

| 操作 | 契約 |
| --- | --- |
| `create_entity(context, id, values)` / `create_fact(context, id, values)` | server発行UUIDに新規作成。同じlabelは独立。既存documentやtombstoneを上書きしない |
| `get_entity(context, id)` / `get_fact(context, id)` | 現在の権限で取得。factは参照entityと出典も再確認 |
| `get_fact(..., for_model=True)` | confirmed・現在の有効期間のみ。元出典/親/参照entity/factの削除・再利用抑止・epochを確認 |
| `update_entity(..., patch, expected_version, expected_deletion_epoch)` | label/aliasesのみ更新。版とepochの両方が一致した場合だけversionを1増やす |
| `update_fact(..., status="retracted", expected_version, expected_deletion_epoch)` | candidate/confirmedを取り消す。現在の依存先読取権限も必要。モデル利用は伴わないのでsuppress_reuseでも取消可能 |

fact本文・出典・visibilityの上書きやconfirmedへの昇格、supersededへの単独遷移は提供しない。訂正はBA-MEM-03のFactTransaction.prepare/saveで旧factのsuperseded化と新factの作成を原子的に実装した。表示・共有範囲を変更するAPIも今回の対象外。

## 2. 現在の認可と出典

全メソッドに `TenantContext` が必須で、任意パスを受け取らない。パスは検証したtenant UUIDとresource UUIDからのみ生成する。`CurrentAuthorizer`、`SourceResolver`、独立 `ControlLedger` はサーバー構成時に必須注入する。`FirestoreTransactions` は既存の `brainoid_persistence` のadapterを使う。memoryパッケージはAPI/framework/coreへ依存しない。

`CurrentAuthorizer` はcontextとは独立に保持した認証済み主体から現在のcredentialsを検査し、Subjectと有効区間 `[valid_from, valid_to)` を持つ `VerifiedCaller` を返す。有効区間は検証済みtokenの発行/期限と現在の委譲期限の共通部分で、期限を証明できなければ拒否する。deviceはcredential family・generationを現在状態で確認し、family等に期限があればvalid_toへ反映する。contextのactorや申告expiryをコピーして認可済みと扱うadapterは禁止する。repositoryも現在のtenant、membership、account、device/session、台帳を検査し、contextのscope/認可版を証明として信用しない。worker専用contextは直接受け付けず、後続adapterが正本jobから認証済みinitiatorのcontextとSubjectを解決する。workerの場合も検証済みのworker資格情報・job/leaseの期限・引き続き必要なinitiatorのcredentials/委譲期限を共通部分へ含め、失効したtokenを推測で延長しない。

`allow_untracked_bootstrap=True` は既存の未台帳追跡fixture/bootstrap移行専用で、既定はFalse。追跡済みの権限変更やpending denyを回避しない。Firebase UIDはopaque値として等価検索し、slashを含んでもパスへ埋め込まない。admin/ownerでも他人のprivateを取得できない。Piは現在のdisplay_policyも検査する。MEM-02では操作対象のapplianceにentity allowlist、roomにroom allowlistを適用し、person/activity/topicにはscope・所有者・表示方針を適用する。

`SourceRef` はkind・resource UUID・versionだけを持ち、パスを指定できない。`CanonicalSources` はnoteをtenant配下から取得する。messageには、同transactionの正本から親会話UUIDを一意に解決する `MessageLocator` が必要で、locatorはパスを返せない。repository側で出典ID/tenant/version/deleted/epoch/suppress_reuseと親の現在権限を確認する。親が削除/抑止されたmessageも利用できない。message locator未設定・import未実装・存在不明・不正メタデータは拒否する。import用resolverを後続で追加する場合も同じ現在状態・正本読取の契約が必須。

shared factがprivate entityを参照することや、private出典からshared factを作ることは拒否する。private入力の明示共有には独立したshared出典を作る後続フローが必要。UI履歴取得とモデル再利用は別で、suppress_reuseは後者と新しいfact作成を禁止する。出典の版が変わった場合も古いfactの取得を拒否し、古い内容の再利用を防ぐ。

display_policyも元出典・参照entityから広げない。`never` の依存先からは `never` のfactだけ、`authenticated_session` の依存先からは `never` または `authenticated_session` だけを保存可能とする。取得時も同じ条件を検査する。phoneからPiへ送る後続adapterは、取得時のphone権限を転用せず送信直前にPi/sessionの現在権限・表示先を再確認する。今回の戻り値は表示先への転送許可証ではない。

認可・出典・参照entity・対象版の読取はすべて書込前に同一transactionで行う。競合リトライでは再認可する。callbackは読取とDB操作だけで、provider呼出しや外部台帳への書込を行わない。`PolicyDenied`、`VersionConflict`、`AlreadyExists` は本文のない例外で、EntityのHTTPへの変換とETag/冪等性はMEM-02 adapter、FactはMEM-03 adapterが担当する。保存失敗を成功通知に変換してはならない。

最後の業務読取の後、返却/書込を行う直前にI/Oなしで現在時刻を検査する。`VerifiedCaller` の有効区間、正本から再構築したprincipalのPiセッションの絶対/idle期限、モデル用factの有効期間を使い、終端と一致した時点で拒否する。元のcontextの申告セッション期限は使わない。transaction応答の待機中に期限が来る場合も、repositoryが本文を返す前に同じ検査を行う。書込自体が期限前に認可され、commit応答だけが期限後に届いた場合は、返却を拒否しても書込が既に永続化されている可能性がある。後続adapterはこの拒否をrollbackの証明にせず、現在の認可を取り直して同じID/版・冪等性記録から結果を照合する。

## 3. 検証と移行

unit/fakeではschema正負例、同名の照明/別人、2tenant同一ローカルID、UIDのslash、private/表示先/現認可/出典の境界、版競合、再試行中の抑止・失効、commit失敗時の非保存を検証する。fakeはcopy-on-commitとread-before-writeを強制する。Firestore emulatorでは同名とtenant分離、並行更新の一方だけのcommit、出典のscope/版/抑止、tombstoneとmembership失効を検証する。

```sh
mise run setup
mise run check
mise run test
set -a
source .env.emulator.example
set +a
mise run test-emulator
```

新規パッケージの保存形式なので既存DBの自動移行はない。schema_version 1以外や必要メタデータが欠けた旧/不正recordは拒否し、推測で補わない。HTTP/toolの構造schemaと版は維持し、predicate別の業務制約をdescriptionで明示する。MEM-03では入力DTOから保存型への変換とこの検証を接続した。

削除の独立台帳・永続tombstone・物理削除・再生成抑止・restore適用はBA-MEM-06以降で実装する。本repositoryは現存するdeleted recordの再作成やepochのリセットを許さず、モデル利用/新fact生成では正のepochも保守的に拒否する。IDを再発行・再利用しないこと、物理削除後も削除fenceを維持することは後続の削除実装の責務。今回のrollbackは呼出adapterを接続しない状態へ戻し、保存済みrecordを保持する。新schemaのrecordを旧コードへ無検証で渡さない。
