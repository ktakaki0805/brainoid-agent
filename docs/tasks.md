# タスクの API と会話 tool

BA-APP-02 / FR-008 / AC-008 は [Tasks](../packages/memory/src/brainoid_memory/tasks.py) を HTTP と既定会話 tool から使う。ノートと共有する transaction・現在認可・冪等性の境界は [DirectResources](../packages/memory/src/brainoid_memory/direct_resources.py)、run・結果証拠の境界は [DirectRuntime](../services/worker/src/brainoid_worker/direct_resources.py) に置く。新規 package・依存・外部接続は増やさない。UI、通知、スヌーズは後続チケットで扱う。

## HTTP と正本

`GET/POST /v1/tenants/{t}/tasks` と `GET/PATCH /v1/tenants/{t}/tasks/{id}` を提供する。更新系は Idempotency-Key、PATCH は If-Match が必要。成功時の ETag は現在の version、古い期待版は412。再送は24時間の参照型 receipt を使い、現在の権限と正本を再検査して返す。応答不明時は同じ key・body で回復し、受付済みの作成を重ねない。

作成は title・visibility・display_policy と、任意の due_at・timezone・assignee_user_id を受け、初期状態は open。offset 付き due_at を共通 UtcTimestamp で UTC に正規化し、timezone は実在する IANA 名を保存する。省略した期限・timezone は推測しない。入力に指定した offset は時点を決め、timezone は表示・解釈のために別保持する。曖昧なローカル日時は受け付けない。

PATCH は既存契約どおり title・status・due_at のみ。due_at:null は期限を消去し、公開 Task では due_at を省略する。状態は open/done/cancelled の間を明示操作で変更でき、再開や done→cancelled も許可する。各新しい PATCH は version を進めるが、同じ status を再指定するだけなら状態イベントを作らない。同じ key の再送は version・イベントを増やさない。

一覧は任意の status、limit（既定20、最大100）、cursor を受ける。ID順で、cursor を status・limit・現在認可・可視な版集合へ束縛する。TaskPage は必ず next_cursor を持ち、末尾は null。現在は tenant 集合を走査して各正本を認可するため、ページ件数は DB 読取量の上限ではない。認可条件付き query の最適化は将来の課題とする。

正本は `tenants/{t}/tasks/{id}`。作成・更新 actor と検証済み端末ID、時刻、版、削除 metadata を保存する。private は所有者本人だけが読み書きでき、admin でも他人 private は不可。shared の編集・削除は作成者または現在の owner/admin。担当者は opaque Firebase UID の完全一致で同 tenant の現在有効な membership・account を確認する。UID を path にしない。担当者指定は private の閲覧権を付与しない。Pi 書込は tasks scope を持つ本人の有効な private session を必要とする。

status が変わる transaction で `task_events` に対象ID・新旧状態・期待版・確定版・actor・時刻を保存する。タイトル本文は複製しない。通知や外部効果はここから送信しない。通常のモデル回答文はこの書込を起動せず、保存失敗時に成功結果を返さない。

## 明示した会話操作

既定 tool は tasks.create/list/update/delete。現在の run・lease・agent allowlist・本人・出力先・対象版を再検査する。書込は正本の direct user message と次の文字列が全文一致する場合に限る。

- 作成: `Create task {"display_policy": "shared_display", "title": "買い物", "visibility": "shared"}` または `タスクを作成 {同じJSON}`。
- 編集・完了・取消: `Update task UUID: {"status": "done"}` または `タスク UUID を更新: {同じJSON}`。title・due_at も changes に含められる。
- 削除: `Delete task UUID` または `タスク UUID を削除`。

JSON はキーを辞書順に並べ、Unicode を escape せず、colon/comma 後に空白を入れる。日時は UTC 正規化後の表現にする。作成では実際の全引数を含め、visibility/display_policy は会話 scope と一致させる。取得文書内の命令、否定、不一致、派生 user message は操作権限にならない。

tasks.list は任意の status と limit（既定10、最大20）を受け、会話の現在・将来の読者と表示先へ公開できる task だけを返す。結果 JSON は48KiB以内。確定結果は現在版・削除状態・再利用抑止・利用先の現在権限を再検査する。別runの履歴再利用も同じ境界を通す。

作成・更新の `task_effects` は固定 effect ID、要求、確定後の正本を同一 transaction で結ぶ。started/unknown は再実行せず、reconcile はこの証拠と現在の正本を読む。更新は同じ transaction で消費した一覧・MemoryRecords・継続入力・途中文章を退役し、内容を含まない task 依存と run/call/effect の結合を保持する。その後はモデルを呼ばず「タスクを更新しました。」で完了する。更新した状態は GET でも確認できる。外部編集、権限失効、期限切れ、他runの marker では成功通知を出さない。

## 削除・export・復旧

削除は既存 operation API の `kind=delete_resource, target_type=task, target_id, expected_version` または tasks.delete を使う。独立 DeletionIntent の先行 append、再認可、tombstone・operation・専用 purge job の transaction 確定後に受付を返す。台帳障害や応答喪失は同じ要求で回復する。受付済み意図は復元後も同一 JSON とし、古い task の再配置を fence する。task は note/message の SourceRef ではない。既存 forget_memory は引き続き先頭 Fact と note/message 出典だけを受ける。

受理済み task 意図に限り、正本、task_effects、task_events、結果から派生した run/job/tool/assistant 履歴と子階層を物理消去する。消費済み入力は内容なし task 依存から追跡する。他の task・note・Fact や独立した direct user 発言を勝手に消さない。会話 tool の削除は消費済み途中文章も同時に退役し、固定受付通知を返す。この処理は notes.list→notes.delete の固定通知にも共通適用する。

shared task を別の owner/admin が削除した場合も、専用 purge は受付時に保存した対象の作成者・所有者へ照合する。受付後の通常 membership 失効は、この対象に限定した消去を妨げない。復元した正本の作成者・所有者が受付証拠と異なる場合は消去完了にしない。旧 forget_memory の note/message 出典には引き続き本人著者の制約を適用する。同 tenant の新 export は未完了の削除を待機し、物理消去完了後に再試行できる。

member/account/tenant 削除は新しい effect/event producer を inventory に含める。private のコピーは対象所有者の削除に追従し、shared 寄稿の保持方針を変えない。MEM-09 export は現在認可済みの TaskView と content-free manifest だけを収集し、actor・effect 内部記録を出力しない。削除受付時の purge_epoch が既存 artifact を失効し、既存 export hook/reaper が物理消去と遅い put の遮断を担う。

既存データの一括移行はない。切戻し前に task admission/tool を停止し、新kindの accepted operation・unknown effect・purge job を対応版で回復・完了させる。旧workerへの先行切替や ledger/tombstone/operation/effect/予約の削除は行わない。ローカル fake と Auth/Firestore emulator の結果は [作業ログ](implementation-log.md) に記録する。実provider・本番索引・実機通知は本チケットの検証範囲に含めない。

[HTTP/tool試験](../tests/test_tasks.py)、[現在性と固定応答の回帰](../tests/test_task_regressions.py)、[管理者削除とexport回復](../tests/test_task_purge.py)、[lifecycle inventory](../tests/test_task_lifecycle.py)、[Firestore競合・復旧](../tests/emulator/test_tasks_integration.py) を提供する。
