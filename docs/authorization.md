# 現在状態による共通認可

BA-IAM-02（FR-002/006/012）は [policy package](../packages/policy/src/brainoid_policy/evaluator.py) と [transaction adapter](../services/api/src/brainoid_api/auth/authorization.py) に、検索前・リソース取得/変更・worker・出力直前の共通認可を実装する。[認可モデル](03-data-and-authorization.md#5-認可モデル)と[共通の再送処理](common-mutations.md)を前提にする。HTTP業務route、SSE/WSSへの組込み、device token検証、session発行は後続チケットで接続する。

## 信頼境界と呼出し

`UserSubject` は IAM-01 で検証したUIDだけを受け取る。APIでは `user_subject(SelectedTenantIdentity, now=...)` で有効な認証結果を変換し、選択tenantへの所属は別途検査する。`DeviceSubject` は認証済device ID、credentialのconnection generationと選択session IDを持つ。BA-IAM-07のcredential検証前にrequest/toolのdevice IDから生成してはならない。session ID単独は本人確認ではない。どちらもrole・認可済Context・LLM出力を引数に取らない。

`Evaluator` の全入口は、注入された `StateReader` から現在のtenant、membership、device、必要なprivate sessionを読み直す。readerは信頼された正本のread境界であり、callerが保存したContextやLLM/graphの内容を返す実装は契約違反である。型の生成・stamp一致だけでは認可にならない。APIの `transaction_policy(transaction)` は既存のFirestore SDK transactionと同じread集合を使う。同期SDK呼出しはASGI event loop外で行い、mutation callbackでは認可readを終えてからwriteする。

membershipは `user_id` の一致検索を同じtransactionで行い、2件まで読み、重複を拒否する。UIDに `/` が含まれていてもdocument pathに連結しない。既存fixtureの `memberships/{uid}` と将来の安全なdocument keyの両方を扱う。scope・status・所有者・版の欠落や不正値、複数membershipは許可に変換しない。

対象は `ResourceRef(tenant_id, kind, id)` で指定し、readerが正本のscope・owner・creator・version・deletion epochを取得する。対応kindはconversations/entities/facts/notes/tasks/remindersである。PATCH入力や検索索引のmetadataで正本を置き換えない。子message/run/音声は後続repositoryが正本の親へ解決し、親と子の版・削除状態を検査してから接続する。新規作成時の所有者のサーバー確定、scope変更や共有化の遷移制約、Calendar/家電固有の認可は各業務チケットの責務である。

## 許可と拒否

本人privateは本人だけが読書きできる。sharedの既存内容の更新・削除はcreatorまたはowner/adminが可能で、`POST` はconversationへの新規投稿に限定する。未確認Piにはshared + shared_displayだけを許可し、記憶/ノート等の書込は拒否する。本人Pi sessionではdeviceとsession双方のoperation scopeの積を使い、現在の本人membershipも必須とする。

operation scopeは `conversation`、`memory.read`、`memory.write`、`notes`、`tasks`、`reminders` を使う。deviceの `shared` は共有範囲の許可であり、各operation scopeの代わりにはならない。session作成のwire enumには `shared` を含めず、device側の共有許可を維持しながら選択operation scopeだけをsessionと積にする。既存demo device fixtureには具体的なread scopeと `authorization_version` を追記した。scopeの欠落を旧形式として全許可しない。userのコンテンツscopeはactive membershipから確定し、管理権限は別のrole検査で決める。

スマホではprivateの `never` も利用できる。Piへ渡すprivateは本人の現在sessionと `authenticated_session` が必須である。sharedのPi利用は[role表](03-data-and-authorization.md#51-actorcontext)の「同左＋本人private」に合わせ、本人session中も `shared_display` に限定する。resourceにroom/entityの関連があればdevice allowlistも検査し、出力先にはdisplayまたはaudio_output capabilityが必要になる。

`management` はコンテンツgrantを返さない。adminの自己変更、owner付与、既存owner変更、owner移譲、tenant削除を拒否する。ownerからのglobal Firebase account操作も常に拒否し、アカウント削除は本人再認証の別経路に限定する。genericなrole変更/所属解除ではownerを対象にできない。owner移譲は別operationとし、BA-IAM-06の実mutationは最後のowner維持、再認証・監査・台帳手順を別途満たす。`management` の成功だけではこれらの手順を完了した意味にはならない。

別tenant、他人private、失効した所属、存在しないresourceは本文やIDを含まない同一の `resource_not_found` へ変換する。可視resourceに対する編集権不足と管理操作の拒否は `operation_forbidden` である。API側の `policy_call` が既存の固定エラーへ変換し、policy例外に対象や本文を格納しない。

## 検索・出力・worker

`search` はsharedと本人privateを別々の `SearchFilter` として返す。tenant、visibility、private owner、display条件を検索前に適用し、room/entity制限も適用する。出力先を指定した検索はactorと出力先双方の条件の積を使う。表示先に許可されない候補・件数をモデルやtraceへ流さない。filterだけでresourceを確定せず、候補の正本を `resource(..., action=MODEL_READ)` で再検査する。削除、版、epoch、再利用抑止、scopeを索引の古いmetadataから判定しない。

出力直前は `resource(..., output=...)` でactorと出力先の現在状態を再読取する。privateの転送先は同一本人のスマホまたは本人Pi sessionである。保存済grantを再利用する場合は `previous` を渡し、principal・対象版/owner/scope/epoch・出力先のいずれかの変化で拒否する。

`worker(saved_grant, output=...)` はIAMで認証したworkerから、正本のjobに保存したinitiator/対象grantを再検査する入口である。配送payloadやLLMが指定したinitiatorを信用してはならない。worker自体を全tenant/privateの利用者として扱わず、元のuser/device/sessionの現在権限で再検査する。外部副作用の前、結果writeのtransaction内で再度呼ぶ。`deleted` metadataの確定と `suppress_reuse` の投影は後続削除repositoryの責務で、削除台帳やtombstoneを無視してこのadapterだけを削除実装の代わりにしてはならない。

## 版と再利用

`TenantContext.actor.authorization_version` はDBのmembership/device認可版をそのまま保持する。`Principal.stamp` はtenant policy版・deletion epoch・membership role/版・device generation/許可scope・session identity/版/許可scopeを合わせた内部fingerprintであり、tokenの版や新しい権限の証明ではない。

private sessionの絶対期限・idle期限は毎回現在時刻で検査する。内部session metadataには独立の `authorization_version` と、束縛先deviceの `connection_generation` を要求する。session発行側はこれらを正本へ保存し、再ペアリング/切替/権限変更で旧sessionを失効させる。期限値はstampに含めず、同じsessionの正常なidle延長でcursor/replayを失効させない。session交代、主体交代、権限変更は別のstampになる。

現在の `ResourceGrant` から `authorized_scope` を作ると、FND-08のbindingに `authorization_stamp` が入る。出力先もある場合はそのstampも束縛する。これによりuser認可版が同じでもtenant policy版やPi側の変更で古い再送/cursorを拒否する。stamp未指定の従来呼出しは従来のbindingを維持する。保存したgrantの変換だけで済ませず、再送/cursor callbackは毎回現在のtransactionからpolicyを呼び出す。

## 検証と導入範囲

`mise run test` の [policy matrix](../tests/test_policy.py) がT-001/002/020/029/033/034、正常read/write/投稿、display方針、scope不足、room、membership減権限/失効、device/session失効、版と期限の境界を検証する。`mise run test-emulator` の [integration](../tests/emulator/test_policy_integration.py) は固定2tenant fixtureと公式SDKで、同じtransaction readerを使った隔離・検索・現在状態の変更・再送/cursor・worker出力・opaque UID/重複を検証する。稼働中SSE/WSSへの反映はBA-RUN-05/DEV-03、session発行はIAM-09、独立台帳の先行deny/確定grantはIAM-05で接続する。

追加の外部依存はない。policy memberは既存のcontractsと固定済みPydanticだけに依存し、Firebase/FastAPI/SDKをimportしない。内部APIの追加で既存HTTP契約の変更はない。導入時は既存device documentへoperation scopeと認可版を明示してから利用し、欠落時はfail closedになる。切戻し時は権限を緩和せず、policyへ接続した業務入口を停止する。独立認可台帳実装前に復元安全性を達成済みとは扱わない。
