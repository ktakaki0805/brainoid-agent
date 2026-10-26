# 記憶の embedding job と vector 検索

BA-MEM-05 / FR-006 / T-010・T-011。正本は `facts`、vector は派生索引である。
実モデル・region・staging DB は OPEN-005 / BA-OPS-02 の確定が必要であり、この実装は採用値を選ばない。

## 設定と構成入口

`brainoid_memory.vector_store.EmbeddingConfig(model=..., revision=..., dimensions=...)` を必ず指定する。
次元は整数 1〜2048、revision は運用で固定するモデル/前処理版である。model・revision・dimensions・format_version
の hash から `memory_vectors_<hash>` collection を決める。同一設定だけが同じ索引を使い、設定変更で旧索引を上書きしない。
自動モデル切替、暗黙の次元縮小・補完・正規化は行わない。

接続は以下の公開構成入口で行う。環境変数から未確定のモデルやproviderを推測しない。

1. `GoogleEmbeddings(config, GoogleEmbeddingSettings(project=..., location=..., timeout_seconds=...), credentials=...)`
   へ明示した Vertex 接続と、呼出前に更新済みのcredentialを渡す。SDKへはtoken snapshotを渡し、SDK内credential refreshは許可しない。
   timeoutは最大30秒、HTTP retryは1 attempt、redirect/proxy環境の暗黙利用は無効。モデル本文を例外やログへ出さない。
   localでは同じ `Embeddings` protocol のfakeを渡す。
2. `FirestoreVectorIndex(client, config, manifest=...)` へ構成済みFirestore clientと下記の生成済み索引manifestを渡す。
   `VectorSearch(embeddings, index)` は双方の設定が違えば初期化を拒否する。
3. APIは `brainoid_api.app.create_app(vector_search_factory=...)` を使う。factoryはAPI lifespan所有の
   Firestore clientを受け、明示構成した `VectorSearch` を返す。`authentication_lifespan` が実際の `Memories`
   repositoryへ渡す。factoryで設定不足/矛盾があればstartupが失敗する。repository単体もconstructorの
   `vector_search=` を受ける。factory未指定は従来の明示的なローカルlexical経路を保つ。vectorを有効にした構成では
   エラーをlexicalへ自動fallbackしない。cloud配備時はこのfactoryの指定を必須の配備設定として確認する。
4. 会話workerは `RunTurn(..., vector_search=search)`、embedding workerは
   `EmbedMemory(jobs, embeddings, index)` を `Worker(jobs, verifier, {"memory_embed": handler})` に登録する。
   `verifier` と専用queueは環境で明示する。importだけでprovider/credential/clientを起動しない。
5. `EmbeddingDispatcher(handler, pages, embedding_queue).recover(tenant, after=..., limit=...)` を既存dispatcherの
   定期回収入口から呼ぶ。1ページ最大100件、返されたcursorを次ページへ渡す。cursor終端後の次周期は先頭から再走査する。
   作成済みwaiting jobと、新規API/tool保存のwaiting jobを同じ入口で起動する。
   `attributes.dispatch_queue=memory_embed` を保存し、専用Dispatcherは同じ論理queue_nameを指定する。
   共通Dispatcherの既定は `default` で、prepare_dispatchの同一transactionがqueue一致を要求するため、既存interactive dispatcherも
   embedding jobを取得しない。interactive run等の他kindはembedding queueに配送しない。queue failure・lease recoveryは共通規則を維持する。

APIのfactoryに渡したembedding providerのcredential更新と寿命は構成側が管理する。実モデルSDKは既存固定
`google-genai==2.33.0`、Firestoreは `google-cloud-firestore==2.34.1` を利用し、新しい第三者依存/クラウド権限を追加しない。
`model_google` と `persistence` にworkspaceの `brainoid-memory` 依存を追加した。これに伴うprovider/Firestore依存は
Core最小依存へ入らない。SDKのライセンスは従来どおりApache-2.0。実接続には選定済み環境の既存Vertex/Firestore権限が必要。

空のjob attributesは保存しないため、既存kindの保存形式は維持する。`memory_embed` は非空attributesを持つ新kindであり、
MEM-05対応workerと専用queueだけで処理する。古いbinaryへ戻す前にembedding dispatcherを停止し、全memory_embed jobを終端化し、専用queueと既存queueの古い配送をdrainする。
未終端jobが残る場合はMEM-05対応dispatcher/workerを残す。旧Dispatcherは未終端の新kindを認識できないため、新kindを旧workerへ配送してはならない。モデル設定のrollbackは対応worker上で旧索引設定へ戻す操作であり、
古いbinaryで新jobを再処理する操作ではない。

## 正本と外部I/Oの境界

`memory_index` はFact保存と同一transactionの識別子だけのwaiting outboxである。activationは元grant・account stampを
保った設定別 `memory_embed` jobを作る。元grantを現在の広いgrantに差し替えない。waiting outboxをそのまま保持するため、
別revisionへの再生成も同じ入口で到達できる。元の権限/版が無効ならobsoleteとなり、新しい権限を勝手に取得しない。
activationは本文をproviderに渡さない。待機jobに架空の認証期限を付けず、取得済みleaseによる有限の委任下で全文を検証する。

