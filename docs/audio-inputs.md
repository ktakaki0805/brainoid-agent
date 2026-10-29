# 音声入力と transcript 参照

BA-VOICE-01 / FR-005 の API は `POST /v1/tenants/{tenant_id}/audio-inputs`。user/device bearer、16〜128文字の `Idempotency-Key`、`Content-Type: audio/wav` を必須とする。device の個人 session は `X-Private-Session` を追加する。録音 UI・Pi microphone は VOICE-03/04、TTS と speech 出力は VOICE-02 の範囲。

[実装](../services/api/src/brainoid_api/audio_inputs.py)は request stream を最大5MiB、受信15秒でメモリ内に受ける。宣言 Content-Length と MIME だけを信用せず、RIFF/WAVE 全長、各 chunk の長さ・padding、単一の fmt/data、PCM format 1、16kHz・16bit・mono、byte rate/block align、偶数 data 長を確認する。実 PCM data は最大960,000フレーム（60秒）。未知の metadata chunk は最大1,024 chunk/5MiB内で読み飛ばす。ゼロフレーム、全サンプル0は422。微小音・雑音を「無音」と推測せず provider に渡し、空 transcript は422にする。

入力超過413、不正形式/無音422、quota429、未設定/timeout/provider失敗503、切断取消と処理中/失敗済みの同一キー再送409を共通エラーへ変換する。切断済み接続への応答配送は保証しない。アプリは原音声を DB・object storage・通常ログ・一時永続ファイルへ保存しない。adapter は検証済み PCM のみをメモリから送信する。モデルの本文・音声の debug repr と provider 例外は公開しない。外部 provider の保持と地域・処理条件は自サービスの保持とは別の OPEN-005 確認事項。

認可は RUN-04/IAM-02 の現在 user account/membership、device credential/generation、private session/slot/scope と台帳を使う。tenant/actor/session と現在認可 binding を保存し、異なる actor、user と device の間、共有と個人 session の間で参照を移せない。呼出し直前、provider 待機中（250ms間隔）、結果保存、精算後の HTTP 公開と turn 解決時に再検査する。session の実期限/無操作期限・usage 実行期限を transaction の書込みと応答 release まで守る。削除 fence と tenant purge epoch が変わった既存参照は拒否する。epoch は tenant 内の無関係な削除でも参照を無効にする保守的な境界で、再録音を必要とすることがある。

受付時に [RUN-08 台帳](usage.md)で音声秒と同時枠を原子的に予約する。`purpose=audio_input` は会話turn数を増やさず、user/device/tenant の同時枠と音声上限を共有する。予約は実PCM秒数の切上げ。provider最終 `totalBilledDuration` があれば切上げ値で精算し、欠落/timeout/取消/エラーはunknownとして予約額を保持する。実呼出し前に中止したことが確実な場合のみ0秒。実課金額をアプリの秒数から断定しない。provider 呼出しは45秒上限・retryなし、実行枠は60秒期限。API正常終了/取消は枠を解放し、process停止時は既存 `UsageReconciler.recover` が同じ usage_runs を回収する。原音声を保存していないので中断した STT は再実行しない。

transcript は受付時から15分。所有文脈、状態、`expires_at` と Firestore Timestamp の `ttl_at`、本文だけを `audio_inputs` に保存する。時刻が期限と等しくなった時点で参照を拒否する。TTLの非同期削除を認可に使わない。`AudioInputs.expire(tenant, after=..., limit=100)` は bounded page で期限切れ document を消去する。配備時には tenant ごとの sweep と `UsageReconciler.recover` を1分間隔で実行する運用 wiring、または `audio_inputs.ttl_at` の Firestore TTL を明示設定する（OPS配備で確認、今回資源変更なし）。停止期間は消去が遅れ得るが参照は常に拒否する。account/member削除は既存 lifecycle purge の owner 判定にこの collection を含め、tenant削除も対象とする。session終了後は即時参照不可、本文の物理消去はこの15分保持と sweep/TTLに従う。端末側の画面/キャッシュ消去は既存 session と後続録音 UI の責務。

同一 upload key/body は24時間の既存冪等台帳に束縛される。bodyはsha256だけ記録し、原音声を記録しない。処理中/失敗済みは409、成功後は現在認可・15分期限を再検査して同じ結果を返す。期限後は410（sweepで消去後は404）。異なるbodyは409。自動再課金を行わず、再録音は新しいkeyを使う。

