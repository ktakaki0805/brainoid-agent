# Brainoid Agent 実装チケット

| 項目 | 内容 |
| --- | --- |
| 文書版 | 0.2.0 |
| 更新日 | 2026-10-26（日本時間） |
| 状態 | 開発着手用の計画。現在の実装状況は[進捗台帳](implementation-status.json)を参照。外部Issue未登録 |
| プロジェクト名 | Brainoid Agent（確定） |
| 責任ロール | 技術責任者が依存・PR境界を管理し、各担当とプロダクト責任者がレビュー |
| 対象 | 既存のMVP全機能、端末・クラウドの構築、検証、OSS公開準備 |
| 関連文書 | [要件](01-requirements.md)、[基本設計](02-architecture.md)、[データ・権限](03-data-and-authorization.md)、[API](04-interfaces.md)、[開発・テスト](05-development-and-testing.md)、[運用](06-deployment-and-operations.md) |

既存設計を **96件の実装チケット**に分割した。各チケットを原則1PRとして、機能・契約・必要な検証・関連文書を同じ変更単位で扱う。IDはこの文書内の識別子であり、GitHub等のIssue番号ではない。予定パスは、これから作る実装リポジトリの配置を示す。

各詳細節の「状態」と受入条件のチェック欄は計画作成時点の記録。現在の実装状況は[進捗台帳](implementation-status.json)、実施した検証・レビュー・修正・コミットは[作業ログ](implementation-log.md)を正本とする。ローカルの実装完了とPRのmerge・配備は区別する。Codexでは `$implement-ticket`（次の1件）または `$implement-ticket BA-FND-01`（ID指定）で[実装Skill](../.agents/skills/implement-ticket/SKILL.md)を利用できる。

## 1. 1PRの境界と共通の完了条件

一つのチケットでは、利用者または呼出元から確認できる一つの目的を完了する。その目的に必要なAPI・repository・adapter・検証・文書更新は同じPRに含める。別の機能追加、無関係な整形、別のprovider対応を同時に持ち込まない。

Sは単一componentの狭い変更、Mは一つの目的に必要な複数層の変更を表す。工数や行数の保証ではない。調査後に1PRで安全にレビューできない範囲と分かった場合は、最初の実装前に追加IDへ分割し、依存と受入条件を更新する。

- 受入条件を満たし、必要な正例・負例を確認する。試験未実施を合格として扱わない。
- 認可・削除・外部副作用の検証を、後の総合QAまで先送りしない。
- 型・通信契約・関連する文書を同じPRで整合させる。
- 秘密情報・本番の会話・音声・個人予定をcommitや通常logへ入れない。
- fake/emulatorで確認可能なPRは、それで独立してmergeできるようにする。実provider・実機の確認は外部前提と後続の結合確認へ分ける。
- 部分実装は未完成の機能を公開しない設定でmergeする。公開時には全MVP受入条件を満たす。

優先度P0は基盤・権限・副作用・削除・重要な先行条件、P1は残るMVP機能・体験・公開準備を表す。**P1もMVP完了条件に含む。** 自動で将来版へ移す意味ではない。

## 2. 着手順序と並行開発

M0〜M5は既存の開発段階の目安であり、日程や厳密な直列順序ではない。**着手の先行条件は各チケットの「依存PR」**を正本とする。例えば、後段機能で使うデータ基盤を早い段階で先行実装してよい。依存先の契約が変更された場合は、後続チケットと生成clientを同じ変更で見直す。

| 段階 | 目標 |
| --- | --- |
| M0 | リポジトリ、型・スキーマ、fake/emulator、認証の基礎、開発・公開方針 |
| M1 | テキスト会話、端末登録、通信、最初のstaging |
| M2 | グラフ記憶、忘却、許可した家電操作、中心シナリオのE2E |
| M3 | ノート、タスク、通知、カレンダー、確認後の再開 |
| M4 | 音声、個人セッション、引継ぎ、実機・音響の基準 |
| M5 | 削除・exportの利用者導線、復旧、配布、総合受入、公開 |

最初はBA-FND-01を完了する。その後はAPIの起動基盤、共通型、emulator、Terraform基礎、OSS運営・ライセンス判断を並行できる。API契約からclientが生成できればUIのfake実装を進め、端末サービスはcloud側の完成を待たずローカルの境界を検証する。

文字会話の最初の縦断は、RUN-04/05、UI-03、端末側のDEV-10を結合して確認する。M2の中心シナリオはQA-02で「スマホで共有記憶を保存→Piで照明を設定→スマホで訂正」を確認する。いずれも全機能が終わるまで結合を待たない。

### 最初の着手候補

