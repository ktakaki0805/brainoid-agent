# 文単位の音声出力

BA-VOICE-02 / FR-005・FR-012 は `output_mode=speech` の turn から確定文を順次公開する。[実装](../packages/runs/src/brainoid_runs/speech.py)は日本語の `。！？`、`!?`、改行、および空白が続く `.` を文末とし、最後の句点なし部分は成功した最終 commit で確定する。小数点は分割しない。閉じ括弧・連続する終端記号だけの断片はtextに残し、新たな合成を課金しない。文法解析や略語判定は行わない。LLM token の分割を文の境界とせず、永続 `streamed_text` の位置と採番を使う。text delta の既存250ms/1KiB flush に合わせ、最初の文を最終応答前に取得できる。家電操作を含む run は既存の操作結果公開制約を維持するため、内部の前置きは早期読み上げしない。

`text.delta`、`speech_outputs` 参照、本文を含まない `speech.ready` を同じ認可付き transaction で作る。参照は run から決定的に採番した ID、0始まり `sentence_index`、会話/run/owner/scope、本文の開始・終了位置、作成時刻、5分期限を持つ。参照に本文や音声を複製しない。worker retry、tool継続、checkpoint再開、最終残文も同じ位置情報を利用し、発行済みID/期限を延長しない。text出力にはspeechイベントを追加しない。SSE再接続は既存のseq履歴を使い、イベント履歴の24時間と音声参照の5分を区別する。

初期上限は1文4,000 UTF-8 bytes、1 run 64文。上限を超える speech run は公開transactionを拒否し、失敗扱いになる。これにより最終commitのFirestore書込み数とprovider入力を制限する。句点がない長い文を任意の位置で読み上げ用に改変しない。テキスト出力の既存上限はそのまま利用できる。長文用の分割方式変更は別途互換性を確認する。

`GET /v1/tenants/{tenant_id}/speech-outputs/{speech_id}/audio` は user/device bearer と、個人Piでは `X-Private-Session` を受ける。任意文章やvoiceを送るendpointは提供しない。取得者は保存済み出力subjectと一致する必要があり、別の利用者・Pi・同じ利用者の新しい個人sessionへ参照を移さない。端末credential/family/generation、originの現在権限、account/membership、現在の会話scope/display_policy、calendar出典、削除fence/purge epoch、run状態を検査する。scope変更は既存参照を保守的に無効化する。runはrunning/waiting_confirmation/completedのみ許可し、取消/失敗後は拒否する。未設定時はturnのspeech受付と音声GETを503にする。

[取得処理](../services/api/src/brainoid_api/speech_outputs.py)は合成直前、待機中250msごと、結果受領、利用量精算後、HTTP応答直前に現在状態を再検査する。session実期限/無操作期限、bearer、実行期限と5分期限はtransaction終了およびthread handoff後のrelease guardでも検査する。期限と同時刻は失効済み。各attemptの受付時に固定した60秒deadlineは、精算でactive slotを解放した後も音声結果に保持し、HTTP側の最終thread handoffまで検査する。期限による音声拒否は消費事実の精算と独立し、確認済みの利用秒を返却不可を理由に払い戻さない。進行中の親runはusageのactive状態とhard deadlineを直接検査し、worker停止やreconcilerの遅延で期限を延長しない。completed runは解放済みの親slotを要求せず、参照の5分期限内の取得を許可する。停止/失効後の遅い音声は返さない。応答はメモリ上の16kHz・16bit・mono PCM WAV、最大60秒。RIFF全長、fmt/data、音声形式、サイズ、無音を検査し、不正なprovider結果を成功audioとして返さない。成功には `Cache-Control: no-store` と `X-Content-Type-Options: nosniff` を付ける。音声バイトはDB・bucket・通常log・一時ファイルへ保存せず、例外にprovider本文を載せない。

参照ごとの合成は成功・失敗を合わせて最大2回。同時実行は1回で409、上限は429。GET再取得は新しい有料callになり得るが、replica間でもtransactionでattempt数を制限する。HTTP/providerの自動retryはない。admissionの応答消失はcall未実施でも1attempt消費し、曖昧な成功を再実行しない。各attemptを `purpose=speech_output` の独立した利用量実行として扱い、会話turn数を増やさずtenant/user/deviceの音声60秒と同時枠を事前予約する。上限不足は呼出し前に429。正常なWAV長から切上げ音声秒を精算し、失敗/timeout/取消/結果不明では予約60秒をunknownとして保持する。実呼出し前に中止した証拠がある場合のみ0秒へ戻す。これはアプリの音声利用量であり、文字数等によるGoogleの料金を表さない。provider側の費用上限は別途設定する。

同時枠は会話runと合成attemptで共有するため、早期読み上げには双方の枠が必要になる。`concurrent_runs=1` のscopeではrun実行中のGETは429、run完了後は取得できる。通常既定値の範囲で順次取得し、上限を迂回しない。APIの合成待機は45秒、REST adapterは認証取得を含め40秒、永続実行枠は60秒。停止したprocessの枠は `UsageReconciler.recover` が60秒後にunknownを保持して回収する。回収後の遅い結果は実行fenceで拒否する。