`POST turns` は `text` または `audio_input_id` の一方のみ、`output_mode=text|speech`（speechは[TTS設定](speech-outputs.md)が必要）。新規turnのtransactionで未失効参照を解決し、既存のuser message/run/jobへ通常の入力文字として保存する。それ以後は通常の会話保存・削除規則となり、一時参照の期限で会話履歴は消さない。同じturn key/bodyの再送は、新規音声利用ではなく既に受付済みrunの参照であり、現在の会話/actor/session認可の後に同じrunを返す。15分後でもその再送は可能だが、新しいturn keyに同じ失効音声IDを使うことはできない。失効actor/sessionに既存runを返す例外はない。

設定は既定 `STT_PROVIDER=disabled`。実adapterを有効にする場合だけ staging/production で `STT_PROVIDER=google-v2`、`STT_LOCATION`、`STT_MODEL`、`STT_RECOGNIZER` と既存の明示 GCP project/認証を全て設定する。local環境では実providerを禁止し、テストは明示的な注入でfakeを使う。region/modelの既定や環境credentialからの暗黙採用はない。region別 endpoint、inline PCM、明示 LINEAR16/16kHz/mono/ja-JP、`configMask=*` で recognizer の隠れた既定を引き継がない。redirect/retry/proxy環境参照を無効化し、応答128KiB・本文8,000文字を上限にする。利用には `speech.recognizers.recognize` が必要だが、今回 IAM を変更していない。

既存固定依存 HTTPX 0.28.1（BSD-3-Clause）、google-auth 2.62.0（Apache-2.0）を API の直接依存として宣言し lock を更新した。新しい外部distributionや版は増やしていない。HTTPXはencode/httpx、google-authはGoogleの保守対象で、既存 [依存台帳](toolchain.md)の license 表示を配布時も維持する。Google Speech SDK は追加せず REST v2 を Core外の [adapter](../services/api/src/brainoid_api/adapters/google_speech.py)に閉じ込める。認証更新と明示 Speech HTTPS endpoint への通信が追加の実行能力であり、保存 bucket・追加 GCP 資源・権限付与は不要。

一次資料（2026-10-29確認）: [Speech v2 recognize/usage metadata](https://docs.cloud.google.com/speech-to-text/docs/reference/rest/v2/projects.locations.recognizers/recognize)、[explicit decoding](https://docs.cloud.google.com/speech-to-text/docs/reference/rest/v2/projects.locations.recognizers#ExplicitDecodingConfig)、[同期認識](https://docs.cloud.google.com/speech-to-text/docs/sync-recognize)。provider上限にかかわらず本APIの60秒/5MiBを優先する。

ローカル確認は [HTTP・WAV・fake transport・認可・usage試験](../tests/test_audio_inputs.py) と [Firestore競合試験](../tests/emulator/test_audio_inputs_integration.py)。通常runへ渡してworker完了、他actor/session/tenant、15分境界、取消・失効・削除・実行期限、quota・未知usage、実REST adapterのwire形式/取消を検証する。raw audioは合成値のみ。

日本語短音声のstaging照合は未実施。OPEN-005の採用model/region/保持条件、短命認証、料金上限が未確定であり、環境credentialがあっても有料callを行わない。docs08共通条件に従いlocal受入と実providerの公開前条件を分ける。設定未確定の間はdisabledを維持する。公開前の担当者は次を実施する。

1. 対象project、対応region/model/ja-JP、保持条件、短命認証と料金上限を記録する。固定artifactでexplicit設定し、テナント/user/deviceの voice_seconds=10、concurrent_runs=1 を設定する。
2. 個人情報を含まない日本語「こんにちは」等を5秒以内の16kHz/16bit/mono WAVにし、認証済みuserから1回だけ送る。retryせず、10秒・1呼出しの上限を守る。原音声・bearer・transcript全文を通常ログや証跡へ残さない。
3. status=201、期待した日本語と意味が一致、同一key再送で追加provider呼出しなし、text出力の通常turn完了、音声秒の予約/最終値を本文なしで照合する。失敗時は自動再試行せずprovider呼出し上限の扱いを確認する。
4. `STT_PROVIDER=disabled`へ戻し、15分後の参照拒否とsweep/TTLによる消去、usage枠回収を確認する。artifact digest、時刻、設定識別子、合否・未実施を記録する。これが済むまで実provider受入完了やMVP音声全体の公開可とは扱わない。

スマホ側の録音・変換・破棄と公開前の両OS検証は [mobile audio](mobile-audio.md)を参照。
