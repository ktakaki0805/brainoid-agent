# 独立認可台帳の利用契約

BA-IAM-05 は、DB を巻き戻しても失効や減権限を失わないための内部基盤を提供する。[台帳型と状態遷移](../packages/policy/src/brainoid_policy/control_ledger.py)は SDK/FastAPI に依存せず、[GCS adapter](../services/api/src/brainoid_api/adapters/gcs_authz_ledger.py)へ構成済み bucket を注入する。[repository](../services/api/src/brainoid_api/repositories/authz_changes.py)が DB transaction と外部記録を分離する。BA-IAM-04 で[招待 API と既存 tenant API](invitations.md)へ接続済み。bucket 作成、資格情報の発行は行わない。

## 1. 記録と順序

`Change` は operation UUID、tenant UUID、主体、DB の整数認可版、deny/grant、確定後の role/status/scope を固定する allowlist DTO である。複合 `authorization_stamp` を整数版として使わない。本文、任意 PATCH、token、credential、表示名を受け取らず、UID を含む識別情報もログへ出さない。DTO の妥当性は操作する権限の証明ではない。

制御 bucket には次のオブジェクトを `if_generation_match=0` で追記する。上書きや削除をアプリへ許可しない。

| オブジェクト | 意味 |
| --- | --- |
| `authz/v1/operations/{operation_id}.json` | operation を主体・要求全体へ一意に束縛。別 payload/主体への再利用を拒否 |
| `authz/v1/subjects/{subject_hash}/anchor.json` | 初回の DB 認可版を固定。異なる初期版が並行して別の chain を開始することを防ぐ |
| `authz/v1/subjects/{subject_hash}/intents/{version}.json` | 主体・版の単一 slot。ここへの記録が完了すると先行 deny が有効 |
| `authz/v1/subjects/{subject_hash}/commits/{version}.json` | 同一 operation・版・payload の DB 適用確認済み記録 |

subject hash は tenant・種別・opaque UID/device UUID の JSON 配列から算出する。UID を path へ連結しない。既存 membership は UID と canonical tenant collection で解決し、重複を拒否する。新規 membership の path は hash を使う。

次の版は直前の版が確定した場合だけ確保できる。同版の並行作成では一方だけが成功し、未確定の主体は停止する。operation の予約だけでは権限や主体 slot を取得しない。予約後・slot 作成前の失敗は同じ operation の再送で再開する。競合した操作は記録を消さず、最新の主体状態を確認して処理する。

`execute(change, authorize=...)` は次の順序を守る。

1. 現在の DB transaction で `authorize` を呼び、現在の主体版または同一 operation の適用結果を確認する。
2. transaction の外で operation、anchor、intent を記録する。失敗時は成功を返さない。
3. 別の DB transaction で再認可し、目標の認可版・operation・allowlist payload と `suspended` を保存する。
4. commit 応答後、別 transaction で現在の業務認可と DB の operation・版・payload の一致を再確認する。
5. transaction の外で committed marker を記録する。
6. 最新台帳、現在の業務認可と DB の operation を再確認し、確定後の status を適用する。

effect と最終 status は別である。admin→member や device scope 縮小は `deny` と `active` の組合せを使い、処理中は停止、確定後は縮小した権限で利用できる。grant も確定前は停止する。競合や途中失敗で元の権限へ戻さず、同じ操作を roll-forward する。

## 2. 認可と API 接続

`TransactionPolicyState` / `transaction_policy` の `ledger=` を設定すると、現在の台帳を読み、未確定、DB と異なる版、異なる role/scope/status を `suspended` として扱う。台帳障害・破損も拒否する。GCS が成功して DB が失敗し、DB 自体は古い `active` のままでも、この境界では先行 deny が有効である。GCS の書込を transaction callback から呼ばない。

IAM-04 で現在の tenant API と招待 API に同じ ledger を注入済み。IAM-06/08 以降も認可を行うすべての read/write/worker/output 経路へ同じ ledger を注入する。`ledger=None` の従来経路を残したまま先行 deny 対応済みと扱わない。外部台帳と DB に分散 transaction はなく、認可読取と同時進行する変更は、後続処理の副作用・出力直前でも再評価する。長期の snapshot/cache を認可の根拠に使わない。

`authorize` は信頼された業務コードとして、同一 transaction から actor・tenant・対象版・現在の権限と業務条件を読み直す。role 変更の範囲、最後の owner、本人再認証、監査、複数主体の owner 移譲をこの単一主体基盤だけで満たしたと扱わない。招待の再受諾は IAM-04 でこの境界へ接続し、最新認可版から固定された新しい operation を作る。membership deny の初回 DB 適用は tenant の invitation_epoch も原子的に増やし、既存の未使用招待を無効化する。復元で異なる operation を適用する場合も epoch を増やし、同じ操作の retry/reapply は再増分しない。古い受諾の receipt や committed grant を再送しても、後続 deny の版を上書きできない。