生成は次の順序で行う。

1. current job/lease/account/membership/session/outputと、Factのstatus・validity・suppress・version・deletion_epoch・
   entity・source・revision linkを共通 `MemoryRepository` で検査する。
2. `Execution.effect` がdurable started checkpointを作り、その後もう一度全文の認可を検査する。
   既に確認した期限をprovider直前にも確認する。
3. transaction外でdocument embeddingを1回呼ぶ。送信内容はpredicate/value/qualifiers/entity参照のみ。
   tenant/owner/creator UID、出典本文、job/grantは含めない。
4. effectのDB-only `commit` hookが現在の正本・全依存・leaseを再検査する。そのtransactionでvectorとcompleted checkpointを
   原子的に保存する。checkpoint/resultは索引key・元版・epochのみで、本文やvectorをjobへ保存しない。
5. 後続 `Jobs.complete` のtransactionが正本と保存済みvectorの実在・設定・版・epochを検査し、jobをsucceededにする。

従ってvector/checkpointとjob succeededは別transactionである。中間で停止してもcompleted checkpointから再開し、
providerを再呼出ししない。vectorが失われていればobsoleteとし、成功や自動再生成にしない。競合によるSDK callback再実行も
provider呼出しを含まない。生成中の訂正・忘却・source/entity失効は古い結果を保存せずobsolete。lease切れでstartedだけが
残る場合やprovider応答喪失は共通の `failed/reason=outcome_unknown` となり、自動再課金しない。
復旧担当が結果・費用を確認するまでfailedをpendingへ書き換えてはならない。再生成する場合は明示的な新revisionを使い、
旧世代の状態を保持する。生成済みでも後の訂正/忘却で古くなったvectorは検索時の正本検査で不採用となる。物理purgeはBA-MEM-08。

## 検索

認可transactionで許可scopeを取得し、transaction外でquery embeddingを生成する。queryはユーザー入力だけであり、
canonical Fact本文をquery providerへ混ぜない。tenant直下の世代別collectionへ、server生成 `scope_key` と `status=ready`
の等値filterを付けてから `find_nearest` を実行する。scope keyはtenant・visibility・display_policy・private ownerのhashで、
sharedにはownerを含めない。privateは本人UIDで区分する。クエリは最大40件、距離はEUCLIDEANを固定する。

結果はID/版/epochだけを保持し、新しいtransactionで全正本・依存・現権限を再検査した後に既存2hop検索へ渡す。
HTTP、Core自動文脈、typed `memory.search` がこの経路を使う。会話ではconversation/outputのscopeに先に絞る。
2hop/40fact/20entityの既存予算、完全なserver証跡とモデル送信projection、result/historyの再検査を維持する。
複数scopeのquery embeddingは1回で共有する。1回の検索要求は1回の有料embedding候補であり、検索APIキーはprovider課金の
重複排除保証ではない。query retry・品質/原価評価・run token量への独自換算は行わない（総合評価はBA-QA-03/04）。

## 索引資材と限定staging確認

[索引資材](../infra/gcp/firestore-indexes/README.md)の手順で、明示モデル/版/次元からJSONを生成する。
manifestの比較は配置予定との整合検査であり、実DBへの配置/READY状態を証明しない。実DBでのmissing indexや次元違いなどの
SDKエラーは `memory_vector_unavailable` に正規化し、空結果/別モデル/lexicalで隠さない。HTTP検索は503となる。

OPEN-005とBA-OPS-02が確定し、staging操作が承認された後に、対象project/database・model/location・dimension・revision・
生成manifest digestとREADYな実索引構成を記録する。fixtureだけを使い、2tenantとshared/本人private/他人private各1〜2件を
作って同じadapterで書込・nearest・canonical照合を実行する。Pi display/sessionの正負例、設定不一致とmissing indexの失敗、
生成中訂正/forget、2世代併存を限定件数で確認する。操作上限/料金/latencyを記録しfixtureを削除する。
本PRでは実cloud provider、staging index deploy、実modelの対応次元/品質/料金を検証していない。

## ローカル検証と公式契約

`tests/test_embedding_fencing.py` はfake生成競合、unknown/replay、再生成、公式SDK query構築とGenAI transportを検証する。
`tests/test_vector_runtime.py` はHTTP/Core/toolの接続、`tests/emulator/test_embedding_integration.py` は公式SDKで実emulatorへの
Vector保存・scope付きnearest・旧結果廃棄・transaction retryを検証する。emulatorは実索引の必要性/配置を再現しないため、
この成功をstaging確認の代わりにしない。

2026-10-26に [Firestore vector search](https://firebase.google.com/docs/firestore/vector-search) と
[公式Google Gen AI SDK](https://googleapis.github.io/python-genai/#embed-content) を参照し、上記固定SDKの
`Vector`・`VectorQuery`・`EmbedContentConfig`・`HttpRetryOptions` のローカル実装とも照合した。
