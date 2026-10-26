# 永続 job・dispatcher・worker

BA-RUN-01 は業務更新と outbox を同じ transaction で保存し、プロセス停止後も同じ job を回収する。対象は [jobs](../packages/jobs/src/brainoid_jobs/repository.py)、[dispatcher](../services/worker/src/brainoid_worker/dispatcher.py)、[worker](../services/worker/src/brainoid_worker/entry.py)。会話・通知・purge の handler と実 Cloud Run/Tasks 配備は、それぞれ BA-RUN-04 / BA-APP-04 / BA-MEM-08 / BA-OPS-03 で追加する。

## 保存と source の版

`Jobs(store, ledger, ...)` は独立認可台帳を必須にする。`add_pending(tx, operation_id=..., kind=..., grant=...)` を業務の read・認可後、write 前に呼び、残りの業務 write も同じ `tx` に行う。job ID は tenant・handler kind・業務 operation ID から UUIDv5 で決定する。同じ ID の異なる grant/設定を上書きしない。transaction callback が再試行されても queue や provider を呼ばない。

API repository の callback では、同じ現在状態 reader で grant を得て次の順に保存する。`operation_id` は既存の冪等受付で確定したIDを使う。

```python
def accept(tx):
    grant = transaction_policy(tx, ledger=ledger).resource(
        tenant_id, verified_subject, source_ref, action=Action.WRITE,
    )
    # この時点までに業務readを済ませる。以後は同じtxでwriteだけを行う。
    job = jobs.add_pending(tx, operation_id=operation_id, kind="example", grant=grant)
    tx.create(operation_path, {"job_id": job.id, "status": "accepted"})
    return job

job = store.run(accept)  # commitが成功してから受付を返す。
```

source 自体を更新する場合は `source_replacement` を渡す。この場合 WRITE grant と現在版を再検査し、version がちょうど +1 で、scope・owner・creator・削除 epoch・room/entity 等の認可 metadata が不変であることを確認する。repository が replacement と新版 grant の job を同時に保存する。更新前 grant の job を作ってから source だけ更新する使い方は、worker が `source_changed` で拒否する。scope/削除状態の遷移、新規子 resource の作成、親と run/message 等の子の版の解決は各業務 repository の責務であり、この汎用更新オプションに委ねない。

保存場所は `tenants/{tenant_id}/jobs/{job_id}`。正本には initiator の grant、source の version/deletion epoch、出力先、global account stamp、利用可能時刻、試行数、配送世代、lease 世代と期限、checkpoint、固定終了理由を持つ。配送 payload は tenant/job ID と配送世代だけで、本文・主体・grant を渡さない。クライアントの任意 job 書込みを許可する API は提供しない。

## 配送と回収

`Dispatcher.recover(tenant, after=None, limit=100)` は最大100文書のID順ページを読む。各候補は個別 transaction で読み直し、準備した配送世代を保存してから queue に投入する。戻り値の `next_cursor` を次ページへ渡し、走査完了後は次の巡回を先頭から始める。呼出し側は cursor と巡回スケジュールを保持する。tenant の列挙・Scheduler・本番の選択的 due query/index は BA-OPS-03 の配備側で接続する。各呼出しの read は有界だが、終端 job を含む全巡回の費用は保持件数に比例する。