初回 tenant/owner 作成（IAM-03）にはまだ台帳がない。通常稼働での導入時に限り、信頼された構成で `allow_untracked_bootstrap=True` を選べる。これは台帳が存在せず DB に authorization_operation_id/authorization_change もない主体にだけ現在の DB 権限を使う互換モードであり、追跡済み主体の台帳欠落や台帳障害を無視しない。初回の変更は DB の現在版を anchor に固定する。新規所属は存在しない状態の `expected_version=0` から始める。復元環境ではこの互換モードを使わず、既存の未記録 owner を安全とみなさない。

device の台帳は認可状態だけを記録する。秘密 credential や token は復元しない。IAM-07/08 は credential/token を現在の認可版や credential generation へ束縛し、古い認証値を拒否する必要がある。OPS-07 の隔離復元では device credential と個人 session を失効させ、再登録を要求する。確定 grant の再適用は、backup 内の旧 credential を再利用してよいという証明ではない。

## 3. 再試行と隔離復元

台帳 I/O の失敗は `temporarily_unavailable`（503）、競合・旧操作は `operation_forbidden` とする。エラーへ payload を含めない。SDK は generation 条件付き retry を使い、保存済みだが応答を失った場合の 412 は既存オブジェクトとの完全一致で解決する。DB commit の応答喪失では、同じ operation の再送が DB の適用結果を確認する。別の operation ID で盲目的にやり直さない。

`reapply(change)` は停止済みの隔離 DB/保守処理用で、引数を主体の識別にだけ使い、必ず最新台帳を読み直す。旧 grant を渡しても最新の deny を適用する。

| 最新の状態 | 再適用結果 |
| --- | --- |
| committed、DB が同版以下 | 確定した target と operation・版を適用。二回目は DB write をしない |
| pending | target の版で `suspended` を保存。committed marker を作らない |
| 台帳なし | 既存主体を `suspended` にし、結果を `missing` として返す |
| DB が台帳より新しい | DB 版を減らさず `suspended`。整合を証明できるまで再開しない |
| 台帳破損・読取不能 | 503。復元 gate を開けない |

`reapply` の `suspended` / `missing` は手当てが必要な候補で、復元完了を意味しない。通常の `execute` 再試行は現在の業務認可を再実行して pending を確定できるが、復元側はこれを自動呼出ししない。復元手順は全書込元を止め、台帳と DB の全主体を列挙して最新状態を照合し、未解決主体が残る tenant の API を閉じたままにする（OPS-07）。台帳の保持・欠落検知・全主体列挙・実 backup 復元の実行は同チケットで実装する。

制御 bucket はすべての復元可能 DB/backup から独立させる。anchor、operation binding、版 chain を部分的に削除しない。初期30日以上かつ全復元可能 backup より長い保持、専用 IAM、削除権限分離を OPS-02 で実装する。現実装は主体の全 chain を強整合 read/list で検査するため、増大した履歴の整理・集約は検証可能な checkpoint 設計を先に追加する。台帳を削除して通常の bootstrap に戻す運用は不可。

## 4. 検証と展開

[障害行列](../tests/test_authz_ledger.py)は immutable write の前後、DB 適用・確認・有効化の前後、同版競合、旧 grant と次版 deny の競合、opaque UID、payload 再利用、部分減権限、古い DB からの再適用を確認する。[SDK 契約テスト](../tests/test_gcs_authz_ledger.py)は実際の Storage Client と HTTP transport を使い、条件付き multipart upload、503→retry→412、checksum 付き download、list、403 を検証する。実 bucket を作らず外部通信もしない。[emulator 結合](../tests/emulator/test_authz_ledger_integration.py)は公式 Firestore SDK の transaction と独立 fake bucket を組み合わせ、T-028 の認可部分と冪等な再適用を確認する。

変更後は `mise run setup`、`mise run check`、`mise run test`、明示的に `.env.emulator.example` を load した `mise run test-emulator` を実行する。GCS の実 IAM/retention、実 backup 復元、稼働中 socket への失効通知はこのローカル検証の対象外である。

導入時は guard と mutation を一緒に接続し、bucket を明示注入する。IAM-04 の runtime 設定は[招待の運用契約](invitations.md)に従う。認可版・status・opaque ID が欠けたデータを暗黙に許可しない。切戻し時も台帳を削除せず、台帳を読めない旧実装へ権限変更経路を戻さない。対応版へ roll-forward するまで該当操作を停止する。
