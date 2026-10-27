# 構築・リリース・運用手順書

| 項目 | 内容 |
| --- | --- |
| 状態 | 初版の設計ベースライン案。クラウド構築・配布・運用は未実施 |
| 版 | 0.2.0 |
| 更新日 | 2026-10-25（日本時間） |
| 責任者 | Platform担当。Device担当・Security担当・リリース責任者と共同管理 |
| 対象 | 公式ホスティング運営者と、OSSを自分のGCP環境へ導入する開発者 |

本書は、初回構築から更新・復旧までの手順と合格条件を定める。[PR検証workflow](../.github/workflows/check.yml) は提供済み。Terraform、署名build・deployのCI、Pi installerはこれから実装する。実際のproject ID、URL、連絡先、署名鍵、artifact digestは環境ごとの運用台帳で管理し、この文書へ秘密値を記入しない。

関連文書: [要件定義](01-requirements.md)、[基本設計](02-architecture.md)、[データ・権限](03-data-and-authorization.md)、[インターフェース](04-interfaces.md)、[開発・テスト](05-development-and-testing.md)、[SECURITY](../SECURITY.md)。

## 1. 配置と環境分離

`development`、`staging`、`production`は別GCP projectとし、Firebase設定、OAuth client、Secret、Cloud Tasks queue、Firestore、制御用Cloud Storage bucketを分ける。個人の開発はfake/emulatorを既定とする。stagingにproductionの会話・音声・記憶をコピーしない。

