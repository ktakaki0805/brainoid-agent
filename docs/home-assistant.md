# Home Assistant adapter と inventory

BA-DEV-06 / FR-011 は Pi の保護領域から固定 HA origin へ接続し、照明・空調の候補を canonical entity と command target に同期する。[DEV07実行器](device-commands.md) がgrant・重複排除・実行台帳へ接続する。会話・local HTTP から直接操作する経路はない。

## 導入と秘密

1. Pi を登録し、owner/admin が `POST /v1/tenants/{tenant}/integrations/home-assistant` へ `device_id` と `name` を送る。返る integration ID は一つの HA 接続の namespace になる。
2. daemon を停止し、service user だけが読める 0600 ファイルを作る。JSON フィールドは `origin`、`token`、`integration_id`、`allowed_entity_ids`。最後の配列は `light.room_lamp`、`climate.room` のような HA ID を最大100件指定する。token を argv・環境変数・会話へ入れない。
3. 同じ device 環境で `python -m brainoid_device configure-home-assistant < /operator/private/config.json` を実行し、入力ファイルを管理者の運用に従って消去する。処理は既存の daemon lock を取れた登録済み端末だけで成功し、値を stdout へ出さない。
4. daemon を再起動する。初回および60秒間隔で inventory を同期する。クラウド側の `allowed_entity_ids` は変更されない。owner/admin が発見された canonical ID を別途端末 allowlist へ追加するまで、既存 command API は実行対象として認可しない。

origin は path/query/userinfo のない HTTPS origin、または明示設定した RFC1918・ULA・loopback literal IP の HTTP origin のみ。LAN HTTP はその信頼ネットワーク内に token が流れる設定であり、TLS を使える構成では HTTPS を選ぶ。DNS による cleartext routing、環境 proxy、redirect、TLS 検証無効化は使わない。API 呼出先は内部で組み立てる固定 path のみで、会話入力から URL/service/entity/任意引数を注入できない。

設定は CredentialStore の0700 directory/0600 atomic file と既存 `home_assistant` credential reference を使い、registration epoch に束縛する。設定変更には停止・再起動が必要。同じintegration IDのorigin変更は拒否し、別HAへの接続には新integrationを登録する。reset は設定と token を消去し、進行中応答を無効化する。未設定・壊れた設定でも offline/local recovery は起動可能。device-only 署名 artifact に module が入り、新しい依存 package、SQLite migration、contract import は追加しない。旧版 rollback はこの設定を使わず、更新前後とも reset が消去できる。

## 状態と操作の契約

[HA REST API](https://contributors.home-assistant.io/core/internals/rest-api/) の `/api/states` と `/api/states/{entity_id}` で観測し、`/api/services/{domain}/{service}` で状態設定する。HTTP 200 の service response は `accepted` であり、観測結果や物理成功ではない。別の GET が返した値だけが `observed` になる。DEV06 は `succeeded` を生成しない。POST の応答喪失・timeout・送信後の失効は `outcome_unknown`、送信前の不許可・不明・unavailable・malformed は `failed`。POST は自動再送しない。

対応する action は `light.turn_on`、`light.turn_off`、`climate.set_temperature`、`climate.set_hvac_mode` の4つ。[light](https://www.home-assistant.io/integrations/light/) は対応 color mode がある場合だけ1〜100%の輝度を指定できる。[climate](https://www.home-assistant.io/integrations/climate/) は `/api/config` の `unit_system.temperature` が `°C` で TARGET_TEMPERATURE capability がある場合だけ16〜30°Cかつ機器の min/max 内の目標温度を受け付ける。通常の climate state に unit 属性があるとは仮定しない。°F の温度変換は未提供。mode は機器が現在対応する `off/heat/cool/auto` の部分集合だけで、空調の停止は `off` で表す。

HTTP 全体は8秒、response は1MiB、取得 state 数は2000、同期候補は100件まで。未対応 entity は除外し、allowlist 外 state の label/attributes をクラウドへ転送しない。token、origin、全 attributes、service response 本文はクラウド同期に含めない。クラウド送信直前に既知の token/origin を含む label は外部IDへ置換し、その他の textual field に同じ値が残る場合は同期を拒否する。拒否した provider text は記録しない。runtime は同期 task と HTTP client を所有し、停止時に回収する。

## canonical identity と権限

同期 route は user token で呼べない。device bearer の現在 family・credential epoch・device generation と権限、tenant、integration、登録した管理者の現在 membership/account を transaction 内で照合する。入力 DTO に owner/visibility/provider/token/URL はない。server が shared/shared_display scope を決める。

`tenant + integration + provider + exact external_id` の SHA256 index を使い、同名 ID を統合しない。`entities`、`entity_external_ids`、`home_inventory`、既存 Commands が読む `home_targets` を一つの transaction で更新する。新規 ID の legacy 照合は exact external reference の `array_contains` query を最大2件に制限し、未索引の同じ外部IDがあれば登録を拒否する。無関係な tenant catalog/history を走査せず、100件以上の索引済み entity があっても別 integration の登録は可能。既存 index の同期は legacy query を行わない。削除 fence・private・suppressed entity を再利用しない。

同期は完全 snapshot。欠落した target は revoked、再出現した同じ ID は同じ entity になる。canonical 側で削除・抑止された ID は snapshot に残っていても除外し、他の有効な entity の同期を続ける。HA での表示名変更は provider metadata に反映し、利用者の canonical label/alias を上書きしない。変更のない同期では target version を変えない。inventory の active/known key は別の `home_inventory_states` に保存し、無関係な entity の追加・削除で既存 command の integration fingerprint を変えない。対象自身の capability 縮小・削除は command binding を無効化する。権限変更後の replay は現在認可で拒否し、古い成功 key の replay は現在 inventory を返すので、削除済み target を復活させない。並行 snapshot は transaction commit 順に適用する。Pi は一つの同期 loop を直列実行する。1 integration の生涯外部 ID は100件までで、履歴を無制限に増やさない。

HA の管理・失効は `GET/DELETE /v1/tenants/{tenant}/integrations/{integration}/home-assistant`。DELETE は Idempotency-Key を要し、owner/admin の現在権限を検査する。metadata 取得・変更・replay・応答公開も integration の削除 fence に従う。generic integration get/list/delete は既存 Calendar 契約のまま。HA 失効は以後の同期と command 受付を拒否し、現在の binding を持つ端末の同期には410を返す。Pi は401/410で同期を停止し、通常のresource拒否では設定を保持して次周期に回復できる。Pi の token 消去には local reset を行う。

## 検証と外部前提

`tests/test_home_assistant.py` は synthetic HA の同名ID、能力・単位、値範囲、禁止 action/引数、malformed state に対する送信拒否、redirect、応答上限、timeout、受付と観測の差、応答喪失、reset、固定 numeric loopback HTTP、resource closure を検証する。`tests/test_home_inventory.py` は実 API/CloudClient composition、秘密の反射、削除後の継続同期、現在権限、immutable index、metadata fence、replay と existing Commands への接続を検証する。`tests/emulator/test_home_inventory_integration.py` は official Firestore の並行処理・response loss・失効・期限・最大100件の複数 integration を検証する。

実 HA・照明・空調・Pi はこの実装作業では接続していない。許可されたテスト HA と照明1台の状態取得および物理動作確認は、[共通完了条件](08-implementation-tickets.md#1-1prの境界と共通の完了条件)が分離する後続結合・release の外部前提である。fake の `observed` を実機合格の証拠にはしない。
