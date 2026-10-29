# エクスポート・退出・削除の画面

BA-UI-12 / FR-013 / AC-013、T-022・T-034の実装契約。法的な保持説明の確定はBA-GOV-03、公開Web URLはBA-UI-13で扱う。

## 範囲と受付

[共通画面](../packages/ui/src/data-controls/view.tsx)は本人スマホで会話・記憶・退出・account・tenantの違いを示す。記憶は既存の記憶一覧のforget確認を利用する。会話は現在取得できる一覧から選び、再取得した版と派生記憶の同時削除（既定true）を確認する。共有会話全体の削除には参加者の発言も含む。退出/account削除の共有寄稿保持とは異なる。

account要求はglobal endpointの `{kind: delete_account, confirm: true}` に固定し、UID入力を持たない。tenantは現在選択中のIDだけ、全体削除はownerと現在名の完全一致を要求する。最後のownerは会員設定で移譲するかtenant削除を選ぶ。3種のlifecycle操作はサーバーの300秒以内auth_time判定を利用し、拒否時は同一本人の実再認証へ接続する。token refreshを再認証とみなさない。

[controller](../packages/ui/src/data-controls/controller.ts)は確認後の要求body・key・endpointを固定し、応答喪失の再送に再利用する。通常tenant contextを破棄し、同じglobal認証・選択世代に限って非本文の受付番号/kind/opaque tenant IDと本人進捗経路を保持する。確認に用いた会話title/tenant名を進捗の表示ラベルに残さない。tenant削除bodyのconfirmation_nameのみexact再送に必要なメモリー内値として保持する。ユーザー/tenant切替、logout、pagehideで破棄し、storageへ保存しない。会員管理の再認証queue/signout手順を共有する。

202は台帳への受付であり物理消去の完了ではない。pending/running/failed/outcome_unknown/succeededを区別する。accountはprovider完了とsucceededが同transactionで保存され、その後の本人status GETは404（実認証では401の場合もある）となる。404/401を消去完了へ変換しない。最初の明確な拒否だけ選び直し可能とし、応答喪失後は同key再送を維持する。export等の終端結果は閉じて次の要求へ進める。

共有ディスプレイにはスマホでの操作案内のみを表示し、個人操作controller・フォーム・受付番号を作らない。

## ZIPの認可と保存

[既存export契約](data-export.md)の固定APIへbearer付きでアクセスする。SDKの `parseAs: stream` を明示し、application/zip、no-store、最大4MiB、宣言lengthとの一致、ZIP開始/終端構造と全stream完了を確認する。期限・現在context・abortをchunkごとと保存直前に検査する。blobは状態/storageへ保存しない。ブラウザーのobject URLは保存操作中だけ作り、finallyで失効する。public/signed URLやbearerをURLへ入れない。

認可の正本はサーバーのdownload（ヘッダー・各body境界・最終境界で検査）。status GETは進捗取得で、manifest/forget/期限の再認可ではない。実APIではstatus succeededのままdownloadが拒否されるケースを試験する。完全な取得後のbytesや他アプリへ共有済みのコピーは後から回収できず、利用者による削除が必要である。

[native adapter](../apps/mobile/data-controls/native.ts)はFilesystemのCache領域に `brainoid-export-v1-<random UUID>.zip` だけを書き、Shareへそのfile URIを渡す。API URLやcredentialをnative bridgeへ渡さない。書込中の中断は完了後に清掃し、共有中の中断は清掃を開始する。共有成功・キャンセル・失敗のfinallyでも同じファイルを清掃する。清掃失敗はサインアウト後も警告を表示し、次の保存を止める。起動時は当prefixのfileだけを回収し、他機能のcacheやdirectoryを削除しない。

