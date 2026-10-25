# 共通型と通信スキーマの対応

BA-FND-03・04、更新日2026-10-26。[brainoid_contracts](../packages/contracts/src/brainoid_contracts/__init__.py) はAPI・worker・共有packageで利用するPythonの公開契約である。HTTP・JSON Schemaの正本を置き換えず、後続実装が同じ入力検証と保存表現を使うための型を提供する。生成TypeScript clientは [packages/api-client](../packages/api-client/src/index.ts) から公開する。

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

検証は [test_contracts.py](../tests/test_contracts.py) とworkspaceのpackage import検証に含める。IDの型/長さ/形式、日付・月・年跨ぎ、offset、DST fold、UTC上下限、IANA名、version型/値、scope全組合せ、不変Context、JSON入出力を外部APIなしで確認する。実際の会員権限照合は後続チケットの対象である。

## HTTPクライアントと契約検証

BA-FND-04では業務84操作とBA-FND-02のhealth 2操作、計86操作からHTTP SDKを生成する。`operationId` と同じ名前の関数、要求・応答型、fetch transportを公開する。HTTP handler、Firebase SDK、WebSocket接続管理やSSE再接続の業務制御は実装しない。型はコンパイル時の補助であり、認可・入力値・応答値の実行時検証を代替しない。

```typescript
import { createBrainoidClient, getCurrentUser } from "@brainoid-internal/api-client";

const client = createBrainoidClient({
  baseUrl: "https://api.example.test", // 配備先のservice root。/v1は含めない
  auth: async (scheme) => getCurrentToken(scheme.key), // 呼出元が実装するtoken provider
  fetch: platformFetch, // 呼出元が実装するtransport。省略時はglobalThis.fetch
});
const { data, error, response } = await getCurrentUser({ client });
```

`auth` は生のbearer値または同期/非同期関数。`userBearer` / `deviceBearer` のscheme keyで資格情報を選び、認証が必要な要求ごとに取得する。health等の公開操作では呼ばない。clientはインスタンスごとに作り、各SDK関数へ渡す。生成器はpath単位の `servers` を反映しないため、公開factoryでhealthだけservice rootへ向け、業務操作には `/v1` を前置する。生成ファイルのglobal clientへ認証値を設定する運用は使わない。HTTPエラーは `error` と `response.status` で扱い、必要時だけ `throwOnError: true` を指定する。通常HTTP要求の自動再試行は行わない。

