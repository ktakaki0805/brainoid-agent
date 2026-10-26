# Scope付き記憶検索と会話への組込み

BA-MEM-04はFR-006とT-002/T-020/T-029に対応する。`POST /v1/tenants/{tenant_id}/memories:search`は既存OpenAPIの`MemorySearch`を受け、`MemoryPage`を返す。`Idempotency-Key`は16–128文字で必須だが、読取だけなのでreceiptを作らない。最大40件、cursorはnull。検索を使うための索引job完了は不要。

## 検索と探索

`brainoid_memory.retrieval`は最初に現在credential/account/member/台帳/device/sessionと出力先を確認し、visibility・display_policy・private ownerをstorageの等値queryへ渡す。sharedと本人privateは別queryで、意味検索providerへ渡す前にcanonical Factと全依存先を認可する。非公開candidateの本文・ID・件数はprovider入力、結果、traceへ入れない。Piでは追加の出力先allowlistと現在sessionを検査し、記憶アクセスでidle期限を延ばさない。

Entityのlabel/aliasと、認可済み正本external_refsの外部ID完全一致で候補を解決し、同名候補は別IDのまますべてseedに残す。先頭を本人と断定したり、名前で統合したりしない。同じ外部IDが別integrationの異なるEntityにある場合も複数候補とし、外部IDの一致を権限付与に使わない。traceの`ambiguous_entities`に曖昧さを記録し、Coreのreference framingは異なるIDを別候補として利用者へ確認するよう指示する。private Factがshared Entityを参照する場合も、その可視Entity名からprivateの辺を探索できる。辺はそのFactのscope内だけで辿り、private辺をshared探索へ混ぜない。

意味検索境界`SemanticCandidates`の初版はDB内で動く決定的な`LexicalCandidates`で、実embeddingではない。providerは候補ID/version/epochを返し、必ずscope内のcanonical値へ照合する。出典の版、confirmed、有効期間、削除epoch、suppress_reuse、参照Entity、訂正リンクを各利用時に再確認する。実vector adapterと待機索引jobの起動はMEM-05で実装する。現段階はscopeごとのcanonical候補走査を使い、DB read総数の固定上限を保証するものではない。

探索は検索全体で最大2hop・40fact・20entity。要求limitは40以下の追加のfact上限になる。visited集合で循環を止める。traceには可視の探索fact/entity数、最大hop、fact/entity/hop上限到達と曖昧さだけを保存する。検索文、本文、ID、落とした候補数をtraceへ記録しない。ちょうど上限に達したscopeで止まり、後続scopeやqueueを未探索のまま残す場合にもglobal上限到達を記録する。残りにデータがあるか、非公開かを数えて判定しない。HTTPはcontent-freeログ、workerは内部runの`memory_trace`に記録する。

## 現在の出典と会話の読者

workerの`BoundMemory`はCoreのMemoryStore検索portへ接続し、検索結果をrunの`memory_records`へ保存してからモデルを呼ぶ。直接入力の空source_refsはuser_explicitと正本created_by/recorded_atで証明し、架空のmessage参照を生成しない。モデル向けにはresource ID・content/source_refs・version・記録時刻・origin/statusだけを明示投影し、内部tenant/owner/creator UIDを除く。full canonical MemoryRecordはサーバー側の認可と出典証明に保持する。自動検索とmemory.search/remember/reviseのToolReplyは同じ投影を使う。モデルへは出典とversionを含むReference dataとして渡し、記憶文をsystem命令や実行権限にしない。

現在の本人が読めるだけでは、shared会話へ保存できる証明にならない。会話のvisibility/owner/display_policyと現在出力先の両方に収まるscopeだけを検索する。各model event前、stream書込、最終保存、次turnのhistory読取も正本を再照合する。最終messageに`memory_records`を継承するため、後からsourceを忘却/失効させても古い応答を履歴経由でモデルへ戻さない。全読取後と遅延応答後にもcaller/session/factの期限を確認する。共有Jobs storeはtransaction単位の`GuardedTransactions`で、消費したcurrent/history/tool結果/自己訂正の過去入力の有効区間とcaller/outputのlease/session/credential期限を保持する。各write直前・callback完了時・DB応答後に、保存した根拠と時計だけを使って再検査する。全証跡のno-I/O capabilityをReadBridgeのasync受渡し後まで保持し、Coreの認可待機cleanup後とprovider開始Task内の同期releaseでも再検査する。MeteredModelのbyte計量後・実provider呼出し直前にも同じcapabilityを検査する。新規検索が空でも履歴/tool/自己訂正入力の期限を保持する。guardに追加DB読取や期限延長を含めず、別transactionのfailed/cancel/usage解放へ持ち越さない。commit後の応答遅延で期限を超えた場合は本文を返さないが、期限内に確定した保存をrollback済みとは扱わない。権限基盤の一時障害を空結果に変えず、workerの再照合へ返す。

## Message出典の対応範囲