2026-10-29に[公式Filesystem v8](https://capacitorjs.com/docs/apis/filesystem)・[公式Share v8](https://capacitorjs.com/docs/apis/share)、npm metadata、導入された型とlicenseを確認した。Filesystem **8.1.5**、Share **8.0.3**を固定し、両peer `@capacitor/core >=8.0.0` は既存8.5.3と互換。Ionicの公式pluginでMIT、[Filesystem表示](../apps/mobile/LICENSE.capacitor-filesystem)・[Share表示](../apps/mobile/LICENSE.capacitor-share)を同梱し、推移依存Synapse 1.0.4もlockで固定し、[表示](../apps/mobile/LICENSE.capacitor-synapse)を同梱する。Synapseのpackage.jsonはlicenseをISCと宣言する一方、同梱LICENSE.md本文はMITであり、ここには後者をそのまま複写した。このupstream内の出典差を記録し、法的な解釈を付け加えない。cache共有はAndroidの既定FileProvider範囲を利用し、広い外部ストレージ権限を要求しない。native配布時にはplugin syncとiOS PrivacyInfo.xcprivacyのFileTimestamp理由C617.1を設定する。今回Xcode/Android SDK・native platformを導入/配備せず、実機のOS共有結果は未実施。adapterの実plugin型接続、fake native race、Web buildを検証し、実機成功と同一視しない。

## 会話削除の必要な契約統合

既存仕様03 §8に会話削除がある一方、以前のoperation routeはnote/taskだけを実装していた。この不足を埋めるため `delete_resource/target_type=conversation` を既存ForgetMemories/DeletionLedger/PurgeJobsへ接続した。別削除システムは追加しない。

現在のpolicy WRITE（sharedは作成者またはadmin/owner、privateは本人）とexpected_versionを要求する。予約→独立台帳→tombstone/operation/purge jobの順序は既存と同じ。予約中は会話変更・新規turnに加えてRunStateのモデル/tool/完了・失敗commitおよびcancelの親変更を同transactionで拒否し、受付後は既存resource fenceが読取り・model・遅延job・exportを拒否する。全会話子階層を既存postorder walkerで消去し、該当runは本文なしの失効証拠を残してevent/音声参照等の子階層を消す。現在音声producerは未提供だが、選択済み会話/run配下の参照はunknown/ghost子も含めて処理する。外部音声providerの独立消去を実装済みとはしない。

派生Factは選択会話のcanonical message ID・tenant・parent・versionに一致するsourceだけを選び、消去証拠とtombstoneを残す。別会話の原文や独立した出典は消さない。include_derived_memories=falseではFact本体を保持するが、消えた出典の再利用を許可しない。既存export無効化hook・zero-change検証・再配送/復元fenceを利用する。

会話の派生判定はbounded walkerが各canonical messageを読んだ時点で、tenant・message ID・parent・versionを本文なしのoperation配下 `purge_messages` に保存する。先行forgetで原本が消えている場合は、独立intent・受理済みjob/operation・exact tombstoneを検証して同じ帰属を補完する。未受理のDB flagや別tenant/parent/versionは根拠としない。これにより会話親の物理消去後の再配送も同じ範囲で続ける。帰属不明のコピーは既存legacy_pendingで保持し、既知範囲の消去を進めてもsucceededにはしない。証拠復旧後は同じoperationを再開する。

Fact原本が無いmemory_effectsは正規record/previousのtenant・ID・版・predecessorとsource帰属を検査する。receiptのexact hashに束縛した `purge_effects` 証拠は、そのreceiptと子階層だけの消去権限であり、同IDの別内容の現在Fact/vectorを消す権限には使わない。原本や出典の欠落を対象外と推測して完了にしない。

既存schema_version=1のdelete_resource intentにconversation targetを追加する互換拡張である。旧workerは未対応targetのPydantic検証でfail closedし、このintentを完了できない。API・reader・専用purge workerを対応版へ揃えてから受付を開ける。旧版への切戻しでは受付/workerを停止し、受理済み台帳・予約・tombstoneを削除せず対応版へroll-forwardする。新targetの受付を旧workerに任せない。

## 検証

[HTTP/worker試験](../tests/test_conversation_deletion.py)、[controller/native試験](../tests/test_ui_data_controls.mjs)、[実API応答→SDK/React試験](../tests/test_ui_data_controls_render.mjs)を通常suiteに含める。[ブラウザー試験](../tests/ui/data_controls_browser.mjs)はmobile/displayをbuildし、確認・再認証・受付/完了・失効・切替後の消去と狭い画面を確認する。Firestoreの実cloud、本番provider、実iOS/Android/Piは今回未実施である。

BA-UI-13の[公開Web](public-web-contract.md)は同じ削除フォーム・進捗表示にaccount-only controllerを渡し、申請前の実再認証を毎回必須にする。mobileの通常導線は既存のサーバーrecent-auth判定と拒否後の再認証を維持する。公開entryだけが非credentialのlocatorを保存し、認可済みglobal GETで回復する。mobile/displayの受付情報は従来どおりstorageへ保存しない。