同一配送試行の Tasks 名は `{job_id}-d{dispatch_generation}`。enqueue 前の失敗、enqueue 後の応答喪失は保存済みの同じ名前で再試行する。成功 ACK 後も job が未開始なら既定2分後に新しい配送世代を作る。Cloud Tasks の削除後の名前保持を永久な完了証明にしない。古い配送世代・順不同要求は実行しない。Cloud Tasks の[明示名と重複抑止](https://docs.cloud.google.com/tasks/docs/reference/rest/v2/projects.locations.queues.tasks/create)に従い、同名の `ALREADY_EXISTS` だけを受付済みとして扱う。

`pending → running → succeeded / obsolete / failed` が基本遷移。lease は既定60秒、同時配送では一つの transaction だけが取得できる。正常な `check(renew=True)` は現在の認可と未失効の同一 lease を確認して延長する。期限ちょうどから旧 lease を無効にし、回収時に配送世代を進め、次の取得で lease 世代と試行数を増やす。旧世代・期限切れの結果を commit しない。既定5回・最大20回の試行上限に達したもの、未知 handler、壊れた job は固定理由付き `failed` にする。未知 handler を後で登録しても終端 job は自動復活しない。

## 認証・現在の認可

[HTTP factory](../services/worker/src/brainoid_worker/app.py) の `/tasks` は4KiBまでの payload を受け、本文を含まない400/401/413/503を返す。worker は注入された verifier を最初に呼び、正本 job の tenant・配送世代を照合してから処理する。Tasks の名前や任意の `X-CloudTasks-*` header を認証に使わない。

[GoogleCaller / CloudTasksQueue](../services/worker/src/brainoid_worker/cloud_tasks.py) は具体的な OIDC 検証・REST 送信 adapter。署名、issuer、有効期限、audience を公式 google-auth で検証し、さらに構成した service account email、subject、email_verified を検査する。queue path、HTTPS worker URL、audience、service account、access token supplier は構成時に明示する。SDK default credential の探索や、認証失敗時の local fallback はしない。Google の [HTTP task OIDC](https://docs.cloud.google.com/tasks/docs/creating-http-target-tasks) と [token 検証 API](https://googleapis.dev/python/google-auth/latest/reference/google.oauth2.id_token.html) を利用する。

lease取得、外部呼出し開始、checkpoint 結果保存、最終業務commitで `Evaluator.worker(saved_grant)` を再実行する。membership/device の独立台帳、private session の slot・認可版・期限、global account の削除/版、source の現在版・削除 epoch を読み直す。保存 stamp だけでは許可しない。`allow_untracked_bootstrap` の既定は false。明示した local/bootstrap fixture でも、tracked な membership/device が台帳から消えた場合に迂回できない。理由は `authorization_changed` / `account_changed` / `source_changed` / `source_deleted` で追跡する。

独立台帳が読めない、または台帳内容の破損により現在権限を確定できない場合は、確定した失効と区別してworkerは本文のない503を返す。lease取得前ならpendingを保持し、実行開始後なら同じlease世代を条件に制御metadataだけをpendingへ戻す（`authorization_unavailable`）。この中断はhandlerの試行上限を消費しない。副作用と業務結果のcommitは行わず、復旧後に同じjobを回収して現在認可を再検査する。`completed` checkpointは再利用し、既に外部呼出しを開始して`started`だけが残る場合は `outcome_unknown` として結果照合を待つ。台帳が復旧しただけで外部呼出しを繰り返さない。実際の失効・source削除は従来通りobsoleteとなる。APIの既存policy例外変換は変更せず、台帳確認不能も従来の公開拒否コードへ変換する。

DB adapter の `PersistenceUnavailable` もworkerの既知の基盤障害として扱い、恒久的な `handler_error` には変換せず503を返す。読取、開始marker、checkpoint結果保存、最終commit、失敗記録のどの境界でも同じ扱いである。制御transactionが利用できれば、正本jobの状態・lease世代・checkpointを読み直して `persistence_unavailable` で再待機する。commit応答喪失でも既にsucceeded/failedになったjobを上書きせず、completed checkpointを再利用し、startedしか確認できない副作用はoutcome_unknownで止める。単なる例外を「DBへ何も保存されなかった」証明にはしない。認識済みの基盤例外と `RetryableError` 以外のhandler例外は従来通り恒久失敗とし、外部provider固有の一時障害はhandlerがcheckpointと整合する形で明示分類する。

DBが中断の制御記録も書けないまま不可用なら、workerは503を返して保存済みlease/試行数を残す。DB復旧後はlease期限切れを通常回収し、残試行数があれば再開、上限ならattempts_exhausted、startedがあればoutcome_unknownになる。lease取得のcommit応答を失って自分の取得世代を確定できない場合も、他workerのleaseを推測して解放せず同じ回収に委ねる。DBへ中断を原子的に記録できなかった試行の予算返却や、残試行数ゼロからの自動復活は保証しない。DB停止中に回収・結果照合が完了したとは扱わず、運用側の復旧と必要な業務reconcilerを待つ。

共通 transaction port は [contracts](../packages/contracts/src/brainoid_contracts/transactions.py)、SDK adapter は [persistence](../packages/persistence/src/brainoid_persistence/firestore.py)、正本認可 reader は [policy](../packages/policy/src/brainoid_policy/transactions.py) に置く。worker は API 内部 repository を import しない。API の従来 import は互換 shim とし、APIError の公開変換を維持する。policy/core は HTTP・SDK を import しない。

## handler と外部副作用の境界

handler は `run(execution)` と `commit(tx, job, result)` を実装する。前者は DB transaction 外で走り、外部呼出しには `execution.effect(step_key, invoke)` を使う。後者は最新認可・leaseを検査した同じ transaction で呼ばれる DB 専用 callback で、read を write より先に行う。commit callback は再試行されるため、provider・queue・外部台帳への書込みを行わない。

`effect` は固定 step key の `started` を保存してから provider を呼ぶ。同じ lease/key を並行しても開始を新規保存した呼出しだけが invoke できる。provider には `{job_id}:{step_key}` の安定キーを渡す。`completed` は保存結果を返し、lease 回収後の再実行でも provider を再呼出ししない。開始後の通信断・process停止・期限切れでは `outcome_unknown` を保持し、自動再呼出ししない。後続 handler は provider の冪等キー・結果照合・業務 checkpoint を実装し、結果不明からの回復を設計する必要がある。

この基盤は外部 provider と DB を原子的に commit せず、exactly-once を保証しない。開始記録直後の権限変更や lease失効は既に開始した外部呼出しを取り消せないが、その結果を古い認可/leaseで DB に確定しない。外部呼出し前に結果が不明な仕事を盲目的に再実行しない。長時間処理は安全なステップ間で `check` / `renew` し、provider 呼出しの timeout と idempotency を handler が設定する。

## local 実行と検証

`LocalQueue` と `LocalCaller` は明示的な local composition 用。固定時計と fake provider を使い、次のように同じ dispatcher/worker を実行する。`store` は構成済みの `FirestoreTransactions` またはテストの transaction store、`ledger` は独立した正本台帳、`handler` は上記契約の実装を渡す。

```python
jobs = Jobs(store, ledger, now=clock.now)
queue = LocalQueue()
dispatcher = Dispatcher(jobs, store, queue)
worker = Worker(jobs, LocalCaller(), {"example": handler})
page = dispatcher.recover(tenant_id)
queue.drain(worker)
```

通常の `mise run test` は [unit](../tests/test_jobs.py) で enqueue 前後の故障、名前保持、同時配送・同一step、lease奪取、認可/削除/source版/account変更、最大試行、poison job、固定HTTPエラー、REST/OIDC adapter を検証する。`mise run test-emulator` は [結合](../tests/emulator/test_job_recovery.py) で同一transactionの新版source/jobとrollback、別Python processの再起動回収、実SDK競合、commit前失効、名前保持と有界cursorを確認する。設定・起動停止は[既存のemulator手順](05-development-and-testing.md#52-authfirestore-emulatorの起動と初期化)に従う。

追加した外部distributionはない。既存の固定済み Firestore/Pydantic/FastAPI を新memberでも宣言し、google-auth 2.62.0 の requests extra をworkerの直接依存にした。Google保守・Apache-2.0・Python 3.13対応は既存lockと導入metadataで確認し、[公式source](https://github.com/googleapis/google-cloud-python/tree/main/packages/google-auth)を参照した。旧単独repositoryはarchive済みで、保守先はgoogle-cloud-pythonへ移管されている。署名鍵取得と、明示構成したTasks endpointへの送信が実adapterの外部接続であり、IAM権限付与・資源作成は行わない。通常テストはネットワークを代替し、emulator試験はdemo/loopback限定。配布時は既存依存と推移依存のlicense表示を維持する。

既存DBの移行は不要。新規jobはschema_version=1で保存する。切戻し時はdispatcher/workerを停止してjobを保持し、本文・grantをログへ出さず固定理由を運用で確認する。互換性のないjobを旧workerに処理させない。本番Tasks/OIDC/IAM、Scheduler、索引・負荷検証、業務handlerの結果照合はこのローカル合格の範囲に含めない。

BA-RUN-07の確認待ちは`waiting` jobとしてleaseを解放し、通常recover/acquireは配送しない。内部確認transactionが同じcanonical jobをpendingへ戻し、dispatch/lease世代を進める。正常な待機移行は失敗attemptを消費しない。業務toolは`execution.effect`と同等のstart/result fenceを持つExecutorを利用し、モデルcheckpointとは別に一回開始・結果再利用を保証する。詳細は[確認checkpointと再開](conversations-and-runs.md#確認checkpointと再開)を参照。

BA-RUN-08の[利用量照合](usage.md)はworker終端と周期的sweepで実行する。queue詰まり・worker強制終了はrunのhard期限で停止し、未知のprovider消費量を予約額のまま保持する。
