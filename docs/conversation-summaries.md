# 会話要約と出典の継承

BA-MEM-06はFR-004/006、T-010/T-011に対応する。初版は**直接ユーザー発言の要約**であり、assistantの助言やtoolの結果を含む会話全体の要約ではない。Factの自動抽出・自動確定、物理purgeは行わない。

## 入力と保存

`MemoryRepository.summaries(tx, context)`は現在の本人・account・membership・台帳・conversation・出力先を検査する。`SummarizeConversation.schedule(opened, conversation_id)`へ渡すopenerは、認証済みの`CurrentAuthorizer`を持つserver構成のrepositoryを開く。HTTP/modelからopenerやgrantを受け取らない。現在はuser主体の受付に限定する。deviceのcredential familyを保持する受付契約がないため、device contextだけからbackground権限を作らない。

jobは元のconversation MODEL_READ grant、account stamp、認証の有効区間、source message集合・版・deletion_epoch・scope・owner・内容と抑止状態のhashを保存する。本文はjob/checkpointへ保存しない。最大100 message、入力合計64,000文字、出力8,000文字、1,024 output token、provider待機30秒を上限とする。モデルrevisionをserver構成で固定し、モデルやプロンプトが変わる場合は別revisionとして扱い、同じsource IDに異なるrevisionを上書きしない。

sourceの正規パスはtenantとconversationから組み立てる。直接userかつ非派生の本文だけをproviderへ渡す。assistant/tool、非空source_refs、memory_records/tool_bindings/model snapshotを持つuserは除外する。Factの`CanonicalSources`は従来どおり直接userのみで、要約をFactの根拠として通す例外は設けない。本文がないsourceを要約から再構築しない。

再利用抑止はUI履歴とは別に適用する。`suppress_reuse`、deleted、非ゼロepochはmessage全体を除外する。`suppressed_spans`は`start`/`end`（Unicode文字offset、終端を含まない）、`version`、元本文UTF-8の`text_sha256`を各spanに持つ。現在の版・本文hash・範囲を全て検証できるときだけ該当spanを`[omitted]`へ置換し、一つでも不正・不明ならmessage全体を除外する。このmarkerにTTLはなく、forget受付による設定はMEM-07が担当する。Fact SourceRefにはspanの識別がないため、非空または不正なspan markerを持つsource由来FactはMODEL_READを保守的に拒否し、UI READは維持する。

保存先は`tenants/{tenant}/conversations/{conversation}/summaries/{summary_id}`。summary_idは元conversationとsource集合から決定する。`sources`内に元conversation版、owner（sharedではcreator UIDまたはnull）、scope/display、各message ID/版/epoch/hashを保持する。私的会話からsharedのsummaryやFactを作る入力欄はない。保持期間は親conversationの生存期間に従い、親に明示的な`expires_at`がある場合はその値を継承して利用時にも検査する。現行conversation APIは独自の保持設定を提供していないため、欠落を新しい期限や無期限の別policyへ変換しない。

## 生成と再試行

serverは明示的なModelProviderを`SummarizeConversation(jobs, model, revision=...)`へ注入し、`Worker`の`conversation_summary` handlerとして登録する。配送と回収は既存`Dispatcher(..., queue_name="conversation_summary")`を使う。標準queueやembedding queueへ混ぜない。実providerを暗黙に選択する起動経路はなく、fake/emulatorから検証できる。

provider呼出しの直前、生成結果保存transaction、job完了transactionでsource・認可・account・leaseを再確認する。生成中は元conversation版の完全一致を要求する。元の認証期限とsession期限をjobで延長しない。外部生成はtransactionの外側で一度だけ実行し、summaryと本文を含まないcompleted checkpointを同一transactionへ書く。transaction再実行ではproviderを呼ばない。startedだけが残る結果不明は自動再課金せず、既存Jobsのoutcome_unknownとして停止する。providerの各非同期待機を独立taskとして管理し、30秒の生成期限とは別にclose待機を250msで制限する。最初のcancelがproviderのfinallyへ入る場合も、250msの猶予と再cancelで回収する。専用event loopの終了処理にも同じ上限を適用し、無期限のshutdown待機を残さない。終了が失敗・期限超過・取消となった結果はsummaryもcompleted checkpointも保存しない。adapterには繰り返しの取消へ協調する契約を要求する。

## Coreへの取得

RunTurnは同じconversationの保存済みprefix候補を、現在のrun/leaseと読者の権限で再検証する。生成時のgrantを書き換えて再利用するのではない。後続turnによる親version増加は許すが、owner/scope/display/epoch/期限、prefix内のmessage集合・版・hashが一致しなければ候補を除外する。これにより正常な会話が続いても過去のprefixを利用でき、訂正・削除・抑止された古い要約は使われない。

採用した要約だけがCoreの認可receiptと期待contextへ入り、対応する直接user messageをモデル履歴から置換する。投影はsummary ID、要約本文、元message ID/版/epochだけで、tenant/owner/creator UIDや本文hashは渡さない。参照データとして「直接ユーザー発言の要約」「assistant/toolの完全な要約ではない」と明示する。モデルの出力からsource_refsを採用しない。

通常履歴も同じ抑止関数を使う。訂正・本文欠落・削除・抑止・sequence欠落があるprefix以降は、依存の再構築を証明できない派生assistant/tool/derived userを保守的に除外する。後続の新しい直接user発言は利用できる。残る履歴のmemory_records/tool_bindingsには従来のcanonical dependency検証を適用する。UI履歴の保存内容は書き換えない。

## 検証と切戻し

`tests/test_summary_cleanup.py`は停止したprovider close/finally、timeout、Python task取消、例外の有限終了・資源/task回収・再課金抑止とtask開始直前のguardを確認する。`tests/test_summary_provenance.py`は正本・scope/owner・抑止span・生成競合・再試行を、`tests/test_summary_runtime.py`は公開turn受付からCoreまでのprefix置換、複数turnでの継続利用、source欠落時の派生履歴除外を確認する。`tests/emulator/test_summary_integration.py`は実Firestoreでprivate継承・後続turn・生成中の訂正/抑止・source削除・SDK transaction retryを確認する。通常の`mise run setup/check/test`と、明示sourceした`.env.emulator.example`によるAuth/Firestore emulatorで実行する。実cloud/課金provider・品質評価・物理purgeはこの検証範囲外。

既存Fact/API/schemaの変更・データ移行は不要。追加されたsummary/jobはserver内部モデルで、既存クライアントへ新しい必須fieldを追加しない。停止時はsummary専用dispatcher/handlerを止め、保存データとcheckpointを保持する。旧workerは抑止された履歴を安全に処理する契約が異なるため、抑止済みconversationを旧版へ戻して継続しない。source_refsやsuppression markerを消すrollbackは行わない。

関連: [記憶検索](memory-retrieval.md)、[会話run](conversations-and-runs.md)、[忘却と削除](03-data-and-authorization.md#8-削除忘却保持)。
