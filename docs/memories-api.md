# 明示記憶の保存・出典・訂正API

BA-MEM-03はFR-006/T-008/T-010として、`GET/POST /v1/tenants/{tenant_id}/memories` と `GET/PATCH /v1/tenants/{tenant_id}/memories/{memory_id}` を提供する。HTTPの`memories`は、MEM-01の正本`tenants/{tenant_id}/facts/{id}`に対応する。索引の存在を読取条件にしない。

## 1. 入力と出典

POSTはsubjectとobject/valueの排他、predicate別qualifier、scope、有効期間を保存モデルでも検証する。`origin=user_explicit`、`status=confirmed`、所有者、記録者、時刻、version/epochはserverが決定する。これらの権限フィールドを要求で指定できない。`valid_from`省略時はサーバー時刻を使い、`valid_to`はその時刻より後でなければならない。

`source_refs`は省略時に空配列。直接フォーム入力に既存出典がない場合、空配列のまま保存し、レスポンスの`created_by`と`recorded_at`を本人・記録時刻の出典表示に使う。架空のmessageを生成しない。保存型でも空出典を許すのはuser_explicitのconfirmedと、そのsuperseded/retracted履歴だけ。imported/inferred/candidateは実在する出典を必須とする。`created_by`は出典表示用の追加レスポンスフィールドで、Piの個人セッション経由でも本人UIDを返す。端末IDは内部の`created_via_device_id`へ保存する。

noteはtenant配下の正本を参照する。messageは`SavedMessages`が同一transaction内の正本conversation一覧と実message childから一意の親UUIDを解決し、保存済みMessageの構造も検証する。BA-MEM-04では直接user messageのみ対応し、assistant/toolおよび派生metadata/source_refsを持つmessageは[派生出典の制限](memory-retrieval.md#message出典の対応範囲)により拒否する。既存assistant由来Factの検索・GETもこの制限を受ける。任意パス、要求の本文、旧contextを出典の証明にしない。message IDの重複、不存在、親IDの不一致は拒否する。現在のpersistence portには一般的なmessage collection-group resolverがないため、現段階ではtenant内の会話IDを走査する。大規模tenant向け索引化は将来の最適化であり、索引を導入しても正本の親・存在・版を再確認する。

記憶の新規保存時は出典の現在version、epoch、deleted、suppress_reuse、実親のscope、subject/objectの存在・権限を同一transactionで読む。shared記憶へprivate出典/参照entityを含めず、依存先よりdisplay_policyを広げない。取得・一覧・冪等再試行でも依存先の現在権限を確認する。UI履歴の取得では抑止された出典を表示できるが、新規保存とモデル再利用では抑止/正のepochを拒否する。

## 2. 原子的な保存と訂正

POSTにはIdempotency-Key、PATCHにはIdempotency-KeyとIf-Matchが必要。成功応答はcommit後にのみ返す。fact、索引job、共通冪等receiptを一つのFirestore transactionで保存する。最終読取後とcommit応答後に、検証済みcredentialと現在private_sessionの期限も検査する。応答喪失やcommit後の期限切れはrollbackの証明ではなく、現在の認可を取り直した同じキーの再試行で照合する。

PATCHはreplacementを新ID/version 1で保存し、旧factをsuperseded/version+1へ更新し、新factに`supersedes_fact_id`を保存する。旧version・epoch・statusと現在権限を比較し、古いIf-Matchには412を返す。旧factと新factとjobとreceiptが部分的に保存されない。reasonは要求の冪等性照合に含めるが、本文やjobへ保存しない。

訂正は旧factのvisibility/display_policy/ownerを継承し、replacementで公開範囲を変更する要求は拒否する。記録者は今回の本人とする。取得時もリンク先の現在scope・owner・epochを検査し、privateの旧fact IDを共有リンクとして返さない。公開範囲を変更するには別の明示フローが必要で、本チケットでは提供しない。

一覧は現在閲覧可能なconfirmedのみを返す。旧IDのGETは`status=superseded`と更新後のETagを返し、旧内容を現行factとして表示しない。作成receiptの再送が訂正後に届いた場合も、現在のsuperseded状態を返す。履歴・候補の一覧は後続機能で扱う。[BA-MEM-04](memory-retrieval.md)はscope検索と有効時刻によるモデル用選択を接続した。HTTPとtoolの保存・訂正は公開MemoryMutationsに共通化している。

## 3. 索引ジョブの境界

RUN-01の共通Jobsに`add_created_memory`を追加した。既存source向け`add_pending`の契約は変更しない。memory adapterの`FactTransaction.prepare`が完全なFact・依存先・訂正fenceを検証した後、Jobsはmemory kind・MODEL_READ・version 1/epoch 0・user_explicit/confirmed・creator・正本metadataとgrantの一致を確認する。さらに現在principal/search policy、memory.write、scope/owner/display、account stamp、実DBのfact/job不存在を再確認してから、新factとjobを保存する。任意の権限正本を仮置換する仕組みは使わない。

jobは`memory_index`で、新fact ID・version・epochと現在認可に結び付ける。MEM-05の索引handlerが未実装の間は`waiting`で保持し、dispatcherは送出しない。未登録handlerによる恒久失敗やダミー索引を作らない。デプロイを戻しても保存fact/receipt/jobは保持し、索引がなくてもGETできる。

MEM-05が待機jobを有効化するときは、現在factのconfirmed状態・有効期間・suppress_reuse・version/epochと、出典/subject/object/訂正リンクの現在権限・版・抑止をmemory層で再検査する。handlerの取得時と最終保存transactionでも同じ検査が必要。共通Jobsのgrantはfact本体のmetadataを保護するもので、source_refsや参照entityの継続的な認可を保証しない。出典だけの失効でも古い索引を書き戻さない。旧supersededのwaiting jobは再開せずobsoleteとして扱う。実索引処理・有効化入口はMEM-05の範囲。

## 4. 認可・検証・運用

スマホとPiはMEM-02と同じ現在credential/account/membership/device family/台帳検証を使う。Pi書込には本人private_sessionとmemory.writeが必要。端末のappliance/room allowlistは参照entityにも適用する。記憶操作はセッションのidle期限を延長しない。未知話者の共有Piから書き込めない。cursorは現在actor・scope・可視factの版・query/limitへ結び付ける。非公開の候補は件数/位置を決める前に除外し、権限基盤の障害は空一覧に変換しない。

通常検証は`mise run setup`、`mise run check`、`mise run test`。実SDKの結合検証は`.env.emulator.example`を明示sourceして`mise run test-emulator`を使う。`tests/test_memories.py`と`tests/emulator/test_memories_integration.py`が即時GET、同時訂正、出典の権限差、コミット失敗/応答喪失、出典変更によるtransaction再試行、旧索引jobの最終保存拒否を検証する。実provider・cloud・実機は不要。既存source不備や未対応importを推測で補う移行は実施しない。

関連: [保存モデル](memory-repository.md)、[Entity API](entities-api.md)、[通信契約](04-interfaces.md#62-記憶の保存訂正忘却)、[OpenAPI](../specs/openapi.yaml)。
