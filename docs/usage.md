# 利用量の予約・精算

BA-RUN-08 / FR-015 / T-026の実装。本文を持たない[共通ledger](../packages/usage/src/brainoid_usage/ledger.py)で、会話turnの受付と各provider呼出しの資源消費を別々に記録する。音声adapter、課金、価格、dashboard、配備先Schedulerは後続チケット。

## 集計と上限

`tenants/{tenant}/usage_policy/current`はサーバー管理の[Policy](../packages/usage/src/brainoid_usage/models.py)。クライアントから更新するAPIはない。未設定時の初期値はtenantが月10,000要求・同時32run、user/deviceが月2,000要求・同時8run。各単位のtokenは月100,000,000、音声は月36,000秒、runの有効期間は受付から1,800秒、1回のmodel入力予約は262,144token。これらはローカル運用の設定値であり、販売枠・請求額・本番原価の確定値ではない。設定はstrict検証し、上限は0以上、run期限は60〜3,600秒。単位は整数のinput/output token、切上げた音声秒。token上限はinputとoutputの合計。

月はUTCの`YYYY-MM`。受付要求は受付月、provider資源はその呼出し月に計上する。月が変わっても既存runの同時枠は維持する。tenant全体と、起点のuser/device全てを同じtransactionで検査・更新する。private sessionのPi要求はtenant・本人・deviceに計上する。スマホからPiへの表示は起点userに計上し、表示先deviceへの二重計上はしない。UIDは不透明値としてSHA-256化し、pathへ直接連結しない。

turn受付の要求数・同時枠はmessage/run/job/outbox/idempotencyと原子的に保存する。競合時も上限を超えず、拒否した要求はこれらを作らない。同じIdempotency-Keyの再送は再計上しない。超過は共通`429 rate_limited`、`retryable=true`、`Retry-After: 60`を返す。60秒は再確認までの待機であり、枠が回復する保証ではない。月次消費は月変更まで、同時枠はrun終端・照合まで待つ場合がある。受理後にprovider段の枠へ到達した場合はrunの`error_code=rate_limited`で終了する。

## providerと精算の共通契約

`UsageLedger.prepare_admit`はDBのreadを行い、writeだけのclosureを返す。呼出側は他の認可・業務readを終えてからclosureを実行する。`reserve(tx, tenant, run, key, Amounts)`はliveなrun、現在policy、全scopeの資源残量を同じtransactionで検査する。開始済み予約を再実行の許可に使えない。同じkeyは拒否し、終了結果の再利用はdurable job checkpointが担う。1runは予約64件まで、modelは既存の最大8段を維持する。

RunTurnは各`model`/`model.N`の外部呼出し前に固定IDで予約する。modelの入力予約は設定の固定予算、出力予約はCoreの`max_output_tokens`。MeteredModelは本文・署名・tool定義を含む実際の要求JSONのUTF-8 byte長が入力予算以内であることを検査してから、元providerへ要求をそのまま渡す。これは保守的なアプリの入力量予算であり、provider固有のtokenizerや思考tokenを含む請求量を再現しない。完了したproviderが報告したinput/output累積値を各々一度だけ精算し、cached inputを重ねて加算しない。Core内部の長さ打切り・途中失敗で観測した途中累積値を最終値と扱わない。MeteredModelはUsageをCoreへ渡す前に単位別の累積最大値を捕捉する。Coreが直後の認可・event数・出力token数検査でそのeventを処理せず終了しても、捕捉した量を下限として精算へ渡す。本文の返却権限とは独立した消費事実である。

`settle(tenant, run, key, Settlement)`はadapterが確認した最終値だけを受ける。不明な単位は`None`を渡す。`observed=Amounts(...)`で途中観測の累積量を渡せる。不明な単位は予約額と保存された観測下限の大きい方を維持し、片側だけ既知ならその側だけ精算する。観測下限は単位ごとの最大値を保存し、少ない後発報告や同じ報告の再送で下げない。`observe(...)`は実行中に下限だけを永続化する共通interfaceであり、provider終了や同時枠解放の根拠にはならない。provider429・接続断・worker異常終了を0消費と断定しない。実値が予約を超えた場合は超過も全て加算し、後続予約を抑止する。明らかにprovider未呼出しなら0確定できる。音声adapterも`Amounts(voice_seconds=...)`と同じ契約を使い、後から確認した最終秒数で精算する。

精算はrunの現在認可・leaseと独立した、サーバー内部の消費事実の記録。失効後・期限後に返った確定量も元の予約月へ反映できる。本文やtool結果を公開する権限にはならない。確定済みの値は異なる確定値で上書きできない。最終値が観測下限を下回る場合は拒否するが、同時に渡された有効な高い観測下限は保存する。確定後にそれを上回る累積量を観測した場合は矛盾した確定状態を解除し、予約額・観測下限以上をunknownとして保持してから、十分な最終量の照合を要求する。古い予約に観測下限fieldがない場合は観測0として読み、予約額を維持する。不明から確定への変更は差分更新で、競合・同じ結果の再送でも二重加算や二重返却はない。HTTPからこの内部interfaceや任意userの明細を呼ぶ入口はない。

