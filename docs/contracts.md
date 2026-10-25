# 共通型と通信スキーマの対応

BA-FND-03、更新日2026-10-26。[brainoid_contracts](../packages/contracts/src/brainoid_contracts/__init__.py) はAPI・worker・共有packageで利用するPythonの公開契約である。HTTP・JSON Schemaの正本を置き換えず、後続実装が同じ入力検証と保存表現を使うための型を提供する。生成TypeScript clientはBA-FND-04で提供する。

| Python公開型 | OpenAPI `components/schemas` と主な使用箇所 | JSON Schemaの対応 | 検証・正規化 |
| --- | --- | --- | --- |
| `ResourceId` | `ResourceId`。tenant/resource/device/session ID、`Error.error.request_id` | device/run/toolの `format: uuid` | ハイフン付き36文字。大文字も受理して小文字化。UUID versionは限定しない |
| `FirebaseUid` | `FirebaseUid`。`User.user_id`、membership、owner/assignee user ID | device `session_started.payload.user_id` | 1〜128文字のopaque string。UUID制約・空白除去・大小文字変換なし |
| `ProviderId` | `ProviderId`。Calendarのcalendar/event ID、calendar allowlist | tool `calendar_id` / `event_id` | 1〜1024文字。内容を変換しない。HA `external_id` 等の固有上限255文字は利用先モデルで追加する |
| `UtcTimestamp` | `UtcTimestamp`。created/updated/due/expiry、Calendar `date_time` | `format: date-time`。device `sent_at`、run `occurred_at`、toolの期限/予定時刻 | timezone付きRFC 3339またはaware `datetime`。内部保存はUTCのaware `datetime`、JSONは `Z` 表記 |
| `IanaTimezone` | `IanaTimezone`。user/tenant/task/reminder、CalendarTime | tool `timezone` | 1〜80文字、`zoneinfo.ZoneInfo` が解決するIANA名。`UTC`・IANA aliasを受理。case-sensitive。OS固有のlocaltime/posixrules、posix/rightツリーは禁止 |
| `Version` | `Version`。resource `version` | tool `expected_version`、deviceの正整数版 | 1以上の整数。bool・浮動小数点・文字列を整数へ変換しない。0開始のepoch/cursorには使わない |
| `Visibility` / `DisplayPolicy` / `Scope` | 同名schema。各create/resourceのvisibility/display_policy | tool MemoryCreate等の同じenum/条件 | private+shared_displayを拒否。残り5組合せを受理。Scopeは追加フィールドを拒否し、生成後は不変 |
| `ActorContext` / `TenantContext` | 内部専用。HTTPの入力schemaには公開しない | tool引数から取得しない | user/device/workerをdiscriminatorで分離し、userはFirebaseUid、deviceはResourceId、worker主体はProviderIdを保持 |

IDとIANA名のクラスは直接コンストラクタでも検証する。`UtcTimestamp` と `Version` はPydanticの `Annotated` 型であり、モデルフィールドまたは `TypeAdapter` で検証する。モデルの `model_construct()` や検証を省く `model_copy(update=...)` は信頼済み値専用であり、入力検証には使用しない。

```python
from pydantic import TypeAdapter
from brainoid_contracts import ResourceId, UtcTimestamp, Version

resource_id = ResourceId("FCAE923B-E348-4366-943C-F2634AD6C09B")
instant = TypeAdapter(UtcTimestamp).validate_python("2026-01-01T00:30:00+09:00")
version = TypeAdapter(Version).validate_python(1)
```

この日時のUTC表現は `2025-12-31T15:30:00Z`。秒を必須とし、小数秒は最大6桁を保持する。未知offsetを意味する `-00:00`、うるう秒、7桁以上の小数秒、UTC変換後の年1〜9999範囲外は黙って丸めず拒否する。lowercaseの `t/z` は受理して正規化する。IANA timezoneは元の別フィールドに保持し、日時に無いoffsetを補わない。DSTの曖昧な壁時計時刻・自然言語の解釈はこのpackageの責務ではない。対応開発OSのLinux/macOSにあるIANA zoneinfoデータを使用する。

JSON Schemaの `date-time`・`uuid` はformat検証を有効にしたvalidatorが必要で、`iana-timezone` は本プロジェクトのcustom formatである。IANA名の実在や上記の日時保存プロファイルはPython側でも検証する。OpenAPIでは共通schemaへの参照を追加し、provider IDの空文字・timezoneの空文字を明示的に拒否するよう整合した。JSON Schemaで数学的に整数とみなす `1.0` もPythonのVersion入力では拒否するため、生成clientはversionを整数表現で送る。

`TenantContext` は一つの `tenant_id` と `actor` をまとめる。actorは認可版と不変の許可scope集合を持ち、userはactive membership、deviceはactive device statusを表す。deviceの任意の `PrivateSessionContext` はIAMで解決済みのuser・session IDと期限を保持する。Contextの生成は形の検証だけであり、token・membership・tenant所属・scope・認可版・sessionの有効期限を証明しない。BA-IAM-02等のIAM層で照合してから生成し、操作時の再検査もIAM層が行う。LLM/tool引数や未検証のリクエストbodyから直接構築して認可根拠にしてはならない。

`Scope` はvisibilityとdisplay_policyが揃った値に適用する。PATCHでは保存済み値と入力を合成してから再検証する。private所有者の必須性や表示端末への転送可否など、リソース固有の条件・実際の認可は後続のサービス実装で追加する。

検証は [test_contracts.py](../tests/test_contracts.py) とworkspaceのpackage import検証に含める。IDの型/長さ/形式、日付・月・年跨ぎ、offset、DST fold、UTC上下限、IANA名、version型/値、scope全組合せ、不変Context、JSON入出力を外部APIなしで確認する。実際の会員権限照合・全OpenAPI/JSON Schema検証・client生成はそれぞれ後続チケットの対象である。