公開 `streamRunEvents` は生成SDKを包むadapterを使用する。生成SDKが選ぶURL・認証方式を保持し、HTTP transportから受け取ったbodyを既存の `eventsource-parser` で解析する。CRLFの途中やUTF-8文字の途中で分割されたchunk、複数の `data` 行、event/id/retryを扱い、chunk境界でイベントの内容を変えない。[SSEの解析規則](https://html.spec.whatwg.org/multipage/server-sent-events.html#parsing-an-event-stream)

OpenAPIの `text/event-stream` のwire schemaは文字列だが、adapterは各イベントの `data` をJSON decodeして返すため、公開の `StreamRunEventsResponse` とiterator/callbackのdata型は `unknown` とする。JSONでないdataは文字列のまま保持する。利用側は [run-events.schema.json](../specs/run-events.schema.json) に照らして検証してから利用する。SSEは1回の呼出しにつき1接続で、認証・要求はiteratorの初回読取り時に行う。HTTP 401/403・token provider・通信の失敗はiteratorからthrowし、`onSseError` にも通知する。自動再接続・暗黙retryは行わない。再接続は呼出元が認証・session状態とcursorを確認したうえで新たに `streamRunEvents` を呼び、更新後のtokenを取得する。失効時は再接続を停止する。iteratorの終了時はreaderをcancelして解放する。生成SDKやclientの低水準 `sse` を直接使わず、この公開adapterを利用する。

`signal` のabort時は未配信dataを破棄し、iteratorを正常終了する。待機中の利用側validator/transformerは完了を待つが、abort後にその結果をcallback/iteratorへ渡さない。callbackからabortした場合も、そのdataをiteratorへyieldしない。要求の変更には `client.interceptors.request` を使う。生成runtime専用の `onRequest` は公開SSEオプションに含めない。

| コマンド | 検証・出力 |
| --- | --- |
| `mise run check:contracts` | Redoclyの既存 `spec` rulesetと明示ruleでOpenAPI 3.1の構文・構造・参照・operationId・path parameter・security定義を検査。openapi-spec-validatorがinline/componentのSchema ObjectをJSON Schema 2020-12メタスキーマで検査。Ajv CLIのDraft 2020-12 compileで別ファイル3個のJSON Schemaのメタスキーマ適合と参照を検査 |
| `mise run generate-client` | 固定した [生成設定](../openapi-ts.config.mjs) から一時領域へTypeScriptを生成し、型検査して `src/generated` にES moduleとTypeScript宣言を出力 |
| `mise run check:client` | 同じ生成処理を一時領域で実行し、生成物のファイル集合・内容を比較。未追跡の生成物も比較し、checkoutは変更しない |
| `mise run check:compatibility` | [基準commit](../specs/openapi-baseline.ref) のOpenAPIと作業版をoasdiffで比較。WARN以上を非ゼロ終了にする |

validator・generator・compiler・比較ツールはmanifest/lock/miseに完全版を固定する。[実行ラッパー](../scripts/validate_contracts) に独自のOpenAPI/JSON Schema判定は置かない。Ajvは `ajv-formats` のUUID・日時等を使い、独自 `iana-timezone` は警告付きの注釈として扱う。IANA名の存在検証は上記Python型の担当。`--strict=false` はAjv固有の追加制約を無効にするもので、メタスキーマ検証は有効のままである。

生成器内部はTypeScript 7が提供しないcompiler APIを使うため、開発専用の [openapi-generator member](../packages/openapi-generator/package.json) に生成器とTypeScript 5.9.3を固定する。生成ソースはworkspaceのTypeScript 7で `strict`・`noUncheckedIndexedAccess` を有効にしてコンパイルする。upstream fetch runtimeは `exactOptionalPropertyTypes` に未対応のため、その内部コンパイルでは有効にしない。公開宣言・factory・利用側・型負例は既存の `exactOptionalPropertyTypes` を含むworkspace設定で検証する。生成物はBiome整形の対象外で、直接編集せず再生成する。同梱runtimeの [MIT表示](../packages/api-client/LICENSE.hey-api) を保持する。

互換性の基準は作業ブランチ先頭へ自動追従させない。初期基準はBA-FND-03完了commit。別の比較対象は `CONTRACT_BASE_REF=<commit> mise run check:compatibility`、または `mise exec -- scripts/check_contract_compatibility <old.yaml> <new.yaml>` で明示する。履歴不足時は比較を省略せず失敗するため、基準commitを取得してから実行する。破壊的変更が必要な場合はAPI版・移行期間・旧client対応を決め、変更前基準による検出結果をレビューし、その後に基準を更新する。現在のcheckerはHTTP契約を比較し、別ファイルのdevice/run/tool schemaの互換性判定は含めない。oasdiffの `$dynamicRef` / `$dynamicAnchor` と `components.pathItems` の制限にも注意し、現契約ではこれらを使わない。[oasdiffの3.1対応範囲](https://github.com/oasdiff/oasdiff/blob/v1.34.1/docs/OPENAPI-31.md)

[契約受入テスト](../tests/test_api_contracts.mjs) は全操作のexport、未解決参照、重複operationId、必須項目不足、schema型不正、`multipleOf: 0`・重複required、path parameter不足、未定義認証方式、JSON Schema負例、操作削除・要求必須項目追加・enum縮小と非破壊的追加を確認する。注入したfetchだけでURL・認証更新・公開health・body/header/query・エラー、SSE JSON data・cursor・401/403拒否・明示再接続時のtoken更新・失効を確認する。SSEはCRLF・複数data行を全byte境界で分割し、UTF-8・event/id/retry保持と購読終了時のcancelも検証する。実APIへは接続しない。[型負例](../packages/api-client/type-tests/client.ts) で必須path/body/headerと応答型、SSEデータを文字列として扱えないことも確認する。