## 待機・取消・異常終了と照合

queued/running/waiting_confirmationは同時枠を保持する。確認待ちでworker leaseを解放しても利用枠は解放しない。再開は同じrun・要求枠を使い、次のmodel段では新しい資源予約を必須とする。期限は待機や再開で延長しない。

正常終了は結果commitと同じtransactionで同時枠を解放する。取消はjob世代をfenceして以後の実行を止める。開始済みprovider予約が残る場合、取消だけではprovider停止を証明できないため同時枠を維持する。workerが戻った後の照合、またはhard期限後の照合が枠を解放する。未知の資源消費はどちらでも予約額・観測下限以上を維持する。予約額の保持と同時枠の解放は別処理である。

[UsageReconciler](../services/worker/src/brainoid_worker/usage_reconciler.py)は各transactionでrun/job/予約/slotを読み直す。terminal jobを照合し、期限を超えたqueued/running/waiting runは既存cancellationでjob/run世代を停止して枠を解放する。残るstarted予約はunknownとして保持する。lease失効だけで資源を返却しない。期限は論理的なrun実行枠の上限であり、停止不能な外部providerをDBから強制終了する保証ではない。providerは既存の有限deadline/cancellation契約を守る必要がある。

通常workerは処理終了後に照合する。プロセス強制終了・queue詰まりは周期的なbounded sweepで回収する。Dispatcherのrecoverと同じ運用ループから、次の処理をtenantごとに呼ぶ。ページのcursorを次回へ引き継ぎ、一巡後は先頭へ戻す。Scheduler/Cloud Runの配備設定はBA-OPS-03で接続する。

```python
reconciler = UsageReconciler(jobs)
next_cursor = reconciler.recover(tenant_id, pages, after=cursor, limit=100)
```

予約・集計は`usage_runs`、`usage_reservations`、`usage_buckets`、`usage_slots`に保存する。tenant/run ID、scope hash、期間、状態、期限、単位別数値だけを持ち、会話本文・provider署名・tool引数・例外本文・credentialを保持しない。本文なしの開始・精算履歴として予約記録を残す。unknownの消去・新keyでの再発行を復旧方法にしない。

## 利用量APIと検証

`GET /v1/tenants/{tenant_id}/usage`はuser bearer専用。現在account・membership・認可台帳を検査し、memberへ本人の集計、owner/adminへtenant総量を返す。query selectorは拒否し、他人の明細APIは提供しない。既存OpenAPIの`period/turns/voice_seconds/remaining_turns/quota_state`を維持する。音声秒は未確定の予約も含む保守的な使用量。memberの`remaining_turns`は本人枠とtenant枠の小さい方で、`quota_state`は両方の枠を評価する。owner/adminは`user=None`でtenant枠だけを評価し、残り回数と状態は本人の枠を含まない。各集計範囲の月次枠到達でexhausted、同時枠到達でlimited、それ以外availableを返す。管理者のtenant集計がavailableでも、本人の月次枠・同時実行枠に達していれば同じ本人のturn要求は429となる。本文・個別UID・token内訳は公開しない。

[fake試験](../tests/test_usage_limits.py)、[確認後の次段予算](../tests/test_run_checkpoint.py)、[Firestore試験](../tests/emulator/test_usage_limits_integration.py)で競合予約、429、queue期限、provider不明量、音声精算、月跨ぎ、取消・再開、workerプロセスの強制終了、再照合と遅延結果、本人/admin/別tenantを検証する。実provider・本番負荷・請求照合はこのチケットの検証範囲外。

新memberは既存固定版Pydanticとcontractsだけに依存し、framework/SDKの依存を持たない。既存distributionの更新はない。導入前は旧workerへの配送を止め、既存active runを完了または停止してから再開する。利用量予約がない旧active runを新workerが実行することは拒否する。過去利用量の遡及推定は行わない。切戻しでもledgerを保持し、旧workerへ未処理runを渡さず、unknownを削除して枠を戻さない。shared databaseで新旧workerを同時に動かすローリング移行は未対応。

BA-UI-11 の[会員・招待・利用量の画面](ui-tenant-settings.md)は、上記APIの現在権限・版・固定要求の再送契約を利用する。

BA-VOICE-01 の独立STTは同じ usage_runs/reservations/slots を `purpose=audio_input` で使う。音声秒は実PCM長の切上げを受付時に原子的に予約し、会話turn数は増やさない。通常turnへ投入した時に1回だけturnを加算する。STT実行期限60秒（provider待機上限45秒）後は既存 UsageReconciler でslotを回収し、遅い結果を拒否する。provider最終使用量がない失敗/取消はunknownとして予約額を保持する。実呼出し前の確定拒否だけ0秒に精算する。詳しくは[音声入力](audio-inputs.md)。