canonical message SourceRefは、正規conversation childの直接user messageに限定する。assistant/tool messageは依存配列が空でも過去の会話やtool結果から派生し得るため、現段階では一律拒否する。user messageにもmemory_records/tool_bindings/model snapshotが付くものや、非空source_refsを持つ派生・循環候補は受け付けない。既存Message契約どおりsource_refsの省略は空配列とし、明示nullや不正型は拒否する。再帰を行わずfail closedとし、直接user messageと明示入力の空source_refsは維持する。

この制限はSavedMessagesの親照合後に正本を読む共通CanonicalSources validatorにあるため、HTTP・tool・自動検索・訂正の全入口に適用する。旧実装が保存したassistant由来Factも再利用できず、正本GETなど依存先を読むUI操作も拒否される。既存Factを書き換えて出典を消す移行は行わない。将来派生messageをサポートするには、多段history・memory・tool-result・自己訂正proofについて、現在consumer/audience・版・抑止・全期限を検証する循環検出付きの有限な共通契約が必要である。

## Toolと原子的な訂正

`brainoid_tools.memory`がmemory.search/remember/reviseの厳密な引数を登録する。workerは現在のrun/lease、agent allowlist、正規user入力内の明示保存/訂正依頼、対象と出力先を改めて確認する。初版は正規user入力全体が`Remember that …` / `Save that …` / `Correct the memory: …`（revise/update/changeも可）、または`記憶して: …` / `保存して: …` / `記憶を訂正して: …`（修正/更新も可）に一致する限定的な明示命令だけを許す。引用・質問・否定・単語の説明要求を含む入力やkeyword単独では許可しない。未対応表現は記憶APIから明示操作できる。グラフ文やモデル引数から依頼・権限を作らない。Toolの保存scopeは会話と同一にし、private会話からの暗黙の共有保存を拒否する。

HTTPとtoolは同じ公開`MemoryMutations`を使う。API内部へのworker importはなく、共通の認可scope/idempotency/error型は`brainoid_contracts`、message正本locatorは`brainoid_runs`へ抽出した。新Fact・旧Fact superseded・新旧リンク・waiting memory_index job・24時間receiptを同一transactionへ保存する。Toolのeffect IDは既存Executorが決め、追加の`memory_effects`証跡も同じtransactionへ保存する。DBの副作用であり、外部providerはtransaction内で呼ばない。

検索済みFactを同じrunのmemory.reviseで訂正したときは、旧snapshot、新旧Fact、tool実行IDと同時保存proofの完全一致を検査する。その旧内容は当該訂正の過去入力としてだけ照合でき、次stageの現行検索には新confirmed Factだけを渡す。旧出典の現在権限・版・有効期間・抑止も検査し続ける。外部訂正、別runの変更、proof不一致、旧出典の失効をこの例外で許可しない。既存ResultConsumer/ResultUseでtool結果の再利用と履歴の読者を検証する。永続ToolResult/ResultBindingは内部identityを含む正本を保持し、投影済みReplyは正本から再計算した期待値と完全一致しなければ拒否する。旧workerが保存した未投影のmemory ToolReplyを含む履歴も暗黙変換せず拒否する。

Executorのstarted/unknownは再実行許可ではない。応答喪失では同一effectの原子的proofを読取照合し、proofがない不明状態を失敗確定や再保存に変えない。Coreの自動検索portによるremember/revise/forgetの直接呼出しは拒否し、副作用は明示intentとdurable effect IDを持つtyped tool経由に限定する。forgetの実装はMEM-07。

## 検証と運用

`tests/test_memory_release.py`はreader/Core cleanup/provider開始Taskの各非同期境界で全依存期限を検証し、モデル未開始・利用量ゼロ・枠解放と各memory操作/次履歴の投影・正本binding改竄拒否を確認する。`tests/test_memory_revalidation.py`は最終読取・遅延応答後の全依存期限、派生messageの間接再利用拒否、外部IDとglobal上限を検証する。`tests/test_scoped_retrieval.py`はscope/provider/trace隔離、同名Entity、shared Entityへのprivate辺、循環・多分岐、status/time/source版、Pi session、遅延応答、HTTP、実workerのtool/stream/final/historyを検証する。`tests/emulator/test_memory_retrieval_integration.py`は実Firestore transactionで検索済みFactの自己訂正と次turn、stream/final/historyの期限境界、派生出典の拒否と外部IDを検証する。既存memory/tool/run/usageの回帰も実行する。

通常検証は`mise run setup`、`mise run check`、`mise run test`。`.env.emulator.example`を明示sourceした`mise run test-emulator`はAuth/Firestoreを起動・停止する。実embedding/cloud/実機検証はこのチケットでは行わない。既存Factの移行は不要。rollback時にもFact/job/receiptを保持するが、検索の利用を止める場合は記憶に由来する履歴の再利用も止める。旧workerへ戻して新しい`memory_records`の検査を省略した会話を継続しない。

関連: [保存repository](memory-repository.md)、[明示記憶API](memories-api.md)、[会話run](conversations-and-runs.md)、[Tool実行](tool-execution.md)、[検索予算](03-data-and-authorization.md#43-検索手順と予算)。

BA-MEM-05の外部embedding分離、scope別vector索引、API/worker構成入口と世代移行は[vector契約](memory-vectors.md)を参照する。
