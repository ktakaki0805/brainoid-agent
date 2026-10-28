# 端末設定・失効・再登録

BA-IAM-08 は FR-003 / T-004 の API 側を提供する。[device routes](../services/api/src/brainoid_api/routes/devices.py)、[repository](../services/api/src/brainoid_api/repositories/devices.py)、[共通検査](../services/api/src/brainoid_api/services/device_authorization.py)が現在の本人・tenant・membershipと[独立認可台帳](control-ledger.md)を確認する。開いたsocketへの通知・60秒以内の切断はDEV-03/04、Pi側の秘密ファイル消去はDEV-02/09、個人session APIはIAM-09、command API/dispatchはAPP-06/DEV-07へ引き継ぐ。

## 管理 API

すべて `/v1/tenants/{tenant_id}` 配下で、Firebase本人認証と現在のowner/admin権限を必要とする。member、device token、別tenantのactorは利用できない。端末管理にはアカウント/tenant削除向けの直近再認証条件を追加しない。

| API | 動作 |
| --- | --- |
| `GET /devices` | cursor付き一覧。cursorは現在のactor権限と一覧内容へ束縛する |
| `GET /devices/{device_id}` | 現在metadataとETag。未確定台帳はsuspendedとして返す |
| `PATCH /devices/{device_id}` | `name`、`allowed_scopes`、`allowed_entity_ids`、`allowed_room_ids`のみ。空body/null/未知scopeは拒否 |
| `DELETE /devices/{device_id}` | 独立台帳へ先行denyを記録し、deviceをrevokedへ進める。204には秘密を含めない |

PATCH/DELETEは `Idempotency-Key` と引用符付きversionの `If-Match` が必須。同keyの別対象・本文・ETagは409、古い版は412、ETag欠落は428。現在のactor権限が変わった場合、成功receiptの再送でも再認可を省略しない。本文の配列は順序・重複を正規化する。権限の一覧にはshared、conversation、memory.read/write、notes、tasks、reminders、calendar.read/propose、home.controlを使い、認可層が各機能の利用条件を別途検査する。

entity/roomのIDは同じtenantのcanonical `entities` 文書でID・tenant・未削除を確認する。roomにはさらに `kind=room` を要求する。別tenant、欠落、削除済み、pathとmetadataが不一致のIDを拒否する。同じ検査を初回pairing確認にも適用する。設定は家電の実在/到達性を保証せず、操作直前にも現在の対象を確認する。

名前だけの変更は通常の `version` を増やし、認可版とgenerationを保持する。scope/entity/roomの実質的変更は拡大も縮小も `authorization_version` と `connection_generation` を一つ増やす。縮小・失効はdeny、純粋な拡大はgrantとして記録し、両方とも確定まで停止する。scope変更後は旧accessだけでなくrefresh familyも使えない。**再接続には管理者が明示失効してから新しいpairingを承認する必要がある**。旧refreshで縮小後の権限を取得する経路は用意しない。

## 障害・競合と監査

deviceごとに一件のmutationを予約し、actorの現在認可stamp、旧device、要求binding、固定した台帳Changeを `device_operations` に保存する。他のmutationを同時に開始せず、障害時は同key/本文/ETagでroll-forwardする。別deviceの操作は独立する。会員変更向けのtenant全体のowner維持予約は使わない。

各DB phaseで現在のactor・tenantと予約・対象を再確認し、外部台帳writeはtransaction callbackの外で実行する。順序は予約、先行intent、DB suspended、確認、commit marker、active/revoked、metadata/versionと成功監査である。intentだけが保存された状態も旧credential・待機処理を拒否する。台帳障害時に旧状態へ戻さない。返却成功前に最新のoperation・版・statusを確認する。

generationの増分はdeviceの認可変更をDBへ初回適用するときだけ行う。既存schema_version=1のDeviceTargetは変更せず、既存記録と同operation再送を読める。同操作の再送、DB応答喪失後の継続、reapplyの反復では二重増分しない。隔離復元で別operationを適用する場合は、復元DBのgenerationを一つ進める。generationは独立台帳の復元正本ではないため、最新認可版とOPS-07の外部credential epoch更新も必須である。復元DBのgenerationだけを根拠に古いcommandを再実行しない。

監査はactor user ID、target device ID、result、request ID、時刻だけを保存する。予約時はpending、全phase完了でsucceededへ進む。秘密、scope本文、entity一覧、端末名を監査へ複製しない。管理操作receiptにcredentialを保存しない。

## 同じinstallationの再登録

Piは旧credentialを使わず、新しいpoll secret/codeを持つpairingを開始する。現在owner/adminが `PairingConfirm.replaces_device_id` に旧IDを明示する。旧deviceが最新台帳でcommitted revoked、処理予約が完了済み、installation indexが同tenant/旧deviceを指す場合だけ新しいdevice UUIDを作成する。縮小後の再登録では旧scope/roomを引き継ぎ、entityは管理者が改めて確認する。

旧deviceのrevoked記録を残し、installationのtenant束縛を保持する。indexには最初のdevice IDを残し、後継device IDの更新と新device・pairing確認を同じtransactionでcommitする。同確認operationの再送は同じ後継を返し、並行確認や別pairingで二台を増発しない。旧IDは後継へalias解決しない。nonceを変えた物理装置の同一性は推定できず、別installationとして管理者確認を要求する。

旧access/refresh/family、旧pairing confirm/exchange receiptは新IDへ移れない。確認済みで未交換のpairingも、確認時の認可版/generationを固定しているため、設定変更後の新しい権限で資格情報を発行できない。新しい秘密は新pairingのpoll secretを持つPiだけが交換する。

## command・個人session・outputの境界

`DeviceBinding` はtenant/device/認可版/generationを固定した内部値で、信頼された永続commandに保存する。`DeviceAuthorization.current` を副作用/保存と同じtransaction、出力直前に再実行する。現在の台帳、tenant/device状態、版/generation、任意scope/entityを検査する。個人sessionを伴う呼出しは現在session・membership・期限と選択scopeも確認する。session開始前のdevice検査も同境界を使う。

既存の `policy.worker(saved, output=...)` / `resource(previous=...)` は保存grantと現在の出力先stampを比較する。失効後も、再登録で新しいdeviceができた後も、古いcommand binding/output grantは拒否される。新credentialを取得しても古いcommandのbindingを新しいものへ置換してはいけない。OPS-07ではbackup内のdevice command/sessionを再開せず失効させ、外部epoch更新と再登録を行う。実際のdispatch/socket通知は後続チケットでこの境界に接続する。

## 検証と展開

[fake/HTTP検証](../tests/test_devices.py)と[Firestore emulator検証](../tests/emulator/test_devices_integration.py)でT-004のAPI失効、縮小中の再送、tenant境界、台帳write/DB応答喪失、現在actor、同時mutation/再登録、旧credential・未交換pairing・個人session・保存output grantの拒否を確認する。`mise run setup`、`mise run check`、`mise run test`、`.env.emulator.example`を明示loadした `mise run test-emulator`を使う。

新依存・外部resourceの追加はない。既存IAM-07 deviceのscope/room/認可版/generationを使用する。切戻しで台帳や予約を消さず、同じoperationを扱える互換版でroll-forwardする。失効deviceをDBだけでactiveに戻す運用は禁止する。実socket切断時間、実機秘密消去、実backup復元、staging IAMはこの検証の対象外である。

スマホからの登録・許可設定・失効、応答不明と現在認可の扱いは [UI-09の画面と検証](device-ui.md)を参照する。
