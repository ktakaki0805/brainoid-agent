# リマインダーと予定世代

BA-APP-03 / FR-009 は [Reminders](../packages/memory/src/brainoid_memory/reminders.py) を HTTP と既定会話 tool から利用する。認可・transaction・冪等性はノート・タスクの [DirectResources](../packages/memory/src/brainoid_memory/direct_resources.py)、run・結果証拠は [DirectRuntime](../services/worker/src/brainoid_worker/direct_resources.py) を共有する。AC-009 の実配送・周期走査・通知本文取得・既読APIは BA-APP-04/05、UI は BA-UI-07 の範囲。繰り返し予定は提供しない。

## API と時刻

`GET/POST /v1/tenants/{t}/reminders`、`GET/PATCH /v1/tenants/{t}/reminders/{id}`、`POST /v1/tenants/{t}/reminders/{id}:snooze` を提供する。作成・更新・スヌーズは Idempotency-Key、PATCH は If-Match、スヌーズは本文の expected_version を必要とする。取消は PATCH の `status=cancelled`。直接 DELETE や reminders.delete は追加しない。

作成は title・due_at・timezone・delivery_target・visibility・display_policy と、任意の task_id を受ける。UTC 時点は既知の明示 offset 付き入力から共通 UtcTimestamp で正規化し、IANA timezone は別に保持する。入力 offset が時点を決めるため DST の重複時刻も一意になる。offset のないローカル時刻、未知 offset `-00:00`、無効な IANA 名を拒否する。timezone から日時・ユーザーIDを補完しない。

作成・実日時変更・スヌーズの due_at は固定したサーバー時計より未来でなければならない。title だけの編集や取消は期限を過ぎても可能。スヌーズは due_at・timezone・expected_version が全て必須。PATCH は title・due_at・timezone・status=cancelled のみ、1項目以上で null を受け付けない。

| 操作 | version | schedule_version | status |
| --- | --- | --- | --- |
| 作成 | 1 | 1 | scheduled |
| title の編集、同じ日時や状態の再指定 | +1 | 維持 | 維持 |
| due_at または timezone の実変更 | +1 | +1 | scheduled |
| cancelled への変更 | +1 | +1 | cancelled |
| 明示 snooze（同じ日時を含む） | +1 | +1 | scheduled |
| 同じ key・同じ要求の再送 | 維持 | 維持 | 現在の正本 |

日時変更と取消を同じ PATCH に指定すると予定版は一回だけ進み、cancelled が優先する。スヌーズは dispatched/acknowledged/cancelled/expired からも未来へ再設定できる。古い期待版は412、同じキーの別本文は409。同一要求の再送は24時間の共通 receipt と現在認可を使い、既に期限を過ぎた確定済み効果も再実行せず照合する。

一覧は status、limit（既定20、最大100）、cursor を受ける。ID順で現在認可・可視版集合・条件に cursor を束縛し、ReminderPage の next_cursor は末尾でも null として返す。現状は tenant 集合から各正本を認可するため、ページ件数は DB 読取量の上限ではない。

## 権限と通知先

private は本人所有、shared の編集は作成者または現在の owner/admin。Pi 書込・スヌーズは reminders scope を持つ本人の現在有効な private session が必要。現在の tenant・membership・account・device・独立 control ledger を共通境界で再検査する。

user の通知先は opaque UID の完全一致で同 tenant の有効会員・account を確認し、UID を DB path にしない。private の user 宛先は所有者本人のみ。device は UUID と同 tenant の現在有効な端末を確認する。private の device 宛先は authenticated_session の場合だけ受理する。受理は将来の内容表示権限を与えない。BA-APP-04/05 は配送・再生・取得の直前に、端末能力、現在の所有者本人の reminders session、出力先、表示方針と予定世代を再確認し、未確認共有端末へ private 本文を出してはならない。push payload に私的本文を入れないという既存方針を維持する。

task_id は現在閲覧できる同 tenant の Task に限る。private Task を shared Reminder に関連付けない。取得・編集・tool result 再利用・export でも現在の Task 可視性を確認するため、関連 Task が削除・失効した Reminder は利用不可となる。task_id から本文や権限をコピーしない。Task 復旧時も既存の独立削除 fence を尊重する。

## 遅延イベントの共通判定

型付き ScheduleReference は tenant_id・reminder_id・schedule_version だけを持つ。`current_schedule(record, reference)` は正本の tenant・ID・予定版を一致確認し、削除・cancelled・expired を拒否する。title だけの変更では同じ予定として扱う。APP-04/05 の job・通知・ack・完了処理は、現在認可した同 transaction の正本にこの判定を使い、合格後だけ自身の状態遷移や配送 claim を実行する。判定自体は認可や配送 claim を発行しない。古い通知をクライアントで開く場合も最新世代を取得して確認する。

T-038 は通知済み正本の snooze 後、古い job・ack・完了を遅延投入し、この実際の共通関数が新しい due/status/世代への書込を遮断することを fake と Firestore transaction で検証する。実際の配送、音声、通知 provider は呼ばない。

## 明示した会話操作と回復

既定 tool は reminders.create/list/update/snooze。作成は `Create reminder {全作成引数JSON}` または `リマインダーを作成 {同じJSON}`、編集は `Update reminder UUID: {changes JSON}`、スヌーズは `Snooze reminder UUID: {due_at/timezone JSON}` と direct user の全文を照合する。JSON はキー辞書順、Unicode を escape せず colon/comma 後に空白を入れ、日時は UTC 正規化後の文字列を使う。会話 scope と要求 scope を一致させ、取得文書・派生 user message・否定文から書込権限を導かない。モデルの成功文章だけでは正本を変更しない。

一覧 tool は status と limit（既定10、最大20）、結果は48KiB以内。run・lease・allowlist・現在の正本・再利用抑止・利用先 audience を再検査する。reminder_effects は固定 effect ID・要求・期待版・確定正本を原子的に保存し、snooze は内部の型付き再予定要求で update と区別する。started/unknown の回復は証拠照合で行い、元効果を再実行しない。

更新・取消・スヌーズの確定 transaction は、消費した一覧・memory・継続入力・途中文章を退役し、本文なしの依存と run/call/effect 結合を残す。その後モデルを再実行せず「リマインダーを更新しました。」で完了する。外部編集、権限失効、lease切れ、別runの marker では完了通知を出さない。

## Export・削除・移行

MEM-09 は明示的 ReminderView と content-free manifest を使い、actor・内部 effect・再利用制御は出力しない。現在版・現在認可・task/通知先を再検査する。member/account/tenant lifecycle は reminder 正本と reminder_effects の private コピー、private 会話に所属する run/tool/history を既存 inventory で消去する。shared 寄稿保持と独立 ledger の保存方針は維持する。reminder 単体の物理削除 API はこのチケットでは公開せず、既存 note/task/Fact/immutable DeletionIntent の JSON・所有規則を変更しない。

既存データの一括移行や依存追加はない。切戻し時は reminder API/tool admission を止め、未確定 run/effect と受理済み lifecycle purge・export を対応版で回復・完了させる。先に旧workerへ切り替えたり、effect・ledger・fence を削除して再実行しない。

[HTTP/tool/T-038](../tests/test_reminders.py)、[結果現在性・回復](../tests/test_reminder_regressions.py)、[lifecycle](../tests/test_reminder_lifecycle.py)、[Firestore競合・復旧](../tests/emulator/test_reminders_integration.py) を提供する。実cloud・provider・本番索引・実機通知は未検証で、BA-APP-04/05 と後続結合確認で扱う。
