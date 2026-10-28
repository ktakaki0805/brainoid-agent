# ノートの管理画面

BA-UI-05 / FR-007。認証済みスマホで一覧・検索・作成・編集・削除を提供する。[ノートAPI](notes.md)の生成SDKを使い、現在のtenantと本人の権限をサーバーで確認する。共有表示は `shared` かつ `shared_display` のノートだけを表示し、書込み操作をスマホと本人確認の案内に置き換える。通常のPi起動は既存Recoveryのままとし、実Pi bridgeやownerのFirebase資格情報の転用は追加しない。共同リアルタイム編集は対象外。

## 一覧と編集

一覧では個人・共有、表示先、更新日時を示す。公開範囲の絞込みは取得した認可済みノートに適用する。検索はtitle/bodyへの部分一致をAPIへ要求し、200文字まで、各ページ100件でcursorを最後まで読む。100ページを超えた場合や不正なcursorは取得失敗として通知する。ページの重複ID、tenant、private owner、型を検査し、不正な内容を表示しない。

題名は1〜200文字、本文は8000文字まで。公開範囲は作成時に選び、編集では変更できない。表示先は編集できるが、privateとshared_displayの組合せは拒否する。共有ノートの作成者は公開DTOにないため推定せず、現在の編集・削除権限はAPIの判定に従う。

入力中に検索しても入力は保持する。別ノートの選択・新規作成・公開範囲の絞込みへ移るには「入力を破棄して閉じる」または削除確認を閉じる操作が必要。保存成功後は検索を解除して現在の一覧を取得する。一覧再取得だけに失敗した場合は保存応答の正本を残し、一覧全体を取得できていないことを表示する。

GETのETagをIf-MatchとしてPATCHし、412では編集開始時と再取得した最新版を並べて入力を保持する。最新版の取得に失敗しても入力を捨てない。「最新版を確認して編集を続ける」で最新版を編集の基準にし、利用者が内容を反映してから別の保存要求を送る。さらに競合した場合も同じ確認が必要。削除の412では古い対象確認を外し、最新版を選び直す。

## 受付と削除進捗

作成・編集は冪等キー、編集は期待版、削除は `delete_resource / note / target_id / expected_version / include_derived_memories` と冪等キーを固定する。応答喪失、503、不正な成功応答では同じ要求で受付を再確認し、結果が確定するまで変更と別の書込みを止める。受付の再確認は後続編集済みの現在正本を返す場合があり、要求した旧本文で置き換えない。

削除は対象を再取得して確認し、派生記憶を消去するか記録を残すかを選ぶ。どちらも削除ノートを出典とした回答への再利用を停止する。202とdelete_resourceのOperationを検査した後で通常一覧から外す。対象IDと返却operation IDを現在のcontextに結び付け、進捗GETも同じIDとkindを検査する。Operation DTOに存在しないtarget情報を推定しない。pending、running、failed、outcome_unknownを完了へ置き換えず、succeededだけを消去完了と表示する。消去失敗でも受付済みの対象を一覧へ戻さない。

## 認証境界と検証

controllerはUI-02のSessionContextに所有される。tenant/UID切替、signout、pagehide、画面disposeで本文・入力・receiptを同期消去する。各要求はcontextと画面自身のAbortSignalに束縛し、15秒の期限を持つ。旧要求の遅い403やJSON本文を新しいcontextへ反映しない。要求を直列に扱い、古い一覧取得が保存や削除の結果を復活させない。401の1回更新では同じ本文・キー・期待版を再送する。

ノート本文、入力、receiptはブラウザの永続領域へ保存しない。画面を再読込すると進捗の一時表示は消え、一覧はサーバーから再取得する。contextを閉じても送信済み要求を取り消せたとはみなさない。

通常検証は `mise run check` と `mise run test`。[状態テスト](../tests/test_ui_notes.mjs)は競合・再送・認可・不正応答・遅延・期限・scopeと消去境界を検査する。[実Chromium検証](../tests/ui/notes_browser.mjs)は `UI_ARTIFACTS` と `UI_CHROMIUM` を指定し、スマホ幅と共有表示で一覧、空、入力拒否、応答喪失、競合、削除確認・失敗・結果不明・完了、context消去を確認する。専用localhostポート5187/5188を使い、所有するbrowser/serverを終了する。

[HTTP/Firestore結合](../tests/emulator/test_ui_notes_integration.py)は架空の本人identityをAPI dependencyへ注入し、生成SDK/controllerから実HTTP、公式Firestore、独立したローカル削除台帳へ接続する。保存、表示先変更、検索、競合後の入力保持、応答喪失、両include_derived_memoriesの受付と正本の削除抑止を検査する。本人認証自体は既存UI-02のAuth Emulator試験、purge本体はAPP-01試験を使う。実クラウド・provider・iOS/Android/Pi実機は実施範囲に含まない。依存・API・保存形式の変更はなく、画面だけを戻す場合も受理済み削除は既存workerが継続する。