初期リージョン候補は `asia-northeast1`。実際に使うmodel、STT、TTS、Firestore、Cloud Run、Tasksの提供場所・処理場所を確認して確定する。Firestoreは作成後にlocationを変更できないため、DB作成前の確認項目とする。DBの保存場所だけで外部モデルの処理場所まで決まったと扱わない。[Firestore locations](https://firebase.google.com/docs/firestore/locations)

| 資源 | 用途と分離方針 |
| --- | --- |
| Cloud Run API | user/deviceの入口。TLS、アプリ認証、tenant認可、streaming |
| Cloud Run Worker | 非同期処理・dispatcher。IAM認証を必須にし、一般公開しない |
| Firestore Standard | 業務状態、graph記憶、outbox、job、command、operation |
| Cloud Tasks | worker配送。会話に直接必要な処理と大量の保守処理でqueueを分ける |
| Cloud Scheduler | reminder期限確認、outbox回収、削除job・停滞jobの点検 |
| Secret Manager | OAuth秘密値、署名鍵、連携credential。用途ごとに閲覧権限を限定 |
| Cloud Storage制御bucket | 復元時に巻き戻さない削除・認可変更台帳。通常データと権限を分け、署名済みPi配布物も別bucketで扱う |
| Artifact Registry | Cloud Runのimmutable image。digestでreleaseを特定 |
| Logging/Monitoring | 構造化log、metrics、alert、releaseと障害の追跡 |

原音声は自システムでは通常保存しない。音声処理、OAuth、pushなど外部provider固有の保持条件は公開前に確認し、利用者向け説明へ反映する。

## 2. 身元と権限の割当て

実行用service account、queue呼出し用、deploy用、復旧用を分ける。projectのOwner/Editorを通常のruntimeへ付与しない。具体的なIAM roleとresource bindingはTerraformで管理し、定義したアクセスが拒否・許可されることをstagingで確認する。

| Principal | 必要なアクセス | 付与しないアクセス |
| --- | --- | --- |
| API runtime | 業務DB、必要なmodel/speech、指定queueへのenqueue、必要Secret、削除・認可変更台帳の追記 | IAM管理、無関係なSecret、backup破棄、任意bucket管理 |
| Worker runtime | jobが扱うDB、必要model/API、指定Secret、削除台帳参照・確認 | IAM管理、公開配布物への任意署名、利用者権限の自由な変更 |
| Task invoker | 対象WorkerのCloud Run Invoker | DB、Secret、APIの管理操作 |
| Scheduler invoker | 対象Worker呼出し。route側でも許可する処理を制限 | 任意user/device APIの代理利用 |
| CI deploy | 所定image登録、対象serviceのdeploy、承認済みTerraform適用 | 利用者の会話・記憶の通常閲覧 |
| Restore operator | 指定backup、隔離DB、削除台帳、復旧用設定 | 常時の本番書込。必要時間だけ権限を有効化 |
| Pi | 専用device APIと割当て済みLAN連携 | Firestore、GCP service account、他deviceの資格情報 |

Cloud Tasksからprivate Cloud Runへの呼出しには、呼出し用service accountと対象serviceのInvoker権限を設定する。Task生成側にはqueueへのenqueueと、利用する呼出しidentityに必要な権限を限定して付与する。[公式手順](https://docs.cloud.google.com/run/docs/triggering/using-tasks)

APIはスマホ・Piから到達できる入口を持つが、アプリケーションがFirebase user tokenまたはdevice tokenを検証する。WorkerのIAM認証とは混同しない。MVPではクライアントからFirestoreへ直接接続させず、Security Rulesも不要なクライアントアクセスを閉じる。サーバー経由のテナント分離はAPI/Workerの認可コードで保証する。[サーバークライアントの認証](https://firebase.google.com/docs/firestore/security/rules-conditions)

CIはGitHub OIDCからGCPの短命credentialを取得する。信頼条件をrepository、許可branch/tag、environmentへ限定し、実際に発行されるclaimで検証する。長期service account keyをGitHub secretへ保存する構成を既定にしない。[公式ガイド](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-google-cloud-platform)

## 3. 初回構築の手順

以下はPlatform担当が実装・実行する順序である。各段階の記録をrelease台帳へ残す。

1. **前提を確定する。** 正式project、責任者、課金管理者、region、model、対応実機、ドメイン、OAuth redirect URI、通知設定、公開ライセンスを確定する。リージョン未確定のままFirestoreを作成しない。
2. **Terraform管理基盤を作る。** 環境別state、stateへの限定アクセス、変更レビュー、state復旧方法を用意する。既存資源はimportと差分確認を行い、共有資源の無関係な削除を避ける。
3. **IAM・API・Secretを用意する。** 必要APIを有効化し、runtime/invoker/deploy identityを作成する。Secretの値は手動または専用フローで投入し、Terraform plan・state・CI logへ平文を残さない。
4. **保存先を作る。** Firestore、必要索引、client access制限、daily backup、削除台帳bucketと配布bucket、lifecycle policyを設定する。公開前の空DBで権限と削除を検証する。
5. **配送先を作る。** private Workerを配備し、Tasks queue、最大dispatch/並列数、retry範囲、Schedulerの1分周期の期限確認を設定する。停止中jobの回収条件も設定する。
6. **APIを配備する。** 認証・認可、許可origin、request上限、stream timeout、instance上限を設定する。生存確認は `GET /health/live`、依存先確認は `GET /health/ready` とする。BA-FND-02はhealthのみ提供し、DB等の実adapterとprobeは後続チケットで登録する。ready=200だけでは未実装の外部接続を確認したことにならない。起動コマンド・設定は[開発ガイド](05-development-and-testing.md#51-api基盤の起動)を参照する。
7. **外部連携を設定する。** Firebaseログイン、Calendar OAuth、FCM/APNs、モデル・音声、Home Assistantテスト環境を結合する。許可scopeとテスト用アカウントを照合する。
8. **検証用tenantとPiを登録する。** 別tenant、private/shared、失効端末のfixtureを使い、スマホからPiまでの縦断シナリオを実行する。
9. **監視と復旧を実証する。** alertが担当者へ届くこと、backupから隔離復元できること、削除台帳の再適用、API/Piのrollbackを確認する。
10. **公開gateを満たす。** 第11節の条件と未決事項を確認し、release候補のdigest・schema・model設定・検証結果を記録して配布する。

初期配置はAPIとWorkerの2サービスとする。新しいサービスを増やす場合は、権限・負荷・障害分離のどの問題を解くかを[基本設計](02-architecture.md)の判断記録へ追加する。

会話runはAPIがrunとjobを永続化して受付202を返し、Cloud Tasks経由のWorkerでAgentCoreを動かす。API応答後のbackground threadに処理を委ねない。Workerは差分を250ms目安のbatchと状態としてFirestoreへ保存し、APIがSSE/WSSで配送する。毎tokenのDB書込は行わず、cancel、lease、fencing、session失効を各処理境界で確認する。interactive run queueをembeddingや大量削除のqueueから分け、待ち時間とFirestore操作数を監視する。

## 4. クラウドリリースと互換性

releaseにはapplication version、Git commit、container digest、API/schema version、device protocol version、model/prompt/tool version、Terraform plan、検証結果を紐付ける。同じtagのimageを上書きしない。

### 4.1 通常の展開

1. 信頼されたmainからimageをbuildし、stagingへ同じdigestを配備する。
2. schema変更は追加から始める。旧client/旧device/旧workerが読み書きできる状態で索引・新fieldを用意する。
3. 認可、忘却、外部操作、再接続の回帰と実機試験を実行する。
4. 旧schemaと新schemaを読めるWorkerを先行配備し、その後APIを段階展開する。互換性上この順序が成立しない変更は専用手順を用意する。
5. APIは初期案として5%→25%→100%へ段階展開し、各段階でerror、遅延、再接続、未知のcommand結果を確認する。流量が少ないときは時間だけで合格にせずテスト用要求を実行する。
6. 旧fieldや旧protocolの削除は対応端末の更新率、復旧可能期間、旧版の稼働終了を確認した別releaseで行う。

Firestoreのindex作成や大量backfillを同期API request内で行わない。backfillはcheckpoint・上限・pause・再実行を持ち、tenantとdeletion epochを維持する。停止した旧workerが後から結果を書き戻さないようversionとleaseを確認する。

### 4.2 常時接続の扱い

Cloud RunのWebSocketはrequest timeoutの対象で、現在の上限は60分。開いた接続があるinstanceはactiveとして課金される。新しい接続が同じinstanceへ戻る保証にも依存できない。設計上は55分前後にjitter付きで再接続を開始し、durable command/run stateを再取得する。[Cloud Run WebSockets](https://docs.cloud.google.com/run/docs/triggering/websockets)

rollout時の新規接続比率と実際の接続台数は別に監視する。停止予定のrevisionは新しい長時間処理を受けず、旧接続へ再接続を促す。切断はcommandの失敗・取消・成功を意味しない。プロセス内の接続表だけで操作結果を管理しない。

家電操作はPiが実行直前にclaimし、serverが現在の状態・期限・generation・認可を確認したcommand_start/grantを受けてから開始する。grant発行後は単なる配送待ちへ戻さず、取消を保証しない状態として扱う。grantの応答喪失、Pi再起動、結果ack喪失ではexecution ID、永続状態、Home Assistantの現在状態を照合し、未確認のまま新しい実行として再送しない。

### 4.3 切戻し

API/Workerは直前の検証済みdigestと設定へ戻す。先に現在のDB/schemaを旧版が扱えるかを確認する。認可・削除epoch・command重複排除を理解できない旧版への切戻しは行わず、該当機能を停止して修正版へ進める。データを伴うrollbackでbackup全復元を通常の手段にしない。

model/prompt変更もreleaseと同じ単位で扱い、固定評価セットと使用量を比較する。問題があれば直前の検証済み設定へ戻す。model名をコードに散在させず、provider側の終了・変更情報を定期確認する。

## 5. Piの導入・更新・回復

対応構成はRaspberry Pi 5 8GB、64bit OS、指定の電源・冷却・画面・マイク・スピーカーを組み合わせて実機確認する。OS image、kernel、ブラウザ、Python、音声設定、周辺機器型番を対応表に記録する。Piの性能や音声品質は検証前の保証値としない。

### 5.1 導入時の手順

BA-DEV-09の再実行可能な導入処理、未検証のPi OS候補、ローカル復旧画面、本人確認付きreset、アンインストールと実機gateは[Pi導入手順](device-setup.md)を参照する。

1. 公開された対応OSとdigestを確認して書込み、時刻同期・network・音声入出力を確認する。
2. 制限された専用userとsystemd serviceを用意する。device serviceのlocal APIはloopbackにbindし、Host/Origin/sessionを検証する。
3. Chromium kioskをdevice serviceが配信するUIへ向ける。クラウドdevice credentialをブラウザから読める保存先へ置かない。
4. device serviceを起動し、一回限り・期限付きcodeを表示する。スマホでtenantと権限を選んで登録する。
5. shared表示・テキスト・タップ音声・再起動復帰・Home Assistantの許可操作を確認する。
6. 導入台帳へhardware、OS、app/protocol version、登録時刻を保存する。秘密tokenやWi-Fi passwordは記録しない。

Home Assistantは利用者のLAN上のinstanceを最初の連携先とする。照明・空調の許可したentity/serviceだけを設定する。Piとの同居を選ぶ場合は、資源競合と更新・復旧を別に検証する。

### 5.2 アプリ更新

BA-DEV-08のローカル実装、manifest、起動方法、schema 2固定の切戻し制約と電源断手順は[Piアプリの署名更新と復旧](device-updates.md)を参照する。production root・署名公開pipeline・実機導入は別途必要である。

更新はAgentのtool実行とは独立したupdaterで行う。配布物はversion、hash、対応OS/architecture、要求protocol、metadata有効期限、署名を持たせ、出荷時に信頼したrootから検証する。署名鍵のrotation、古いmetadataの再送、期限切れ、破損fileを扱える既存の検証方式を採用する。TUFのmetadataと署名・hash・version・expiryの仕組みを初期方式の基準とする。[TUF概要](https://theupdateframework.io/docs/overview/)

配備先はversionごとに分け、検証と展開が成功してから起動先をatomicに切り替える。SQLite migrationは互換性を確認し、rollbackに必要な状態を保つ。再起動前に個人セッションを終了し、進行中の家電操作を結果不明のまま再実行しない。

BA-DEV-02の[保存層の移行・復旧手順](device-storage.md#移行と復旧)は、別の `DEVICE_STATE_DIR`、transaction migration、future版拒否、破損/欠落時の操作禁止を定める。command台帳を消して旧版を動かす切戻しや、稼働中のhot journal削除は行わない。

配布は開発端末→少数pilot→残りの順に行う。健康確認にはservice起動、local UI、SQLite読書き、クラウド認証、protocol互換、音声device列挙を含める。更新後に正常化しなければ直前の検証済みアプリへ戻し、再失敗では更新を停止する。正確な待機時間・回数は実機検証で固定する。

OS更新はアプリ更新と別releaseとする。初期の回復手段は検証済みOS imageの再書込みと再登録であり、OS全体の自動A/B rollbackが完成しているとは扱わない。長期間offlineの端末には、互換性確認前に古いcommandを送らない。

### 5.3 端末移譲・紛失・廃棄

紛失はサーバーでdeviceを失効させ、既存streamと個人sessionを終了する。オンライン失効の反映目標は60秒。Piは資格情報、Home Assistant token、private cache、音声bufferを消去して再登録待ちへ戻る。物理的に回収できないoffline端末の保存領域消去まで遠隔で保証しないため、private情報の通常の長期保存を行わない。

移譲時はクラウドの登録解除、端末のlocal reset、Home Assistant側のcredential失効、再起動後の未登録状態を確認する。新tenantへtokenや記憶cacheを引き継がない。

BA-DEV-02の[内部local reset](device-storage.md#local-reset)は、durable markerで資格情報取得を遮断してからcloud/HA file・私的一時データ・SQLite登録/cursor/台帳を消去する。途中失敗・再起動では全消去完了までmarkerを保持し、通信/操作を再開しない。確認UIはBA-DEV-09の[ローカル初期化](device-setup.md#本人確認と初期化)で接続する。実機での移譲検証は外部条件として未実施。

## 6. 監視と日常点検

初期目標は検証用の値であり、対外SLAではない。公開βの計測で閾値を校正する。

| 観測対象 | 指標 | 対応の目安 |
| --- | --- | --- |
| API | 成功率、5xx、p50/p95、認証/認可拒否、同時run | 急増時にrelease差分とproviderを確認 |
| 音声 | STT/LLM/TTS各遅延、無音率、停止後の再生 | text経路への切替と出力停止を確認 |
| 記憶 | query読取数、候補数、graph展開数、stale結果数 | 2hop/40fact/20entityの予算超過、権限filterを確認 |
| 会話run | queue待ち、lease競合、差分batch遅延、DB読書き | 全体の応答目標から閾値を割り当て、保守queueと分離して測定 |
| 保守非同期 | pending最古age、retry、failed、outbox未配送 | 滞留5分を初期注意値とし原因別に対処 |
| 操作 | command成功/失敗/expired/outcome_unknown、観測遅延 | 結果不明の割合上昇で新規実行を制限 |
| 端末 | online数、再接続率、version、disk/温度状態、失効反映 | 対応構成との違い、更新障害、資格情報を確認 |
| 削除 | 未完了age、台帳追記失敗、purge失敗、restore適用状況 | 追記失敗は即時、24時間超のpurgeは担当へ通知 |
| 費用 | model token、音声秒数、DB操作、接続時間、tenant別使用量 | 日次上限と月次予測、急増機能を確認 |

構造化logにはrequest/run/operation/commandの相関ID、release、状態、時間、error分類を残す。本文とtokenを残さない。高cardinalityなtenant/user IDを共通metricsのlabelへ無制限に載せず、必要なtenant別利用量はアクセス制限された集計へ分ける。

日次でqueue・削除・backup・費用、週次で失敗傾向・依存更新・端末version、release前にrestore/rollbackの変更点を点検する。初期の復元訓練は月1回とし、成功可否だけでなく所要時間と欠損範囲を記録する。

## 7. backup、忘却、復元

### 7.1 保持と目標

Firestoreはdaily backup、保持7日を初期案とする。初期復旧目標はRPO 24時間、RTO 4時間とし、訓練で実現性を確認する。これらは測定前の内部目標であり、region全体の障害などを含む保証ではない。より短いRPOが必要になった時点でPITR等を比較する。

Firestore backupは元DBと同じlocationに置かれ、復元先は新しいDBになる。TTL policyはbackupに含まれないため復元時に再設定する。元DBやbackup scheduleの削除だけで既存backupが削除されたと判断しない。[公式backup仕様](https://firebase.google.com/docs/firestore/backups)

### 7.2 削除台帳の先行記録

バックアップ内のtombstoneだけに頼ると、過去時点への復元で忘却要求が失われる。以下を削除APIの受付契約とする。

1. 現在のactor/tenant権限を確認し、対象・operation ID・epochを確定する。
2. 復元対象DBから独立した制御bucketへ、認可済みの削除意図をdurableに追記する。本文を含めず、opaqueな対象ID・操作ID・epoch・時刻を保存する。
3. Firestore transactionでtombstoneとpurge jobを保存する。この時点から通常query、LLM入力、表示、exportで対象を拒否する。
4. 両方の記録を確認して受付202を返す。台帳追記に失敗した場合は503で受理未完了とし、成功したふりをしない。
5. 台帳成功後にDBが失敗した場合は、同じoperation IDで台帳から再構築・再適用する。transaction内にCloud Storageの副作用を入れない。

台帳は初期30日保持とし、常に全復元可能backupの期間より長く保つ。backup保持を延ばす変更では台帳保持も先に見直す。APIは追記のみ、復旧専用identityは読取、台帳の期限管理は別の管理権限に分ける。台帳の消失・不整合時は復旧gateを開けない。

purgeは原本、会話messages、factの出典、要約、vector、job payload、cache、通知preview、連携credentialを対象別に処理する。提供済みforget workerの保存先inventory、専用queue、bounded pageと再開、cache/export hook、24時間超の監視状態および未提供サービスとの境界は[忘却の物理消去と再開](memory-purge.md)を参照する。削除対象の子コレクションを明示的に列挙・再帰処理する。Firestore documentの通常削除はsubcollectionを自動削除しない。[公式削除仕様](https://firebase.google.com/docs/firestore/manage-data/delete-data#delete_documents)

通常queryからの拒否はtombstone保存直後、primary/derivedのpurgeは24時間以内を設計目標とする。offline端末は再接続時に削除epochを同期するまで古いprivate cacheを利用しない。backupの残存期間とprovider側の保持は利用者向け説明で区別する。

### 7.3 認可変更を巻き戻さない台帳

membership/deviceの失効、role変更、owner移譲は、現在の認可versionと変更対象を独立した制御台帳へ記録する。保持は30日以上、かつ全復元可能backupより長くする。削除台帳と同じ制御bucketを使う場合もrecord種別と書込経路を分ける。個人の会話本文や秘密tokenを含めない。

失効・権限縮小・owner移譲で旧権限を止める操作は、危険な権限復活を防ぐためdenyの意図を先行記録する。権限付与・昇格を復元するには、DBへの適用を確認した確定recordと最新versionの照合を必要とする。競合で不成立になった変更や、確定を確認できない書込意図からgrantを復元しない。具体的なrecordと状態遷移は[データ・権限設計](03-data-and-authorization.md)を正本とする。

### 7.4 復元手順

1. Incident担当が影響と復旧点を決め、API書込、通知、tool実行、Scheduler/queue配送を停止する。最新の削除台帳と認可変更台帳を保全する。
2. backupから新しい隔離DBへ復元する。アプリと利用者から到達させない。
3. IAM、索引、TTL、必要設定を確認し、現在のapplication/schema互換性を検査する。
4. **現時点の全有効削除台帳**を読み、tombstone・purgeを再適用する。restore開始中に受付した削除があれば、最後に再照合して欠落をなくす。
5. 最新の認可変更台帳を再適用し、membershipの失効・role変更・owner移譲とdevice失効を照合する。最新状態を証明できないmembershipはすべてsuspendedとし、当該tenantへの対外APIを再開しない。device credential・個人sessionは失効させて再登録を要求する。人が再認証できたことだけを理由に、過去のmembershipやowner権限を復活させない。
6. command、Calendar、reminder、jobを点検する。期限切れは閉じ、外部副作用はproviderの現在状態と照合する。古いbackupに戻ったことを理由に再実行しない。
7. テナント分離、private表示拒否、忘却済みfixtureの非出現、認証、read/writeを隔離環境で確認する。
8. 新DBを指すrevisionを段階展開し、read→write→新しい外部操作→通知の順に再開する。復旧点、欠損、所要時間を記録する。

復元後の再開判断は「DBが読める」だけでは行わない。削除台帳と権限の整合、操作の重複防止が成立することを必要条件とする。

## 8. 代表的な障害の手順

| 事象 | 初動 | 調査・回復 | 再開条件 |
| --- | --- | --- | --- |
| LLM/STT/TTS停止 | 該当providerの新規retryを制限し、UIへ状態を返す | request数・quota・region・provider statusを確認。音声障害時はtext経路を利用 | stagingの短い試験で正常化。MVPでは未実装のlocal LLMへ切替しない |
| Pi接続が大量切断 | 接続数、直前release、timeoutを確認 | jitter再接続、generation、Firestore watchを確認。queueの古いcommandを送らない | 2回以上の再接続試験で重複操作なし |
| Home Assistant結果不明 | 当該操作をoutcome_unknownにし、盲目的な再送を止める | HAの現在状態とcommand ID、送信時刻を照合 | 観測結果を確定するか、利用者が新しい操作を明示 |
| queue滞留 | 処理種別、最古age、retry原因を分類 | provider/DB負荷を確認し、dispatchを調整。業務jobと保守jobを分ける | backlogとerrorが下がり、二重実行/削除復活なし |
| 削除台帳失敗 | 削除受付を成功にせず、即時alert | bucket IAM、quota、region、operation IDを照合。durable記録から再適用 | 台帳とtombstoneが一致し、purge再開可能 |
| テナント越境の疑い | 影響するAPI/検索/toolを停止し、証跡を保全 | 認可経路、cache、モデル入力、共有設定を調べる。秘密・sessionを失効 | 修正と再現テスト、影響範囲・通知方針をSecurity担当が確認 |
| credential漏えい | 対象鍵・tokenを失効し、関連session/queueを点検 | 発行経路と使用logを確認し、限定された権限で再発行 | 旧資格情報が使えないことを実試験 |
| Pi更新失敗 | rolloutを停止し、旧版へ戻す | 署名、対応OS、disk、SQLite migration、電源状況を確認 | 対応実機で更新・電源断・rollback試験が合格 |
| 課金・利用量急増 | 該当tenant/機能に新規要求上限を適用 | loop/retry、長時間socket、検索展開、音声時間、model変更を確認 | 原因除去と上限試験。データ読取・削除経路は必要に応じ維持 |

Cloud Tasksは重複実行が起こり得るため、queueの再開や再投入でexactly-onceを仮定しない。操作ID・version・lease・外部状態を確認する。[公式の制限](https://docs.cloud.google.com/tasks/docs/common-pitfalls)

インシデント記録には開始・検知・影響・封じ込め・回復時刻、影響tenantの範囲、release、原因、再発防止、担当を残す。個人情報を一般公開Issueへ転記しない。利用者への説明は確認できた影響に基づき、推測を確定事実として伝えない。

## 9. 費用と容量の管理

月額の一律見積りは、model・会話長・音声時間・同時接続・DB操作の計測前に固定しない。初期pilotは100 tenants、最大50 Pi接続、20 active conversationsを仮説として測定する。tenant/月、1会話、音声1分、Pi待機1日を別々に集計し、販売価格や無料枠の判断に使う。

アプリ側でtenant/user/device単位のrequest rate、同時run、model token予算、音声時間、graph展開、job retryを制限する。上限到達は429等の契約済みerrorで通知し、終わりのない再試行をしない。Cloud Runのinstance上限とqueueのdispatch上限も設定する。

GCPのalerts-only budgetは支出を停止させるものではない。対象サービスではspend cap budgetも検討できるが、適用範囲・遅延・継続する費用を確認して利用する。これらとtenantごとのアプリ上限を併用する。[budget](https://docs.cloud.google.com/billing/docs/how-to/budgets)、[spend cap](https://docs.cloud.google.com/billing/docs/how-to/budgets-spend-caps)

個人のOSS導入者には必要なGCP資源、外部API、対応hardware、常時接続による費用要素を説明する。公式ホスティングの利用料と、self-host時に各自が負担する費用・運用責任を分ける。

## 10. 運用の担当と確認頻度

| 役割 | 責任 |
| --- | --- |
| リリース責任者 | gate、release記録、段階展開と切戻し判断 |
| Platform担当 | GCP、IAM、監視、backup/restore、費用 |
| Device担当 | 対応hardware、OS、音声、OTA、現地復旧 |
| Agent担当 | model/prompt、記憶品質、tool結果、provider変更 |
| Security担当 | 脆弱性受付、権限・秘密情報の事故対応、修正確認 |
| Product担当 | 利用者向け仕様、保持方針、公開説明、通知内容 |

担当者名と連絡手段はOPEN-002で設定する。公開するサポート時間と緊急連絡方法を、その時点の実際の体制に合わせる。体制がない24時間対応を文書で約束しない。

## 11. 公開前のgate

以下は公開判定で確認する項目であり、現在完了していることを示すchecklistではない。

- FR-001〜015の受入結果と、[開発ガイド](05-development-and-testing.md)の重要な回帰ケースが揃っている。
- 別tenant・private記憶・未認証話者・失効端末の拒否を実機を含めて確認した。
- Calendarの確認付き書込とHome Assistantの重複/結果不明を確認した。
- GCP IAM、Secret、予算・上限、監視先、失効手順を確認した。
- daily backup、独立削除台帳、隔離restore、忘却維持を実証した。
- API/Piの更新・rollback、長時間offlineからの復帰を確認した。
- 名称Brainoid Agentに合わせたrepository・配布識別子、担当/連絡先、ライセンス採用を確定した。
- 依存library、model、音声、画像・キャラクター素材の利用条件を棚卸しした。
- OAuth、APNs/FCM、ストア配布、削除導線、利用者向けプライバシー説明を確認した。
- READMEの導入手順を新規環境で試し、未対応条件と既知の制限を記載した。
- SECURITYの非公開報告窓口を実際に使える状態にし、対応対象versionを公表した。

## 12. 未確定事項

| 項目 | 担当 | 確定時期 |
| --- | --- | --- |
| OPEN-001: repository・domain・配布ID | Product/Tech Lead | 名称Brainoid Agentは確定。配布IDやOAuth callbackを登録する前 |
| OPEN-002: 運用・Security窓口 | Product/Platform | 外部pilot開始前 |
| OPEN-003: 実機BOM・OS・復旧媒体 | Device/Mobile | 対応hardwareの告知前 |
| OPEN-004: ライセンス最終採用 | Maintainer | OSS公開前 |
| OPEN-005: region/model/voice/上限値 | Platform/Agent | DB作成前および有料API結合前 |
| OPEN-006: OAuth・通知・ストア手続 | Mobile/Platform | 外部アカウントによる結合・配布前 |
| OPEN-007: SLO・RTO/RPOの校正 | Platform/QA | restore訓練・負荷試験後、公開β前 |

## 13. 一次資料

公式仕様の確認日: 2026-10-25。保持日数、展開割合、内部目標、運用頻度は本プロジェクトの初期案である。

- [Cloud Run WebSocket制約と課金](https://docs.cloud.google.com/run/docs/triggering/websockets)
- [Cloud Tasksからprivate Cloud Runへの認証](https://docs.cloud.google.com/run/docs/triggering/using-tasks)
- [Cloud Tasksの重複実行・制限](https://docs.cloud.google.com/tasks/docs/common-pitfalls)
- [Firestore locations](https://firebase.google.com/docs/firestore/locations)
- [Firestore backupと復元](https://firebase.google.com/docs/firestore/backups)
- [Firestoreのdocument・subcollection削除](https://firebase.google.com/docs/firestore/manage-data/delete-data)
- [Firestore Security Rulesとserver client](https://firebase.google.com/docs/firestore/security/rules-conditions)
- [GitHub ActionsとGCP OIDC](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-google-cloud-platform)
- [TUFによる更新metadataの検証](https://theupdateframework.io/docs/overview/)
- [GCP budget](https://docs.cloud.google.com/billing/docs/how-to/budgets)
- [GCP spend cap budget](https://docs.cloud.google.com/billing/docs/how-to/budgets-spend-caps)
