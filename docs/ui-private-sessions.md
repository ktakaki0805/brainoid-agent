# Pi個人sessionと会話引継ぎUI

BA-UI-10、FR-012、AC-012、T-020/021/035。[スマホの同意と出力先](../packages/ui/src/output-target/view.tsx)、[本番Pi画面](../apps/display/session/view.tsx)、[本人API](private-sessions.md)と[端末snapshot](device-snapshot.md)を接続する。音声hardware/provider・声紋・自動延長は追加しない。現在のTurnCreateはtextだけで、画面も音声が未接続であることを示す。

## 本人の確認と出力先

スマホで所属テナントとPi画面のdevice UUIDを照合し、許可scopeと共有空間への露出を確認する。owner/admin限定の端末管理一覧を普通member向けの選択APIに流用しない。本人は管理者でなくても既存APIの現在の所属・対象Piのscope検査を通して開始できる。開始は既存の`Idempotency-Key`、`acknowledge_shared_space: true`とscope集合を固定して送る。応答不明・再確認時の429では同じ操作を保持する。終了/失効後に同じ開始receiptが返っても再開しない。

開始しても出力先はスマホのままで、利用者が別操作で「確認したPiにも表示する」を選ぶ。`display_policy: never`の既存個人会話は暗黙変更しない。会話の説明と確認ボタンで`authenticated_session`への既存PATCHを明示し、version/If-Matchと操作keyを固定する。会話の表示許可、短命session、各turnの出力指定はいずれも必要である。表示設定の応答が不明なら、現在の設定が変更済みでも専用の再確認操作を残す。別会話の選択や出力消去後にも元の会話・body・key・If-Matchを保持し、そのreceiptを現在認可で再確認してから送信を再開する。

turnには既存`output_device_id`と正式な追加一致条件`expected_output_private_session_id`を送る。この値は委任を付与せず、本人user→明示Pi出力だけの前提条件である。同じtransactionで現在のslot・本人・対象を一致させ、その後も既存のactive session/grant検査を通す。旧sessionの要求自体が遅延し、新session開始後にAPIへ到達しても新sessionへ出力を移さない。device主体、出力先なし、明示nullは拒否する。body fingerprintに含めるため同keyの前提だけの変更は409となる。新sessionでgrant stampが変わった既存receiptは、現在認可が先に403で拒否する場合がある。どちらも新runを作らない。

以前のclientによる前提省略は互換性のため従来どおり受付時slotを使う。省略時には新fieldをfingerprintから外し、以前のreceiptを壊さない。今回UIはPi出力の一致条件を必ず送る。旧APIは未知body fieldを拒否するため、新UIの条件を黙って捨てない。新APIから先に配備し、新UI/SDKを続ける。旧UIへ戻すと到達遅延に対する保護は失われるので、個人引継ぎを停止して互換版へ戻す。

## 期限、遅延応答と消去

両画面に個人/共有状態、対象、最大5分・無操作60秒と現在の期限、文字出力先/音声未接続、終了操作を表示する。取得/heartbeat/再接続は延長操作ではない。同versionの正当なidle更新は受け入れるが、固定5分期限は変えない。wall clockと単調時計の最初の基準を保持し、同じmetadataの取得やrun割当の変化で既知の期限を延ばさない。idle timestampの正当な更新だけを固定最大期限内で反映する。時刻逆行・期限一致で停止し、同じaccept内や同IDのbinding変更で失効を取り消さない。時計異常は別sessionへ切り替えても解除しない。100ms監視はHTTP応答を待たず動き、非表示/pagehideでも本文・途中表示・選択中のrun・登録された再生cleanupを消す。

[OutputLifetime](../packages/ui/src/output-target/lifetime.ts)は将来の検索/TTS/playback adapterも登録できる消去境界であり、実音声の接続を意味しない。adapterはcleanupを登録し、保存したgenerationを表示/再生直前にも検査する。終了後のcallbackは新しいgenerationを借りない。音声自動再生は実装せず、snapshot/history/speech.readyも再生を開始しない。保存済みcursorを持つ実行中runのreplacement historyは同じSSE上で現在のtyped情報を再確認し、受信順にcursorを合わせて後続live eventを処理する。historyだけで接続を開き直さず、途中で切れたときは次の明示/定期再同期へ戻る。

スマホのturn再送keyは本文・Pi・同意sessionへ固定する。出力先変更や終了で古い選択/stream/本文を退役させる。本人のFirebase認証で利用する他画面のメモ/記憶等は、このPi委任を閲覧資格にしていない。Pi委任の終了を本人アカウント全体の削除やサインアウトと混同しない。

## 本番Piと端末側の終了

`apps/display/src/main.tsx`の本番entryで既存Recoveryと会話出力を構成する。ブラウザは同一originのlocal cookie/CSRFだけを使用する。Recoveryと会話出力の並行bootstrapを共有してcookie/CSRFの組違いを避け、401/403後は再bootstrapする。resetは既存診断/消去機能を維持する。Firebase userやdevice bearer/refresh credentialをブラウザに渡さない。