5分期限は認可規則であり、TTL削除を待たない。`SpeechOutputs.expire(tenant, after=..., limit=100)` は参照だけを消すbounded sweepで、`speech_outputs.ttl_at` はFirestore Timestampである。配備担当は毎分のtenant sweep、または同fieldへのFirestore TTLを設定する。**usage枠の回収は独立して毎分 `UsageReconciler.recover` を実行する必要があり、TTL/sweepでは代用できない。** 参照が先に消えてもusage_runs/reservationsから回収できる。これらの運用wiring/TTL設定を今回クラウドへ適用していない。停止期間の物理消去遅延はアクセス期限を延長しない。会話削除の派生producerとaccount/member/tenantのlifecycle削除inventoryへ登録済み。session終了後は即時参照拒否、物理参照の除去は5分保持+sweep/TTLに従う。端末キュー/停止はVOICE-03/04/05で扱う。

設定は既定 `TTS_PROVIDER=disabled`。staging/productionで明示的に `TTS_PROVIDER=google-v1`、`TTS_LOCATION`、`TTS_VOICE`（ja-JPのvoice名）と既存GCP project/認証をそろえた場合のみREST adapterを構成する。locationごとの明示Google endpointを使用し、globalへのfallback、redirect、環境proxy、自動credential探索は行わない。`input.text`、voice名、ja-JP、LINEAR16/16kHz、`x-goog-user-project` を明示する。REST応答は2,600,000bytes、decoded音声は1,921,024bytesを上限とする。SSML・長時間合成・bucket出力は提供しない。localでは実providerを禁止し、fakeをテストから注入する。既存固定版HTTPX/google-authを利用し、新しい依存・lock更新・IAM権限付与はない。

一次資料（2026-10-29確認）: [Cloud TTS v1 text.synthesize](https://docs.cloud.google.com/text-to-speech/docs/reference/rest/v1/text/synthesize)（inline base64、LINEAR16のWAV header、cloud-platform scope）、[endpoint指定](https://docs.cloud.google.com/text-to-speech/docs/endpoints)（voice/region対応を有効化前に照合）、[入力quota](https://docs.cloud.google.com/text-to-speech/quotas)（一般的な5,000bytes上限に対し本実装は4,000bytes）。採用voice/regionが同期v1/LINEAR16/ja-JPをサポートすることはOPEN-005で確定する。

ローカル受入は [HTTP・stream・late result・REST transport試験](../tests/test_speech_outputs.py)、[Firestore競合と回収試験](../tests/emulator/test_speech_outputs_integration.py)、[実producerからの会話削除](../tests/test_conversation_deletion.py)。T-007のrun取消とT-021のsession失効競合はfakeで確認し、端末で実際に音が止まることは後続の実機試験に分ける。

実TTS呼出し・可聴品質・region適合・実課金照合は未実施。OPEN-005のvoice/region/保持条件/費用上限と短命認証が未確定のため、disabledを維持する。docs08共通条件に従いローカル実装完了と公開判定を分離する。公開前は次の上限付き手順を同じartifactで実施し、結果がそろうまで実provider受入済みとはしない。

1. 採用voiceとlocationの対応、project、保持条件、短命認証、費用上限/停止条件を記録する。音声scopeごと `voice_seconds=120`、`concurrent_runs=2` を設定し、本文を含まないprovider call数を確認できるようにする。
2. 合成fixture「こんにちは。確認できました。」だけの2文runを1回作り、各文を1回取得する。合計2call・120予約秒以内、provider retryなし。各結果は5秒以内を確認する。失敗時は停止し追加GETを実行しない。
3. 最終run前に初文取得可能、音声順序/日本語品質、音声秒精算、session終了直後/5分期限後の既知ID拒否を確認する。拒否確認はproviderを呼ばない。音声・本文・bearerを通常ログや証跡へ残さない。
4. disabledへ戻し、参照sweep/TTLと独立usage recoveryで残留物/枠を確認する。artifact digest、日時、設定識別子、call数、合否/未実施だけ記録する。実機のキュー/停止/音響評価はVOICE-03/04/05とQAのrelease gateで別に実施する。

切戻しは `TTS_PROVIDER=disabled` と旧artifactへ戻す。保存済みspeech参照は再生させず5分期限で拒否し、sweep/TTLとusage recoveryを維持する。schema変更は既存のspeech.ready/GET契約の実装と説明更新で、既存text clientは影響を受けない。

スマホの順序付きqueue・native停止・遅い結果の拒否は [mobile audio](mobile-audio.md)を参照。

Pi daemon の run/session/generation queue、live speech.ready、停止と失効は [Pi音声adapter](device-audio.md)を参照する。