| 順序 | チケット | 完了後に進められること |
| --- | --- | --- |
| 最初 | [BA-FND-01](#ba-fnd-01-モノレポと依存バージョンの固定) | 全領域が使うworkspaceと依存固定 |
| 続いて並行 | [BA-FND-02](#ba-fnd-02-apiの起動設定ヘルス共通エラー)、[BA-FND-03](#ba-fnd-03-共通id時刻version型の定義)、[BA-FND-05](#ba-fnd-05-authfirestoreエミュレーターと2テナントfixture) | API・共通型・2テナントfixture |
| 続いて並行 | [BA-OPS-01](#ba-ops-01-terraform-stateproject分離基盤iam)、[BA-GOV-01](#ba-gov-01-リポジトリ運営連絡先非公開報告窓口)、[BA-GOV-02](#ba-gov-02-ライセンス確定と第三者素材依存物台帳) | 環境基礎、運営・公開ライセンスの判断 |
| 共通型が完成後 | [BA-FND-04](#ba-fnd-04-openapi検証とtypescriptクライアント生成)、[BA-FND-07](#ba-fnd-07-coreの依存インターフェースとfake-adapter)、[BA-DEV-01](#ba-dev-01-pi常駐サービスとloopback管理境界) | client生成、fake adapter、Piサービス |
| 各依存が完成後 | [BA-IAM-01](#ba-iam-01-firebase認証とactorcontextの生成)、[BA-UI-01](#ba-ui-01-共有react-uiとスマホpiのentry)、[BA-FND-06](#ba-fnd-06-prのlint型契約検証ci) | 認証、UIの並行開発、共通CI |

## 3. PR外の前提作業

次の作業は、コードのPRをmergeするだけでは完了しない。該当チケットの「外部前提」にも記載する。作業の結果は秘密を除いた設定・検証記録としてPRへ結び付け、審査待ち・実機未準備を実装済みと混同しない。

| 前提 | 決める・用意するもの | 主な担当・関連 |
| --- | --- | --- |
| EXT-01 | repository、package/application ID、domain、担当者・連絡先 | メンテナ、OPEN-001/002。名称Brainoid Agentは確定 |
| EXT-02 | GCP課金、project権限、region、model・音声、利用上限 | Platform/Agent、OPEN-005 |
| EXT-03 | Google OAuth、FCM/APNs、Android/iOS署名、配布アカウント、必要な審査 | Mobile/連携担当、OPEN-006 |
| EXT-04 | Pi・画面・音響・電源・冷却、テスト用HA機器、対応スマホ | Device/Mobile、OPEN-003 |
| EXT-05 | OSSライセンスと素材の配布条件、運用主体、保持・説明方針 | メンテナ/Product、OPEN-004/005 |
| EXT-06 | 負荷試験の費用・停止条件、復元試験の隔離環境、公開判定 | QA/Platform/Product、OPEN-007 |

## 4. 分野別の件数

| 分野 | ID prefix | 件数 |
| --- | --- | --- |
| 開発基盤と共通契約 | BA-FND | 8 |
| 認証・テナント・端末権限 | BA-IAM | 10 |
| 会話とエージェント実行 | BA-RUN | 8 |
| グラフ記憶・忘却・エクスポート | BA-MEM | 9 |
| ノート・タスク・通知・家電API | BA-APP | 6 |
| Google Calendar連携 | BA-CAL | 5 |
| Raspberry Piと端末通信 | BA-DEV | 10 |
| スマホ・ディスプレイ・Webの画面 | BA-UI | 13 |
| 音声入力・合成・停止 | BA-VOICE | 5 |
| GCP・配布・復旧・運用 | BA-OPS | 11 |
| 跨層・実機・受入検証 | BA-QA | 6 |
| OSS公開と利用者向け文書 | BA-GOV | 5 |

## 5. チケット一覧

IDの並びは分野別であり、実装の直列順序ではない。詳細には観測できる受入条件と検証方法を記載している。

| ID | 目的となる変更 | 段階 | 優先度・規模 | 依存PR |
| --- | --- | --- | --- | --- |
| [BA-FND-01](#ba-fnd-01-モノレポと依存バージョンの固定) | モノレポと依存バージョンの固定 | M0 | P0・M | なし |
| [BA-FND-02](#ba-fnd-02-apiの起動設定ヘルス共通エラー) | APIの起動設定・ヘルス・共通エラー | M0 | P0・S | [BA-FND-01](#ba-fnd-01-モノレポと依存バージョンの固定) |
| [BA-FND-03](#ba-fnd-03-共通id時刻version型の定義) | 共通ID・時刻・version型の定義 | M0 | P0・S | [BA-FND-01](#ba-fnd-01-モノレポと依存バージョンの固定) |
| [BA-FND-04](#ba-fnd-04-openapi検証とtypescriptクライアント生成) | OpenAPI検証とTypeScriptクライアント生成 | M0 | P0・M | [BA-FND-01](#ba-fnd-01-モノレポと依存バージョンの固定)、[BA-FND-03](#ba-fnd-03-共通id時刻version型の定義) |
| [BA-FND-05](#ba-fnd-05-authfirestoreエミュレーターと2テナントfixture) | Auth・Firestoreエミュレーターと2テナントfixture | M0 | P0・S | [BA-FND-01](#ba-fnd-01-モノレポと依存バージョンの固定) |
| [BA-FND-06](#ba-fnd-06-prのlint型契約検証ci) | PRのlint・型・契約検証CI | M0 | P0・S | [BA-FND-01](#ba-fnd-01-モノレポと依存バージョンの固定)、[BA-FND-04](#ba-fnd-04-openapi検証とtypescriptクライアント生成) |
| [BA-FND-07](#ba-fnd-07-coreの依存インターフェースとfake-adapter) | Coreの依存インターフェースとfake adapter | M0 | P0・M | [BA-FND-03](#ba-fnd-03-共通id時刻version型の定義) |
| [BA-FND-08](#ba-fnd-08-冪等性etagcursorの共通処理) | 冪等性・ETag・cursorの共通処理 | M0 | P0・M | [BA-FND-02](#ba-fnd-02-apiの起動設定ヘルス共通エラー)、[BA-FND-03](#ba-fnd-03-共通id時刻version型の定義)、[BA-FND-05](#ba-fnd-05-authfirestoreエミュレーターと2テナントfixture) |
| [BA-IAM-01](#ba-iam-01-firebase認証とactorcontextの生成) | Firebase認証とActorContextの生成 | M0 | P0・M | [BA-FND-02](#ba-fnd-02-apiの起動設定ヘルス共通エラー)、[BA-FND-03](#ba-fnd-03-共通id時刻version型の定義)、[BA-FND-05](#ba-fnd-05-authfirestoreエミュレーターと2テナントfixture) |
| [BA-IAM-02](#ba-iam-02-テナントprivate表示先の認可関数) | テナント・private・表示先の認可関数 | M0 | P0・M | [BA-IAM-01](#ba-iam-01-firebase認証とactorcontextの生成) |
| [BA-IAM-03](#ba-iam-03-テナント作成選択と初期owneragent) | テナント作成・選択と初期owner・agent | M0 | P0・M | [BA-IAM-02](#ba-iam-02-テナントprivate表示先の認可関数)、[BA-FND-08](#ba-fnd-08-冪等性etagcursorの共通処理) |
| [BA-IAM-04](#ba-iam-04-期限付き招待コードと参加受付) | 期限付き招待コードと参加受付 | M1 | P0・M | [BA-IAM-03](#ba-iam-03-テナント作成選択と初期owneragent)、[BA-IAM-05](#ba-iam-05-独立認可台帳の先行deny確定grant) |
| [BA-IAM-05](#ba-iam-05-独立認可台帳の先行deny確定grant) | 独立認可台帳の先行deny・確定grant | M1 | P0・M | [BA-IAM-02](#ba-iam-02-テナントprivate表示先の認可関数)、[BA-FND-08](#ba-fnd-08-冪等性etagcursorの共通処理) |
| [BA-IAM-06](#ba-iam-06-会員role変更所属失効owner移譲) | 会員role変更・所属失効・owner移譲 | M1 | P0・M | [BA-IAM-03](#ba-iam-03-テナント作成選択と初期owneragent)、[BA-IAM-04](#ba-iam-04-期限付き招待コードと参加受付)、[BA-IAM-05](#ba-iam-05-独立認可台帳の先行deny確定grant) |
| [BA-IAM-07](#ba-iam-07-端末ペアリングと専用tokenの発行更新) | 端末ペアリングと専用tokenの発行・更新 | M1 | P0・M | [BA-IAM-03](#ba-iam-03-テナント作成選択と初期owneragent)、[BA-FND-08](#ba-fnd-08-冪等性etagcursorの共通処理) |
| [BA-IAM-08](#ba-iam-08-端末許可設定資格情報失効) | 端末許可設定・資格情報失効 | M1 | P0・M | [BA-IAM-05](#ba-iam-05-独立認可台帳の先行deny確定grant)、[BA-IAM-07](#ba-iam-07-端末ペアリングと専用tokenの発行更新) |
| [BA-IAM-09](#ba-iam-09-pi個人セッションの開始失効scope) | Pi個人セッションの開始・失効・scope | M4 | P0・M | [BA-IAM-02](#ba-iam-02-テナントprivate表示先の認可関数)、[BA-IAM-08](#ba-iam-08-端末許可設定資格情報失効) |
| [BA-IAM-10](#ba-iam-10-退会アカウントテナント削除の受付) | 退会・アカウント・テナント削除の受付 | M5 | P0・M | [BA-IAM-06](#ba-iam-06-会員role変更所属失効owner移譲)、[BA-MEM-07](#ba-mem-07-忘却受付削除台帳元出典再利用抑止)、[BA-MEM-08](#ba-mem-08-派生データと子階層のpurge-worker) |
| [BA-RUN-01](#ba-run-01-outboxdispatcherworker-lease基盤) | outbox・dispatcher・worker lease基盤 | M1 | P0・M | [BA-FND-05](#ba-fnd-05-authfirestoreエミュレーターと2テナントfixture)、[BA-FND-08](#ba-fnd-08-冪等性etagcursorの共通処理)、[BA-IAM-02](#ba-iam-02-テナントprivate表示先の認可関数) |
| [BA-RUN-02](#ba-run-02-google-gen-aiのmodelprovider-adapter) | Google Gen AIのModelProvider adapter | M1 | P0・M | [BA-FND-07](#ba-fnd-07-coreの依存インターフェースとfake-adapter) |
| [BA-RUN-03](#ba-run-03-人格設定とテキストagentcore) | 人格設定とテキストAgentCore | M1 | P0・M | [BA-FND-07](#ba-fnd-07-coreの依存インターフェースとfake-adapter)、[BA-IAM-03](#ba-iam-03-テナント作成選択と初期owneragent)、[BA-RUN-02](#ba-run-02-google-gen-aiのmodelprovider-adapter) |
| [BA-RUN-04](#ba-run-04-会話messageturn受付と永続run実行) | 会話・message・turn受付と永続run実行 | M1 | P0・M | [BA-RUN-01](#ba-run-01-outboxdispatcherworker-lease基盤)、[BA-RUN-03](#ba-run-03-人格設定とテキストagentcore)、[BA-IAM-02](#ba-iam-02-テナントprivate表示先の認可関数) |
| [BA-RUN-05](#ba-run-05-sse差分seq履歴再開) | SSE差分・seq・履歴再開 | M1 | P0・M | [BA-RUN-04](#ba-run-04-会話messageturn受付と永続run実行) |
| [BA-RUN-06](#ba-run-06-型付きtoolexecutorと実行権限の検査) | 型付きToolExecutorと実行権限の検査 | M2 | P0・M | [BA-RUN-03](#ba-run-03-人格設定とテキストagentcore)、[BA-IAM-02](#ba-iam-02-テナントprivate表示先の認可関数)、[BA-FND-08](#ba-fnd-08-冪等性etagcursorの共通処理) |
| [BA-RUN-07](#ba-run-07-run取消確認checkpointresume) | run取消・確認checkpoint・resume | M3 | P0・M | [BA-RUN-04](#ba-run-04-会話messageturn受付と永続run実行)、[BA-RUN-05](#ba-run-05-sse差分seq履歴再開)、[BA-RUN-06](#ba-run-06-型付きtoolexecutorと実行権限の検査) |
| [BA-RUN-08](#ba-run-08-利用量予約集計と同時実行上限) | 利用量予約・集計と同時実行上限 | M1 | P0・M | [BA-RUN-04](#ba-run-04-会話messageturn受付と永続run実行)、[BA-IAM-02](#ba-iam-02-テナントprivate表示先の認可関数) |
| [BA-MEM-01](#ba-mem-01-entityfactの保存モデルとrepository) | entity・factの保存モデルとrepository | M2 | P0・M | [BA-FND-03](#ba-fnd-03-共通id時刻version型の定義)、[BA-FND-05](#ba-fnd-05-authfirestoreエミュレーターと2テナントfixture)、[BA-IAM-02](#ba-iam-02-テナントprivate表示先の認可関数) |
| [BA-MEM-02](#ba-mem-02-entityの登録別名外部id照合api) | entityの登録・別名・外部ID照合API | M2 | P0・M | [BA-MEM-01](#ba-mem-01-entityfactの保存モデルとrepository)、[BA-FND-08](#ba-fnd-08-冪等性etagcursorの共通処理) |
| [BA-MEM-03](#ba-mem-03-明示記憶の保存出典訂正api) | 明示記憶の保存・出典・訂正API | M2 | P0・M | [BA-MEM-02](#ba-mem-02-entityの登録別名外部id照合api)、[BA-RUN-01](#ba-run-01-outboxdispatcherworker-lease基盤) |
| [BA-MEM-04](#ba-mem-04-scope付き2hop検索と会話への文脈組込み) | scope付き2hop検索と会話への文脈組込み | M2 | P0・M | [BA-MEM-03](#ba-mem-03-明示記憶の保存出典訂正api)、[BA-RUN-03](#ba-run-03-人格設定とテキストagentcore)、[BA-RUN-04](#ba-run-04-会話messageturn受付と永続run実行)、[BA-RUN-06](#ba-run-06-型付きtoolexecutorと実行権限の検査) |
| [BA-MEM-05](#ba-mem-05-埋め込みjobとvector検索adapter) | 埋め込みjobとvector検索adapter | M2 | P0・M | [BA-MEM-04](#ba-mem-04-scope付き2hop検索と会話への文脈組込み)、[BA-RUN-01](#ba-run-01-outboxdispatcherworker-lease基盤) |
| [BA-MEM-06](#ba-mem-06-会話要約と出典scope版の継承) | 会話要約と出典・scope・版の継承 | M2 | P1・M | [BA-MEM-03](#ba-mem-03-明示記憶の保存出典訂正api)、[BA-RUN-01](#ba-run-01-outboxdispatcherworker-lease基盤)、[BA-RUN-02](#ba-run-02-google-gen-aiのmodelprovider-adapter)、[BA-RUN-04](#ba-run-04-会話messageturn受付と永続run実行) |
| [BA-MEM-07](#ba-mem-07-忘却受付削除台帳元出典再利用抑止) | 忘却受付・削除台帳・元出典再利用抑止 | M2 | P0・M | [BA-MEM-03](#ba-mem-03-明示記憶の保存出典訂正api)、[BA-RUN-01](#ba-run-01-outboxdispatcherworker-lease基盤)、[BA-RUN-06](#ba-run-06-型付きtoolexecutorと実行権限の検査) |
| [BA-MEM-08](#ba-mem-08-派生データと子階層のpurge-worker) | 派生データと子階層のpurge worker | M2 | P0・M | [BA-MEM-05](#ba-mem-05-埋め込みjobとvector検索adapter)、[BA-MEM-06](#ba-mem-06-会話要約と出典scope版の継承)、[BA-MEM-07](#ba-mem-07-忘却受付削除台帳元出典再利用抑止) |
| [BA-MEM-09](#ba-mem-09-認可付きエクスポート生成取得失効) | 認可付きエクスポート生成・取得・失効 | M5 | P0・M | [BA-MEM-07](#ba-mem-07-忘却受付削除台帳元出典再利用抑止)、[BA-MEM-08](#ba-mem-08-派生データと子階層のpurge-worker) |
| [BA-APP-01](#ba-app-01-ノートcrudとノートtool) | ノートCRUDとノートtool | M3 | P1・M | [BA-RUN-06](#ba-run-06-型付きtoolexecutorと実行権限の検査)、[BA-FND-08](#ba-fnd-08-冪等性etagcursorの共通処理)、[BA-MEM-08](#ba-mem-08-派生データと子階層のpurge-worker) |
| [BA-APP-02](#ba-app-02-タスクの作成一覧更新完了取消とtool) | タスクの作成・一覧・更新・完了・取消とtool | M3 | P1・M | [BA-RUN-06](#ba-run-06-型付きtoolexecutorと実行権限の検査)、[BA-FND-08](#ba-fnd-08-冪等性etagcursorの共通処理) |
| [BA-APP-03](#ba-app-03-リマインダースヌーズ予定版api) | リマインダー・スヌーズ・予定版API | M3 | P1・M | [BA-APP-02](#ba-app-02-タスクの作成一覧更新完了取消とtool)、[BA-RUN-01](#ba-run-01-outboxdispatcherworker-lease基盤) |
| [BA-APP-04](#ba-app-04-期限走査と重複しない通知job) | 期限走査と重複しない通知job | M3 | P0・M | [BA-APP-03](#ba-app-03-リマインダースヌーズ予定版api)、[BA-RUN-01](#ba-run-01-outboxdispatcherworker-lease基盤) |
| [BA-APP-05](#ba-app-05-通知apifcm登録汎用push配送) | 通知API・FCM登録・汎用push配送 | M3 | P1・M | [BA-APP-04](#ba-app-04-期限走査と重複しない通知job)、[BA-IAM-02](#ba-iam-02-テナントprivate表示先の認可関数) |
| [BA-APP-06](#ba-app-06-家電command-apiとhome-tool) | 家電command APIとhome tool | M2 | P0・M | [BA-RUN-06](#ba-run-06-型付きtoolexecutorと実行権限の検査)、[BA-MEM-02](#ba-mem-02-entityの登録別名外部id照合api)、[BA-IAM-08](#ba-iam-08-端末許可設定資格情報失効) |
| [BA-CAL-01](#ba-cal-01-calendar-oauth開始callback秘密管理) | Calendar OAuth開始・callback・秘密管理 | M3 | P0・M | [BA-IAM-02](#ba-iam-02-テナントprivate表示先の認可関数)、[BA-FND-08](#ba-fnd-08-冪等性etagcursorの共通処理) |
| [BA-CAL-02](#ba-cal-02-calendar予定一覧時刻正規化と参照tool) | Calendar予定一覧・時刻正規化と参照tool | M3 | P1・M | [BA-CAL-01](#ba-cal-01-calendar-oauth開始callback秘密管理)、[BA-RUN-06](#ba-run-06-型付きtoolexecutorと実行権限の検査) |
| [BA-CAL-03](#ba-cal-03-calendar操作proposalと確認内容の固定) | Calendar操作proposalと確認内容の固定 | M3 | P0・M | [BA-CAL-02](#ba-cal-02-calendar予定一覧時刻正規化と参照tool)、[BA-RUN-06](#ba-run-06-型付きtoolexecutorと実行権限の検査)、[BA-RUN-07](#ba-run-07-run取消確認checkpointresume) |
| [BA-CAL-04](#ba-cal-04-本人確認後のcalendar実行照合resume) | 本人確認後のCalendar実行・照合・resume | M3 | P0・M | [BA-CAL-03](#ba-cal-03-calendar操作proposalと確認内容の固定)、[BA-RUN-07](#ba-run-07-run取消確認checkpointresume)、[BA-RUN-01](#ba-run-01-outboxdispatcherworker-lease基盤) |
| [BA-CAL-05](#ba-cal-05-calendar連携解除token失効再接続案内) | Calendar連携解除・token失効・再接続案内 | M3 | P0・M | [BA-CAL-01](#ba-cal-01-calendar-oauth開始callback秘密管理)、[BA-CAL-04](#ba-cal-04-本人確認後のcalendar実行照合resume)、[BA-MEM-08](#ba-mem-08-派生データと子階層のpurge-worker) |
| [BA-DEV-01](#ba-dev-01-pi常駐サービスとloopback管理境界) | Pi常駐サービスとloopback管理境界 | M1 | P0・M | [BA-FND-01](#ba-fnd-01-モノレポと依存バージョンの固定)、[BA-FND-03](#ba-fnd-03-共通id時刻version型の定義) |
| [BA-DEV-02](#ba-dev-02-sqlite設定cursorcommand台帳) | SQLite設定・cursor・command台帳 | M1 | P0・M | [BA-DEV-01](#ba-dev-01-pi常駐サービスとloopback管理境界) |
| [BA-DEV-03](#ba-dev-03-wss接続認証generationheartbeat) | WSS接続認証・generation・heartbeat | M1 | P0・M | [BA-IAM-07](#ba-iam-07-端末ペアリングと専用tokenの発行更新)、[BA-IAM-08](#ba-iam-08-端末許可設定資格情報失効)、[BA-RUN-01](#ba-run-01-outboxdispatcherworker-lease基盤) |
| [BA-DEV-04](#ba-dev-04-端末snapshotsessionrun割当通知) | 端末snapshot・session・run割当通知 | M4 | P0・M | [BA-DEV-03](#ba-dev-03-wss接続認証generationheartbeat)、[BA-IAM-09](#ba-iam-09-pi個人セッションの開始失効scope)、[BA-RUN-05](#ba-run-05-sse差分seq履歴再開)、[BA-DEV-10](#ba-dev-10-piのクラウド接続httpsse-bridge)、[BA-APP-05](#ba-app-05-通知apifcm登録汎用push配送)、[BA-APP-06](#ba-app-06-家電command-apiとhome-tool) |
| [BA-DEV-05](#ba-dev-05-command-start-claimgrantと取消競合) | command start claim・grantと取消競合 | M2 | P0・M | [BA-DEV-03](#ba-dev-03-wss接続認証generationheartbeat)、[BA-APP-06](#ba-app-06-家電command-apiとhome-tool) |
| [BA-DEV-06](#ba-dev-06-home-assistant-adapterとentity同期) | Home Assistant adapterとentity同期 | M2 | P0・M | [BA-DEV-01](#ba-dev-01-pi常駐サービスとloopback管理境界)、[BA-MEM-02](#ba-mem-02-entityの登録別名外部id照合api)、[BA-IAM-08](#ba-iam-08-端末許可設定資格情報失効)、[BA-DEV-10](#ba-dev-10-piのクラウド接続httpsse-bridge) |
| [BA-DEV-07](#ba-dev-07-piコマンド実行観測再接続照合) | Piコマンド実行・観測・再接続照合 | M2 | P0・M | [BA-DEV-02](#ba-dev-02-sqlite設定cursorcommand台帳)、[BA-DEV-03](#ba-dev-03-wss接続認証generationheartbeat)、[BA-DEV-05](#ba-dev-05-command-start-claimgrantと取消競合)、[BA-DEV-06](#ba-dev-06-home-assistant-adapterとentity同期)、[BA-DEV-10](#ba-dev-10-piのクラウド接続httpsse-bridge) |
| [BA-DEV-08](#ba-dev-08-署名更新の検証起動切替rollback) | 署名更新の検証・起動切替・rollback | M5 | P0・M | [BA-DEV-01](#ba-dev-01-pi常駐サービスとloopback管理境界)、[BA-DEV-02](#ba-dev-02-sqlite設定cursorcommand台帳) |
| [BA-DEV-09](#ba-dev-09-pi導入手順kiosk起動offline回復画面) | Pi導入手順・kiosk起動・offline回復画面 | M4 | P1・M | [BA-DEV-01](#ba-dev-01-pi常駐サービスとloopback管理境界)、[BA-DEV-02](#ba-dev-02-sqlite設定cursorcommand台帳)、[BA-UI-01](#ba-ui-01-共有react-uiとスマホpiのentry) |
| [BA-DEV-10](#ba-dev-10-piのクラウド接続httpsse-bridge) | Piのクラウド接続・HTTP/SSE bridge | M1 | P0・M | [BA-DEV-01](#ba-dev-01-pi常駐サービスとloopback管理境界)、[BA-DEV-02](#ba-dev-02-sqlite設定cursorcommand台帳)、[BA-DEV-03](#ba-dev-03-wss接続認証generationheartbeat)、[BA-IAM-07](#ba-iam-07-端末ペアリングと専用tokenの発行更新)、[BA-RUN-05](#ba-run-05-sse差分seq履歴再開)、[BA-UI-01](#ba-ui-01-共有react-uiとスマホpiのentry) |
| [BA-UI-01](#ba-ui-01-共有react-uiとスマホpiのentry) | 共有React UIとスマホ・Piのentry | M0 | P0・M | [BA-FND-01](#ba-fnd-01-モノレポと依存バージョンの固定)、[BA-FND-04](#ba-fnd-04-openapi検証とtypescriptクライアント生成) |
| [BA-UI-02](#ba-ui-02-ログインテナント選択context切替) | ログイン・テナント選択・context切替 | M1 | P0・M | [BA-UI-01](#ba-ui-01-共有react-uiとスマホpiのentry)、[BA-IAM-03](#ba-iam-03-テナント作成選択と初期owneragent) |
| [BA-UI-03](#ba-ui-03-会話画面sse表示2d状態表示) | 会話画面・SSE表示・2D状態表示 | M1 | P0・M | [BA-UI-02](#ba-ui-02-ログインテナント選択context切替)、[BA-RUN-05](#ba-run-05-sse差分seq履歴再開) |
| [BA-UI-04](#ba-ui-04-記憶の出典共有訂正忘却画面) | 記憶の出典・共有・訂正・忘却画面 | M2 | P0・M | [BA-UI-02](#ba-ui-02-ログインテナント選択context切替)、[BA-MEM-03](#ba-mem-03-明示記憶の保存出典訂正api)、[BA-MEM-04](#ba-mem-04-scope付き2hop検索と会話への文脈組込み)、[BA-MEM-07](#ba-mem-07-忘却受付削除台帳元出典再利用抑止) |
| [BA-UI-05](#ba-ui-05-ノート一覧編集削除画面) | ノート一覧・編集・削除画面 | M3 | P1・S | [BA-UI-02](#ba-ui-02-ログインテナント選択context切替)、[BA-APP-01](#ba-app-01-ノートcrudとノートtool) |
| [BA-UI-06](#ba-ui-06-タスク一覧期限完了画面) | タスク一覧・期限・完了画面 | M3 | P1・S | [BA-UI-02](#ba-ui-02-ログインテナント選択context切替)、[BA-APP-02](#ba-app-02-タスクの作成一覧更新完了取消とtool) |
| [BA-UI-07](#ba-ui-07-リマインダー通知スヌーズ画面) | リマインダー・通知・スヌーズ画面 | M3 | P0・M | [BA-UI-02](#ba-ui-02-ログインテナント選択context切替)、[BA-APP-03](#ba-app-03-リマインダースヌーズ予定版api)、[BA-APP-05](#ba-app-05-通知apifcm登録汎用push配送) |
| [BA-UI-08](#ba-ui-08-calendar連携予定確認カード) | Calendar連携・予定・確認カード | M3 | P0・M | [BA-UI-02](#ba-ui-02-ログインテナント選択context切替)、[BA-CAL-02](#ba-cal-02-calendar予定一覧時刻正規化と参照tool)、[BA-CAL-03](#ba-cal-03-calendar操作proposalと確認内容の固定)、[BA-CAL-04](#ba-cal-04-本人確認後のcalendar実行照合resume)、[BA-CAL-05](#ba-cal-05-calendar連携解除token失効再接続案内) |
| [BA-UI-09](#ba-ui-09-piペアリング許可設定失効画面) | Piペアリング・許可設定・失効画面 | M1 | P0・M | [BA-UI-02](#ba-ui-02-ログインテナント選択context切替)、[BA-IAM-07](#ba-iam-07-端末ペアリングと専用tokenの発行更新)、[BA-IAM-08](#ba-iam-08-端末許可設定資格情報失効) |
| [BA-UI-10](#ba-ui-10-pi個人sessionと会話引継ぎui) | Pi個人sessionと会話引継ぎUI | M4 | P0・M | [BA-UI-03](#ba-ui-03-会話画面sse表示2d状態表示)、[BA-UI-09](#ba-ui-09-piペアリング許可設定失効画面)、[BA-IAM-09](#ba-iam-09-pi個人セッションの開始失効scope)、[BA-DEV-04](#ba-dev-04-端末snapshotsessionrun割当通知) |
| [BA-UI-11](#ba-ui-11-会員招待role利用量の管理画面) | 会員・招待・role・利用量の管理画面 | M3 | P0・M | [BA-UI-02](#ba-ui-02-ログインテナント選択context切替)、[BA-IAM-04](#ba-iam-04-期限付き招待コードと参加受付)、[BA-IAM-06](#ba-iam-06-会員role変更所属失効owner移譲)、[BA-RUN-08](#ba-run-08-利用量予約集計と同時実行上限) |
| [BA-UI-12](#ba-ui-12-エクスポート退会削除の利用者導線) | エクスポート・退会・削除の利用者導線 | M5 | P0・M | [BA-UI-02](#ba-ui-02-ログインテナント選択context切替)、[BA-MEM-09](#ba-mem-09-認可付きエクスポート生成取得失効)、[BA-IAM-10](#ba-iam-10-退会アカウントテナント削除の受付) |
| [BA-UI-13](#ba-ui-13-webからのアカウント削除申請導線) | Webからのアカウント削除申請導線 | M5 | P0・S | [BA-UI-02](#ba-ui-02-ログインテナント選択context切替)、[BA-UI-12](#ba-ui-12-エクスポート退会削除の利用者導線)、[BA-IAM-10](#ba-iam-10-退会アカウントテナント削除の受付) |
| [BA-VOICE-01](#ba-voice-01-音声uploadstttranscript参照) | 音声upload・STT・transcript参照 | M4 | P0・M | [BA-RUN-04](#ba-run-04-会話messageturn受付と永続run実行)、[BA-IAM-02](#ba-iam-02-テナントprivate表示先の認可関数)、[BA-RUN-08](#ba-run-08-利用量予約集計と同時実行上限) |
| [BA-VOICE-02](#ba-voice-02-文単位ttsと認可付き音声取得) | 文単位TTSと認可付き音声取得 | M4 | P0・M | [BA-RUN-05](#ba-run-05-sse差分seq履歴再開)、[BA-IAM-09](#ba-iam-09-pi個人セッションの開始失効scope)、[BA-RUN-08](#ba-run-08-利用量予約集計と同時実行上限) |
| [BA-VOICE-03](#ba-voice-03-スマホ録音pcm変換再生adapter) | スマホ録音・PCM変換・再生adapter | M4 | P0・M | [BA-UI-01](#ba-ui-01-共有react-uiとスマホpiのentry)、[BA-VOICE-01](#ba-voice-01-音声uploadstttranscript参照)、[BA-VOICE-02](#ba-voice-02-文単位ttsと認可付き音声取得) |
| [BA-VOICE-04](#ba-voice-04-pi録音再生queue停止adapter) | Pi録音・再生queue・停止adapter | M4 | P0・M | [BA-DEV-01](#ba-dev-01-pi常駐サービスとloopback管理境界)、[BA-DEV-02](#ba-dev-02-sqlite設定cursorcommand台帳)、[BA-VOICE-01](#ba-voice-01-音声uploadstttranscript参照)、[BA-VOICE-02](#ba-voice-02-文単位ttsと認可付き音声取得) |
| [BA-VOICE-05](#ba-voice-05-音声状態と停止取消権限拒否のui) | 音声状態と停止・取消・権限拒否のUI | M4 | P0・M | [BA-UI-03](#ba-ui-03-会話画面sse表示2d状態表示)、[BA-UI-10](#ba-ui-10-pi個人sessionと会話引継ぎui)、[BA-VOICE-03](#ba-voice-03-スマホ録音pcm変換再生adapter)、[BA-VOICE-04](#ba-voice-04-pi録音再生queue停止adapter)、[BA-RUN-07](#ba-run-07-run取消確認checkpointresume) |
| [BA-OPS-01](#ba-ops-01-terraform-stateproject分離基盤iam) | Terraform state・project分離・基盤IAM | M0 | P0・M | [BA-FND-01](#ba-fnd-01-モノレポと依存バージョンの固定) |
| [BA-OPS-02](#ba-ops-02-firestore索引backup制御bucket) | Firestore・索引・backup・制御bucket | M1 | P0・M | [BA-OPS-01](#ba-ops-01-terraform-stateproject分離基盤iam)、[BA-FND-03](#ba-fnd-03-共通id時刻version型の定義) |
| [BA-OPS-03](#ba-ops-03-cloud-runtasksschedulerの配備定義) | Cloud Run・Tasks・Schedulerの配備定義 | M1 | P0・M | [BA-OPS-01](#ba-ops-01-terraform-stateproject分離基盤iam)、[BA-FND-02](#ba-fnd-02-apiの起動設定ヘルス共通エラー)、[BA-RUN-01](#ba-run-01-outboxdispatcherworker-lease基盤) |
| [BA-OPS-04](#ba-ops-04-secretとprovider設定アクセス権) | Secretとprovider設定・アクセス権 | M1 | P0・S | [BA-OPS-01](#ba-ops-01-terraform-stateproject分離基盤iam)、[BA-RUN-02](#ba-run-02-google-gen-aiのmodelprovider-adapter) |
| [BA-OPS-05](#ba-ops-05-oidcimage-digeststaging配備ci) | OIDC・image digest・staging配備CI | M1 | P0・M | [BA-FND-06](#ba-fnd-06-prのlint型契約検証ci)、[BA-OPS-02](#ba-ops-02-firestore索引backup制御bucket)、[BA-OPS-03](#ba-ops-03-cloud-runtasksschedulerの配備定義)、[BA-OPS-04](#ba-ops-04-secretとprovider設定アクセス権) |
| [BA-OPS-06](#ba-ops-06-本文なしの監視alert利用量集計) | 本文なしの監視・alert・利用量集計 | M3 | P0・M | [BA-RUN-08](#ba-run-08-利用量予約集計と同時実行上限)、[BA-OPS-05](#ba-ops-05-oidcimage-digeststaging配備ci) |
| [BA-OPS-07](#ba-ops-07-隔離復元と削除認可台帳の再適用) | 隔離復元と削除・認可台帳の再適用 | M5 | P0・M | [BA-OPS-02](#ba-ops-02-firestore索引backup制御bucket)、[BA-IAM-05](#ba-iam-05-独立認可台帳の先行deny確定grant)、[BA-MEM-08](#ba-mem-08-派生データと子階層のpurge-worker) |
| [BA-OPS-08](#ba-ops-08-pi配布artifactの署名と段階配布) | Pi配布artifactの署名と段階配布 | M5 | P0・M | [BA-DEV-08](#ba-dev-08-署名更新の検証起動切替rollback)、[BA-OPS-01](#ba-ops-01-terraform-stateproject分離基盤iam)、[BA-FND-06](#ba-fnd-06-prのlint型契約検証ci) |
| [BA-OPS-09](#ba-ops-09-android署名buildと内部配布手順) | Android署名buildと内部配布手順 | M5 | P1・M | [BA-UI-03](#ba-ui-03-会話画面sse表示2d状態表示)、[BA-VOICE-03](#ba-voice-03-スマホ録音pcm変換再生adapter)、[BA-APP-05](#ba-app-05-通知apifcm登録汎用push配送)、[BA-FND-06](#ba-fnd-06-prのlint型契約検証ci) |
| [BA-OPS-10](#ba-ops-10-ios署名buildと内部配布手順) | iOS署名buildと内部配布手順 | M5 | P1・M | [BA-UI-03](#ba-ui-03-会話画面sse表示2d状態表示)、[BA-VOICE-03](#ba-voice-03-スマホ録音pcm変換再生adapter)、[BA-APP-05](#ba-app-05-通知apifcm登録汎用push配送)、[BA-FND-06](#ba-fnd-06-prのlint型契約検証ci) |
| [BA-OPS-11](#ba-ops-11-self-hostの初回導入と設定診断) | self-hostの初回導入と設定診断 | M5 | P1・M | [BA-OPS-05](#ba-ops-05-oidcimage-digeststaging配備ci)、[BA-DEV-09](#ba-dev-09-pi導入手順kiosk起動offline回復画面)、[BA-GOV-04](#ba-gov-04-対応機材bomos音響の検証記録) |
| [BA-QA-01](#ba-qa-01-権限境界のcross-component回帰) | 権限境界のcross-component回帰 | M5 | P0・M | [BA-IAM-10](#ba-iam-10-退会アカウントテナント削除の受付)、[BA-MEM-09](#ba-mem-09-認可付きエクスポート生成取得失効)、[BA-DEV-04](#ba-dev-04-端末snapshotsessionrun割当通知)、[BA-RUN-06](#ba-run-06-型付きtoolexecutorと実行権限の検査)、[BA-VOICE-02](#ba-voice-02-文単位ttsと認可付き音声取得) |
| [BA-QA-02](#ba-qa-02-スマホの記憶からpi照明操作までのe2e) | スマホの記憶からPi照明操作までのE2E | M2 | P0・M | [BA-UI-03](#ba-ui-03-会話画面sse表示2d状態表示)、[BA-UI-04](#ba-ui-04-記憶の出典共有訂正忘却画面)、[BA-UI-09](#ba-ui-09-piペアリング許可設定失効画面)、[BA-MEM-04](#ba-mem-04-scope付き2hop検索と会話への文脈組込み)、[BA-DEV-07](#ba-dev-07-piコマンド実行観測再接続照合) |
| [BA-QA-03](#ba-qa-03-日本語の記憶tool音声評価セット) | 日本語の記憶・tool・音声評価セット | M4 | P1・M | [BA-MEM-05](#ba-mem-05-埋め込みjobとvector検索adapter)、[BA-CAL-04](#ba-cal-04-本人確認後のcalendar実行照合resume)、[BA-DEV-07](#ba-dev-07-piコマンド実行観測再接続照合)、[BA-VOICE-05](#ba-voice-05-音声状態と停止取消権限拒否のui) |
| [BA-QA-04](#ba-qa-04-負荷遅延待機原価の計測シナリオ) | 負荷・遅延・待機原価の計測シナリオ | M5 | P1・M | [BA-OPS-05](#ba-ops-05-oidcimage-digeststaging配備ci)、[BA-OPS-06](#ba-ops-06-本文なしの監視alert利用量集計)、[BA-VOICE-05](#ba-voice-05-音声状態と停止取消権限拒否のui)、[BA-RUN-08](#ba-run-08-利用量予約集計と同時実行上限)、[BA-DEV-07](#ba-dev-07-piコマンド実行観測再接続照合) |
| [BA-QA-05](#ba-qa-05-pi断線電源断更新復旧の実機試験) | Pi断線・電源断・更新復旧の実機試験 | M5 | P0・M | [BA-DEV-07](#ba-dev-07-piコマンド実行観測再接続照合)、[BA-DEV-08](#ba-dev-08-署名更新の検証起動切替rollback)、[BA-DEV-09](#ba-dev-09-pi導入手順kiosk起動offline回復画面)、[BA-OPS-08](#ba-ops-08-pi配布artifactの署名と段階配布)、[BA-VOICE-04](#ba-voice-04-pi録音再生queue停止adapter)、[BA-DEV-04](#ba-dev-04-端末snapshotsessionrun割当通知) |
| [BA-QA-06](#ba-qa-06-mvp全受入基準の証跡と公開判定) | MVP全受入基準の証跡と公開判定 | M5 | P0・M | [BA-QA-01](#ba-qa-01-権限境界のcross-component回帰)、[BA-QA-02](#ba-qa-02-スマホの記憶からpi照明操作までのe2e)、[BA-QA-03](#ba-qa-03-日本語の記憶tool音声評価セット)、[BA-QA-04](#ba-qa-04-負荷遅延待機原価の計測シナリオ)、[BA-QA-05](#ba-qa-05-pi断線電源断更新復旧の実機試験)、[BA-OPS-07](#ba-ops-07-隔離復元と削除認可台帳の再適用)、[BA-OPS-09](#ba-ops-09-android署名buildと内部配布手順)、[BA-OPS-10](#ba-ops-10-ios署名buildと内部配布手順)、[BA-OPS-11](#ba-ops-11-self-hostの初回導入と設定診断)、[BA-GOV-01](#ba-gov-01-リポジトリ運営連絡先非公開報告窓口)、[BA-GOV-02](#ba-gov-02-ライセンス確定と第三者素材依存物台帳)、[BA-GOV-03](#ba-gov-03-保持外部送信削除の利用者向け説明)、[BA-GOV-04](#ba-gov-04-対応機材bomos音響の検証記録)、[BA-UI-05](#ba-ui-05-ノート一覧編集削除画面)、[BA-UI-06](#ba-ui-06-タスク一覧期限完了画面)、[BA-UI-07](#ba-ui-07-リマインダー通知スヌーズ画面)、[BA-UI-08](#ba-ui-08-calendar連携予定確認カード)、[BA-UI-11](#ba-ui-11-会員招待role利用量の管理画面)、[BA-UI-12](#ba-ui-12-エクスポート退会削除の利用者導線)、[BA-UI-13](#ba-ui-13-webからのアカウント削除申請導線) |
| [BA-GOV-01](#ba-gov-01-リポジトリ運営連絡先非公開報告窓口) | リポジトリ運営・連絡先・非公開報告窓口 | M0 | P0・S | [BA-FND-01](#ba-fnd-01-モノレポと依存バージョンの固定) |
| [BA-GOV-02](#ba-gov-02-ライセンス確定と第三者素材依存物台帳) | ライセンス確定と第三者素材・依存物台帳 | M0 | P0・S | [BA-FND-01](#ba-fnd-01-モノレポと依存バージョンの固定) |
| [BA-GOV-03](#ba-gov-03-保持外部送信削除の利用者向け説明) | 保持・外部送信・削除の利用者向け説明 | M5 | P0・S | [BA-MEM-08](#ba-mem-08-派生データと子階層のpurge-worker)、[BA-IAM-10](#ba-iam-10-退会アカウントテナント削除の受付)、[BA-CAL-05](#ba-cal-05-calendar連携解除token失効再接続案内)、[BA-VOICE-01](#ba-voice-01-音声uploadstttranscript参照) |
| [BA-GOV-04](#ba-gov-04-対応機材bomos音響の検証記録) | 対応機材BOM・OS・音響の検証記録 | M4 | P1・S | [BA-DEV-09](#ba-dev-09-pi導入手順kiosk起動offline回復画面)、[BA-VOICE-04](#ba-voice-04-pi録音再生queue停止adapter) |
| [BA-GOV-05](#ba-gov-05-初回リリースノートと互換既知制限表) | 初回リリースノートと互換・既知制限表 | M5 | P1・S | [BA-QA-06](#ba-qa-06-mvp全受入基準の証跡と公開判定) |

## 6. チケット詳細

### FND 開発基盤と共通契約

#### BA-FND-01 モノレポと依存バージョンの固定

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | 基盤 |
| 段階・優先度・規模 | M0・P0・M |
| 関連要件 | FR-014 |
| 依存PR | なし |

**目的**

全担当が同じ依存関係とディレクトリ境界で開発を始められるよう、Brainoid Agentのモノレポを初期化する。

**主な変更範囲**

- apps、services、packages、infraの予定構造をworkspaceとして定義する。
- 既存の `pyproject.toml`・`uv.lock` をuv workspaceへ拡張し、Python依存は `uv add`・`uv remove` で管理する。共通の開発ツールはルートのdev依存、サービス・共有パッケージの依存は各memberへ追加する。
- 既存のmise設定を基にPython/Nodeとpackage managerのアプリ対応を確認し、formatter・型検査を設定する。
- `mise run setup` をworkspaceのlockfileからの導入とtoolchain検査へ拡張し、秘密値やクラウド変更を必要としない。

miseによるツール固定、uvプロジェクトとlockfile、チケット検証用setup/check/testは提供済み。setupは `uv sync --locked`、Python検証タスクは `uv run --locked --offline` を使う。[開発ツールのセットアップ](toolchain.md)を基に拡張する。このチケットはworkspace・アプリ依存lock・formatter・型検査の受入条件まで満たして完了とする。

**成果物・変更先（予定）**

- `pyproject.toml・uv.lock`（既存プロジェクトをworkspaceへ拡張）
- `package.json・pnpm-workspace.yaml・pnpm-lock.yaml`
- `mise.tomlのsetup/checkタスク`
- `docs/toolchain.md`

**受入条件**

- [ ] 新しい作業環境で指定のtoolchainとlockfileから依存を解決できる。
- [ ] workspace間の依存は公開packageを通り、サービス内部への相対importを置かない。
- [ ] 未対応toolchainでは説明付きで停止し、.envや鍵ファイルが追跡対象にならない。

**検証方法**

- 空の作業環境でsetup・format・型検査を実行し、lockfileに意図しない差分がないことを確認する。

**このPRに含めないもの**

- APIや画面の動作はBA-FND-02・BA-UI-01で実装する。
- 実クラウド構築はBA-OPS-01へ分ける。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-FND-02 APIの起動設定・ヘルス・共通エラー

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Backend |
| 段階・優先度・規模 | M0・P0・S |
| 関連要件 | FR-015 |
| 依存PR | [BA-FND-01](#ba-fnd-01-モノレポと依存バージョンの固定) |

**目的**

APIの起動失敗と依存先の不調を観測でき、クライアントが一定の形式でエラーを処理できる入口を作る。

**主な変更範囲**

- FastAPIの起動・終了処理と型付き環境設定を定義する。
- livenessと依存先確認用readinessを分離し、依存状態は注入可能にする。
- 共通エラーcode・request_id・retryableを返し、ログから認証headerと本文を除外する。

**成果物・変更先（予定）**

- `services/api/app.py`
- `services/api/settings.py`
- `services/api/errors.py`
- `services/api/health.py`

**受入条件**

- [ ] 設定不足・矛盾で起動が失敗し、別project/providerへ黙って接続しない。
- [ ] production設定にfake認証やemulator接続があれば起動を拒否する。
- [ ] 依存先の異常をreadinessで表し、livenessと区別できる。

**検証方法**

- T-027として本番設定へのemulator混入を拒否する。
- 例外を発生させ、共通エラーとrequest_idの対応、秘密値のログ非出力を確認する。

**このPRに含めないもの**

- 本人認証・業務APIはBA-IAM-01以降へ分ける。
- 監視基盤・通知先の構築はBA-OPS-06で行う。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-FND-03 共通ID・時刻・version型の定義

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Backend |
| 段階・優先度・規模 | M0・P0・S |
| 関連要件 | FR-001、FR-006 |
| 依存PR | [BA-FND-01](#ba-fnd-01-モノレポと依存バージョンの固定) |

**目的**

Pythonと生成クライアントがID・時刻・更新版を同じ意味で扱い、後続APIごとの解釈差をなくす。

**主な変更範囲**

- サーバーUUIDとFirebase UID/provider IDのopaque stringを別の型にする。
- RFC 3339のUTC変換、IANA timezone、正整数version、visibility/display_policyを共通化する。
- 認可済み主体とtenantを受け渡すContext型を定義し、検証処理自体はIAM層へ委ねる。

**成果物・変更先（予定）**

- `packages/contracts/identifiers.py`
- `packages/contracts/time.py`
- `packages/contracts/context.py`
- `specs/openapi.yamlとの対応表`

**受入条件**

- [ ] UUIDでないFirebase UIDを正しく受け入れ、UUIDリソースには誤形式を受け入れない。
- [ ] timezoneのない日時や不正なIANA名を拒否し、保存時刻をUTCへ正規化できる。
- [ ] privateとshared_displayの禁止組合せが共通の検証対象になっている。

**検証方法**

- 境界値、日付跨ぎ、offset付き時刻、禁止されたscope組合せで正負例を確認する。

**このPRに含めないもの**

- 日時の自然言語解釈はBA-RUN-03・BA-CAL-02で扱う。
- 実際の会員権限照合はBA-IAM-02で実装する。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-FND-04 OpenAPI検証とTypeScriptクライアント生成

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | API |
| 段階・優先度・規模 | M0・P0・M |
| 関連要件 | FR-014 |
| 依存PR | [BA-FND-01](#ba-fnd-01-モノレポと依存バージョンの固定)、[BA-FND-03](#ba-fnd-03-共通id時刻version型の定義) |

**目的**

既存のOpenAPI・JSON Schemaを実装契約として利用し、APIとスマホ/ディスプレイの型のずれを検出する。

**主な変更範囲**

- OpenAPI 3.1とJSON Schema 2020-12に対応する既存validatorを固定し、構文・メタスキーマ適合・ローカル参照・pathパラメーター・認証方式の定義を検証する。独自の文書検証スクリプトは前提にしない。
- HTTPクライアントをTypeScriptへ生成し、認証・base URL・transportを注入する入口を設ける。
- 生成設定を固定し、再生成差分と基準版からの破壊的契約変更を検出する。

**成果物・変更先（予定）**

- `scripts/validate_contracts`
- `scripts/generate_api_client`
- `packages/api-client`
- `mise.tomlのgenerate-client/checkタスク`

**受入条件**

- [ ] 既存84操作の契約を解析し、未解決参照と重複operationIdを失敗として検出できる。
- [ ] 生成クライアントが型検査でき、設定から認証情報を受け取りコードへ埋め込まない。
- [ ] 再生成だけでは差分がなく、意図的な破壊的変更を契約検証が検出する。

**検証方法**

- 既存契約を生成・検証し、未解決参照、必須項目不足の入力例、基準版からの操作削除を負例として検出する。

**このPRに含めないもの**

- APIハンドラーの実装は各業務チケットで行う。
- 本番配備用CIはBA-OPS-05へ分ける。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-FND-05 Auth・Firestoreエミュレーターと2テナントfixture

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Backend |
| 段階・優先度・規模 | M0・P0・S |
| 関連要件 | FR-001、FR-002 |
| 依存PR | [BA-FND-01](#ba-fnd-01-モノレポと依存バージョンの固定) |

**目的**

課金や実ユーザーデータを使わず、テナント分離と同時更新を再現できるローカル環境を用意する。

**主な変更範囲**

- Auth/Firestore emulatorの接続と終了・初期化を再現できるようにする。
- 2テナント、owner/admin/member、private/shared、登録/失効Piの固定fixtureを作る。
- seedはlocal専用projectに限定し、外部providerはfakeを選ぶ。

**成果物・変更先（予定）**

- `firebase.json`
- `tests/fixtures/tenants`
- `scripts/seed_demo`
- `mise.tomlのdev-emulator/seed-demoタスク`

**受入条件**

- [ ] 同じseedを再実行してもfixtureが増殖せず、IDと時刻が再現できる。
- [ ] productionの接続先ではseedを拒否し、実ユーザーの本文やtokenを含まない。
- [ ] emulatorで未確認の索引・制限はstagingで検証する旨を起動ガイドに記載する。

**検証方法**

- seedの二重実行と初期化を確認し、prod識別子の設定ではDBへ書き込まないことを確認する。

**このPRに含めないもの**

- 本番と同一の索引検証はBA-OPS-02で行う。
- 役割ごとの実認可はBA-IAM-02以降のテストで確認する。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-FND-06 PRのlint・型・契約検証CI

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Platform |
| 段階・優先度・規模 | M0・P0・S |
| 関連要件 | FR-014 |
| 依存PR | [BA-FND-01](#ba-fnd-01-モノレポと依存バージョンの固定)、[BA-FND-04](#ba-fnd-04-openapi検証とtypescriptクライアント生成) |

**目的**

PRを取り込む前に、整形・型・通信契約の破損を一貫して検出する。

**主な変更範囲**

- Python/TypeScriptのformat・lint・型検査、BA-FND-04の契約validator、既存のMarkdownリンク検証ツールをPRで実行する。文書の相対リンクと見出しリンクの破損を検出し、チケットのID・依存関係・進捗台帳の検証は既存の `check:tickets` を利用する。
- lockfileに基づく依存導入とAPI client再生成差分を確認する。
- forkの変更にも実行できる最小のread権限にし、deploy credentialや本番secretを渡さない。

**成果物・変更先（予定）**

- `.github/workflows/check.yml`
- `mise.tomlのcheck/testタスク`
- `CONTRIBUTING.mdの検証手順`

**受入条件**

- [ ] 同じ検証をローカルとCIで実行でき、失敗した工程と再現方法が分かる。
- [ ] 生成ファイルの更新漏れと壊れたスキーマがmerge前に検出される。
- [ ] 文書の存在しない相対リンク先・見出しリンク先を失敗として検出できる。
- [ ] このworkflowからクラウドの書込や有料provider呼出しが発生しない。

**検証方法**

- 型エラー、生成差分、壊れた相対リンク・見出しリンクを入れた試験PR相当で失敗を確認し、修正後に通ることを確認する。

**このPRに含めないもの**

- 署名build・deployはBA-OPS-05・BA-OPS-09・BA-OPS-10で追加する。

**外部前提**

- BA-GOV-01で確定するリポジトリへのworkflow設定権限。branch保護設定はBA-GOV-01の対象。

#### BA-FND-07 Coreの依存インターフェースとfake adapter

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Agent |
| 段階・優先度・規模 | M0・P0・M |
| 関連要件 | FR-004、FR-006 |
| 依存PR | [BA-FND-03](#ba-fnd-03-共通id時刻version型の定義) |

**目的**

AgentCoreをクラウド・端末・LLM SDKから独立させ、後続機能をfakeで並行開発できるようにする。

**主な変更範囲**

- ModelProvider、MemoryStore、ToolExecutor、SpeechProvider、Clockの入出力をPythonの型で定義する。
- 生成差分・tool要求・usage・timeout・取消を共通イベントとして表す。
- fakeで遅延、不正引数、途中停止、権限外候補、結果不明を再現可能にする。

**成果物・変更先（予定）**

- `packages/agent_core/ports`
- `packages/agent_core/types`
- `tests/fakes/model.py`
- `tests/fakes/memory_speech_tools.py`

**受入条件**

- [ ] coreの型をimportしてもFastAPI、Firebase、Pi固有モジュールが不要である。
- [ ] fakeは乱数や実時刻に依存せず、呼出し履歴と設定した失敗を再現できる。
- [ ] provider固有型を利用者側へ露出せず、同じ契約を実adapterに適用できる。

**検証方法**

- fakeを入れ替えた契約検証で通常完了・tool要求・取消・timeoutを観測する。

**このPRに含めないもの**

- Google Gen AI実adapterはBA-RUN-02、会話ループはBA-RUN-03で実装する。
- ローカルLLMの実装はMVP対象外。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-FND-08 冪等性・ETag・cursorの共通処理

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Backend |
| 段階・優先度・規模 | M0・P0・M |
| 関連要件 | FR-004、FR-007、FR-008 |
| 依存PR | [BA-FND-02](#ba-fnd-02-apiの起動設定ヘルス共通エラー)、[BA-FND-03](#ba-fnd-03-共通id時刻version型の定義)、[BA-FND-05](#ba-fnd-05-authfirestoreエミュレーターと2テナントfixture) |

**目的**

通信再送や同時編集で重複作成・無言の上書きが起きない共通処理を提供する。

**主な変更範囲**

- tenant/actor/operation/keyと正規化body hashを束縛し、24時間の冪等記録を扱う。
- ETag/If-Matchの検査と原子的なversion比較更新を提供する。
- cursorをtenant/actor/query/認可版へ束縛し、呼出元から現在の認可済みscopeを必須で受け取る。

**成果物・変更先（予定）**

- `services/api/common/idempotency.py`
- `services/api/common/versioning.py`
- `services/api/common/pagination.py`
- `tests/integration/common_mutations`

**受入条件**

- [ ] 同じキー・bodyは同じ結果に収束し、異なるbodyは409となる。
- [ ] 更新版の欠落は428、古い版は412で、競合した両要求を成功にしない。
- [ ] 再送でも現在の認可を通し、別scopeのcursorや失効後のキャッシュ結果を返さない。

**検証方法**

- 同時作成・応答喪失後の再送・body差替えをemulatorで試す。
- T-012の版競合と、tenant/actorの違うcursorの拒否を確認する。

**このPRに含めないもの**

- 各業務の状態遷移はBA-RUN-04・BA-APP-01以降で実装する。
- token発行の秘密情報を汎用レスポンスキャッシュへ入れない。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

### IAM 認証・テナント・端末権限

#### BA-IAM-01 Firebase認証とActorContextの生成

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Backend |
| 段階・優先度・規模 | M0・P0・M |
| 関連要件 | FR-001 |
| 依存PR | [BA-FND-02](#ba-fnd-02-apiの起動設定ヘルス共通エラー)、[BA-FND-03](#ba-fnd-03-共通id時刻version型の定義)、[BA-FND-05](#ba-fnd-05-authfirestoreエミュレーターと2テナントfixture) |

**目的**

利用者の本人確認と、要求ごとの信頼できるActorContextを構成する入口を作る。

**主な変更範囲**

- Firebase ID tokenの署名・audience・issuer・期限・失効を検査する。
- ユーザーUIDを本文のuser_idで上書きせず、tenantは選択値として後続の所属照合へ渡す。
- GET /users/meと直近再認証を判定する補助関数を提供し、認証tokenをログから除外する。

**成果物・変更先（予定）**

- `services/api/auth/firebase_identity.py`
- `services/api/auth/context.py`
- `services/api/routes/users.py`
- `tests/integration/authentication`

**受入条件**

- [ ] 未認証・期限切れ・別project・失効tokenは401で拒否する。
- [ ] 正常なUIDはUUID形式を要求せず受け入れる。
- [ ] 本文に別user_id/roleを入れても主体や権限が変化しない。

**検証方法**

- emulatorを使う正常系と署名検証adapterの負例で、偽署名・異なるaudience・古い再認証時刻を拒否する。
- T-027の本番でのfake認証混入も確認する。

**このPRに含めないもの**

- membershipによる認可はBA-IAM-02、端末専用tokenはBA-IAM-07で実装する。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-IAM-02 テナント・private・表示先の認可関数

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Backend |
| 段階・優先度・規模 | M0・P0・M |
| 関連要件 | FR-002、FR-006、FR-012 |
| 依存PR | [BA-IAM-01](#ba-iam-01-firebase認証とactorcontextの生成) |

**目的**

テナント管理者を含め、許可されていない個人情報や操作へ到達できない共通の認可境界を作る。

**主な変更範囲**

- 現在のmembership/device状態、resource所有者、visibility、display_policyからread/write/outputの可否を決める。
- owner/adminの管理権限とprivateの閲覧権を分け、未確認Piはshared+shared_displayに限定する。
- 検索の前段、worker、出力直前から使える認可済みscopeを返し、LLMやグラフ由来のroleを採用しない。

**成果物・変更先（予定）**

- `packages/policy/evaluator.py`
- `packages/policy/scopes.py`
- `tests/unit/policy_matrix`
- `tests/integration/tenant_isolation`

**受入条件**

- [ ] 他tenantと他人privateの取得・変更を拒否し、存在を含む応答を返さない。
- [ ] adminによる自己昇格やowner操作、未確認Piの記憶書込を許可しない。
- [ ] 権限変更後は古い認可版を再利用せず、検索と表示先の両方で検査できる。

**検証方法**

- T-001・T-002・T-020・T-029・T-033・T-034の境界を表形式のfixtureで検証する。

**このPRに含めないもの**

- SSE/WSSでの逐次再検査の組込みはBA-RUN-05・BA-DEV-03、個人sessionの発行はBA-IAM-09で行う。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-IAM-03 テナント作成・選択と初期owner・agent

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Backend |
| 段階・優先度・規模 | M0・P0・M |
| 関連要件 | FR-001、FR-004 |
| 依存PR | [BA-IAM-02](#ba-iam-02-テナントprivate表示先の認可関数)、[BA-FND-08](#ba-fnd-08-冪等性etagcursorの共通処理) |

**目的**

単独利用でもテナントを持ち、同じエージェント設定を端末間で参照する初期状態を作る。

**主な変更範囲**

- テナント作成時に本人ownerと初期agentを同じtransactionで保存する。
- 本人が所属するテナント一覧・個別取得・管理者による設定更新を実装する。
- tenant選択のたびに現在のmembershipを照合し、作成/更新に冪等性とversionを適用する。

**成果物・変更先（予定）**

- `services/api/routes/tenants.py`
- `services/api/repositories/tenants.py`
- `services/api/repositories/memberships.py`
- `tests/integration/tenant_bootstrap`

**受入条件**

- [ ] 同じ作成要求の再送でテナントや初期agentが重複しない。
- [ ] 処理途中の失敗でownerのいないテナントを残さない。
- [ ] 一覧に未所属テナントが現れず、URLのID差替えでも取得できない。

**検証方法**

- 2テナントfixtureで作成・一覧・設定更新・応答喪失時再送を実行し、T-001の越境拒否を確認する。

**このPRに含めないもの**

- 人格の会話実行への反映はBA-RUN-03、招待とrole変更はBA-IAM-04・BA-IAM-06で行う。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-IAM-04 期限付き招待コードと参加受付

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Backend |
| 段階・優先度・規模 | M1・P0・M |
| 関連要件 | FR-002 |
| 依存PR | [BA-IAM-03](#ba-iam-03-テナント作成選択と初期owneragent)、[BA-IAM-05](#ba-iam-05-独立認可台帳の先行deny確定grant) |

**目的**

管理者が発行した招待を本人が受諾し、失効後の再招待も含めて意図したテナントへ安全に参加できるようにする。

**主な変更範囲**

- owner/adminによる期限付き招待の作成と、本人による受諾を実装する。
- コードはハッシュで保存し、照合頻度・期限・一回使用を原子的に検査する。
- 招待からowner権限を付与せず、参加/再参加を同じoperation単位で冪等化する。
- BA-IAM-05の最新認可版を照合し、再参加もDB適用確認済みのgrantでのみ有効にする。

**成果物・変更先（予定）**

- `services/api/routes/invitations.py`
- `services/api/repositories/invitations.py`
- `services/api/services/accept_invitation.py`
- `tests/integration/invitations`

**受入条件**

- [ ] 期限切れ・使用済み・別コードの受諾は成功せず、コードの総当たりを制限する。
- [ ] 同時受諾でも一つの参加結果に収束し、再送で複数membershipを作らない。
- [ ] memberによる招待発行と、招待引数によるowner自己昇格を拒否する。
- [ ] 失効後は新しい招待と確定grantで再参加でき、古い受諾の再送や未確定grantで所属が復活しない。

**検証方法**

- 期限境界・二重受諾・権限不足・照合上限をfake clockとemulatorで確認する。
- 失効→再招待→受諾を実行し、台帳/DBの途中失敗と旧受諾要求の再送でも旧権限を復活させないことを確認する。

**このPRに含めないもの**

- 招待メール等の送信はMVP対象外。コード共有UIはBA-UI-11で実装する。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-IAM-05 独立認可台帳の先行deny・確定grant

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Backend |
| 段階・優先度・規模 | M1・P0・M |
| 関連要件 | FR-002、FR-003、FR-013 |
| 依存PR | [BA-IAM-02](#ba-iam-02-テナントprivate表示先の認可関数)、[BA-FND-08](#ba-fnd-08-冪等性etagcursorの共通処理) |

**目的**

DBの障害や古いbackupの復元で、剥奪した会員・端末の権限が復活しないための制御記録を提供する。

**主な変更範囲**

- operation ID・主体・認可版・deny/grant・確定状態を持つ独立台帳の型とGCS adapterを作る。
- 先行deny→DB反映→確定記録を分離し、外部書込をFirestore transaction内で実行しない。
- DB反映を確認できないgrantは有効にせず、競合や途中失敗をsuspendedと再適用候補にする。

**成果物・変更先（予定）**

- `packages/policy/control_ledger.py`
- `services/api/adapters/gcs_authz_ledger.py`
- `services/api/repositories/authz_changes.py`
- `tests/integration/authz_ledger`

**受入条件**

- [ ] 台帳書込失敗では権限変更を成功扱いせず、同じoperationで安全に再試行できる。
- [ ] 台帳成功後にDBを失敗させてもdenyが失われず、未確定grantを再生しない。
- [ ] 再適用処理が同一記録を重複適用せず、本文やtokenを台帳へ保存しない。

**検証方法**

- 書込前後の障害と競合を注入し、T-028の権限復活防止に必要な再適用結果を確認する。

**このPRに含めないもの**

- 各会員/端末APIへの適用はBA-IAM-06・BA-IAM-08、backup復元の実行はBA-OPS-07で行う。
- 実bucketとIAMの構築はBA-OPS-02に分ける。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-IAM-06 会員role変更・所属失効・owner移譲

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Backend |
| 段階・優先度・規模 | M1・P0・M |
| 関連要件 | FR-002 |
| 依存PR | [BA-IAM-03](#ba-iam-03-テナント作成選択と初期owneragent)、[BA-IAM-04](#ba-iam-04-期限付き招待コードと参加受付)、[BA-IAM-05](#ba-iam-05-独立認可台帳の先行deny確定grant) |

**目的**

会員管理を権限縮小とownerの維持を含めて実装し、管理操作が他人の個人情報や他テナントへ波及しないようにする。

**主な変更範囲**

- role更新、所属失効、owner移譲を認可台帳とversion照合を通して行う。
- owner付与/移譲はownerのみとし、adminによるowner変更・自己昇格を拒否する。
- 最後のownerを失う変更と並行する移譲を原子的に検査し、監査へ対象と結果だけを記録する。

**成果物・変更先（予定）**

- `services/api/routes/memberships.py`
- `services/api/services/membership_changes.py`
- `tests/integration/membership_management`

**受入条件**

- [ ] 二つのrole変更が競合しても最後のownerを失わない。
- [ ] 所属失効後は古いtokenでもそのtenantへ入れず、他tenantの所属は変わらない。
- [ ] 管理者へ対象ユーザーのprivate本文やグローバルアカウント削除権限を与えない。

**検証方法**

- T-033・T-034と、最後のownerの離脱・同時移譲・台帳途中失敗を確認する。

**このPRに含めないもの**

- 本人の退会受付とprivateデータのpurgeはBA-IAM-10、管理画面はBA-UI-11で実装する。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-IAM-07 端末ペアリングと専用tokenの発行・更新

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Backend |
| 段階・優先度・規模 | M1・P0・M |
| 関連要件 | FR-003 |
| 依存PR | [BA-IAM-03](#ba-iam-03-テナント作成選択と初期owneragent)、[BA-FND-08](#ba-fnd-08-冪等性etagcursorの共通処理) |

**目的**

スマホの利用者資格情報をPiへ渡さず、確認した端末だけに独立した接続資格情報を発行する。

**主な変更範囲**

- 未登録Piのpairing開始、owner/adminの確認、poll secretによる交換を実装する。
- 5分期限・コード照合失敗5回で失効・rate limitを適用し、poll secretをブラウザやログへ露出させない。
- 端末access tokenを15分で発行し、refresh credentialをハッシュ保存・ローテーションする。

**成果物・変更先（予定）**

- `services/api/routes/device_pairings.py`
- `services/api/auth/device_tokens.py`
- `services/api/repositories/device_credentials.py`
- `tests/integration/device_pairing`

**受入条件**

- [ ] 期限切れ・再利用・別端末のpoll secret・無権限の確認では登録できない。
- [ ] 発行tokenは割当tenant/deviceに束縛され、Firebaseの利用者tokenとして扱えない。
- [ ] 同一exchange/refreshの再送を識別し、有効credentialの無制限な増発や汎用キャッシュへの平文保存をしない。

**検証方法**

- T-003で期限/総当たり/二重交換、token更新の並行要求と応答喪失を確認する。
- 使用済みrefresh credential、改ざんtoken、別deviceへの差替えを拒否する。

**このPRに含めないもの**

- 端末失効APIはBA-IAM-08、Pi側の資格情報保管はBA-DEV-02で扱う。
- ペアリング画面はBA-UI-09へ分ける。

**外部前提**

- stagingでの結合にはBA-OPS-04の署名鍵管理と実行時権限が必要。PR内の検証はテスト鍵を使う。

#### BA-IAM-08 端末許可設定・資格情報失効

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Backend |
| 段階・優先度・規模 | M1・P0・M |
| 関連要件 | FR-003 |
| 依存PR | [BA-IAM-05](#ba-iam-05-独立認可台帳の先行deny確定grant)、[BA-IAM-07](#ba-iam-07-端末ペアリングと専用tokenの発行更新) |

**目的**

登録Piごとに許可する家電と機能を限定し、紛失・移譲・権限変更時にその資格情報を失効できるようにする。

**主な変更範囲**

- 端末一覧・設定更新・失効をowner/adminに限定し、entity allowlistを同一tenant内で検証する。
- 許可縮小と失効を認可台帳へ記録し、認可版と接続generationを更新する。
- 失効後のrefresh・新規操作・個人sessionを拒否する共通検査と監査を実装する。

**成果物・変更先（予定）**

- `services/api/routes/devices.py`
- `services/api/services/device_authorization.py`
- `tests/integration/device_revocation`

**受入条件**

- [ ] 他tenantの端末・家電IDを設定に混ぜられず、memberは許可を拡大できない。
- [ ] 失効commit後は既存access tokenでも新規要求とrefreshを拒否する。
- [ ] 再登録では旧credentialを使わず、待機commandが旧権限のまま実行開始できない。

**検証方法**

- T-004のAPI側失効、許可縮小中の再送、別tenantのentity指定、台帳障害を確認する。

**このPRに含めないもの**

- 開いたsocketへの通知と60秒以内の終了はBA-DEV-03・BA-DEV-04へ組み込む。
- ローカル初期化とcredential消去はBA-DEV-02・BA-DEV-09で扱う。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-IAM-09 Pi個人セッションの開始・失効・scope

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Backend |
| 段階・優先度・規模 | M4・P0・M |
| 関連要件 | FR-012 |
| 依存PR | [BA-IAM-02](#ba-iam-02-テナントprivate表示先の認可関数)、[BA-IAM-08](#ba-iam-08-端末許可設定資格情報失効) |

**目的**

本人が選択したPiにだけ、期限付きで個人情報の利用範囲を委任できるようにする。

**主な変更範囲**

- 本人スマホから対象Pi・許可scope・共有空間の確認付きでsessionを開始/終了する。
- tenant/user/deviceに束縛し、最長5分・無操作60秒をサーバー時刻で検査する。
- X-Private-Sessionを端末bearerと併せて照合し、終了・失効の正規状態をsnapshot側へ提供する。

**成果物・変更先（予定）**

- `services/api/routes/private_sessions.py`
- `services/api/services/private_sessions.py`
- `services/api/auth/private_session_context.py`
- `tests/integration/private_sessions`

**受入条件**

- [ ] session IDだけ、別Pi、別user、期限切れではprivate scopeを取得できない。
- [ ] 通常のheartbeatだけで利用者の無操作期限を無期限延長しない。
- [ ] Calendar実行確認・会員管理・export・削除受付をPiへ委任できず、終了後の遅延応答も許可しない。

**検証方法**

- T-020・T-021の期限境界、scope不足、別端末へのsession差替えをfake clockで確認する。

**このPRに含めないもの**

- session_started/snapshotの配送はBA-DEV-04、画面と音声の消去はBA-UI-10・BA-VOICE-05で実装する。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-IAM-10 退会・アカウント・テナント削除の受付

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Backend |
| 段階・優先度・規模 | M5・P0・M |
| 関連要件 | FR-013 |
| 依存PR | [BA-IAM-06](#ba-iam-06-会員role変更所属失効owner移譲)、[BA-MEM-07](#ba-mem-07-忘却受付削除台帳元出典再利用抑止)、[BA-MEM-08](#ba-mem-08-派生データと子階層のpurge-worker) |

**目的**

退会、本人アカウント削除、ownerによるテナント削除を区別して受け付け、正しい範囲だけをpurgeへ渡す。

**主な変更範囲**

- 本人再認証・所有権・最後のownerを検査し、削除範囲を正規化したoperationを作る。
- BA-MEM-07の先行削除台帳とBA-MEM-08のpurge基盤を利用し、受付後の新規参照/操作を止める。
- 本人アカウントの全所属処理と、単一tenantの管理者破棄を別のtarget resolverに分離する。

**成果物・変更先（予定）**

- `services/api/routes/operations.py`
- `services/api/services/account_deletion.py`
- `services/api/services/tenant_deletion.py`
- `tests/integration/deletion_requests`

**受入条件**

- [ ] 再認証が5分を超えた破壊的要求、他人のglobal account、他tenant削除を拒否する。
- [ ] 最後のownerは移譲またはtenant削除の選択が必要で、単純離脱で孤立tenantを残さない。
- [ ] 202は台帳とtombstoneの記録後だけ返し、再送でも同じoperationの進捗を取得できる。

**検証方法**

- T-022・T-034として削除中のexport/読取り、複数所属ユーザー、処理途中の再送、scope差替えを確認する。

**このPRに含めないもの**

- 各データ階層の物理消去実装はBA-MEM-08を再利用する。
- 削除の確認画面とストア向け導線はBA-UI-12・BA-GOV-03へ分ける。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

### RUN 会話とエージェント実行

#### BA-RUN-01 outbox・dispatcher・worker lease基盤

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Backend |
| 段階・優先度・規模 | M1・P0・M |
| 関連要件 | FR-004、FR-009、FR-014 |
| 依存PR | [BA-FND-05](#ba-fnd-05-authfirestoreエミュレーターと2テナントfixture)、[BA-FND-08](#ba-fnd-08-冪等性etagcursorの共通処理)、[BA-IAM-02](#ba-iam-02-テナントprivate表示先の認可関数) |

**目的**

APIの受付後にプロセスが停止しても、保存した仕事を回収できるようにする。以後の業務jobが共用する配送・実行の土台を作る。

**主な変更範囲**

- job repositoryと、業務データと同一transactionへpending jobを追加する関数を実装する。
- 決定的なjob IDでTasksへ投入するdispatcherと、未配送・lease期限切れjobの回収処理を作る。
- worker入口で呼出し主体・tenant・対象版を検査し、lease generation、試行上限、obsolete/failedを保存する。
- 外部呼出しをDB transaction外で行うhandler interfaceと、ローカル実行adapterを提供する。

**成果物・変更先（予定）**

- `packages/jobs/`
- `services/worker/dispatcher.py`
- `services/worker/entry.py`
- `tests/integration/test_job_recovery.py`

**受入条件**

- [ ] DB commit後のenqueue失敗から、同じjobを再投入できる。
- [ ] 同時配送でも有効なleaseは一つで、古いgenerationの結果はcommitできない。
- [ ] 権限喪失・削除済みsourceのjobは副作用を開始せず、終了理由を追跡できる。

**検証方法**

- fake queueと固定時計でenqueue直前/直後の障害・lease奪取を注入する。
- T-031の土台として、再起動後も永続jobを回収する結合試験を追加する。

**このPRに含めないもの**

- Cloud Run/Tasksの実資源構築はBA-OPS-03。
- 会話・通知・purgeのhandler本体はBA-RUN-04、BA-APP-04、BA-MEM-08で登録する。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-RUN-02 Google Gen AIのModelProvider adapter

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Agent |
| 段階・優先度・規模 | M1・P0・M |
| 関連要件 | FR-004 |
| 依存PR | [BA-FND-07](#ba-fnd-07-coreの依存インターフェースとfake-adapter) |

**目的**

AgentCoreからモデル固有のSDK型を分離し、設定したGoogle Cloudモデルで文字生成とtool提案を取得できるようにする。

**主な変更範囲**

- FNDのModelProvider契約に、入力変換・生成差分・tool候補・usage・終了理由を実装する。
- model ID、地域、timeout、最大出力量を型付き設定で受け、暗黙の別providerへの切替をしない。
- providerの429・timeout・不正な構造化応答を共通エラーへ変換し、本文と認証値をログへ出さない。

**成果物・変更先（予定）**

- `packages/agent_core/providers/google_genai.py`
- `tests/contract/test_model_provider.py`
- `docs/toolchain.md`

**受入条件**

- [ ] 同じfake SDK応答から、通常文とtool提案を区別した共通型が得られる。
- [ ] 打切り・失敗時に無限retryせず、usage不明を0と断定しない。
- [ ] SDK依存がadapter外へ漏れず、Coreの試験は実APIなしで実行できる。

**検証方法**

- fake transportでstream途中失敗、tool引数破損、usage欠落を検証する。
- 実providerの疎通は選定済みmodel/regionで上限付きの手動確認とし、結果を記録する。

**このPRに含めないもの**

- 品質・日本語の比較評価はBA-QA-03。
- 音声providerはBA-VOICE-01/02。

**外部前提**

- 実疎通にはOPEN-005で決めたmodel/region、課金上限、stagingの短命認証が必要。

#### BA-RUN-03 人格設定とテキストAgentCore

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Agent |
| 段階・優先度・規模 | M1・P0・M |
| 関連要件 | FR-004 |
| 依存PR | [BA-FND-07](#ba-fnd-07-coreの依存インターフェースとfake-adapter)、[BA-IAM-03](#ba-iam-03-テナント作成選択と初期owneragent)、[BA-RUN-02](#ba-run-02-google-gen-aiのmodelprovider-adapter) |

**目的**

同じagent設定を両クライアントから使える、文字会話の最小Coreを作る。人格設定と、実行権限の判定を混同しない。

**主な変更範囲**

- agent設定の取得と管理者による版付き更新を実装し、初期agentを利用する。
- 認可済みの会話・設定・MemoryStore出力を受け取るcontext builderを作る。
- 文字生成、時間・出力予算、終了理由をCoreへ実装し、MemoryStoreはfakeでも動かせるようにする。

**成果物・変更先（予定）**

- `packages/agent_core/core.py`
- `packages/agent_core/definition.py`
- `services/api/routes/agents.py`
- `tests/unit/test_agent_core.py`

**受入条件**

- [ ] 同じagentの版を指定したスマホ/Pi相当の呼出しで、同じ人格設定が使われる。
- [ ] memberの人格管理操作を拒否し、promptに書かれた権限拡張は採用しない。
- [ ] CoreをFastAPI/Firebase/実端末なしで起動できる。

**検証方法**

- T-005の人格共有をfake providerで確認する。
- 許可外データをcontext builderへ混ぜないことを、入力捕捉用fakeで確認する。

**このPRに含めないもの**

- 永続run・message APIはBA-RUN-04。
- tool実行はBA-RUN-06、実記憶の検索はBA-MEM-04。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-RUN-04 会話・message・turn受付と永続run実行

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Backend |
| 段階・優先度・規模 | M1・P0・M |
| 関連要件 | FR-004 |
| 依存PR | [BA-RUN-01](#ba-run-01-outboxdispatcherworker-lease基盤)、[BA-RUN-03](#ba-run-03-人格設定とテキストagentcore)、[BA-IAM-02](#ba-iam-02-テナントprivate表示先の認可関数) |

**目的**

会話と発話をAPIで受け付け、202応答後も追跡できる永続runとして実行する。まず文字入力から最終message保存までを通す。

**主な変更範囲**

- 会話作成・一覧・取得・設定更新とmessage一覧を契約どおり実装する。
- turnを認可し、入力message・queued run・pending jobを同一transactionで保存する。
- workerがCoreを呼び、現在の権限を再検査してfinal messageとcompleted/failedを保存する。
- run再配送で既存の処理と結果を照合し、APIの応答後background threadへ依存しない。

**成果物・変更先（予定）**

- `services/api/routes/conversations.py`
- `services/api/routes/runs.py`
- `services/worker/jobs/run_turn.py`
- `tests/integration/test_turn_run.py`

**受入条件**

- [ ] 同じ冪等キーのturn再送で、messageとrunが増殖しない。
- [ ] 202直後にAPIを停止してもworkerが継続し、run取得で結果を確認できる。
- [ ] private会話、別tenant、失効actorへの読取り・書込みを拒否する。

**検証方法**

- T-001/T-005/T-031として、2テナントfixtureとAPI停止・worker再配送を検証する。
- provider失敗時に、成功messageや外部実行成功を捏造しないことを確認する。

**このPRに含めないもの**

- 差分のSSE配信はBA-RUN-05。
- 音声入力と取消・確認待ちはBA-VOICE-01とBA-RUN-07。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-RUN-05 SSE差分・seq・履歴再開

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Backend |
| 段階・優先度・規模 | M1・P0・M |
| 関連要件 | FR-004、FR-014 |
| 依存PR | [BA-RUN-04](#ba-run-04-会話messageturn受付と永続run実行) |

**目的**

文字生成の途中経過を、切断・再接続後も順序どおり表示できるようにする。配信前にも現在の認可を適用する。

**主な変更範囲**

- run内の単調増加seqと確定イベントを永続化し、差分を250msまたは1KiB目安で集約する。
- 認証header付きGETへSSEを返し、Last-Event-IDから重複なく再開できるようにする。
- 24時間の履歴期限、期限後410、heartbeat、現在の会話/出力先権限の確認を実装する。

**成果物・変更先（予定）**

- `packages/agent_core/events.py`
- `services/api/routes/run_events.py`
- `services/worker/run_event_writer.py`
- `tests/integration/test_sse_replay.py`

**受入条件**

- [ ] 同じseqを再受信してもUI側が一回分として扱える契約になる。
- [ ] 失効・削除後の新しいbatchを配送せず、履歴期限後は最終状態取得へ誘導する。
- [ ] token一個ごとのDB書込みにならず、最終イベントは欠落しない。

**検証方法**

- T-023/T-032として、途中切断、seq重複、認可失効、履歴期限切れを試す。
- 固定stream入力で書込回数と最終messageの一致を確認する。

**このPRに含めないもの**

- Piへのrun割当通知はBA-DEV-04。
- 読み上げ音声の生成はBA-VOICE-02。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-RUN-06 型付きToolExecutorと実行権限の検査

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Agent |
| 段階・優先度・規模 | M2・P0・M |
| 関連要件 | FR-004、FR-011 |
| 依存PR | [BA-RUN-03](#ba-run-03-人格設定とテキストagentcore)、[BA-IAM-02](#ba-iam-02-テナントprivate表示先の認可関数)、[BA-FND-08](#ba-fnd-08-冪等性etagcursorの共通処理) |

**目的**

モデルが返すtool候補を、許可された業務操作へ変換する共通実行層を作る。モデルの引数や成功文を認可・結果の根拠にしない。

**主な変更範囲**

- 既存tool schemaを読み、name/argumentsの型・未知field・許可toolを検証する。
- ActorContext、TenantContext、確認状態、実行予算はサーバーから注入し、対象を実行直前に再認可する。
- tool_call_idとoperation/command IDを関連付け、成功・失敗・確認待ち・結果不明を共通結果型へ正規化する。
- 業務handlerの登録点を作り、外部副作用をDB transactionやmodel出力の直接転送で実行しない。

**成果物・変更先（予定）**

- `packages/tools/executor.py`
- `packages/tools/registry.py`
- `packages/tools/results.py`
- `tests/contract/test_tool_execution.py`

**受入条件**

- [ ] tenant_id注入、未知tool、禁止引数、権限変更後の実行を拒否する。
- [ ] providerが結果不明なら成功を返さず、同じ副作用を盲目的に再実行しない。
- [ ] 本文・tokenを含まない監査記録からtool_callと結果を追える。

**検証方法**

- T-018/T-029の不正toolとprompt由来の権限拡張をfake handlerで検証する。
- 受付後の応答喪失を注入し、結果不明の型と再照合経路を確認する。

**このPRに含めないもの**

- 業務handlerはBA-MEM-04、BA-APP-01、BA-CAL-03、BA-APP-06等で登録する。
- Calendar confirmをモデルtoolへ公開しない。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-RUN-07 run取消・確認checkpoint・resume

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Agent |
| 段階・優先度・規模 | M3・P0・M |
| 関連要件 | FR-004、FR-010 |
| 依存PR | [BA-RUN-04](#ba-run-04-会話messageturn受付と永続run実行)、[BA-RUN-05](#ba-run-05-sse差分seq履歴再開)、[BA-RUN-06](#ba-run-06-型付きtoolexecutorと実行権限の検査) |

**目的**

会話の取消しと確認待ちからの再開を、workerの常時待機なしで扱う。完了済みtoolを再開時に繰り返さない。

**主な変更範囲**

- run取消APIと、生成・tool開始前・batch送出前に確認する取消状態を実装する。
- run/tool_call/proposal/checkpointの関連を保存し、waiting_confirmationでworker leaseを解放する。
- 確認結果を受ける内部関数が、状態遷移とresume jobを原子的に保存するようにする。
- 再開時の認可・session・削除状態・確定済みtool結果を照合し、否認・期限切れ・取消を規定の終端へ進める。

**成果物・変更先（予定）**

- `packages/agent_core/checkpoints.py`
- `services/api/routes/run_control.py`
- `services/worker/jobs/resume_run.py`
- `tests/integration/test_run_checkpoint.py`

**受入条件**

- [ ] 確認待ちのworker要求は終了し、再起動後にcheckpointから再開できる。
- [ ] 同じ結果やresume jobが届いても、同じtoolを二重消費しない。
- [ ] 取消後の副作用開始を抑止し、実行済み操作を取消済みと表示しない。

**検証方法**

- T-007/T-037として、fake確認結果の重複、確認と取消の競合、session失効を検証する。

**このPRに含めないもの**

- Calendar固有のconfirm/reject APIと外部書込はBA-CAL-03/04。
- 端末の読み上げ停止はBA-VOICE-03/04。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-RUN-08 利用量予約・集計と同時実行上限

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Backend |
| 段階・優先度・規模 | M1・P0・M |
| 関連要件 | FR-015 |
| 依存PR | [BA-RUN-04](#ba-run-04-会話messageturn受付と永続run実行)、[BA-IAM-02](#ba-iam-02-テナントprivate表示先の認可関数) |

**目的**

利用枠を超えた同時実行や、失敗時の無制限呼出しを防ぐ。利用者と運用者が、権限に応じて使用量を確認できるようにする。

**主な変更範囲**

- tenant/user/device別の要求数・同時runと、token数・音声秒数等の単位を持つ共通の予約/精算interfaceを提供する。
- provider呼出し前の上限検査、終了時の確定、途中失敗・期限切れ予約の照合を実装する。
- usage APIを本人分とtenant総量に分け、本文を持たない集計を返す。

**成果物・変更先（予定）**

- `packages/usage/`
- `services/api/routes/usage.py`
- `services/worker/usage_reconciler.py`
- `tests/integration/test_usage_limits.py`

**受入条件**

- [ ] 同時要求でも利用枠の予約が競合制御され、超過を429として返す。
- [ ] providerの結果が不明な消費量を無条件に未使用へ戻さず、後続adapterが音声秒数を同じ契約で精算できる。
- [ ] memberが他人の明細を取得できず、監査と集計から会話本文が漏れない。

**検証方法**

- T-026として、同時run超過、provider429、worker異常終了を注入する。
- 本人・admin・別tenantによるusage取得範囲を確認する。

**このPRに含めないもの**

- 音声adapterの実装はBA-VOICE-01/02、監視表示とalertはBA-OPS-06。
- 販売価格・課金決済はMVPに含めない。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

### MEM グラフ記憶・忘却・エクスポート

#### BA-MEM-01 entity・factの保存モデルとrepository

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Backend |
| 段階・優先度・規模 | M2・P0・M |
| 関連要件 | FR-006 |
| 依存PR | [BA-FND-03](#ba-fnd-03-共通id時刻version型の定義)、[BA-FND-05](#ba-fnd-05-authfirestoreエミュレーターと2テナントfixture)、[BA-IAM-02](#ba-iam-02-テナントprivate表示先の認可関数) |

**目的**

グラフ記憶の保存形式を、権限と時間・出典を含む型として固定する。後続APIがtenantパスを取り違えないrepositoryを提供する。

**主な変更範囲**

- entity/factとsource_refs、visibility、display_policy、version、deletion_epochの型を定義する。
- 目的語ID/valueの排他、privateとshared_displayの禁止、predicate別qualifierを検証する。
- TenantContext必須のFirestore repositoryを作り、保存・取得・条件付き更新を提供する。

**成果物・変更先（予定）**

- `packages/memory/models.py`
- `packages/memory/repository.py`
- `tests/unit/test_memory_models.py`
- `tests/integration/test_memory_repository.py`

**受入条件**

- [ ] tenantなしのrepository呼出しや、別tenantへのパス指定を受け付けない。
- [ ] 不正な目的語・表示方針・型を保存前に拒否する。
- [ ] 同じlabelでも異なるIDのentityを独立して保存できる。

**検証方法**

- schema正負例と、2テナントの同じローカルIDを使った越境拒否を検証する。
- T-009の同名fixtureを作り、名前が主キーにならないことを確認する。

**このPRに含めないもの**

- entity解決APIはBA-MEM-02。
- ベクトルモデル・実索引はBA-MEM-05とBA-OPS-02。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-MEM-02 entityの登録・別名・外部ID照合API

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Backend |
| 段階・優先度・規模 | M2・P0・M |
| 関連要件 | FR-006、FR-011 |
| 依存PR | [BA-MEM-01](#ba-mem-01-entityfactの保存モデルとrepository)、[BA-FND-08](#ba-fnd-08-冪等性etagcursorの共通処理) |

**目的**

人・部屋・家電を安定したIDで参照し、同名の対象を誤って統合・操作しないようにする。

**主な変更範囲**

- entity作成・一覧・取得・更新を共通の冪等性とversion検査で実装する。
- 別名とprovider外部IDを扱い、登録Piと家電entityを別の種類として解決する。
- 曖昧なlabel検索は候補を返し、権限外の候補・別名を返さない。

**成果物・変更先（予定）**

- `services/api/routes/entities.py`
- `packages/memory/entity_resolver.py`
- `tests/integration/test_entity_resolution.py`

**受入条件**

- [ ] 同じ照明名が2部屋にある場合、1台に勝手に決定しない。
- [ ] 同じ外部IDの再登録で重複entityを増やさず、別tenantの外部IDは分離される。
- [ ] private entityの存在が共有一覧・検索候補に現れない。

**検証方法**

- T-001/T-009で、同名・別室・別tenant・privateの候補解決を確認する。

**このPRに含めないもの**

- HA機器からの実際の一覧取得と同期はBA-DEV-06。
- 人の本人認証やmembership付与には利用しない。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-MEM-03 明示記憶の保存・出典・訂正API

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Backend |
| 段階・優先度・規模 | M2・P0・M |
| 関連要件 | FR-006 |
| 依存PR | [BA-MEM-02](#ba-mem-02-entityの登録別名外部id照合api)、[BA-RUN-01](#ba-run-01-outboxdispatcherworker-lease基盤) |

**目的**

利用者が明示した記憶を、出典と共有範囲を確認できる形で保存・訂正する。索引の生成を待たず保存済みの記憶を取得可能にする。

**主な変更範囲**

- 記憶一覧・取得・保存を実装し、sourceとsubject/objectの現在の権限・存在を検査する。
- 明示保存のorigin/statusをサーバーが決め、factと索引jobを同一transactionで記録する。
- 訂正は旧factをsupersededにし、新fact・関連・jobを版チェック付きで保存する。
- 共有記憶がprivate entity/sourceを経由して情報を漏らさないよう制約を適用する。

**成果物・変更先（予定）**

- `services/api/routes/memories.py`
- `packages/memory/mutations.py`
- `tests/integration/test_explicit_memory.py`

**受入条件**

- [ ] commit前には保存成功を返さず、commit後は索引がなくてもIDで取得できる。
- [ ] 古いIf-Matchは412となり、旧内容を現行factとして返さない。
- [ ] 架空の出典IDや他人のprivate出典を採用しない。

**検証方法**

- T-008/T-010として、即時再取得、同時訂正、出典の権限差を検証する。

**このPRに含めないもの**

- model toolへの登録とcontext組込みはBA-MEM-04。
- 自動推測の確定と忘却は本PRに含めず、忘却はBA-MEM-07。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-MEM-04 scope付き2hop検索と会話への文脈組込み

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Agent |
| 段階・優先度・規模 | M2・P0・M |
| 関連要件 | FR-006 |
| 依存PR | [BA-MEM-03](#ba-mem-03-明示記憶の保存出典訂正api)、[BA-RUN-03](#ba-run-03-人格設定とテキストagentcore)、[BA-RUN-04](#ba-run-04-会話messageturn受付と永続run実行)、[BA-RUN-06](#ba-run-06-型付きtoolexecutorと実行権限の検査) |

**目的**

質問に関係する許可済みの記憶を、出典付きでAgentCoreへ渡す。関係探索の上限と、共有/個人の境界を明確にする。

**主な変更範囲**

- sharedと本人privateを検索前に分離し、Piではdisplay_policyと現在sessionを追加検査する。
- entity解決と最大2hop/40fact/20entityの展開、有効期間・status・出典版の再検査を実装する。
- MemoryStoreを会話contextへ接続し、memory.search/remember/reviseを既存mutationへ委譲するtoolとして登録する。
- 意味検索のprovider境界を設け、初版はfake vector結果でも契約検証できるようにする。

**成果物・変更先（予定）**

- `packages/memory/retrieval.py`
- `services/api/routes/memory_search.py`
- `packages/tools/memory.py`
- `tests/integration/test_scoped_retrieval.py`

**受入条件**

- [ ] 権限外の識別語が検索結果・LLM入力・traceのいずれにも現れない。
- [ ] 訂正済み、期限切れ、削除済みfactを確定した現行事実として使わない。
- [ ] 探索上限到達を追跡でき、循環した関係で無限探索しない。

**検証方法**

- T-002/T-020/T-029で、private混入とグラフ内の命令文を検証する。
- 循環・多分岐fixtureで探索数とsource付きcontextを確認する。

**このPRに含めないもの**

- 実embeddingとFirestore vector adapterはBA-MEM-05。
- 専用グラフDB・2hop超の探索は将来対応。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-MEM-05 埋め込みjobとvector検索adapter

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Agent |
| 段階・優先度・規模 | M2・P0・M |
| 関連要件 | FR-006 |
| 依存PR | [BA-MEM-04](#ba-mem-04-scope付き2hop検索と会話への文脈組込み)、[BA-RUN-01](#ba-run-01-outboxdispatcherworker-lease基盤) |

**目的**

記憶を意味検索できるようにしながら、古い埋め込みで訂正・忘却を巻き戻さないようにする。

**主な変更範囲**

- embeddingモデル・2048以下の次元・版を設定し、scope別のvector索引と検索adapterを実装する。
- 元factの版とdeletion_epochを読んでから外部で生成し、commit直前に再検査する。
- 検索時にscopeを絞り、候補の正本を再照合してBA-MEM-04へ渡す。
- モデル/次元変更時は別索引へ再生成できるjob属性とcheckpointを残す。

**成果物・変更先（予定）**

- `packages/memory/vector_store.py`
- `services/worker/jobs/embed_memory.py`
- `infra/gcp/firestore-indexes/`
- `tests/integration/test_embedding_fencing.py`

**受入条件**

- [ ] 訂正・forgetと競合した古い生成結果は保存されず、obsoleteとして終了する。
- [ ] 別tenant/privateのvectorを検索候補としてCoreへ渡さない。
- [ ] 設定した次元と索引が不一致なら、暗黙変換せず検出できる。

**検証方法**

- T-010/T-011をfake embeddingで検証する。
- stagingで対象次元・scope filter・必要索引を限定件数で確認する。

**このPRに含めないもの**

- 検索品質・原価の全体評価はBA-QA-03/04。
- 自動的なモデル切替は含めない。

**外部前提**

- 実vector確認にはBA-OPS-02のstaging DBと、OPEN-005のembeddingモデル/次元が必要。

#### BA-MEM-06 会話要約と出典・scope・版の継承

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Agent |
| 段階・優先度・規模 | M2・P1・M |
| 関連要件 | FR-004、FR-006 |
| 依存PR | [BA-MEM-03](#ba-mem-03-明示記憶の保存出典訂正api)、[BA-RUN-01](#ba-run-01-outboxdispatcherworker-lease基盤)、[BA-RUN-02](#ba-run-02-google-gen-aiのmodelprovider-adapter)、[BA-RUN-04](#ba-run-04-会話messageturn受付と永続run実行) |

**目的**

長い会話を必要な文脈へ要約し、要約によって個人情報の共有範囲や削除状態が変わらないようにする。

**主な変更範囲**

- 会話要約jobにsource message集合・版・scope・deletion_epochを記録する。
- 再利用抑止されたspan/messageを除外し、sourceが変わった場合は生成結果を破棄する。
- 要約の所有者・表示方針・保持を元会話から引き継ぎ、context builderから出典付きで取得する。

**成果物・変更先（予定）**

- `services/worker/jobs/summarize_conversation.py`
- `packages/memory/summaries.py`
- `tests/integration/test_summary_provenance.py`

**受入条件**

- [ ] private会話からshared要約やfactを自動生成しない。
- [ ] 削除・訂正と競合した要約は保存・利用されない。
- [ ] 要約から元sourceと版を特定でき、元の本文がなくても出典を捏造しない。

**検証方法**

- T-010/T-011として、生成中のsource更新・削除・再利用抑止を確認する。

**このPRに含めないもの**

- 会話からの自動fact抽出・自動確定は将来対応。
- 物理purgeはBA-MEM-08。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-MEM-07 忘却受付・削除台帳・元出典再利用抑止

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Backend |
| 段階・優先度・規模 | M2・P0・M |
| 関連要件 | FR-006、FR-013 |
| 依存PR | [BA-MEM-03](#ba-mem-03-明示記憶の保存出典訂正api)、[BA-RUN-01](#ba-run-01-outboxdispatcherworker-lease基盤)、[BA-RUN-06](#ba-run-06-型付きtoolexecutorと実行権限の検査) |

**目的**

忘却の受付直後から、対象記憶と残した会話の再利用を止める。バックアップ復元でも削除要求を失わない受付経路を作る。

**主な変更範囲**

- 認可・対象版を確認し、削除意図を独立台帳へ先行記録してからtombstoneとpurge jobをcommitする。
- suppress_reuse/delete_sourceを扱い、残すsourceの該当spanまたはmessage全体へ再利用抑止を付ける。
- 同じoperation IDで途中失敗から再構築し、model toolにも明確な利用者意図を要求する。
- query・context・要約へtombstone/抑止判定を接続し、後続export向けの判定hookも提供する。

**成果物・変更先（予定）**

- `packages/memory/forget.py`
- `packages/control/deletion_ledger.py`
- `services/api/routes/operations.py`
- `packages/tools/memory.py`

**受入条件**

- [ ] 台帳失敗は503となり、両方の永続化前に202を返さない。
- [ ] 受付後は検索とmodel contextから対象を除外し、UI履歴を残す選択と区別する。
- [ ] 他人のsource削除や、共有範囲を越えるforgetを拒否する。

**検証方法**

- T-011/T-022の基礎として、台帳前/後・DB commit前/後の障害と同じキーでの再試行を検証する。

**このPRに含めないもの**

- 物理削除と台帳の復元適用はBA-MEM-08/BA-OPS-07。
- 利用者向け忘却画面はBA-UI-04。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-MEM-08 派生データと子階層のpurge worker

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Backend |
| 段階・優先度・規模 | M2・P0・M |
| 関連要件 | FR-013 |
| 依存PR | [BA-MEM-05](#ba-mem-05-埋め込みjobとvector検索adapter)、[BA-MEM-06](#ba-mem-06-会話要約と出典scope版の継承)、[BA-MEM-07](#ba-mem-07-忘却受付削除台帳元出典再利用抑止) |

**目的**

忘却・データ削除を、原本と派生データの消去まで完了させる。中断しても再開できる削除workerを共通化する。

**主な変更範囲**

- collectionごとの削除handlerを登録し、messages等の子階層・vector・要約・job payload・通知を走査する。
- cursor、処理数、再試行理由をoperationへ保存し、途中から安全に再開する。
- 残したsourceの再利用抑止と独立台帳を維持し、cache/export等の無効化hookを呼ぶ。
- 関連対象の消去を確認してからoperationを完了にし、期限超過を監視用状態へ出す。

**成果物・変更先（予定）**

- `services/worker/jobs/purge_resource.py`
- `packages/data_lifecycle/handlers.py`
- `tests/integration/test_purge_resume.py`
- `docs/06-deployment-and-operations.md`

**受入条件**

- [ ] 親documentの削除だけで完了せず、対象の子階層と派生情報を処理する。
- [ ] 同じjobを再配送しても完了済み対象を壊さず、削除中のデータを再生成しない。
- [ ] tenant/owner範囲外の原本・共有寄稿を無条件に消さない。

**検証方法**

- T-011/T-022で、各ページ処理の途中停止と古いembedding/summary jobの再送を確認する。

**このPRに含めないもの**

- アカウント/tenant削除の入口・最終owner条件はBA-IAM-10。
- backup復元訓練はBA-OPS-07。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-MEM-09 認可付きエクスポート生成・取得・失効

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Backend |
| 段階・優先度・規模 | M5・P0・M |
| 関連要件 | FR-013 |
| 依存PR | [BA-MEM-07](#ba-mem-07-忘却受付削除台帳元出典再利用抑止)、[BA-MEM-08](#ba-mem-08-派生データと子階層のpurge-worker) |

**目的**

利用者が自分のデータを取得できるようにし、生成したファイルが権限変更や忘却の抜け道にならないようにする。

**主な変更範囲**

- 本人のexport operationから、アクセス可能なデータだけをJSON等の機械可読形式にまとめる。
- token・内部推論・他人privateを除外し、対象ID/版/削除epochのmanifestと24時間の取得期限を保存する。
- 認証付きdownloadで現在の権限とmanifestを毎回検査し、失効・forgetで既存artifactを無効化・消去する。

**成果物・変更先（予定）**

- `services/worker/jobs/export_data.py`
- `services/api/routes/operation_download.py`
- `packages/data_lifecycle/export_manifest.py`
- `tests/integration/test_export_authorization.py`

**受入条件**

- [ ] owner/adminのexportにも他人のprivate会話・記憶は含まれない。
- [ ] 生成後に退出・forgetした場合、以前のdownload URLから取得できない。
- [ ] 一時ファイルとoperationの期限を過ぎた取得を拒否する。

**検証方法**

- T-001/T-022で、生成と削除の競合、生成後の失効、別tenantのdownloadを検証する。

**このPRに含めないもの**

- スマホのdownload導線はBA-UI-12。
- 公開共有リンクによる配布は含めない。

**外部前提**

- 実ファイル取得の結合確認には、BA-OPS-02で用意する非公開の一時保管先が必要。

### APP ノート・タスク・通知・家電API

#### BA-APP-01 ノートCRUDとノートtool

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Backend |
| 段階・優先度・規模 | M3・P1・M |
| 関連要件 | FR-007 |
| 依存PR | [BA-RUN-06](#ba-run-06-型付きtoolexecutorと実行権限の検査)、[BA-FND-08](#ba-fnd-08-冪等性etagcursorの共通処理)、[BA-MEM-08](#ba-mem-08-派生データと子階層のpurge-worker) |

**目的**

ノートを画面と会話の両方から作成・管理できるようにする。同じ業務処理へ委譲し、認可や更新競合を経路ごとに重複実装しない。

**主な変更範囲**

- ノートの作成・一覧/検索・取得・版付き編集を実装する。
- private/sharedの読取りと、作成者/adminによるshared編集の規則を共通認可で適用する。
- notes toolを同じserviceへ接続し、削除は既存operation/purgeへ委譲する。

**成果物・変更先（予定）**

- `services/api/routes/notes.py`
- `packages/tools/notes.py`
- `packages/notes/service.py`
- `tests/integration/test_notes.py`

**受入条件**

- [ ] APIとtoolで同じ保存内容・source/actor記録・権限判定になる。
- [ ] 古いIf-Matchは412となり、他人privateや権限のないshared編集を拒否する。
- [ ] 削除受付後は一覧・取得・model入力から除外される。

**検証方法**

- T-001/T-012の認可と、両者に編集権がある場合の同時更新を検証する。

**このPRに含めないもの**

- 画面はBA-UI-05。
- 自由な外部文書取り込みや高度な全文検索基盤は含めない。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-APP-02 タスクの作成・一覧・更新・完了・取消とtool

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Backend |
| 段階・優先度・規模 | M3・P1・M |
| 関連要件 | FR-008 |
| 依存PR | [BA-RUN-06](#ba-run-06-型付きtoolexecutorと実行権限の検査)、[BA-FND-08](#ba-fnd-08-冪等性etagcursorの共通処理) |

**目的**

タスクの期限と完了状態を正規データとして管理し、AIの文章だけでは完了にしない。

**主な変更範囲**

- 作成・一覧・取得・編集、open/done/cancelledの遷移を実装する。
- 担当者とdue_atを同じtenant内で検査し、現在の権限とIf-Matchを必須にする。
- tasks toolをserviceへ接続し、完了・取消は明示された操作と永続結果に基づいて返す。

**成果物・変更先（予定）**

- `services/api/routes/tasks.py`
- `packages/tools/tasks.py`
- `packages/tasks/service.py`
- `tests/integration/test_tasks.py`

**受入条件**

- [ ] 期限や完了状態が再取得で一致し、保存失敗時に完了したと返さない。
- [ ] 別tenantの担当者指定と、他人privateの変更を拒否する。
- [ ] done/cancelledの再送で重複イベントを生まない。

**検証方法**

- T-012の同時更新と、toolの成功文だけでは状態が変わらない負例を検証する。

**このPRに含めないもの**

- 通知とスヌーズはBA-APP-03/04。
- タスク画面はBA-UI-06。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-APP-03 リマインダー・スヌーズ・予定版API

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Backend |
| 段階・優先度・規模 | M3・P1・M |
| 関連要件 | FR-009 |
| 依存PR | [BA-APP-02](#ba-app-02-タスクの作成一覧更新完了取消とtool)、[BA-RUN-01](#ba-run-01-outboxdispatcherworker-lease基盤) |

**目的**

指定時刻のリマインダーを管理し、スヌーズや変更後に古い通知が新しい予定を壊さないようにする。

**主な変更範囲**

- due_at・IANA timezone・通知先・scopeを検査して作成/取得/編集/取消を実装する。
- snoozeは未来日時とexpected_versionを検査し、schedule_versionを増やしてscheduledへ戻す。
- 日時変更も同じ版更新を行い、旧通知・旧job・旧ackを現行予定へ適用しない判定を共通化する。
- reminder toolを既存serviceへ登録する。

**成果物・変更先（予定）**

- `services/api/routes/reminders.py`
- `packages/tools/reminders.py`
- `packages/reminders/service.py`
- `tests/integration/test_reminder_versions.py`

**受入条件**

- [ ] 日付跨ぎ・タイムゾーンを含む入力が同じUTC時刻として保存される。
- [ ] 旧versionの更新を拒否し、取消・再予定後の旧世代イベントを無効と判定する。
- [ ] 話者未確認Piによる作成・スヌーズを拒否する。

**検証方法**

- T-038として、通知済み予定のsnooze後に旧job/ackを遅延投入する。
- 固定時計で未来日時・過去日時・DST境界を確認する。

**このPRに含めないもの**

- 実配送はBA-APP-04/05。
- 複雑な繰り返しルールは将来対応。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-APP-04 期限走査と重複しない通知job

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Backend |
| 段階・優先度・規模 | M3・P0・M |
| 関連要件 | FR-009 |
| 依存PR | [BA-APP-03](#ba-app-03-リマインダースヌーズ予定版api)、[BA-RUN-01](#ba-run-01-outboxdispatcherworker-lease基盤) |

**目的**

期限が来たリマインダーを、処理の再送があっても一つの論理通知として配送へ渡す。

**主な変更範囲**

- due_atとstatusを走査し、lease付きで対象を確保してoutboxへ配送jobを登録する。
- reminder IDとschedule_versionから通知IDを決め、通知/宛先別の処理状態を保存する。
- 配送直前に取消・版・権限・表示方針を検査し、5分超の古い通知を自動音声再生へ回さない。
- provider受付・端末表示・利用者ackを別状態として扱う。

**成果物・変更先（予定）**

- `services/worker/jobs/scan_due_reminders.py`
- `services/worker/jobs/dispatch_notification.py`
- `packages/notifications/repository.py`
- `tests/integration/test_reminder_dispatch.py`

**受入条件**

- [ ] 二重走査・job再配送でも同じ世代の通知が増殖しない。
- [ ] 取消済み・旧世代・失効主体の通知を配送しない。
- [ ] offline後の古い通知は未確認一覧として扱い、一斉読み上げ対象にしない。

**検証方法**

- T-013/T-014/T-038を固定時計とfake配送先で検証する。

**このPRに含めないもの**

- Schedulerの実資源はBA-OPS-03。
- FCMと通知APIはBA-APP-05、Piへの通知同期はBA-DEV-04、表示はBA-UI-07。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-APP-05 通知API・FCM登録・汎用push配送

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Backend |
| 段階・優先度・規模 | M3・P1・M |
| 関連要件 | FR-009 |
| 依存PR | [BA-APP-04](#ba-app-04-期限走査と重複しない通知job)、[BA-IAM-02](#ba-iam-02-テナントprivate表示先の認可関数) |

**目的**

スマホへ内容を漏らさず通知し、本人がアプリを開いたときに現在の権限で本文を取得できるようにする。

**主な変更範囲**

- 本人専用のpush installation登録・更新・解除と通知一覧・取得・ackを実装する。
- FCM payloadを通知ID・tenant ID・汎用文言に限定し、本文は認証付きAPIで返す。
- 無効tokenを停止し、再登録・所属失効・schedule_version変更後に古い配送先やackを使わない。
- private/sharedと表示先の判定を配送時と本文取得時の双方へ適用する。

**成果物・変更先（予定）**

- `services/api/routes/notifications.py`
- `services/api/routes/push_installations.py`
- `packages/notifications/fcm_adapter.py`
- `tests/integration/test_private_notification.py`

**受入条件**

- [ ] ロック画面向けpayloadに予定・記憶の本文が含まれない。
- [ ] 別ユーザーの通知取得/ackと、古い世代による新予定の完了を拒否する。
- [ ] FCM受付を利用者の既読や端末表示済みとして保存しない。

**検証方法**

- T-030/T-038として、payloadの許可field、失効後の取得、遅れたackを確認する。
- stagingの実通知は許可済みテスト端末に限定する。

**このPRに含めないもの**

- 通知画面はBA-UI-07。
- OSごとの配布・APNs実設定はBA-OPS-09/10。

**外部前提**

- 実配信にはOPEN-006のFCM/APNs設定、通知許可を与えたテスト用アプリ・端末が必要。

#### BA-APP-06 家電command APIとhome tool

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Backend |
| 段階・優先度・規模 | M2・P0・M |
| 関連要件 | FR-011 |
| 依存PR | [BA-RUN-06](#ba-run-06-型付きtoolexecutorと実行権限の検査)、[BA-MEM-02](#ba-mem-02-entityの登録別名外部id照合api)、[BA-IAM-08](#ba-iam-08-端末許可設定資格情報失効) |

**目的**

許可された照明・空調の目標状態を、追跡可能なcommandとして保存する。家電操作の受付と実行成功を分離する。

**主な変更範囲**

- home.set_stateとcommand APIを同じserviceへ接続し、対象entity・device allowlist・操作型・数値範囲を検査する。
- 初期30秒のexpires_at、actor、target、operationを持つqueued commandを冪等に保存する。
- 状態取得と、start claim前の取消しを条件付き更新として実装する。
- toggle・未知service・任意URL・危険操作はtool/入力の許可範囲に含めない。

**成果物・変更先（予定）**

- `services/api/routes/commands.py`
- `packages/tools/home.py`
- `packages/commands/service.py`
- `tests/integration/test_command_creation.py`

**受入条件**

- [ ] 同名・別tenant・許可外の家電へcommandを作成しない。
- [ ] 同じ要求の再送でcommandが増えず、保存だけではsucceededにならない。
- [ ] 実行claim後の取消しは409となり、結果不明を成功として返さない。

**検証方法**

- T-018/T-036のAPI側として、禁止引数・期限・取消しとclaim相当の競合を検証する。

**このPRに含めないもの**

- start claim/grantとPi実行はBA-DEV-05/07。
- Home Assistantとの実接続はBA-DEV-06。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

### CAL Google Calendar連携

#### BA-CAL-01 Calendar OAuth開始・callback・秘密管理

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Backend |
| 段階・優先度・規模 | M3・P0・M |
| 関連要件 | FR-010 |
| 依存PR | [BA-IAM-02](#ba-iam-02-テナントprivate表示先の認可関数)、[BA-FND-08](#ba-fnd-08-冪等性etagcursorの共通処理) |

**目的**

ユーザー単位でCalendarを接続し、別ユーザーへの取り違えや認可callbackの再利用を防ぐ。

**主な変更範囲**

- read/read_writeの要求scope、user/tenant、nonce、期限、許可redirectに束縛した認可開始を実装する。
- state/PKCEと一回性を検査し、callback後の復帰先を固定allowlistへ限定する。
- tokenはSecret Manager adapterへ保存し、DBには所有者・付与scope・secret参照だけを残す。
- queryやログからcode/state/tokenを除去し、失敗時に接続済みと扱わない。

**成果物・変更先（予定）**

- `services/api/routes/calendar_authorization.py`
- `packages/integrations/google_oauth.py`
- `packages/secrets/provider.py`
- `tests/integration/test_calendar_oauth.py`

**受入条件**

- [ ] stateの期限切れ・再利用・別userへの転用を拒否する。
- [ ] 任意redirect URLを使わず、token本文をAPI応答・Firestore・ログへ出さない。
- [ ] readのみの連携から書込scopeがあると推測しない。

**検証方法**

- fake OAuth/Secret storeでcallback差替え・再送・保存失敗を検証する。
- stagingで同意画面から復帰する最小の手動疎通を行う。

**このPRに含めないもの**

- 予定参照はBA-CAL-02、連携解除はBA-CAL-05。
- OAuth審査の待ち時間をPR実装完了と混同しない。

**外部前提**

- OPEN-006のOAuth client/redirect/テストユーザーが必要。実際のsecret値はコード・PRへ記載しない。

#### BA-CAL-02 Calendar予定一覧・時刻正規化と参照tool

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Backend |
| 段階・優先度・規模 | M3・P1・M |
| 関連要件 | FR-010 |
| 依存PR | [BA-CAL-01](#ba-cal-01-calendar-oauth開始callback秘密管理)、[BA-RUN-06](#ba-run-06-型付きtoolexecutorと実行権限の検査) |

**目的**

委任されたカレンダーの予定を、時間と版を保って参照できるようにする。私的予定を共有Piへ無条件に返さない。

**主な変更範囲**

- 本人が選択したcalendarと付与scopeを検査して、期間・ページング付きで予定一覧を取得する。
- 終日dateとtimezone付きdate-timeを区別し、provider ID・ETag・取得時刻を正規化する。
- 認証失効・権限不足・provider失敗を共通エラーへ変換し、許可された表示範囲へ返す。
- calendar.listを既存ToolExecutorへ登録し、本人scopeを注入して同じ参照serviceへ委譲する。

**成果物・変更先（予定）**

- `packages/integrations/calendar_reader.py`
- `services/api/routes/calendar_events.py`
- `tests/contract/test_calendar_time.py`
- `packages/tools/calendar.py`

**受入条件**

- [ ] 終日予定の排他的終了日と日付跨ぎを保持する。
- [ ] 他人の連携や話者未確認Piへのprivate予定返却を拒否する。
- [ ] 部分失敗や認証切れを空の予定一覧として成功扱いしない。
- [ ] 会話からcalendar.listで本人が許可した予定を参照でき、他人の連携やsessionの範囲外へ拡張できない。

**検証方法**

- 固定Calendar fixtureで終日・DST・ページ継続・ETagを確認する。
- T-020に沿って、共有Piと本人sessionで参照可否が異なることを検証する。

**このPRに含めないもの**

- 予定書込はBA-CAL-03/04。
- 他社カレンダーや複雑な系列編集は対象外。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-CAL-03 Calendar操作proposalと確認内容の固定

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Agent |
| 段階・優先度・規模 | M3・P0・M |
| 関連要件 | FR-010 |
| 依存PR | [BA-CAL-02](#ba-cal-02-calendar予定一覧時刻正規化と参照tool)、[BA-RUN-06](#ba-run-06-型付きtoolexecutorと実行権限の検査)、[BA-RUN-07](#ba-run-07-run取消確認checkpointresume) |

**目的**

予定の変更内容を利用者が確認できる固定proposalにし、モデルの提案だけでは外部を書き換えない。

**主な変更範囲**

- 作成/変更/削除の対象、時刻、calendar、ETagを正規化し、本人・args_hash・初期5分期限へ束縛する。
- calendar.proposeを登録し、会話由来ならrun/tool_call/checkpointとの関連をサーバーが記録する。
- proposal取得とrejectを実装し、否認・期限切れをrun再開用の結果として保存する。
- 参加者招待、系列一括変更、calendar.confirmのmodel tool公開を受け付けない。

**成果物・変更先（予定）**

- `packages/integrations/calendar_proposals.py`
- `services/api/routes/calendar_proposals.py`
- `packages/tools/calendar.py`
- `tests/integration/test_calendar_proposal.py`

**受入条件**

- [ ] 引数を書き換えた場合は新しいproposalを必要とする。
- [ ] end<=start、scope不足、違うcalendar/eventのETag指定を検査できる。
- [ ] proposal生成後にworkerが確認を待ち続けず、checkpointから状態を追える。

**検証方法**

- T-015/T-037として、内容改変・期限切れ・否認・別userからの取得を検証する。

**このPRに含めないもの**

- 本人confirmとprovider実行はBA-CAL-04。
- 確認画面はBA-UI-08。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-CAL-04 本人確認後のCalendar実行・照合・resume

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Backend |
| 段階・優先度・規模 | M3・P0・M |
| 関連要件 | FR-010 |
| 依存PR | [BA-CAL-03](#ba-cal-03-calendar操作proposalと確認内容の固定)、[BA-RUN-07](#ba-run-07-run取消確認checkpointresume)、[BA-RUN-01](#ba-run-01-outboxdispatcherworker-lease基盤) |

**目的**

本人が確認した予定変更だけを一度実行し、応答を失った場合は予定の現在状態から結果を照合する。

**主な変更範囲**

- 本人のuser actorに限定してproposal/hash/期限/現権限を検査し、確認状態とjobを原子的に保存する。
- 作成はoperation由来の安定event ID、変更/削除はprovider ETagで対象を固定する。
- 実行結果とcheckpointを保存してrunを再開し、失敗・競合・outcome_unknownを区別する。
- 二重confirmやjob再配送では、既存実行状態とproviderを照合してから進める。

**成果物・変更先（予定）**

- `services/api/routes/calendar_confirm.py`
- `services/worker/jobs/calendar_write.py`
- `packages/integrations/calendar_writer.py`
- `tests/integration/test_calendar_confirm.py`

**受入条件**

- [ ] Pi device actorや未確認/改変/期限切れproposalから書き込めない。
- [ ] 書込後の応答喪失でも同じ予定を盲目的に再作成しない。
- [ ] run取消・連携失効後に新たな副作用を開始せず、実行済み結果は正確に記録する。

**検証方法**

- T-015/T-016/T-037で、二重確認、ETag競合、外部完了後の通信断を検証する。

**このPRに含めないもの**

- 画面の確認カードはBA-UI-08。
- 通知付き招待や繰り返し系列の一括変更は含めない。

**外部前提**

- 実書込確認には、変更してよい専用テストcalendarとread_writeの委任が必要。

#### BA-CAL-05 Calendar連携解除・token失効・再接続案内

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Backend |
| 段階・優先度・規模 | M3・P0・M |
| 関連要件 | FR-010、FR-013 |
| 依存PR | [BA-CAL-01](#ba-cal-01-calendar-oauth開始callback秘密管理)、[BA-CAL-04](#ba-cal-04-本人確認後のcalendar実行照合resume)、[BA-MEM-08](#ba-mem-08-派生データと子階層のpurge-worker) |

**目的**

Calendar連携を解除した後の読み書きと、保存済み資格情報の再利用を止める。再接続が必要な状態を利用者へ返す。

**主な変更範囲**

- 本人の解除要求を認可し、対象integrationを停止して保留proposal/jobを無効化する。
- 資格情報と派生データを既存purge経路で消去し、provider失効の適用範囲と結果を記録する。
- 自然なtoken期限切れ/権限剥奪を再認可が必要な状態へ正規化し、無限refreshしない。
- 対象外のtenant・別利用者の連携を巻き込まないよう処理範囲を固定する。

**成果物・変更先（予定）**

- `packages/integrations/calendar_disconnect.py`
- `services/api/routes/integrations.py`
- `services/worker/jobs/purge_integration.py`
- `tests/integration/test_calendar_disconnect.py`

**受入条件**

- [ ] 解除受付後の予定取得・confirm・再送jobが新しいprovider呼出しを開始しない。
- [ ] 秘密値を残したまま解除完了としない。
- [ ] 連携停止の理由と再接続可否を、秘密を含まないAPI応答で返す。

**検証方法**

- 解除とconfirmの競合、古いjob、provider失効失敗、再登録を検証する。
- 同一利用者が複数連携を持つfixtureで、失効の影響範囲を確認する。

**このPRに含めないもの**

- 再認可UIはBA-UI-08。
- Calendar以外のprovider連携解除は将来の各adapterで追加する。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

### DEV Raspberry Piと端末通信

#### BA-DEV-01 Pi常駐サービスとloopback管理境界

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Device |
| 段階・優先度・規模 | M1・P0・M |
| 関連要件 | FR-003、FR-014 |
| 依存PR | [BA-FND-01](#ba-fnd-01-モノレポと依存バージョンの固定)、[BA-FND-03](#ba-fnd-03-共通id時刻version型の定義) |

**目的**

Piの画面を再読込みしても端末処理を維持できる、常駐サービスの最小実行基盤を作る。

**主な変更範囲**

- 専用ユーザーで動くサービスを起動し、設定検査・停止・生存確認を提供する。
- ビルド済み表示UIの配信口と、loopback限定の状態取得口を設ける。
- Host・Origin・ローカルセッションを検査し、変更系要求をCSRFから保護する。
- 資格情報はサービスだけが読める保存領域へ分離し、ブラウザーへ返さない。

**成果物・変更先（予定）**

- `services/device/app.py`
- `services/device/local_api.py`
- `services/device/config.py`
- `packaging/systemd/brainoid-agent.service`

**受入条件**

- [ ] LAN側から管理APIへ接続できず、不正なOriginの変更要求が拒否される。
- [ ] 設定不足は理由付きで起動失敗し、秘密値をログに出さない。
- [ ] UI再読込みでサービスは再起動せず、正常終了時に子処理を閉じる。

**検証方法**

- loopback・Origin・セッションの正負例と、設定不備の起動失敗を確認する。
- Linux環境でサービス起動・終了とUI再読込みを手動確認する。

**このPRに含めないもの**

- クラウド接続とtoken更新はBA-DEV-10、音声入出力はBA-VOICE-04で実装する。
- OS全体の自動構築は含めず、実機導入手順はBA-DEV-09へ分ける。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-DEV-02 SQLite設定・cursor・command台帳

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Device |
| 段階・優先度・規模 | M1・P0・M |
| 関連要件 | FR-003、FR-014 |
| 依存PR | [BA-DEV-01](#ba-dev-01-pi常駐サービスとloopback管理境界) |

**目的**

Piの再起動をまたいで設定・配送位置・処理済みコマンドを保持し、再実行を防ぐ保存層を作る。

**主な変更範囲**

- 設定、protocol版、配送cursor、command/execution IDと状態をSQLiteで管理する。
- 初回作成と前方互換のmigrationを用意し、保存はtransactionで行う。
- command台帳の初期保持7日と、安全な期限掃除を実装する。
- 音声・私的会話を通常の永続対象に含めず、秘密値は保護ファイルの参照だけを扱う。
- local reset用にtenant紐付け・cursor・待機command・資格情報参照と保護された資格情報ファイルを消去し、私的一時データの掃除を呼び出す処理を用意する。

**成果物・変更先（予定）**

- `services/device/storage.py`
- `services/device/migrations/`
- `tests/device/test_storage.py`

**受入条件**

- [ ] プロセス再起動後もcommand IDの処理済み状態とcursorを復元できる。
- [ ] 書込途中の中断で中途半端な状態を成功扱いにしない。
- [ ] 読書き不能・破損を検知した場合、新規家電操作を許可せず復旧状態を返す。
- [ ] resetを再実行しても安全に完了し、cloudとHome Assistantの旧資格情報をローカルから再取得できない。
- [ ] resetの途中中断・再起動時は、消去完了まで通信・家電操作を再開せず未登録状態へ戻る。

**検証方法**

- T-017に対応する再起動後の重複検知と、書込失敗・期限掃除を確認する。
- 仮の資格情報・tenant設定・待機commandを用意し、resetの再実行と途中中断後の再起動で旧データを再利用しないことを確認する。

**このPRに含めないもの**

- 家電の実行判断はBA-DEV-07へ分ける。
- クラウド全記憶の複製やローカルLLM用キャッシュはMVPに追加しない。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-DEV-03 WSS接続認証・generation・heartbeat

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Backend |
| 段階・優先度・規模 | M1・P0・M |
| 関連要件 | FR-003、FR-014 |
| 依存PR | [BA-IAM-07](#ba-iam-07-端末ペアリングと専用tokenの発行更新)、[BA-IAM-08](#ba-iam-08-端末許可設定資格情報失効)、[BA-RUN-01](#ba-run-01-outboxdispatcherworker-lease基盤) |

**目的**

クラウド側の端末接続を認証し、古い接続や失効端末からの要求を受け付けないWSS入口を作る。

**主な変更範囲**

- 専用device bearerとprotocolを検査してhello/hello_ackを処理する。
- connection_generationとleaseを永続化し、新接続が成立したら旧世代を排除する。
- 15秒heartbeat、失効再確認、JSON形式・64KiB上限・未知type拒否を実装する。
- 接続終了時にwatchやタスクを閉じ、端末scope限定の配送handler登録点を設ける。

**成果物・変更先（予定）**

- `services/api/device_gateway.py`
- `services/api/device_leases.py`
- `tests/integration/test_device_gateway.py`

**受入条件**

- [ ] 同一Piの二重接続では最新世代だけが新規処理を開始できる。
- [ ] 失効端末は新規接続を拒否され、オンラインの既存接続も60秒以内の停止目標を満たす。
- [ ] queryやログへtokenを残さず、別端末の状態を購読できない。

**検証方法**

- T-004・T-023・T-024に対応し、仮想時計でlease切れと世代競合を再現する。

**このPRに含めないもの**

- Pi側の接続clientはBA-DEV-10へ分ける。
- snapshot通知はBA-DEV-04、command配送とgrantはBA-DEV-05で接続する。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-DEV-04 端末snapshot・session・run割当通知

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Backend |
| 段階・優先度・規模 | M4・P0・M |
| 関連要件 | FR-012、FR-014 |
| 依存PR | [BA-DEV-03](#ba-dev-03-wss接続認証generationheartbeat)、[BA-IAM-09](#ba-iam-09-pi個人セッションの開始失効scope)、[BA-RUN-05](#ba-run-05-sse差分seq履歴再開)、[BA-DEV-10](#ba-dev-10-piのクラウド接続httpsse-bridge)、[BA-APP-05](#ba-app-05-通知apifcm登録汎用push配送)、[BA-APP-06](#ba-app-06-家電command-apiとhome-tool) |

**目的**

スマホで開始した個人セッションと出力先runをPiへ確実に知らせ、通知喪失後も現行状態へ戻せるようにする。

**主な変更範囲**

- snapshotに有効session・割当run・未完了command/notificationとcursorを含め、ページングする。
- DB確定後にsession_started・run_available・session_revokedと通知を配送する。
- Piはhello_ack後に全頁を同期し、割当generation・scope・期限を検査する。
- 通知のdisplayedと本人ackを区別し、snapshotにない失効sessionの本文を消去する。

**成果物・変更先（予定）**

- `services/api/device_snapshot.py`
- `services/api/device_notifications.py`
- `services/device/snapshot_sync.py`
- `tests/integration/test_device_snapshot.py`

**受入条件**

- [ ] frameを落として再接続しても、対象Piは現行sessionとrunを復元できる。
- [ ] 他端末向けrunや失効sessionを返さず、旧音声を自動再生しない。
- [ ] 同期中の切断後もcursorから未取得分を回収し、同一notification IDを二重表示しない。

**検証方法**

- T-021・T-030・T-035で、ページ途中の切断、再送、期限切れを検証する。

**このPRに含めないもの**

- セッションを作る認可判断はBA-IAM-09、表示用UIはBA-UI-10へ分ける。
- 予定の生成・スヌーズ処理はBA-APP-03/04を再利用する。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-DEV-05 command start claim・grantと取消競合

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Backend |
| 段階・優先度・規模 | M2・P0・M |
| 関連要件 | FR-011 |
| 依存PR | [BA-DEV-03](#ba-dev-03-wss接続認証generationheartbeat)、[BA-APP-06](#ba-app-06-家電command-apiとhome-tool) |

**目的**

家電操作の取消しと実行開始を同じ正規状態で競合させ、取消済みの操作をPiが始めないようにする。

**主な変更範囲**

- 端末scopeのcommandを監視して配送し、acceptedと結果報告を保存する。
- command_startで現在の認可・期限・session・generationを検査し、transactionでrunningへclaimする。
- execution IDに束縛したgrantとcommand_status_ackを返し、再照会を冪等にする。
- claim前の取消しだけを許可し、Piのrunning自己申告を拒否する。

**成果物・変更先（予定）**

- `services/api/device_commands.py`
- `services/api/command_claims.py`
- `tests/integration/test_command_claims.py`

**受入条件**

- [ ] 取消しが先に確定したcommandへgrantを発行しない。
- [ ] claim後の取消しは409となり、同じexecution IDの再要求で別実行を作らない。
- [ ] 旧generation、期限切れ、観測情報のない成功報告を拒否する。

**検証方法**

- T-018・T-019・T-036で取消し競合、grant応答喪失、結果ack再送を検証する。

**このPRに含めないもの**

- command作成APIとhome toolはBA-APP-06を利用する。
- Home Assistant呼出しはBA-DEV-07のみが行う。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-DEV-06 Home Assistant adapterとentity同期

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Device |
| 段階・優先度・規模 | M2・P0・M |
| 関連要件 | FR-011 |
| 依存PR | [BA-DEV-01](#ba-dev-01-pi常駐サービスとloopback管理境界)、[BA-MEM-02](#ba-mem-02-entityの登録別名外部id照合api)、[BA-IAM-08](#ba-iam-08-端末許可設定資格情報失効)、[BA-DEV-10](#ba-dev-10-piのクラウド接続httpsse-bridge) |

**目的**

Piから許可されたHome Assistantへ接続し、対象機器の識別と状態設定を限定したadapterを作る。

**主な変更範囲**

- 接続URLとtokenをPiの保護領域で設定し、会話入力から任意の接続先を選べないようにする。
- 照明・空調の対応entityと能力を取得し、外部IDをクラウドのentity同期APIへ送る。
- allowlist内のon/off・輝度・目標温度・モード設定と、状態観測を提供する。
- 不明機器、未対応service、値範囲外、接続障害を規定の結果へ変換する。

**成果物・変更先（予定）**

- `services/device/home_assistant.py`
- `services/api/home_assistant_entities.py`
- `tests/device/test_home_assistant.py`

**受入条件**

- [ ] 同名でも異なるHA entity IDを混同しない。
- [ ] 許可外service・entity・任意引数でHAを呼び出さない。
- [ ] クラウド同期にtokenを含めず、観測状態と操作受付を分けて返す。

**検証方法**

- T-009・T-018・T-019のfake HA負例と、テスト用照明1台の状態取得を確認する。

**このPRに含めないもの**

- grantと重複排除を伴う実行制御はBA-DEV-07へ分ける。
- 解錠・任意service実行・全メーカー対応は含めない。

**外部前提**

- 許可されたテスト用Home Assistantと照明または空調。tokenはGitへ保存しない。

#### BA-DEV-07 Piコマンド実行・観測・再接続照合

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Device |
| 段階・優先度・規模 | M2・P0・M |
| 関連要件 | FR-011、FR-014 |
| 依存PR | [BA-DEV-02](#ba-dev-02-sqlite設定cursorcommand台帳)、[BA-DEV-03](#ba-dev-03-wss接続認証generationheartbeat)、[BA-DEV-05](#ba-dev-05-command-start-claimgrantと取消競合)、[BA-DEV-06](#ba-dev-06-home-assistant-adapterとentity同期)、[BA-DEV-10](#ba-dev-10-piのクラウド接続httpsse-bridge) |

**目的**

Piで有効なgrantを持つコマンドを実行し、再起動や通信断時の重複実行と成功の誤表示を防ぐ。

**主な変更範囲**

- 配送されたcommandのtenant・target・期限を検査し、SQLiteへ受領とexecution IDを記録する。
- command_start_resultの有効grantを確認してから開始記録を保存し、HA adapterを呼ぶ。
- 観測結果をsucceeded/failed/outcome_unknownへ正規化し、ackまで結果報告を保持する。
- 再起動・旧接続・応答喪失では既存台帳とHA状態を照合し、新規操作として盲目的に再送しない。

**成果物・変更先（予定）**

- `services/device/command_executor.py`
- `services/device/command_reconciliation.py`
- `tests/device/test_command_executor.py`

**受入条件**

- [ ] 同じcommandとgrantの再受信でHAの副作用を繰り返さない。
- [ ] grant未確認・期限切れ・時計異常ではHA呼出しが0回になる。
- [ ] 結果不明を成功表示に変換せず、再接続後に保存済み結果を再報告できる。

**検証方法**

- T-017〜019・T-036で、開始直前/直後の中断と結果ack喪失を注入する。

**このPRに含めないもの**

- クラウド接続の再確立はBA-DEV-10、cloudのclaimはBA-DEV-05を利用する。
- 実行済み操作を元に戻す自動補償は追加しない。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-DEV-08 署名更新の検証・起動切替・rollback

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Device |
| 段階・優先度・規模 | M5・P0・M |
| 関連要件 | FR-014 |
| 依存PR | [BA-DEV-01](#ba-dev-01-pi常駐サービスとloopback管理境界)、[BA-DEV-02](#ba-dev-02-sqlite設定cursorcommand台帳) |

**目的**

Piアプリを検証済みの配布物にだけ更新し、起動失敗時に直前の動作版へ戻せるようにする。

**主な変更範囲**

- 採用した更新検証ライブラリで署名・hash・version・期限・OS/architectureを検査する。
- 版ごとのディレクトリへ展開し、検証完了後に起動先を原子的に切り替える。
- SQLite互換性と最小ヘルスを確認し、失敗時は最後の正常アプリへ戻す。
- 更新前に個人sessionと音声を終了し、進行中commandを結果不明のまま再実行しない。

**成果物・変更先（予定）**

- `services/device/updater.py`
- `packaging/device/update-manifest.schema.json`
- `tests/device/test_updater.py`

**受入条件**

- [ ] 改変asset、期限切れmetadata、非対応protocolを起動しない。
- [ ] 展開失敗・新アプリ起動失敗では旧アプリの参照を保持する。
- [ ] 秘密情報・command台帳を更新assetに含めず、更新で消失させない。

**検証方法**

- T-025をfake filesystemと失敗注入で確認し、実機電源断試験の手順を添える。

**このPRに含めないもの**

- 署名・公開pipelineはBA-OPS-08、実機の広い障害試験はBA-QA-05へ分ける。
- OS全体の自動A/B rollbackや独自暗号方式は実装しない。

**外部前提**

- 更新方式のライブラリ選定とテスト用信頼root。productionの秘密鍵は不要。

#### BA-DEV-09 Pi導入手順・kiosk起動・offline回復画面

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Device |
| 段階・優先度・規模 | M4・P1・M |
| 関連要件 | FR-003、FR-014 |
| 依存PR | [BA-DEV-01](#ba-dev-01-pi常駐サービスとloopback管理境界)、[BA-DEV-02](#ba-dev-02-sqlite設定cursorcommand台帳)、[BA-UI-01](#ba-ui-01-共有react-uiとスマホpiのentry) |

**目的**

対応Piで画面とサービスを再現して起動でき、クラウド切断時も最低限の設定・復旧案内を使えるようにする。

**主な変更範囲**

- 専用user・systemd・kiosk起動を設定する再実行可能な導入処理を用意する。
- loopbackの表示entryを起動し、network・時刻・保存容量・サービス状態を確認する。
- offline時に状態表示、音量/マイク状態、再接続・初期化案内をローカルで表示する。
- OSと周辺機器の前提、確認コマンド、アンインストール/復旧手順を記録する。
- 端末を管理する本人がローカルで確認して実行するreset導線を用意し、通信・実行・再生を止めてBA-DEV-02の消去処理を呼び、未登録状態へ戻す。

**成果物・変更先（予定）**

- `packaging/device/install.sh`
- `apps/display/recovery/`
- `docs/device-setup.md`
- `services/device/reset_command.py`

**受入条件**

- [ ] 対応OSの初期状態から、再起動後もkioskとサービスが立ち上がる。
- [ ] ネット断でも復旧画面を開け、未実装のオフラインAIを利用可能と表示しない。
- [ ] 導入を再実行してもuser・unit・設定を重複作成しない。
- [ ] 初期化・再起動後は旧資格情報、private本文、音声buffer、待機中の旧操作を再利用せず、再ペアリングを要求する。
- [ ] クラウド登録とHome Assistant資格情報の失効確認は、ローカル消去の完了とは別に表示・案内する。

**検証方法**

- 対応Piで導入2回・再起動・ネット切断を確認し、環境と結果を記録する。
- 仮のprivate表示・音声・待機commandを用意してlocal resetを実行し、再起動後の未登録状態と旧操作を再開しないことを確認する。

**このPRに含めないもの**

- 機材の幅広い対応表はBA-GOV-04、アプリ更新はBA-DEV-08へ分ける。
- 任意のLAN管理画面やWi-Fi資格情報のクラウド保存は追加しない。

**外部前提**

- Raspberry Pi 5 8GBと候補の対応OS・画面・電源・冷却。

#### BA-DEV-10 Piのクラウド接続・HTTP/SSE bridge

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Device |
| 段階・優先度・規模 | M1・P0・M |
| 関連要件 | FR-003、FR-004、FR-014 |
| 依存PR | [BA-DEV-01](#ba-dev-01-pi常駐サービスとloopback管理境界)、[BA-DEV-02](#ba-dev-02-sqlite設定cursorcommand台帳)、[BA-DEV-03](#ba-dev-03-wss接続認証generationheartbeat)、[BA-IAM-07](#ba-iam-07-端末ペアリングと専用tokenの発行更新)、[BA-RUN-05](#ba-run-05-sse差分seq履歴再開)、[BA-UI-01](#ba-ui-01-共有react-uiとスマホpiのentry) |

**目的**

Piサービスが専用資格情報でcloud API・SSE・WSSへ接続し、ブラウザーへ秘密を渡さず会話を中継できるようにする。

**主な変更範囲**

- pairingのexchange・token更新を実装し、資格情報を保護領域へ保存する。
- Authorization付きHTTP/SSE/WSS、heartbeat、1〜30秒の再試行と55分前後の再接続、cursor保持を実装する。
- ローカルUIの契約済み操作を型付きで中継し、任意URLへのproxyにはしない。
- 401は一度更新して再試行し、失効・期限切れでは接続と私的出力を停止する。

**成果物・変更先（予定）**

- `services/device/cloud_client.py`
- `services/device/device_transport.py`
- `services/device/ui_bridge.py`
- `tests/device/test_cloud_transport.py`

**受入条件**

- [ ] Piは所有者のFirebase credentialを使わず、cloud tokenがUIのレスポンス・ログへ出ない。
- [ ] 接続先APIインスタンスが変わっても認証とSSEの再開を行える。
- [ ] 曖昧なmutation失敗は同じ冪等キーで照合し、無制限retryしない。

**検証方法**

- T-004・T-023・T-024でtoken更新失敗、接続切断、旧世代の応答を再現する。

**このPRに含めないもの**

- session/run snapshotの意味づけはBA-DEV-04、家電実行はBA-DEV-07へ分ける。
- ローカルLLMへの切替や全記憶の同期は実装しない。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

### UI スマホ・ディスプレイ・Webの画面

#### BA-UI-01 共有React UIとスマホ・Piのentry

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | UI |
| 段階・優先度・規模 | M0・P0・M |
| 関連要件 | FR-004 |
| 依存PR | [BA-FND-01](#ba-fnd-01-モノレポと依存バージョンの固定)、[BA-FND-04](#ba-fnd-04-openapi検証とtypescriptクライアント生成) |

**目的**

スマホとPiで会話部品を共有しながら、端末固有機能を差し替えられるUIの最小構成を用意する。

**主な変更範囲**

- React/TypeScript/Viteの共通部品と、Capacitor・kioskのentryを作る。
- API clientとClientPlatformAdapterを注入し、fakeだけで両画面を起動できるようにする。
- 共通の状態表示・エラー・確認カードの基本部品を用意する。
- キーボード/タップ操作とスマホ/大画面の基礎レイアウトを確認する。

**成果物・変更先（予定）**

- `packages/ui/`
- `apps/mobile/`
- `apps/display/`
- `packages/ui/platform/`

**受入条件**

- [ ] 両entryを個別buildでき、共通UIを複製せず利用する。
- [ ] 端末固有APIを共通部品から直接呼ばず、fake adapterへ切替できる。
- [ ] 空・loading・error状態が識別でき、主要ボタンに操作ラベルがある。

**検証方法**

- 型検査と両buildを行い、fakeでスマホ幅・Pi幅の主要状態を手動確認する。

**このPRに含めないもの**

- 認証・会話機能はBA-UI-02/03、録音はBA-VOICE-03/04へ分ける。
- ストア署名・配布はBA-OPS-09/10で整備する。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-UI-02 ログイン・テナント選択・context切替

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Mobile |
| 段階・優先度・規模 | M1・P0・M |
| 関連要件 | FR-001 |
| 依存PR | [BA-UI-01](#ba-ui-01-共有react-uiとスマホpiのentry)、[BA-IAM-03](#ba-iam-03-テナント作成選択と初期owneragent) |

**目的**

利用者が所属テナントを明示して利用でき、切替え・サインアウトで前の個人情報を残さないようにする。

**主な変更範囲**

- Firebase認証とplatform側の資格情報管理を接続する。
- 所属一覧・作成・選択画面を実装し、API clientへ現在のcontextを渡す。
- 切替時に旧要求を中断し、cache・SSE・編集中画面をcontext単位で破棄する。
- 401/403/404とサインアウトを処理し、認可拒否から無断で別tenantへ切り替えない。

**成果物・変更先（予定）**

- `apps/mobile/auth/`
- `packages/ui/tenant-context/`
- `tests/ui/test_context_switching.*`

**受入条件**

- [ ] tenant Aの遅延応答が切替後のtenant B画面へ描画されない。
- [ ] サインアウト後に戻る操作でprivate内容を再表示しない。
- [ ] 所属のないtenant IDをURLへ入れても、取得できたような画面にならない。

**検証方法**

- T-001・T-005に対応するcontext切替、失効、遅延応答のUI確認を行う。

**このPRに含めないもの**

- 会員・role管理はBA-UI-11、Piのdevice認証はBA-DEV-10へ分ける。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-UI-03 会話画面・SSE表示・2D状態表示

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | UI |
| 段階・優先度・規模 | M1・P0・M |
| 関連要件 | FR-004、FR-005 |
| 依存PR | [BA-UI-02](#ba-ui-02-ログインテナント選択context切替)、[BA-RUN-05](#ba-run-05-sse差分seq履歴再開) |

**目的**

共通エージェントへのテキスト会話を、進捗・実行結果・再接続を区別して両画面で表示する。

**主な変更範囲**

- 個人/共有を示す会話一覧・入力・message表示を作る。
- 認証付きSSEをseq順に反映し、重複・切断・410時の最終状態再取得を扱う。
- 2Dキャラクターの待機・思考・確認待ち・発話・エラー状態を表示する。
- tool結果カードで受付・成功・失敗・結果不明を区別し、モデル文章だけで成功表示しない。

**成果物・変更先（予定）**

- `packages/ui/chat/`
- `packages/ui/character/`
- `apps/mobile/chat/`
- `apps/display/chat/`

**受入条件**

- [ ] 再接続で同じ差分を二重表示せず、completed後の最終messageと一致する。
- [ ] 個人/共有と応答先が見え、Pi用fake adapterでも表示を確認できる。
- [ ] run失敗と家電のoutcome_unknownを区別して利用者へ示す。

**検証方法**

- T-005・T-019・T-032のfakeイベント列を使い、両画面の状態と再接続を確認する。

**このPRに含めないもの**

- Pi実接続はBA-DEV-10、音声状態の操作はBA-VOICE-05へ分ける。
- 3Dキャラクターや高度なアニメーション編集機能は含めない。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-UI-04 記憶の出典・共有・訂正・忘却画面

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Mobile |
| 段階・優先度・規模 | M2・P0・M |
| 関連要件 | FR-006 |
| 依存PR | [BA-UI-02](#ba-ui-02-ログインテナント選択context切替)、[BA-MEM-03](#ba-mem-03-明示記憶の保存出典訂正api)、[BA-MEM-04](#ba-mem-04-scope付き2hop検索と会話への文脈組込み)、[BA-MEM-07](#ba-mem-07-忘却受付削除台帳元出典再利用抑止) |

**目的**

利用者がAIの記憶を根拠と公開範囲を見て訂正・忘却できる管理画面を作る。

**主な変更範囲**

- 一覧・検索・出典・有効期間・確定状態を表示する。
- 明示保存と訂正のフォームにprivate/shared・display_policyを示す。
- 忘却時に元出典を残して再利用を抑止するか、権限のある出典も削除するかを選ぶ。
- 412では再取得して差分を示し、202では受付とpurge完了を別表示する。

**成果物・変更先（予定）**

- `apps/mobile/memories/`
- `packages/ui/memory-card/`
- `tests/ui/test_memories.*`

**受入条件**

- [ ] privateとshared_displayの組合せをフォームで拒否し、API拒否も表示できる。
- [ ] 訂正後は新factを現行として表示し、旧factを現行の好みと混同しない。
- [ ] 削除受付後は対象を通常一覧から外し、失敗時に完了と表示しない。

**検証方法**

- T-008・T-010・T-011・T-012に対応する保存・競合・削除のfake UI確認を行う。

**このPRに含めないもの**

- グラフ可視化エディターと推測記憶の自動確定は含めない。
- Piは許可済みカードの参照までとし、管理画面はスマホを入口にする。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-UI-05 ノート一覧・編集・削除画面

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | UI |
| 段階・優先度・規模 | M3・P1・S |
| 関連要件 | FR-007 |
| 依存PR | [BA-UI-02](#ba-ui-02-ログインテナント選択context切替)、[BA-APP-01](#ba-app-01-ノートcrudとノートtool) |

**目的**

スマホでノートを一覧・編集し、Piでは許可された内容だけを参照できるようにする。

**主な変更範囲**

- 一覧・検索・作成・編集とprivate/sharedの表示を実装する。
- 保存前の入力を保持し、412では競合を示して無言の上書きを防ぐ。
- 削除操作は対象確認と非同期進捗を表示する。

**成果物・変更先（予定）**

- `packages/ui/notes/`
- `apps/mobile/notes/`
- `apps/display/notes/`

**受入条件**

- [ ] 一覧が現在tenant/scopeの内容だけになり、切替時に旧本文を消す。
- [ ] 競合後も利用者の編集内容が失われない。
- [ ] 共有Piの本人未確認状態では書込み操作を案内に置き換える。

**検証方法**

- 型検査とfakeによる一覧・空・競合・削除失敗・権限拒否の手動確認を行う。

**このPRに含めないもの**

- ノートAPIはBA-APP-01を使用し、共同リアルタイム編集は追加しない。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-UI-06 タスク一覧・期限・完了画面

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | UI |
| 段階・優先度・規模 | M3・P1・S |
| 関連要件 | FR-008 |
| 依存PR | [BA-UI-02](#ba-ui-02-ログインテナント選択context切替)、[BA-APP-02](#ba-app-02-タスクの作成一覧更新完了取消とtool) |

**目的**

期限と完了状態を正規のタスクデータに従って編集・確認できる画面を作る。

**主な変更範囲**

- 一覧、期限、担当、open/done/cancelledを表示する。
- 作成・編集・完了・取消をAPIへ送り、保存結果で画面を更新する。
- 日時は利用者timezoneを示し、412と権限拒否を扱う。

**成果物・変更先（予定）**

- `packages/ui/tasks/`
- `apps/mobile/tasks/`
- `apps/display/tasks/`

**受入条件**

- [ ] AIの成功文だけではタスク表示がdoneへ変わらない。
- [ ] 本人未確認のPiで更新できず、スマホまたは個人sessionの案内が出る。
- [ ] 失敗・競合時に未保存の状態を完了済みとして残さない。

**検証方法**

- T-012とAC-008に対応し、API成功/失敗/競合のfake状態を確認する。

**このPRに含めないもの**

- タスクAPIはBA-APP-02、リマインダー画面はBA-UI-07へ分ける。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-UI-07 リマインダー・通知・スヌーズ画面

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | UI |
| 段階・優先度・規模 | M3・P0・M |
| 関連要件 | FR-009 |
| 依存PR | [BA-UI-02](#ba-ui-02-ログインテナント選択context切替)、[BA-APP-03](#ba-app-03-リマインダースヌーズ予定版api)、[BA-APP-05](#ba-app-05-通知apifcm登録汎用push配送) |

**目的**

リマインダーを設定・確認・スヌーズでき、古い通知やロック画面から個人情報を露出させないようにする。

**主な変更範囲**

- 日時・timezone・配信先を明示するリマインダー作成・取消・スヌーズを作る。
- ネイティブ通知許可とinstallation登録を接続し、拒否時はアプリ内一覧を使う。
- pushから開く際は現在の認証で通知本文を取得し、tenantと予定版を検査する。
- 配信受付・表示・本人確認を別の表示状態として扱う。

**成果物・変更先（予定）**

- `packages/ui/reminders/`
- `apps/mobile/notifications/`
- `apps/mobile/platform/push.ts`

**受入条件**

- [ ] pushにprivate本文を含めず、失効した通知を開いても本文を復元しない。
- [ ] スヌーズ後の旧通知を、新しい予定の完了として表示しない。
- [ ] 通知権限を拒否しても作成と未確認一覧が利用できる。

**検証方法**

- T-013・T-014・T-030・T-038をfakeとOS通知許可の実機確認で検証する。

**このPRに含めないもの**

- FCM配送サーバーはBA-APP-05、Pi通知の配送はBA-DEV-04を利用する。
- 繰り返し予定・OSでの厳密な表示時刻保証は追加しない。

**外部前提**

- Android/iOSのテスト端末、FCM/APNsの開発用設定。秘密鍵はcommitしない。

#### BA-UI-08 Calendar連携・予定・確認カード

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Mobile |
| 段階・優先度・規模 | M3・P0・M |
| 関連要件 | FR-010 |
| 依存PR | [BA-UI-02](#ba-ui-02-ログインテナント選択context切替)、[BA-CAL-02](#ba-cal-02-calendar予定一覧時刻正規化と参照tool)、[BA-CAL-03](#ba-cal-03-calendar操作proposalと確認内容の固定)、[BA-CAL-04](#ba-cal-04-本人確認後のcalendar実行照合resume)、[BA-CAL-05](#ba-cal-05-calendar連携解除token失効再接続案内) |

**目的**

本人のカレンダー連携と、内容を確認してから書き込む体験をスマホで完成させる。

**主な変更範囲**

- システムブラウザーでOAuthを開始し、許可済みcallbackから連携状態を再取得する。
- 選択calendarの直近予定を一覧表示し、終日・日付・timezoneを明示する。
- 固定されたproposal内容とargs_hashを使い、本人の確認/拒否を送る。
- 期限切れ・provider競合・結果不明・連携解除を表示する。

**成果物・変更先（予定）**

- `apps/mobile/calendar/`
- `packages/ui/calendar-proposal/`
- `apps/mobile/platform/oauth.ts`

**受入条件**

- [ ] 表示後に変更した内容は別proposalとなり、以前の確認を流用しない。
- [ ] 二重tapでも同じproposalを別の予定として作成しない。
- [ ] Piから書込確認を代行させず、確認先は本人スマホに限定する。

**検証方法**

- T-015・T-016・T-037に対応し、fakeで期限・競合・二重確認を再現する。

**このPRに含めないもの**

- Calendar workerと結果照合はBA-CAL-04、繰り返し系列・招待送信は含めない。

**外部前提**

- 開発用OAuth clientと許可callback、テストcalendar。審査待ちは実装PRと区別する。

#### BA-UI-09 Piペアリング・許可設定・失効画面

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Mobile |
| 段階・優先度・規模 | M1・P0・M |
| 関連要件 | FR-003 |
| 依存PR | [BA-UI-02](#ba-ui-02-ログインテナント選択context切替)、[BA-IAM-07](#ba-iam-07-端末ペアリングと専用tokenの発行更新)、[BA-IAM-08](#ba-iam-08-端末許可設定資格情報失効) |

**目的**

スマホからPiの所属・共有範囲・操作対象を確認して登録し、失効できる管理画面を作る。

**主な変更範囲**

- QRまたは手入力のcodeを使い、選択tenantとPi情報を確認して承認する。
- 端末名・状態・能力・許可機器を表示し、owner/adminの管理操作を接続する。
- 期限切れ・再使用・別tenant・失効結果を利用者に示す。
- 失効のクラウド反映と、offline端末の物理消去を別の状態として説明する。

**成果物・変更先（予定）**

- `apps/mobile/devices/`
- `packages/ui/device-card/`
- `tests/ui/test_pairing.*`

**受入条件**

- [ ] 登録先tenantを確認せずにpairingを完了できない。
- [ ] コード再使用・期限切れを成功と表示しない。
- [ ] memberに管理操作を見せず、APIで拒否された場合も設定済みと表示しない。

**検証方法**

- T-003・T-004のfake応答を確認し、Piとの一度のpairingを手動確認する。

**このPRに含めないもの**

- 端末資格情報の発行はBA-IAM-07、失効処理はBA-IAM-08を利用する。
- スマホへPiのrefresh credentialを取得・保存しない。

**外部前提**

- 登録用のテストtenantと、BA-DEV-10が動くPiまたは同等のdevice service。

#### BA-UI-10 Pi個人sessionと会話引継ぎUI

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | UI |
| 段階・優先度・規模 | M4・P0・M |
| 関連要件 | FR-012 |
| 依存PR | [BA-UI-03](#ba-ui-03-会話画面sse表示2d状態表示)、[BA-UI-09](#ba-ui-09-piペアリング許可設定失効画面)、[BA-IAM-09](#ba-iam-09-pi個人セッションの開始失効scope)、[BA-DEV-04](#ba-dev-04-端末snapshotsessionrun割当通知) |

**目的**

個人情報をPiで表示・発話する範囲を本人が明示し、終了・期限切れで両画面から消せるようにする。

**主な変更範囲**

- スマホで対象Pi・共有空間への露出・scopeを確認して個人sessionを開始する。
- 両画面に個人/共有状態、終了操作、最大5分・無操作60秒の初期期限、発話先を表示する。
- run出力先を明示的に割り当て、Piが最新snapshotとgenerationで表示を判断する。
- 終了・失効・idle期限で表示、再生queue、一時本文、遅延応答を破棄する。

**成果物・変更先（予定）**

- `apps/mobile/device-session/`
- `apps/display/session/`
- `packages/ui/output-target/`

**受入条件**

- [ ] 本人未確認Piへprivate会話を暗黙に引き継がない。
- [ ] session終了後に届いたTTS/検索応答を表示・再生しない。
- [ ] 切断してもPiが既知の期限で停止し、再接続後に旧音声を一斉再生しない。

**検証方法**

- T-020・T-021・T-035を仮想時計と遅延応答で確認する。

**このPRに含めないもの**

- sessionの認可と作成はBA-IAM-09、配送はBA-DEV-04を使用する。
- 声紋認証や無操作でのsession自動延長は実装しない。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-UI-11 会員・招待・role・利用量の管理画面

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Mobile |
| 段階・優先度・規模 | M3・P0・M |
| 関連要件 | FR-002、FR-015 |
| 依存PR | [BA-UI-02](#ba-ui-02-ログインテナント選択context切替)、[BA-IAM-04](#ba-iam-04-期限付き招待コードと参加受付)、[BA-IAM-06](#ba-iam-06-会員role変更所属失効owner移譲)、[BA-RUN-08](#ba-run-08-利用量予約集計と同時実行上限) |

**目的**

会員と利用量を管理できる画面を作り、管理権限と個人情報の閲覧権限を混同させない。

**主な変更範囲**

- 招待コード、会員一覧、role変更、owner移譲を現在の権限に応じて表示する。
- 最後のownerに移譲またはtenant削除の案内を出し、本人の削除と区別する。
- memberは本人分、owner/adminは許可されたtenant集計の利用量を表示する。
- 管理操作の確認、競合、失効後の画面更新を実装する。

**成果物・変更先（予定）**

- `apps/mobile/tenant-settings/`
- `packages/ui/member-list/`
- `packages/ui/usage-card/`

**受入条件**

- [ ] adminが自己昇格・owner移譲できるUIにならず、サーバー拒否も扱える。
- [ ] 会員管理から他人のprivate会話や他tenantへ遷移できない。
- [ ] role変更後に旧権限のボタンや取得済み画面を使い続けない。

**検証方法**

- T-002・T-033・T-034に対応するrole別fixtureと利用量上限表示を確認する。

**このPRに含めないもの**

- 権限変更の台帳処理はBA-IAM-05/06、利用量計算はBA-RUN-08を利用する。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-UI-12 エクスポート・退会・削除の利用者導線

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Mobile |
| 段階・優先度・規模 | M5・P0・M |
| 関連要件 | FR-013 |
| 依存PR | [BA-UI-02](#ba-ui-02-ログインテナント選択context切替)、[BA-MEM-09](#ba-mem-09-認可付きエクスポート生成取得失効)、[BA-IAM-10](#ba-iam-10-退会アカウントテナント削除の受付) |

**目的**

本人データのエクスポートと削除を、範囲・再認証・進捗が分かるスマホの導線として提供する。

**主な変更範囲**

- exportの要求・進捗・期限付き認可downloadを接続する。
- 会話/記憶/退出/アカウント/tenantの違いを示し、対象を確認して削除を要求する。
- 必要な再認証と最後のownerの移譲条件を案内する。
- 202の受付とpurge完了を分け、失効後は既存downloadや私的表示を閉じる。

**成果物・変更先（予定）**

- `apps/mobile/data-controls/`
- `packages/ui/operation-status/`
- `tests/ui/test_data_controls.*`

**受入条件**

- [ ] 他人のアカウントや他tenantを削除する導線を作らない。
- [ ] 削除受付が失敗した場合に消去済みと表示しない。
- [ ] export生成後の失効・忘却でdownloadが拒否され、古い公開URLを保持しない。

**検証方法**

- T-022・T-034をfake operationと失効応答で確認する。

**このPRに含めないもの**

- purge/外部秘密の破棄はBA-IAM-10・BA-MEM-08、生成処理はBA-MEM-09を利用する。
- 利用者向け保持説明の文章確定はBA-GOV-03へ分ける。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-UI-13 Webからのアカウント削除申請導線

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | UI |
| 段階・優先度・規模 | M5・P0・S |
| 関連要件 | FR-013 |
| 依存PR | [BA-UI-02](#ba-ui-02-ログインテナント選択context切替)、[BA-UI-12](#ba-ui-12-エクスポート退会削除の利用者導線)、[BA-IAM-10](#ba-iam-10-退会アカウントテナント削除の受付) |

**目的**

アプリを削除済みの利用者も、通常のブラウザーから自分のアカウント削除を申請・確認できるようにする。

**主な変更範囲**

- Web entryで既存の認証UI・データ削除フォーム・operation表示を再利用する。
- ブラウザー用認証adapterを接続し、本人の再認証と対象確認を経て既存APIへ申請する。
- 未ログインの案内、受付/進行/失敗/完了を表示し、認証失効後は私的状態を消す。
- BA-GOV-03の公開説明から接続する削除URLと設定項目を文書化する。

**成果物・変更先（予定）**

- `apps/web-data-controls/`
- `docs/public-web-contract.md`
- `tests/ui/test_web_account_delete.*`

**受入条件**

- [ ] アプリ未導入のブラウザーでログインから本人の削除申請まで進められる。
- [ ] 再認証・対象確認を省略せず、別人やtenant削除へ申請を置き換えられない。
- [ ] 同じ本人の有効な認証中は再読込みでoperation進捗を取得でき、202を消去完了と表示しない。

**検証方法**

- T-022・T-034に対応し、ブラウザーで未ログイン・再認証・再読込み・失効を確認する。

**このPRに含めないもの**

- 削除処理はBA-IAM-10、スマホ画面はBA-UI-12を再利用する。
- メール送信代行や管理者による本人確認の迂回、独立した削除システムは追加しない。

**外部前提**

- 公開HTTPS URL、認証の許可domain、BA-GOV-03で確定する説明ページへの配置。

### VOICE 音声入力・合成・停止

#### BA-VOICE-01 音声upload・STT・transcript参照

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Backend |
| 段階・優先度・規模 | M4・P0・M |
| 関連要件 | FR-005 |
| 依存PR | [BA-RUN-04](#ba-run-04-会話messageturn受付と永続run実行)、[BA-IAM-02](#ba-iam-02-テナントprivate表示先の認可関数)、[BA-RUN-08](#ba-run-08-利用量予約集計と同時実行上限) |

**目的**

上限と認可を守って1発話を文字へ変換し、通常の会話runへ渡せる参照を発行する。

**主な変更範囲**

- 16kHz・16bit・mono PCM WAV、60秒/5MiBの形式と上限を実データから検証する。
- tenant/actor/sessionの認可と利用量制御を適用してSTT adapterを呼ぶ。
- 音声は永続保存せず、transcript参照を初期15分で失効させる。
- 無音・不正形式・provider失敗・取消を共通エラーへ変換する。

**成果物・変更先（予定）**

- `services/api/audio_inputs.py`
- `packages/speech/stt_google.py`
- `tests/integration/test_audio_inputs.py`

**受入条件**

- [ ] 他actor/sessionのaudio_input_idと期限切れ参照をturnに使えない。
- [ ] 上限超過や不正WAVではproviderを呼ばない。
- [ ] DB・通常ログ・一時永続ファイルへ原音声を残さない。

**検証方法**

- T-006と音声形式の正負例をfake providerで確認し、日本語の短い音声をstagingで1回照合する。

**このPRに含めないもの**

- 端末録音はBA-VOICE-03/04、リアルタイム音声・常時録音は含めない。

**外部前提**

- OPEN-005で固定したSTT設定と、上限付きの開発用provider認証。

#### BA-VOICE-02 文単位TTSと認可付き音声取得

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Backend |
| 段階・優先度・規模 | M4・P0・M |
| 関連要件 | FR-005、FR-012 |
| 依存PR | [BA-RUN-05](#ba-run-05-sse差分seq履歴再開)、[BA-IAM-09](#ba-iam-09-pi個人セッションの開始失効scope)、[BA-RUN-08](#ba-run-08-利用量予約集計と同時実行上限) |

**目的**

確定した応答文を順次読み上げられるようにし、取得時にも表示先と個人sessionの許可を検査する。

**主な変更範囲**

- 確定文ごとにspeech_outputs参照を作り、speech.readyイベントを永続化する。
- 認可付きGETでrun/session/display_policyと期限を再検査してTTSを合成する。
- 参照の初期5分期限と合成要求のrate/利用量制限を適用する。
- 合成バイトは永続化せず、run取消・session失効後の遅い結果を返さない。

**成果物・変更先（予定）**

- `services/api/speech_outputs.py`
- `packages/speech/tts_google.py`
- `services/worker/speech_references.py`

**受入条件**

- [ ] 最終応答全体を待たずに最初の確定文を取得できる。
- [ ] private session終了後は、既知のspeech IDでも音声を取得できない。
- [ ] 再取得を無制限に課金させず、失敗を再生可能な成功結果として返さない。

**検証方法**

- T-007・T-021で失効競合と遅いprovider応答を検証し、順序付き2文を確認する。

**このPRに含めないもの**

- クライアントの音声queueと停止はBA-VOICE-03/04/05へ分ける。
- 全音声の長期保存や任意文章を合成する汎用endpointは追加しない。

**外部前提**

- OPEN-005で固定したTTS音声と開発用provider設定。

#### BA-VOICE-03 スマホ録音・PCM変換・再生adapter

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Mobile |
| 段階・優先度・規模 | M4・P0・M |
| 関連要件 | FR-005 |
| 依存PR | [BA-UI-01](#ba-ui-01-共有react-uiとスマホpiのentry)、[BA-VOICE-01](#ba-voice-01-音声uploadstttranscript参照)、[BA-VOICE-02](#ba-voice-02-文単位ttsと認可付き音声取得) |

**目的**

Android/iOSで利用者のタップを起点に録音・送信・読み上げを行う、共通の音声adapterを作る。

**主な変更範囲**

- 選定したCapacitor pluginでマイク許可と録音開始/停止/破棄を実装する。
- 収録形式を契約のPCM WAVへ変換し、上限到達で確実に停止する。
- 文単位音声を順序付きqueueで再生し、停止・背景移行・session終了で破棄する。
- 原音声を永続storageへ残さず、音響機器やOS割込みのエラーを共通状態へ返す。

**成果物・変更先（予定）**

- `apps/mobile/platform/audio.ts`
- `apps/mobile/native-audio-config/`
- `tests/mobile/audio-fixtures/`

**受入条件**

- [ ] マイク拒否後も文字入力を続けられ、再許可の案内がある。
- [ ] 録音破棄時にuploadが0件となり、停止後の古い音声が再生されない。
- [ ] Android/iOSで出力WAVの形式と最大長が契約内に収まる。

**検証方法**

- T-006・T-007を両OSのテスト端末で確認し、変換fixtureのヘッダー・長さを検査する。

**このPRに含めないもの**

- 画面全体の音声状態はBA-VOICE-05、ストア配布設定はBA-OPS-09/10へ分ける。
- バックグラウンド常時録音やウェイクワードは含めない。

**外部前提**

- 対応OS候補のAndroid/iOS端末、採用pluginと録音権限設定。

#### BA-VOICE-04 Pi録音・再生queue・停止adapter

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Device |
| 段階・優先度・規模 | M4・P0・M |
| 関連要件 | FR-005 |
| 依存PR | [BA-DEV-01](#ba-dev-01-pi常駐サービスとloopback管理境界)、[BA-DEV-02](#ba-dev-02-sqlite設定cursorcommand台帳)、[BA-VOICE-01](#ba-voice-01-音声uploadstttranscript参照)、[BA-VOICE-02](#ba-voice-02-文単位ttsと認可付き音声取得) |

**目的**

Piサービスがマイクとスピーカーを所有し、UI再読込みやsession終了でも録音・再生を制御できるようにする。

**主な変更範囲**

- 選定した音声backendで入力/出力deviceを列挙し、設定した機器を開く。
- 契約形式のWAVを上限付きで収録し、送信と破棄を区別する。
- run/session/generation付きの再生queueを作り、停止・期限切れ・接続失効で空にする。
- 無音、device抜去、再生失敗をUIへ通知し、原音声を永続化しない。

**成果物・変更先（予定）**

- `services/device/audio_capture.py`
- `services/device/audio_playback.py`
- `tests/device/test_audio_queue.py`

**受入条件**

- [ ] マイク停止後に入力を取り込み続けず、送信破棄ができる。
- [ ] 古いsessionやrunの音声を、新しい個人sessionへ持ち越さない。
- [ ] 音響機器が外れてもサービス全体が無限待機にならない。

**検証方法**

- T-006・T-007・T-021をfake音声backendで確認し、基準Piで短い録音/再生を行う。

**このPRに含めないもの**

- UI状態機械はBA-VOICE-05、機器の対応範囲評価はBA-GOV-04へ分ける。
- 音声本人認証やローカルSTT/TTSモデルは追加しない。

**外部前提**

- Raspberry Pi 5 8GB、候補マイク・スピーカー、選定したOS音声backend。

#### BA-VOICE-05 音声状態と停止・取消・権限拒否のUI

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | UI |
| 段階・優先度・規模 | M4・P0・M |
| 関連要件 | FR-005、FR-012 |
| 依存PR | [BA-UI-03](#ba-ui-03-会話画面sse表示2d状態表示)、[BA-UI-10](#ba-ui-10-pi個人sessionと会話引継ぎui)、[BA-VOICE-03](#ba-voice-03-スマホ録音pcm変換再生adapter)、[BA-VOICE-04](#ba-voice-04-pi録音再生queue停止adapter)、[BA-RUN-07](#ba-run-07-run取消確認checkpointresume) |

**目的**

録音・認識・生成・確認・再生の状態と、それぞれの停止操作を両画面で誤解なく扱えるようにする。

**主な変更範囲**

- 録音をSTTへ送り、audio_input_idとoutput_mode=speechでturnを作り、speech.readyを再生adapterへ渡す。
- Idle/Listening/Transcribing/Thinking/Confirming/Speaking/Errorを接続する。
- offline・muted・private_sessionを独立表示し、音声状態から隠さない。
- 録音送信/破棄、読み上げ停止、run取消、未実行操作の取消を区別する。
- マイク拒否、無音、provider失敗と、期限後の遅延結果を処理する。

**成果物・変更先（予定）**

- `packages/ui/voice-state/`
- `packages/ui/voice-controls/`
- `apps/display/voice/`
- `tests/ui/test_voice_lifecycle.*`

**受入条件**

- [ ] 読み上げ停止で家電操作も取り消されたと表示しない。
- [ ] 停止・失効したgenerationの応答が到着しても、音声やprivate画面を復活させない。
- [ ] 音声が使えなくても文字入力と復旧案内を操作できる。

**検証方法**

- T-006・T-007・T-021のfake状態遷移を確認し、スマホ/Piで停止操作を実機確認する。

**このPRに含めないもの**

- STT/TTS adapterはBA-VOICE-01〜04、Pi transportはBA-DEV-10を使用し、リアルタイム音声への切替は含めない。

**外部前提**

- スマホとPiの最小音声adapterが利用できるテスト環境。

### OPS GCP・配布・復旧・運用

#### BA-OPS-01 Terraform state・project分離・基盤IAM

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Platform |
| 段階・優先度・規模 | M0・P0・M |
| 関連要件 | FR-015 |
| 依存PR | [BA-FND-01](#ba-fnd-01-モノレポと依存バージョンの固定) |

**目的**

環境ごとのGCP資源を再現可能にし、stagingとproductionを誤って同じ権限・stateで操作しない基盤を用意する。

**主な変更範囲**

- Terraform state保存先と環境別backend設定、project IDの必須入力を用意する。
- API有効化・Artifact Registry・実行/deploy主体の基本定義を作り、業務サービスは後続moduleに委ねる。
- 既存projectへ適用する手順とimport方針を記録し、秘密値をstateに入れない。

**成果物・変更先（予定）**

- `infra/gcp/bootstrap`
- `infra/gcp/environments`
- `docs/06-deployment-and-operations.md`

**受入条件**

- [ ] stagingとproductionのplanが別project/stateを参照し、環境名とproject IDの不整合を拒否する。
- [ ] runtimeへOwner/Editorを付与せず、planに想定外の削除がない。
- [ ] 新規環境のbootstrapと既存環境への再実行手順をレビューできる。

**検証方法**

- terraform fmt/validateと、テスト用projectのplan差分を確認する。
- 環境取り違え入力と不足変数を負例として検証する。

**このPRに含めないもの**

- Cloud Run等の業務サービスはBA-OPS-03。
- Firestore・制御bucketはBA-OPS-02。

**外部前提**

- 課金先・project管理権限・region選定。実資源へのapplyは環境の運用権限に従う。

#### BA-OPS-02 Firestore・索引・backup・制御bucket

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Platform |
| 段階・優先度・規模 | M1・P0・M |
| 関連要件 | FR-006、FR-013、FR-015 |
| 依存PR | [BA-OPS-01](#ba-ops-01-terraform-stateproject分離基盤iam)、[BA-FND-03](#ba-fnd-03-共通id時刻version型の定義) |

**目的**

データと制御台帳の保管先を用意し、クライアントからの直接アクセスや復元による忘却要求の巻戻りを防ぐ。

**主な変更範囲**

- Firestore locationを必須入力として固定し、初期クエリの索引とclient access拒否を定義する。
- 日次backupと、DB復元から独立した削除/認可台帳bucketを定義する。
- 台帳の追記・読取・lifecycle管理のidentityを分け、backup期間と台帳保持の整合を検査する。

**成果物・変更先（予定）**

- `infra/gcp/modules/data`
- `infra/gcp/firestore.indexes.json`
- `infra/gcp/firestore.rules`

**受入条件**

- [ ] location未指定では作成計画へ進まない。
- [ ] 台帳保持が復元可能期間以下になる設定を拒否し、初期7日/30日の案を設定として表せる。
- [ ] client accessは拒否され、runtime/restore主体は用途に必要な資源だけにアクセスできる。

**検証方法**

- Terraform検証と、stagingでclient拒否・台帳write/readの正負アクセスを確認する。
- 索引追加が必要な後続PRから同じ定義を更新できることを確認する。

**このPRに含めないもの**

- forget業務処理はBA-MEM-07/08、認可台帳の業務処理はBA-IAM-05。
- 復元の実行フローはBA-OPS-07。

**外部前提**

- Firestore作成前のlocation合意、stagingの課金・IAM権限。

#### BA-OPS-03 Cloud Run・Tasks・Schedulerの配備定義

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Platform |
| 段階・優先度・規模 | M1・P0・M |
| 関連要件 | FR-004、FR-009、FR-015 |
| 依存PR | [BA-OPS-01](#ba-ops-01-terraform-stateproject分離基盤iam)、[BA-FND-02](#ba-fnd-02-apiの起動設定ヘルス共通エラー)、[BA-RUN-01](#ba-run-01-outboxdispatcherworker-lease基盤) |

**目的**

API・worker・配送キューを同じ構成から配備し、公開入口と内部workerの認証境界を固定する。

**主な変更範囲**

- Cloud Run API/worker、会話系と保守系のqueue、期限走査Schedulerを定義する。
- Task/Scheduler用の呼出しidentity・OIDC audience・Invoker権限を対象workerへ限定する。
- timeout、同時実行、instance/dispatch上限を設定化し、workerへの未認証呼出しを閉じる。

**成果物・変更先（予定）**

- `infra/gcp/modules/runtime`
- `infra/gcp/modules/queues`
- `infra/gcp/modules/scheduler`

**受入条件**

- [ ] APIにはアプリ認証の入口があり、workerの直接未認証要求は拒否される。
- [ ] 正しいinvokerからのテストjobだけが実行され、再配送でも同じjobが二重確定しない。
- [ ] queue停止と再開、timeout、上限の設定差分がplanで確認できる。

**検証方法**

- stagingで正しい/誤ったinvokerの要求を確認する。
- fake jobの重複・queue停止再開をBA-RUN-01と結合し、T-023/026の関連条件を確認する。

**このPRに含めないもの**

- 会話処理本体はBA-RUN-04。
- 本番昇格CIはBA-OPS-05。

**外部前提**

- staging project、runtime image、確定したregionとサービスURL。

#### BA-OPS-04 Secretとprovider設定・アクセス権

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Platform |
| 段階・優先度・規模 | M1・P0・S |
| 関連要件 | FR-010、FR-015 |
| 依存PR | [BA-OPS-01](#ba-ops-01-terraform-stateproject分離基盤iam)、[BA-RUN-02](#ba-run-02-google-gen-aiのmodelprovider-adapter) |

**目的**

モデル・OAuth・通知の秘密値をコードやTerraform stateから分離し、実行主体に必要な参照だけを渡す。

**主な変更範囲**

- Secretの容器とresource単位IAMを定義し、値の投入・更新は別の認証済み手順にする。
- MODEL_ID/region/STT/TTS/OAuth等の非秘密設定とsecret参照を分ける。
- 本番へのemulator設定混入、未設定provider、暗黙のglobal fallbackを起動時に拒否する。

**成果物・変更先（予定）**

- `infra/gcp/modules/secrets`
- `services/api/config`
- `services/worker/config`
- `docs/06-deployment-and-operations.md`

**受入条件**

- [ ] plan・state・CI logにtokenやclient secretの本文が出ない。
- [ ] API/workerが無関係なSecretを読めず、必要なSecretのみ取得できる。
- [ ] 秘密値をローテーションしてもコードを変更せず設定版を追跡できる。

**検証方法**

- 設定の不足/矛盾/productionでのfake指定を負例にする（T-027）。
- stagingで許可/拒否されるSecret参照を確認する。

**このPRに含めないもの**

- Calendar OAuthロジックはBA-CAL-01。
- 実アカウントの審査・鍵発行はPR外の前提作業。

**外部前提**

- 利用するモデル・音声・処理地域、OAuth/APNs等の正式な秘密値の発行権限。

#### BA-OPS-05 OIDC・image digest・staging配備CI

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Platform |
| 段階・優先度・規模 | M1・P0・M |
| 関連要件 | FR-014、FR-015 |
| 依存PR | [BA-FND-06](#ba-fnd-06-prのlint型契約検証ci)、[BA-OPS-02](#ba-ops-02-firestore索引backup制御bucket)、[BA-OPS-03](#ba-ops-03-cloud-runtasksschedulerの配備定義)、[BA-OPS-04](#ba-ops-04-secretとprovider設定アクセス権) |

**目的**

検証したartifactをstagingへ再現可能に配備し、未信頼PRにクラウド書込権限を渡さない。

**主な変更範囲**

- GitHub OIDCの信頼条件をrepository・ref・environmentへ限定する。
- 信頼されたmainからimageを一度buildし、digestとcommit/設定版を記録してstagingへ配備する。
- worker/APIの互換順序と、直前digestへ切り戻すworkflowを用意する。

**成果物・変更先（予定）**

- `.github/workflows/deploy-staging.yml`
- `infra/gcp/modules/workload-identity`
- `scripts/release`
- `docs/06-deployment-and-operations.md`

**受入条件**

- [ ] fork PRや許可外refではdeploy用credentialを取得できない。
- [ ] 配備記録から実行imageのdigestとソースcommitを特定できる。
- [ ] 同じartifactの再配備と旧版への切戻しがstagingで確認できる。

**検証方法**

- 許可/不許可refの認証を確認し、smoke testとrollbackをstagingで実行する。
- schema非互換版への切戻しを自動で許可しない条件を確認する。

**このPRに含めないもの**

- 本番への実配布判断はBA-QA-06/BA-GOV-05。
- Pi/mobile配布はBA-OPS-08/09/10。

**外部前提**

- 実リポジトリ、保護environment、CI担当者、stagingのdeploy権限。

#### BA-OPS-06 本文なしの監視・alert・利用量集計

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Platform |
| 段階・優先度・規模 | M3・P0・M |
| 関連要件 | FR-015 |
| 依存PR | [BA-RUN-08](#ba-run-08-利用量予約集計と同時実行上限)、[BA-OPS-05](#ba-ops-05-oidcimage-digeststaging配備ci) |

**目的**

障害・処理停滞・利用量を本文なしで追跡し、費用の急増や失敗を担当者が検知できるようにする。

**主な変更範囲**

- request/run/command/operation IDとreleaseを結ぶ構造化log、段階別latencyを実装する。
- queue最古age、削除台帳失敗、purge遅延、接続数、利用量のdashboardとalertを定義する。
- tenant別集計へのアクセスを制限し、budget通知とアプリquotaを別の制御として扱う。

**成果物・変更先（予定）**

- `infra/gcp/monitoring`
- `services/api/telemetry`
- `services/worker/telemetry`
- `docs/06-deployment-and-operations.md`

**受入条件**

- [ ] 通常logに会話・カレンダー・音声・認証tokenが含まれない。
- [ ] テスト用のqueue滞留と台帳エラーで指定先へ通知される。
- [ ] 1会話/音声時間/Pi待機の費用要素を集計でき、tenant IDを無制限にmetrics labelへ載せない。

**検証方法**

- 合成の識別文字列・tokenを入れた要求でlog漏えいを検査する。
- T-026の上限到達と、alert受領から該当処理を追跡する演習を行う。

**このPRに含めないもの**

- 価格や販売プランの最終決定はプロダクト判断。
- 実負荷・原価の校正はBA-QA-04。

**外部前提**

- 通知先と運用担当、測定対象projectへの限定された監視権限。

#### BA-OPS-07 隔離復元と削除・認可台帳の再適用

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Platform |
| 段階・優先度・規模 | M5・P0・M |
| 関連要件 | FR-013、FR-014 |
| 依存PR | [BA-OPS-02](#ba-ops-02-firestore索引backup制御bucket)、[BA-IAM-05](#ba-iam-05-独立認可台帳の先行deny確定grant)、[BA-MEM-08](#ba-mem-08-派生データと子階層のpurge-worker) |

**目的**

backupから復元したDBを直ちに公開せず、削除と失効の最新状態を再適用してから再開する手順を実装する。

**主な変更範囲**

- 隔離DBへの復元と、削除/認可台帳の再適用を順序付きで実行する。
- 未確定grant・欠落台帳・古いmembershipを検出してsuspended/再開不可にする。
- 期限切れjob・command・通知を閉じ、外部副作用を再実行せず照合する。

**成果物・変更先（予定）**

- `scripts/operations/restore`
- `services/worker/recovery`
- `docs/06-deployment-and-operations.md`

**受入条件**

- [ ] 忘却後の古いbackupから復元しても削除情報を返さない。
- [ ] 失効済み利用者は再ログインしても旧権限を得ない。
- [ ] 台帳不整合があるとAPI/worker/通知の再開gateを開かず、復旧記録を残す。

**検証方法**

- 隔離stagingでT-028と失効・結果不明commandを含む復元fixtureを実行する。
- RPO/RTOの実測と再開不可条件を記録する。

**このPRに含めないもの**

- 稼働中productionをこのPRのテストで復元しない。
- 新しいbackup方式の導入やmulti-regionは別設計。

**外部前提**

- 隔離復元先、restore専用identity、実施責任者、復元点の合意。

#### BA-OPS-08 Pi配布artifactの署名と段階配布

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Platform |
| 段階・優先度・規模 | M5・P0・M |
| 関連要件 | FR-014 |
| 依存PR | [BA-DEV-08](#ba-dev-08-署名更新の検証起動切替rollback)、[BA-OPS-01](#ba-ops-01-terraform-stateproject分離基盤iam)、[BA-FND-06](#ba-fnd-06-prのlint型契約検証ci) |

**目的**

Piの配布物とmetadataを検証可能にし、同じ署名済みartifactを少数端末から段階的に配布する。

**主な変更範囲**

- version/hash/OS/architecture/protocol/期限を含むmetadataを作り、署名済み配布物を登録する。
- 署名鍵を通常runtimeから分離し、開発/pilot/一般の配布channelを用意する。
- 同じversionの上書きを拒否し、配布停止と前版選択を記録する。

**成果物・変更先（予定）**

- `.github/workflows/release-device.yml`
- `scripts/device-release`
- `infra/gcp/modules/device-artifacts`

**受入条件**

- [ ] 同じreleaseから同じdigestの配布物が取得できる。
- [ ] 破損・改変・期限切れmetadataをBA-DEV-08が拒否する。
- [ ] pilot失敗時に一般channelへの昇格を停止できる。

**検証方法**

- テスト鍵で正常/改変artifactを作りupdaterと結合する（T-025）。
- 鍵rotationと期限切れのfixtureで配布metadataの整合を確認する。

**このPRに含めないもの**

- Pi内の切替・rollbackはBA-DEV-08。
- OS全体の自動A/B更新はMVP範囲外。

**外部前提**

- 署名鍵の管理者、信頼rootの配布方法、配布bucketと公開範囲。

#### BA-OPS-09 Android署名buildと内部配布手順

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Mobile |
| 段階・優先度・規模 | M5・P1・M |
| 関連要件 | FR-005、FR-009、FR-014 |
| 依存PR | [BA-UI-03](#ba-ui-03-会話画面sse表示2d状態表示)、[BA-VOICE-03](#ba-voice-03-スマホ録音pcm変換再生adapter)、[BA-APP-05](#ba-app-05-通知apifcm登録汎用push配送)、[BA-FND-06](#ba-fnd-06-prのlint型契約検証ci) |

**目的**

Androidアプリを再現可能にbuild・署名し、指定したテスターへ渡すための配布手順を用意する。

**主な変更範囲**

- Android用toolchainを固定し、非秘密のapplication IDと環境設定を分ける。
- 署名鍵をCIの保護された参照から取得し、versionとartifact digestを記録する。
- 通知登録・マイク・認証callbackを内部配布buildで確認する手順を用意する。

**成果物・変更先（予定）**

- `.github/workflows/build-android.yml`
- `apps/mobile/android`
- `docs/mobile-android-release.md`

**受入条件**

- [ ] 同一commitの署名buildを追跡でき、秘密鍵がrepo/log/artifactに混入しない。
- [ ] 内部配布版でログイン・マイク拒否・通知・deep linkが確認できる。
- [ ] production向け設定とstaging向け設定を取り違えない。

**検証方法**

- 対応実機でT-006/030と基本会話のsmoke testを行う。
- CIの未信頼PRから署名鍵へアクセスできないことを確認する。

**このPRに含めないもの**

- ストア審査待ちをPRのコード変更と同一視しない。
- iOS配布はBA-OPS-10。

**外部前提**

- application ID、署名鍵管理者、配布用アカウント、Android実機、必要な通知設定。

#### BA-OPS-10 iOS署名buildと内部配布手順

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Mobile |
| 段階・優先度・規模 | M5・P1・M |
| 関連要件 | FR-005、FR-009、FR-014 |
| 依存PR | [BA-UI-03](#ba-ui-03-会話画面sse表示2d状態表示)、[BA-VOICE-03](#ba-voice-03-スマホ録音pcm変換再生adapter)、[BA-APP-05](#ba-app-05-通知apifcm登録汎用push配送)、[BA-FND-06](#ba-fnd-06-prのlint型契約検証ci) |

**目的**

iOSの署名・配布設定を再現可能にし、実機で通知と認証復帰を検証できるようにする。

**主な変更範囲**

- Xcode/SDKの対応版、bundle ID、署名設定、環境別configを固定する。
- 証明書・profile・APNs関連秘密を保護された参照で扱い、配布artifactとcommitを紐付ける。
- 内部配布でマイク・通知・OAuth復帰・アプリ休止復帰の検証手順を作る。

**成果物・変更先（予定）**

- `.github/workflows/build-ios.yml`
- `apps/mobile/ios`
- `docs/mobile-ios-release.md`

**受入条件**

- [ ] 配布buildの署名元とソース版が特定できる。
- [ ] 実機で認証・音声停止・通知許可拒否・通知タップが確認できる。
- [ ] 秘密値をコード・logへ出さず、別環境のtokenへ通知しない。

**検証方法**

- iOS実機でT-006/007/030の関連シナリオを実行する。
- 証明書期限切れや不足設定をbuildエラーとして識別する。

**このPRに含めないもの**

- Apple側の登録・審査完了は外部前提として追跡する。
- バックグラウンド常時録音は実装しない。

**外部前提**

- Apple配布用アカウント、bundle ID、署名・APNsの権限、macOS build環境、iOS実機。

#### BA-OPS-11 self-hostの初回導入と設定診断

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Platform |
| 段階・優先度・規模 | M5・P1・M |
| 関連要件 | FR-014、FR-015 |
| 依存PR | [BA-OPS-05](#ba-ops-05-oidcimage-digeststaging配備ci)、[BA-DEV-09](#ba-dev-09-pi導入手順kiosk起動offline回復画面)、[BA-GOV-04](#ba-gov-04-対応機材bomos音響の検証記録) |

**目的**

第三者が自分のGCPとPiで導入できるよう、既存IaC・installerをつなぐ手順と設定診断を提供する。

**主な変更範囲**

- project/region/model/連携範囲を明示した導入順序を、実装済みコマンドで記述する。
- 認証情報・対応版・通信・音声deviceの診断を読取中心で行い、秘密値を出力しない。
- 費用要素、公式サービスとの責任分担、更新・削除・復旧の入口を示す。

**成果物・変更先（予定）**

- `scripts/self-host-doctor`
- `docs/self-hosting.md`
- `README.md`

**受入条件**

- [ ] 未経験の担当者が新しいテスト環境で記載手順を再現できる。
- [ ] 不足設定に対して具体的な修正先を示し、黙って別project/providerへ接続しない。
- [ ] 診断だけでは課金資源作成・実家電操作・production削除を行わない。

**検証方法**

- クリーンなstagingと検証済みPi構成で導入演習を行い、失敗点を手順へ反映する。
- 秘密値入り設定を渡しても診断出力に現れないことを確認する。

**このPRに含めないもの**

- IaCやupdater本体の再実装はしない。
- 未検証の全周辺機器のサポートを宣言しない。

**外部前提**

- 試験用GCPと対応Pi、実際に案内する配布先・サポート窓口。

### QA 跨層・実機・受入検証

#### BA-QA-01 権限境界のcross-component回帰

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | QA |
| 段階・優先度・規模 | M5・P0・M |
| 関連要件 | FR-001、FR-002、FR-003、FR-006、FR-012、FR-013 |
| 依存PR | [BA-IAM-10](#ba-iam-10-退会アカウントテナント削除の受付)、[BA-MEM-09](#ba-mem-09-認可付きエクスポート生成取得失効)、[BA-DEV-04](#ba-dev-04-端末snapshotsessionrun割当通知)、[BA-RUN-06](#ba-run-06-型付きtoolexecutorと実行権限の検査)、[BA-VOICE-02](#ba-voice-02-文単位ttsと認可付き音声取得) |

**目的**

個々のAPIテストを跨ぐ権限漏えいを検出し、検索・生成・音声・export・復元の境界を回帰として固定する。

**主な変更範囲**

- 別tenant、同tenantの他人private、admin自己昇格、失効deviceの共通シナリオを実行する。
- 回答だけでなくモデル入力・SSE・TTS参照・生成済exportへの漏えいも検査する。
- 権限変更中の進行jobと個人session期限のraceを再現する。

**成果物・変更先（予定）**

- `tests/integration/security-boundaries`
- `tests/fixtures/authorization`
- `docs/05-development-and-testing.md`

**受入条件**

- [ ] T-001/002/004/020/021/022/033/034の跨層ケースが実行可能になる。
- [ ] 許可しない情報がfakeモデル入力・レスポンス・logに含まれない。
- [ ] 失効後の既存接続と生成済exportから対象へ再アクセスできない。

**検証方法**

- 固定2tenant fixtureと偽時計で正負シナリオを繰り返す。
- 実装PRのunit testと区別し、複数component経由での結果を検査する。

**このPRに含めないもの**

- 各機能の基本認可テストをこのPRまで先送りしない。
- 網羅的な外部監査や法的適合認証を代替しない。

**外部前提**

追加の外部前提なし。依存PRのfixtureとfake/emulatorで検証する。

#### BA-QA-02 スマホの記憶からPi照明操作までのE2E

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | QA |
| 段階・優先度・規模 | M2・P0・M |
| 関連要件 | FR-003、FR-004、FR-006、FR-011 |
| 依存PR | [BA-UI-03](#ba-ui-03-会話画面sse表示2d状態表示)、[BA-UI-04](#ba-ui-04-記憶の出典共有訂正忘却画面)、[BA-UI-09](#ba-ui-09-piペアリング許可設定失効画面)、[BA-MEM-04](#ba-mem-04-scope付き2hop検索と会話への文脈組込み)、[BA-DEV-07](#ba-dev-07-piコマンド実行観測再接続照合) |

**目的**

スマホの明示記憶をPiの操作につなぐ製品の中心体験を、再現可能な縦断テストとして固定する。

**主な変更範囲**

- スマホ側で共有記憶を保存し、Piから対象照明へ目標状態を送るシナリオを作る。
- 観測結果と記憶の出典を確認し、スマホの訂正が次回の操作に反映されることを検査する。
- tenant差替え、同名機器、結果不明を同じfixtureで再現する。

**成果物・変更先（予定）**

- `tests/e2e/memory-to-device`
- `tests/fixtures/home-assistant`
- `docs/05-development-and-testing.md`

**受入条件**

- [ ] FR-003/004/006/011に対応する縦断シナリオが、fake/テスト用HAで再実行できる。
- [ ] 受信ackだけで成功にならず、操作対象と報告状態を確認する。
- [ ] 別tenantや曖昧な対象へ操作を送らない。

**検証方法**

- T-005/008/009/017/018/019の代表ケースを跨層で実行する。
- 実機実行は明示的に選び、既定のCIは実家庭の機器を操作しない。

**このPRに含めないもの**

- 全画面・全音声ケースはBA-QA-03/05。
- 任意機器・任意serviceの操作は加えない。

**外部前提**

- 実機演習を行う場合のみ、許可されたテスト照明とHA環境。

#### BA-QA-03 日本語の記憶・tool・音声評価セット

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Agent |
| 段階・優先度・規模 | M4・P1・M |
| 関連要件 | FR-004、FR-005、FR-006、FR-010、FR-011 |
| 依存PR | [BA-MEM-05](#ba-mem-05-埋め込みjobとvector検索adapter)、[BA-CAL-04](#ba-cal-04-本人確認後のcalendar実行照合resume)、[BA-DEV-07](#ba-dev-07-piコマンド実行観測再接続照合)、[BA-VOICE-05](#ba-voice-05-音声状態と停止取消権限拒否のui) |

**目的**

モデル・プロンプト・検索・音声の変更を、同じ日本語シナリオと構造化した結果で比較できるようにする。

**主な変更範囲**

- 通常会話、曖昧な人名・機器、日付、訂正、誤ったtool引数、認識困難な音声の合成評価セットを用意する。
- 回答の文字一致ではなく、出典・許可scope・tool対象・確認・最終状態を採点する。
- model/prompt/tool/schemaの版と評価条件、失敗例、使用量を記録する。

**成果物・変更先（予定）**

- `tests/evals/ja`
- `scripts/run-agent-evals`
- `docs/evaluation.md`

**受入条件**

- [ ] 同じdatasetと設定から比較可能な結果ファイルを生成できる。
- [ ] 正しい文章でも許可外操作や誤った対象なら失敗となる。
- [ ] 評価セットに利用者の本番会話や音声を自動流用しない。

**検証方法**

- fake出力で採点器の正負を確認し、件数上限付きで実providerを比較する。
- T-009/015/016/029の観点と日本語音声の失敗分類を含める。

**このPRに含めないもの**

- 特定modelの絶対品質やPiローカルLLM性能を保証しない。
- 大規模学習用データ収集は行わない。

**外部前提**

- 実provider評価時の利用許可、対象model/音声設定、評価費用上限。

#### BA-QA-04 負荷・遅延・待機原価の計測シナリオ

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | QA |
| 段階・優先度・規模 | M5・P1・M |
| 関連要件 | FR-015 |
| 依存PR | [BA-OPS-05](#ba-ops-05-oidcimage-digeststaging配備ci)、[BA-OPS-06](#ba-ops-06-本文なしの監視alert利用量集計)、[BA-VOICE-05](#ba-voice-05-音声状態と停止取消権限拒否のui)、[BA-RUN-08](#ba-run-08-利用量予約集計と同時実行上限)、[BA-DEV-07](#ba-dev-07-piコマンド実行観測再接続照合) |

**目的**

設計上の遅延・同時利用・待機原価を測り、未計測の数値を公開前の判断材料へ変える。

**主な変更範囲**

- 文字初回差分、発話終了から音声開始、家電観測、通知処理開始を別計測にする。
- idle Pi接続とactive会話の負荷を分け、queue待ち・DB操作・provider待ちを記録する。
- 段階的に負荷を増やし、quota・停止条件・最大費用を設定する。

**成果物・変更先（予定）**

- `tests/load`
- `scripts/measure-cost`
- `docs/performance-baseline.md`

**受入条件**

- [ ] 測定条件、母集団、p50/p95、失敗率、費用要素が再現可能に記録される。
- [ ] 成功要求だけを抽出して設計目標を達成したと表示しない。
- [ ] OPEN-007の採用値または変更理由を決定記録へ反映できる。

**検証方法**

- 小規模fake試験で計測器と停止条件を確認してからstagingへ進む。
- T-026を含め、上限超過時に無限retry・費用増幅が起きないことを確認する。

**このPRに含めないもの**

- 利用規模やSLAの未合意値を固定の保証へ変えない。
- productionへの無断負荷試験を行わない。

**外部前提**

- 試験環境・負荷上限・費用上限・停止担当の合意。

#### BA-QA-05 Pi断線・電源断・更新復旧の実機試験

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | QA |
| 段階・優先度・規模 | M5・P0・M |
| 関連要件 | FR-003、FR-005、FR-011、FR-014 |
| 依存PR | [BA-DEV-07](#ba-dev-07-piコマンド実行観測再接続照合)、[BA-DEV-08](#ba-dev-08-署名更新の検証起動切替rollback)、[BA-DEV-09](#ba-dev-09-pi導入手順kiosk起動offline回復画面)、[BA-OPS-08](#ba-ops-08-pi配布artifactの署名と段階配布)、[BA-VOICE-04](#ba-voice-04-pi録音再生queue停止adapter)、[BA-DEV-04](#ba-dev-04-端末snapshotsessionrun割当通知) |

**目的**

Piの再起動・断線・更新失敗を再現し、誤操作やprivate情報の残留を防ぐ復旧動作を確認する。

**主な変更範囲**

- 録音/TTS/command実行/更新の各時点で切断・再起動・電源断を行う手順を用意する。
- grant/ack喪失、SQLite開始記録、旧generation、新snapshotからの復旧を観察する。
- 破損artifact・期限切れmetadata・migration失敗を含む更新試験を実行する。

**成果物・変更先（予定）**

- `tests/hardware`
- `scripts/device-diagnostics`
- `docs/device-test-results.md`

**受入条件**

- [ ] T-007/017/019/021/024/025/035/036の実機結果を版と機材に紐付ける。
- [ ] 結果不明の家電操作を盲目再送せず、期限切れ個人情報を表示・再生しない。
- [ ] 更新失敗で最後の検証済みアプリまたは復旧画面へ戻れる。

**検証方法**

- 許可したテスト家電と対応Piで障害注入を実施する。
- 失敗した条件は再現手順付きで記録し、未実施を合格に数えない。

**このPRに含めないもの**

- すべての周辺機器や電源品質を網羅しない。
- OS全体の自動rollbackを完成済みと扱わない。

**外部前提**

- 検証用Pi・予備媒体・電源断に使う設備・テスト用HA機器。

#### BA-QA-06 MVP全受入基準の証跡と公開判定

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | QA |
| 段階・優先度・規模 | M5・P0・M |
| 関連要件 | FR-001、FR-002、FR-003、FR-004、FR-005、FR-006、FR-007、FR-008、FR-009、FR-010、FR-011、FR-012、FR-013、FR-014、FR-015 |
| 依存PR | [BA-QA-01](#ba-qa-01-権限境界のcross-component回帰)、[BA-QA-02](#ba-qa-02-スマホの記憶からpi照明操作までのe2e)、[BA-QA-03](#ba-qa-03-日本語の記憶tool音声評価セット)、[BA-QA-04](#ba-qa-04-負荷遅延待機原価の計測シナリオ)、[BA-QA-05](#ba-qa-05-pi断線電源断更新復旧の実機試験)、[BA-OPS-07](#ba-ops-07-隔離復元と削除認可台帳の再適用)、[BA-OPS-09](#ba-ops-09-android署名buildと内部配布手順)、[BA-OPS-10](#ba-ops-10-ios署名buildと内部配布手順)、[BA-OPS-11](#ba-ops-11-self-hostの初回導入と設定診断)、[BA-GOV-01](#ba-gov-01-リポジトリ運営連絡先非公開報告窓口)、[BA-GOV-02](#ba-gov-02-ライセンス確定と第三者素材依存物台帳)、[BA-GOV-03](#ba-gov-03-保持外部送信削除の利用者向け説明)、[BA-GOV-04](#ba-gov-04-対応機材bomos音響の検証記録)、[BA-UI-05](#ba-ui-05-ノート一覧編集削除画面)、[BA-UI-06](#ba-ui-06-タスク一覧期限完了画面)、[BA-UI-07](#ba-ui-07-リマインダー通知スヌーズ画面)、[BA-UI-08](#ba-ui-08-calendar連携予定確認カード)、[BA-UI-11](#ba-ui-11-会員招待role利用量の管理画面)、[BA-UI-12](#ba-ui-12-エクスポート退会削除の利用者導線)、[BA-UI-13](#ba-ui-13-webからのアカウント削除申請導線) |

**目的**

MVPを公開してよいかを、全受入基準と測定・復旧の証跡から判断できる状態にする。

**主な変更範囲**

- AC-001〜015に実行結果・対象版・未達事項を対応付ける。
- 日常機能、Calendar確認、音声、削除、ストア向け導線を短いrelease smokeで確認する。
- 性能目標・対応機材・公開範囲・外部審査状況を確認し、公開判断と残る制限を記録する。

**成果物・変更先（予定）**

- `docs/release-readiness.md`
- `docs/acceptance-evidence.md`
- `tests/e2e/release-smoke`

**受入条件**

- [ ] 全ACに証跡または未達の明示があり、重大な権限/削除/副作用の不合格があると公開不可になる。
- [ ] 未計測・未実施・外部審査待ちを合格扱いにしない。
- [ ] 公開候補のartifact digestとAPI/device/model設定版が証跡に一致する。

**検証方法**

- T-001〜038の実行状況を確認し、機能PRのテスト結果と総合結果を照合する。
- 指定版でrelease smokeを実行し、変更差分で影響する項目だけ必要に応じ再検証する。

**このPRに含めないもの**

- このチケットへ未実装機能をまとめて実装しない。
- 実際の公開・ストア配信は責任者のrelease手順に従う。

**外部前提**

- 運用・プロダクト・セキュリティ責任者、配布先、外部審査・署名・本番設定の準備。

### GOV OSS公開と利用者向け文書

#### BA-GOV-01 リポジトリ運営・連絡先・非公開報告窓口

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Maintainer |
| 段階・優先度・規模 | M0・P0・S |
| 関連要件 | FR-015 |
| 依存PR | [BA-FND-01](#ba-fnd-01-モノレポと依存バージョンの固定) |

**目的**

Brainoid Agentの開発参加・レビュー・非公開報告を、実在する担当者と窓口に結び付ける。

**主な変更範囲**

- 正式名Brainoid Agentと選定したrepository/識別子を案内へ反映する。
- 実際のreview owner、連絡先、サポート範囲、main保護の運用を定義する。
- Private vulnerability reportingの設定手順と確認記録を残す。

**成果物・変更先（予定）**

- `README.md`
- `CONTRIBUTING.md`
- `SECURITY.md`
- `.github/CODEOWNERS`

**受入条件**

- [ ] 架空のメール・未設定リンクが公開案内に残らない。
- [ ] 変更領域に応じたレビュアーが明確で、非公開報告が担当者へ届く。
- [ ] OPEN-001/002の確定部分と残件を区別して更新する。

**検証方法**

- 新しい貢献者の立場で導線とリンクを確認する。
- 非公開報告窓口へ機密を含まないテスト報告を送る手順で到達を確認する。

**このPRに含めないもの**

- 脆弱性そのものの修正や製品実装は含めない。
- 実在する人への連絡・招待は担当者の権限で別途行う。

**外部前提**

- 実repository、担当者、連絡先、管理設定を行えるメンテナ。

#### BA-GOV-02 ライセンス確定と第三者素材・依存物台帳

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Maintainer |
| 段階・優先度・規模 | M0・P0・S |
| 関連要件 | FR-014 |
| 依存PR | [BA-FND-01](#ba-fnd-01-モノレポと依存バージョンの固定) |

**目的**

コード・ドキュメント・モデル・音声・素材の配布条件を整理し、候補のライセンスを公開時の正式方針へ確定する。

**主な変更範囲**

- Apache-2.0候補の採否と著作権表示の責任主体を決定記録へ残す。
- 依存library・モデル・音声・キャラクター素材の出典と配布条件の台帳を作る。
- 未確認素材の配布を公開gateで止め、依存追加時に台帳を更新する運用を決める。

**成果物・変更先（予定）**

- `LICENSE`
- `docs/third-party-inventory.md`
- `docs/license-policy.md`

**受入条件**

- [ ] プロジェクト側の採用判断と各第三者素材の条件が混同されていない。
- [ ] 公開物に含む項目へ出典・条件・確認状況が記録される。
- [ ] OPEN-004に決定者・決定日・残件が残る。

**検証方法**

- 依存lockと配布asset一覧を台帳へ照合する。
- 未確認項目があるfixtureで公開確認が未完了になることを確認する。

**このPRに含めないもの**

- 権利者の許諾や法的判断をコードだけで代替しない。
- 未導入の全候補libraryを事前採用しない。

**外部前提**

- ライセンスを決定できるメンテナ、実際に配布する素材の権利情報。

#### BA-GOV-03 保持・外部送信・削除の利用者向け説明

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Product |
| 段階・優先度・規模 | M5・P0・S |
| 関連要件 | FR-013 |
| 依存PR | [BA-MEM-08](#ba-mem-08-派生データと子階層のpurge-worker)、[BA-IAM-10](#ba-iam-10-退会アカウントテナント削除の受付)、[BA-CAL-05](#ba-cal-05-calendar連携解除token失効再接続案内)、[BA-VOICE-01](#ba-voice-01-音声uploadstttranscript参照) |

**目的**

利用者が保存内容・外部送信・忘却・復元時の扱いを理解し、削除や連携解除の実際の導線へ到達できるようにする。

**主な変更範囲**

- 会話/記憶/音声/Calendar/通知/端末データを何のために扱うかを説明する。
- 通常保存・派生データ・backup・独立台帳・外部provider保持を区別し、確定値を反映する。
- アプリとWebの削除導線、問い合わせ、連携解除をリンクし、ホスト版と自己ホストの責任を分ける。

**成果物・変更先（予定）**

- `docs/privacy-and-data.md`
- `docs/account-deletion.md`
- `apps/mobile/public/legal-links.json`

**受入条件**

- [ ] 生音声非保存などの説明が実装・設定と一致し、provider側も保持しないと断定しない。
- [ ] ユーザーが記憶忘却・会話削除・アカウント削除の違いと所要期間を確認できる。
- [ ] 公開先・担当・保持条件が未確定なら未完了として残る。

**検証方法**

- BA-MEM-08/BA-IAM-10の挙動と説明を照合する。
- UI-12/13から利用者向け説明へ到達できることをrelease時に確認する。

**このPRに含めないもの**

- 利用規約の法的適合をこのPRの自動検証で保証しない。
- Web削除フォーム実装はBA-UI-13。

**外部前提**

- 運用主体・公開URL・providerの実契約条件・保持期間の最終決定。

#### BA-GOV-04 対応機材BOM・OS・音響の検証記録

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Device |
| 段階・優先度・規模 | M4・P1・S |
| 関連要件 | FR-005、FR-014 |
| 依存PR | [BA-DEV-09](#ba-dev-09-pi導入手順kiosk起動offline回復画面)、[BA-VOICE-04](#ba-voice-04-pi録音再生queue停止adapter) |

**目的**

第三者が再現できるPi構成と音声品質の基準を、実際に確認した機材・版とともに公開する。

**主な変更範囲**

- Pi5 8GB、電源、冷却、画面、マイク、スピーカー、OS/kernel/browserの組合せを記録する。
- 設置距離・雑音条件・音量・停止挙動・温度等の試験条件を固定する。
- 検証済み・未検証・既知制限を分け、導入と復旧手順へリンクする。

**成果物・変更先（予定）**

- `docs/supported-hardware.md`
- `docs/device-audio-baseline.md`

**受入条件**

- [ ] 部品名だけでなく型番・版・試験日があり同じ構成を準備できる。
- [ ] 未検証の周辺機器まで動作保証として読めない。
- [ ] OPEN-003の対応表と試験結果が一致する。

**検証方法**

- 別担当者が記録構成で起動・音声入出力・停止を再現する。
- 交換した機材は差分として再検証の必要箇所を記録する。

**このPRに含めないもの**

- 新しい音声モデルや任意OSの対応を追加しない。
- 製品の負荷計測はBA-QA-04、故障時の網羅はBA-QA-05。

**外部前提**

- 実機一式と再現可能な試験場所。

#### BA-GOV-05 初回リリースノートと互換・既知制限表

| 項目 | 内容 |
| --- | --- |
| 状態 | 未着手 |
| 担当ロール | Maintainer |
| 段階・優先度・規模 | M5・P1・S |
| 関連要件 | FR-014、FR-015 |
| 依存PR | [BA-QA-06](#ba-qa-06-mvp全受入基準の証跡と公開判定) |

**目的**

初回公開版の機能・対応環境・制限・更新方法を、実際のrelease候補と一致させて案内する。

**主な変更範囲**

- 実装済み機能と既知制限を候補版の証跡に合わせて記載する。
- API/device/schema/model設定の互換表、更新・戻し方、問題報告先を示す。
- 文書の未実装コマンド案を実際に利用できる手順へ更新し、公開準備が完了した状態を記録する。

**成果物・変更先（予定）**

- `CHANGELOG.md`
- `docs/releases/initial-release.md`
- `README.md`

**受入条件**

- [ ] 未実装機能や未検証性能を公開版の機能として宣言しない。
- [ ] READMEの導入・削除・更新が対象artifactと対応し、公開リンクが実在する。
- [ ] QAの公開判断・対象版・実配布手順への参照が揃う。

**検証方法**

- クリーン環境で確認したBA-OPS-11の結果と案内を照合する。
- 公開候補の版・digest・サポート表を相互確認する。

**このPRに含めないもの**

- 公開の操作自体を文書PRへのmergeと同一視しない。
- 残っている不合格機能の実装をここへ持ち込まない。

**外部前提**

- 公開責任者の判定、正式配布URL、外部審査が必要な経路の完了状況。

## 7. 機能要件との対応

受入基準AC-001〜015は同じ番号のFRに対応する。ここでは実装チケットへの追跡を示し、具体的な合格条件の正本は[要件定義](01-requirements.md)とする。

| 機能要件 | 実装・検証チケット |
| --- | --- |
| FR-001 | [BA-FND-03](#ba-fnd-03-共通id時刻version型の定義)、[BA-FND-05](#ba-fnd-05-authfirestoreエミュレーターと2テナントfixture)、[BA-IAM-01](#ba-iam-01-firebase認証とactorcontextの生成)、[BA-IAM-03](#ba-iam-03-テナント作成選択と初期owneragent)、[BA-UI-02](#ba-ui-02-ログインテナント選択context切替)、[BA-QA-01](#ba-qa-01-権限境界のcross-component回帰)、[BA-QA-06](#ba-qa-06-mvp全受入基準の証跡と公開判定) |
| FR-002 | [BA-FND-05](#ba-fnd-05-authfirestoreエミュレーターと2テナントfixture)、[BA-IAM-02](#ba-iam-02-テナントprivate表示先の認可関数)、[BA-IAM-04](#ba-iam-04-期限付き招待コードと参加受付)、[BA-IAM-05](#ba-iam-05-独立認可台帳の先行deny確定grant)、[BA-IAM-06](#ba-iam-06-会員role変更所属失効owner移譲)、[BA-UI-11](#ba-ui-11-会員招待role利用量の管理画面)、[BA-QA-01](#ba-qa-01-権限境界のcross-component回帰)、[BA-QA-06](#ba-qa-06-mvp全受入基準の証跡と公開判定) |
| FR-003 | [BA-IAM-05](#ba-iam-05-独立認可台帳の先行deny確定grant)、[BA-IAM-07](#ba-iam-07-端末ペアリングと専用tokenの発行更新)、[BA-IAM-08](#ba-iam-08-端末許可設定資格情報失効)、[BA-DEV-01](#ba-dev-01-pi常駐サービスとloopback管理境界)、[BA-DEV-02](#ba-dev-02-sqlite設定cursorcommand台帳)、[BA-DEV-03](#ba-dev-03-wss接続認証generationheartbeat)、[BA-DEV-09](#ba-dev-09-pi導入手順kiosk起動offline回復画面)、[BA-DEV-10](#ba-dev-10-piのクラウド接続httpsse-bridge)、[BA-UI-09](#ba-ui-09-piペアリング許可設定失効画面)、[BA-QA-01](#ba-qa-01-権限境界のcross-component回帰)、[BA-QA-02](#ba-qa-02-スマホの記憶からpi照明操作までのe2e)、[BA-QA-05](#ba-qa-05-pi断線電源断更新復旧の実機試験)、[BA-QA-06](#ba-qa-06-mvp全受入基準の証跡と公開判定) |
| FR-004 | [BA-FND-07](#ba-fnd-07-coreの依存インターフェースとfake-adapter)、[BA-FND-08](#ba-fnd-08-冪等性etagcursorの共通処理)、[BA-IAM-03](#ba-iam-03-テナント作成選択と初期owneragent)、[BA-RUN-01](#ba-run-01-outboxdispatcherworker-lease基盤)、[BA-RUN-02](#ba-run-02-google-gen-aiのmodelprovider-adapter)、[BA-RUN-03](#ba-run-03-人格設定とテキストagentcore)、[BA-RUN-04](#ba-run-04-会話messageturn受付と永続run実行)、[BA-RUN-05](#ba-run-05-sse差分seq履歴再開)、[BA-RUN-06](#ba-run-06-型付きtoolexecutorと実行権限の検査)、[BA-RUN-07](#ba-run-07-run取消確認checkpointresume)、[BA-MEM-06](#ba-mem-06-会話要約と出典scope版の継承)、[BA-DEV-10](#ba-dev-10-piのクラウド接続httpsse-bridge)、[BA-UI-01](#ba-ui-01-共有react-uiとスマホpiのentry)、[BA-UI-03](#ba-ui-03-会話画面sse表示2d状態表示)、[BA-OPS-03](#ba-ops-03-cloud-runtasksschedulerの配備定義)、[BA-QA-02](#ba-qa-02-スマホの記憶からpi照明操作までのe2e)、[BA-QA-03](#ba-qa-03-日本語の記憶tool音声評価セット)、[BA-QA-06](#ba-qa-06-mvp全受入基準の証跡と公開判定) |
| FR-005 | [BA-UI-03](#ba-ui-03-会話画面sse表示2d状態表示)、[BA-VOICE-01](#ba-voice-01-音声uploadstttranscript参照)、[BA-VOICE-02](#ba-voice-02-文単位ttsと認可付き音声取得)、[BA-VOICE-03](#ba-voice-03-スマホ録音pcm変換再生adapter)、[BA-VOICE-04](#ba-voice-04-pi録音再生queue停止adapter)、[BA-VOICE-05](#ba-voice-05-音声状態と停止取消権限拒否のui)、[BA-OPS-09](#ba-ops-09-android署名buildと内部配布手順)、[BA-OPS-10](#ba-ops-10-ios署名buildと内部配布手順)、[BA-QA-03](#ba-qa-03-日本語の記憶tool音声評価セット)、[BA-QA-05](#ba-qa-05-pi断線電源断更新復旧の実機試験)、[BA-QA-06](#ba-qa-06-mvp全受入基準の証跡と公開判定)、[BA-GOV-04](#ba-gov-04-対応機材bomos音響の検証記録) |
| FR-006 | [BA-FND-03](#ba-fnd-03-共通id時刻version型の定義)、[BA-FND-07](#ba-fnd-07-coreの依存インターフェースとfake-adapter)、[BA-IAM-02](#ba-iam-02-テナントprivate表示先の認可関数)、[BA-MEM-01](#ba-mem-01-entityfactの保存モデルとrepository)、[BA-MEM-02](#ba-mem-02-entityの登録別名外部id照合api)、[BA-MEM-03](#ba-mem-03-明示記憶の保存出典訂正api)、[BA-MEM-04](#ba-mem-04-scope付き2hop検索と会話への文脈組込み)、[BA-MEM-05](#ba-mem-05-埋め込みjobとvector検索adapter)、[BA-MEM-06](#ba-mem-06-会話要約と出典scope版の継承)、[BA-MEM-07](#ba-mem-07-忘却受付削除台帳元出典再利用抑止)、[BA-UI-04](#ba-ui-04-記憶の出典共有訂正忘却画面)、[BA-OPS-02](#ba-ops-02-firestore索引backup制御bucket)、[BA-QA-01](#ba-qa-01-権限境界のcross-component回帰)、[BA-QA-02](#ba-qa-02-スマホの記憶からpi照明操作までのe2e)、[BA-QA-03](#ba-qa-03-日本語の記憶tool音声評価セット)、[BA-QA-06](#ba-qa-06-mvp全受入基準の証跡と公開判定) |
| FR-007 | [BA-FND-08](#ba-fnd-08-冪等性etagcursorの共通処理)、[BA-APP-01](#ba-app-01-ノートcrudとノートtool)、[BA-UI-05](#ba-ui-05-ノート一覧編集削除画面)、[BA-QA-06](#ba-qa-06-mvp全受入基準の証跡と公開判定) |
| FR-008 | [BA-FND-08](#ba-fnd-08-冪等性etagcursorの共通処理)、[BA-APP-02](#ba-app-02-タスクの作成一覧更新完了取消とtool)、[BA-UI-06](#ba-ui-06-タスク一覧期限完了画面)、[BA-QA-06](#ba-qa-06-mvp全受入基準の証跡と公開判定) |
| FR-009 | [BA-RUN-01](#ba-run-01-outboxdispatcherworker-lease基盤)、[BA-APP-03](#ba-app-03-リマインダースヌーズ予定版api)、[BA-APP-04](#ba-app-04-期限走査と重複しない通知job)、[BA-APP-05](#ba-app-05-通知apifcm登録汎用push配送)、[BA-UI-07](#ba-ui-07-リマインダー通知スヌーズ画面)、[BA-OPS-03](#ba-ops-03-cloud-runtasksschedulerの配備定義)、[BA-OPS-09](#ba-ops-09-android署名buildと内部配布手順)、[BA-OPS-10](#ba-ops-10-ios署名buildと内部配布手順)、[BA-QA-06](#ba-qa-06-mvp全受入基準の証跡と公開判定) |
| FR-010 | [BA-RUN-07](#ba-run-07-run取消確認checkpointresume)、[BA-CAL-01](#ba-cal-01-calendar-oauth開始callback秘密管理)、[BA-CAL-02](#ba-cal-02-calendar予定一覧時刻正規化と参照tool)、[BA-CAL-03](#ba-cal-03-calendar操作proposalと確認内容の固定)、[BA-CAL-04](#ba-cal-04-本人確認後のcalendar実行照合resume)、[BA-CAL-05](#ba-cal-05-calendar連携解除token失効再接続案内)、[BA-UI-08](#ba-ui-08-calendar連携予定確認カード)、[BA-OPS-04](#ba-ops-04-secretとprovider設定アクセス権)、[BA-QA-03](#ba-qa-03-日本語の記憶tool音声評価セット)、[BA-QA-06](#ba-qa-06-mvp全受入基準の証跡と公開判定) |
| FR-011 | [BA-RUN-06](#ba-run-06-型付きtoolexecutorと実行権限の検査)、[BA-MEM-02](#ba-mem-02-entityの登録別名外部id照合api)、[BA-APP-06](#ba-app-06-家電command-apiとhome-tool)、[BA-DEV-05](#ba-dev-05-command-start-claimgrantと取消競合)、[BA-DEV-06](#ba-dev-06-home-assistant-adapterとentity同期)、[BA-DEV-07](#ba-dev-07-piコマンド実行観測再接続照合)、[BA-QA-02](#ba-qa-02-スマホの記憶からpi照明操作までのe2e)、[BA-QA-03](#ba-qa-03-日本語の記憶tool音声評価セット)、[BA-QA-05](#ba-qa-05-pi断線電源断更新復旧の実機試験)、[BA-QA-06](#ba-qa-06-mvp全受入基準の証跡と公開判定) |
| FR-012 | [BA-IAM-02](#ba-iam-02-テナントprivate表示先の認可関数)、[BA-IAM-09](#ba-iam-09-pi個人セッションの開始失効scope)、[BA-DEV-04](#ba-dev-04-端末snapshotsessionrun割当通知)、[BA-UI-10](#ba-ui-10-pi個人sessionと会話引継ぎui)、[BA-VOICE-02](#ba-voice-02-文単位ttsと認可付き音声取得)、[BA-VOICE-05](#ba-voice-05-音声状態と停止取消権限拒否のui)、[BA-QA-01](#ba-qa-01-権限境界のcross-component回帰)、[BA-QA-06](#ba-qa-06-mvp全受入基準の証跡と公開判定) |
| FR-013 | [BA-IAM-05](#ba-iam-05-独立認可台帳の先行deny確定grant)、[BA-IAM-10](#ba-iam-10-退会アカウントテナント削除の受付)、[BA-MEM-07](#ba-mem-07-忘却受付削除台帳元出典再利用抑止)、[BA-MEM-08](#ba-mem-08-派生データと子階層のpurge-worker)、[BA-MEM-09](#ba-mem-09-認可付きエクスポート生成取得失効)、[BA-CAL-05](#ba-cal-05-calendar連携解除token失効再接続案内)、[BA-UI-12](#ba-ui-12-エクスポート退会削除の利用者導線)、[BA-UI-13](#ba-ui-13-webからのアカウント削除申請導線)、[BA-OPS-02](#ba-ops-02-firestore索引backup制御bucket)、[BA-OPS-07](#ba-ops-07-隔離復元と削除認可台帳の再適用)、[BA-QA-01](#ba-qa-01-権限境界のcross-component回帰)、[BA-QA-06](#ba-qa-06-mvp全受入基準の証跡と公開判定)、[BA-GOV-03](#ba-gov-03-保持外部送信削除の利用者向け説明) |
| FR-014 | [BA-FND-01](#ba-fnd-01-モノレポと依存バージョンの固定)、[BA-FND-04](#ba-fnd-04-openapi検証とtypescriptクライアント生成)、[BA-FND-06](#ba-fnd-06-prのlint型契約検証ci)、[BA-RUN-01](#ba-run-01-outboxdispatcherworker-lease基盤)、[BA-RUN-05](#ba-run-05-sse差分seq履歴再開)、[BA-DEV-01](#ba-dev-01-pi常駐サービスとloopback管理境界)、[BA-DEV-02](#ba-dev-02-sqlite設定cursorcommand台帳)、[BA-DEV-03](#ba-dev-03-wss接続認証generationheartbeat)、[BA-DEV-04](#ba-dev-04-端末snapshotsessionrun割当通知)、[BA-DEV-07](#ba-dev-07-piコマンド実行観測再接続照合)、[BA-DEV-08](#ba-dev-08-署名更新の検証起動切替rollback)、[BA-DEV-09](#ba-dev-09-pi導入手順kiosk起動offline回復画面)、[BA-DEV-10](#ba-dev-10-piのクラウド接続httpsse-bridge)、[BA-OPS-05](#ba-ops-05-oidcimage-digeststaging配備ci)、[BA-OPS-07](#ba-ops-07-隔離復元と削除認可台帳の再適用)、[BA-OPS-08](#ba-ops-08-pi配布artifactの署名と段階配布)、[BA-OPS-09](#ba-ops-09-android署名buildと内部配布手順)、[BA-OPS-10](#ba-ops-10-ios署名buildと内部配布手順)、[BA-OPS-11](#ba-ops-11-self-hostの初回導入と設定診断)、[BA-QA-05](#ba-qa-05-pi断線電源断更新復旧の実機試験)、[BA-QA-06](#ba-qa-06-mvp全受入基準の証跡と公開判定)、[BA-GOV-02](#ba-gov-02-ライセンス確定と第三者素材依存物台帳)、[BA-GOV-04](#ba-gov-04-対応機材bomos音響の検証記録)、[BA-GOV-05](#ba-gov-05-初回リリースノートと互換既知制限表) |
| FR-015 | [BA-FND-02](#ba-fnd-02-apiの起動設定ヘルス共通エラー)、[BA-RUN-08](#ba-run-08-利用量予約集計と同時実行上限)、[BA-UI-11](#ba-ui-11-会員招待role利用量の管理画面)、[BA-OPS-01](#ba-ops-01-terraform-stateproject分離基盤iam)、[BA-OPS-02](#ba-ops-02-firestore索引backup制御bucket)、[BA-OPS-03](#ba-ops-03-cloud-runtasksschedulerの配備定義)、[BA-OPS-04](#ba-ops-04-secretとprovider設定アクセス権)、[BA-OPS-05](#ba-ops-05-oidcimage-digeststaging配備ci)、[BA-OPS-06](#ba-ops-06-本文なしの監視alert利用量集計)、[BA-OPS-11](#ba-ops-11-self-hostの初回導入と設定診断)、[BA-QA-04](#ba-qa-04-負荷遅延待機原価の計測シナリオ)、[BA-QA-06](#ba-qa-06-mvp全受入基準の証跡と公開判定)、[BA-GOV-01](#ba-gov-01-リポジトリ運営連絡先非公開報告窓口)、[BA-GOV-05](#ba-gov-05-初回リリースノートと互換既知制限表) |

## 8. チケットをIssueへ移すときの運用

1. 各詳細節をIssue本文へコピーし、この文書のIDをタイトルまたはラベルに残す。
2. 実際の担当者を割り当て、依存IDを対応するIssue/PRへリンクする。
3. 外部前提を確認し、準備できないものはblockerとして見える状態にする。
4. PR本文へ要件ID、変更後の動作、実施した検証、未実施の理由を記載する。
5. 受入条件を満たしてmergeした後に、Issueと文書の状態を更新する。

チケットを分割する場合は元IDを削除せず、置き換えるIDと理由を残す。依存先を変更した際は循環と未定義IDを確認する。要件の追加・削除はこの文書だけで決めず、01〜06と通信スキーマも合わせて更新する。

今回作成したのはチケット本文と依存計画であり、外部サービスにIssue・PRを登録した状態ではない。製品のコードやGCP資源もこの文書作成によって実装・配備されたものではない。