完全な`GET /local/cloud/snapshot`だけを採用し、credential generation、session binding、run assignment generationと対象会話を確認する。Piは割り当てられたrunだけを型付きoperation/SSEで読み、shared状態からprivate履歴を一覧取得しない。終端応答は現在認可されたconversation/runとfinal_message_idに一致するmessage pageから表示する。途中page失敗、旧世代、sessionの置換、切断で古い本文を復旧しない。local bridgeが末頁の`next_cursor: null`を省略する形式も受理し、それ以外の不正cursorは拒否する。最終messageを探す際は100ページごとに区切り、次の再同期で続きから前進する。現在run・conversation・credential/session/assignment・final_message_idに結び付いた有界cursor情報だけを保持し、部分本文は保持・表示しない。cursor循環を定数メモリで検出し、現在のAPIでcursorが失効/拒否されたときは次の確認を先頭からやり直す。停止・期限・別run・snapshot binding変更で継続情報を消去する。本文はメモリだけで、端末の既存保護journalにも保存しない。画面内のsession退役IDは128件、assignment世代履歴は1024件で有界にし、上限では再表示を止めて画面の再読込みを必要とする。

Piの「このPiでの個人表示を終了」は、CSRF付き`POST /local/cloud/private-sessions/{id}/stop`で端末の出力世代を退役させる。cloudの本人限定DELETEとは別で、応答は`status: stopped, cloud_session_ended: false`。スマホにはクラウドでの終了操作を提供し、Piにはクラウド委任がスマホ操作または既知期限で終わることを表示する。

端末の`private-output-stops-v1`はCredentialStore内の0600別journalで、API origin・登録epoch・tenant/device、停止session IDと元の固定expires_atだけを保持する。最大128件で、元の最大期限到達後に削除する。同じsessionの後続snapshot/idle更新、他tab、browser/service再起動でも停止を解除しない。停止receipt再送は新sessionの出力を消さない。書込失敗は本文を消して成功応答を返さず、破損/binding違い/時刻逆行はfail closedとする。端末resetで既存保護storeと一緒に消去する。旧serviceはこの追加journalを解釈しないので、service切戻し前は新規session受付を止め、全sessionの固定最大期限（最大5分）まで待つ。資格情報やcloud receiptをbackupから巻き戻さない。

## 検証と限界

[Node試験](../tests/test_ui_private_sessions.mjs)は仮想時計の期限一致、同version idle、遅延/逆順、tenant/user/device/session/credential/assignment違い、部分page失敗、再送、fake再生queueと遅いSSE/searchの消去を扱う。[Python試験](../tests/test_ui_private_sessions.py)は実API/repository・既存認可・local停止・journal再読込み・保存失敗・前提条件の遅延到達を扱う。

[Chromium結合](../tests/ui/private_sessions_service.py)は実DeviceApplication/Runtime/CloudClient/CredentialStore/SQLiteと実loopback HTTPを使い、上流は既存の実API＋synthetic transaction store/model fixtureである。mobileは依存fixtureで明示開始/出力/許可/終了を操作し、displayはproduction buildのまま現在認可されたprivate runを表示する。CSRF拒否、local停止、別tab、reload、新同意、private本文からpagehide同期消去・履歴復帰を確認する。追加のloopback API listenerはASGITransportのSSE全体bufferを避ける試験専用経路で、実workerの合成modelを途中で待機させる。新しいconversationの初回turn・user message 1件で、保存済みcursorと実get_runのseqを確認する。実UIBridgeが末頁cursorを省略したrunning historyから後続delta・最終本文までを一つのstreamで確認し、terminal historyの同じ末頁形式も採取する。100/101/201ページと複数batch、cursor循環/失効、継続取得中の停止・期限・別run・session/assignment変更はNodeの同等ページ契約で検証する。履歴seq、上流Last-Event-ID、接続数、最終cursor/本文を合成データのJSONへ保存する。スマホでは表示設定のcommit後応答喪失、再選択、別会話からの同一操作再確認、送信再開も操作する。mobileの実bfcacheはpersisted=true、本番local HTTPはno-storeにより新document復帰となる場合があり、`notRestoredReasons`と本文非復活を別に記録する。これは実Firebase/Firestore認証・両端を一つの実スマホで接続した証拠ではない。

~~~bash
export UI_CHROMIUM=/absolute/path/to/chromium
export UI_ARTIFACTS=/tmp/ui10-evidence
PYTHONPATH=tests mise exec -- uv run --all-packages --locked --offline python -B tests/ui/private_sessions_service.py
~~~

専用の5197とOS割当loopback listenerを使い、所有browser/listener/profileを終了・破棄する。`mise run check`（両production build）と`mise run test`、共有UI/Sessionを使う既存browser回帰も確認する。実クラウド/provider、Firebase emulatorのUI10同時結合、物理スマホ/Pi、音声hardware、OS service installは未実施で、UI10のfake/同等service検証からこれらの完了を推定しない。
