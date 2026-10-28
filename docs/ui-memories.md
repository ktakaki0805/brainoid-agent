# 記憶の管理画面

BA-UI-04 / FR-006。認証済みのスマホ画面で、記憶の一覧・検索・明示保存・訂正・忘却を提供する。Piの画面は `shared` かつ `shared_display` の許可済みカードを参照する。実Pi bridge、グラフ編集、推測候補の自動確定は含めない。

## 保存と表示

[明示記憶API](memories-api.md)の生成SDKを使い、一覧はcursorを最後まで読み、検索はAPIの上限40件を使う。カードには公開範囲、表示先、確定状態、有効期間、出典を表示する。有効開始前・終了後の記憶はその状態を表示し、検索はサーバーの有効期間判定に従う。候補や訂正済み・撤回済みの記憶を通常一覧の現行内容へ昇格しない。

既存の対象は権限内のEntity一覧から明示選択する。対象がないときは、種類と名前を入力して、記憶フォームで選んだ公開範囲・表示先の新しいEntityを登録できる。名前から本人や既存対象との同一性を推定しない。対象登録の成功は記憶保存の成功と別に通知する。

直接入力の出典は `source_refs=[]` とし、APIの記録者 `created_by` と `recorded_at` を表示する。既存出典には種別、resource ID、版を表示する。SourceRefに本文やmessageの親会話IDがないため、本文や親を推定して取得しない。訂正した内容も今回の明示入力として記録する。

個人と共有画面表示の組合せはフォームとcontrollerで拒否し、APIの入力拒否も通知する。対象や出典とのscope整合はサーバーが最終判断する。訂正時の公開範囲・表示先は変更せず、GETのETagと同じ旧IDをPATCHする。返された新IDを現行とし、旧IDは除外する。412では入力を保持して旧記憶の現在状態と一覧を再取得し、編集開始時との差分を表示する。再取得失敗を再取得済みと表示しない。自動上書きせず、現行の記憶を明示的に選び直す。

## 忘却と不確かな応答

対象を再取得してから、元出典を残して再利用を止める `suppress_reuse`、または権限のある出典も消す `delete_source` を選ぶ。サーバーが202を返した後に通常一覧から外す。進捗は受付時の対象IDと返却operation IDを現在のtenantに束縛し、同じoperationのGETで更新する。Operation DTOにないtarget情報を推定しない。受付時点の `pending`、実行中、失敗・結果不明、`succeeded` を区別する。失敗時も受付済みの再利用停止を取り消さない。

保存・対象登録・忘却の応答喪失や503では要求本文・冪等キー・期待版を保持し、同じ要求で再確認する。確認まで別の編集・保存・忘却を止める。受付未確認の段階で通常一覧から消したり、成功を通知したりしない。本文、入力、操作receiptはブラウザ永続領域に保存しないため、操作進捗の追跡は現在の画面context内で行う。再読み込み後の記憶一覧はサーバー正本から取得する。

## 認証境界と検証

UI-02のSessionContextにcontrollerを登録し、tenant/UID世代、signout、pagehide、画面disposeで入力・結果・操作receiptを同期消去する。各要求をcontextと画面自身のAbortSignalに束縛し、遅い応答や旧画面の403を新画面へ適用しない。401の1回更新と現在権限の判定は共通contextを使う。

通常検証は `mise run check` と `mise run test`。`tests/test_ui_memories.mjs` は保存・検索、候補/旧版の拒否、412、scope、応答喪失、対象登録、忘却受付とpurge状態、現在認可・遅延応答を確認する。`tests/ui/memories_browser.mjs` は `UI_ARTIFACTS` と `UI_CHROMIUM` を指定して実Chromiumのスマホ/共有画面を確認する。専用ポート5185/5186を使い、終了時に所有browser/serverを閉じる。

`tests/emulator/test_ui_memories_integration.py` は架空の本人identityをAPI dependencyへ注入し、実HTTP・生成SDK・controller・公式Firestore・独立ローカル削除台帳を接続する。保存/訂正/検索、API拒否、応答喪失、両source_policyの202と実出典状態を検査する。本人認証そのものの検証はUI-02のAuth Emulator検証、purge完了の実処理は既存のMEM-08検証を使う。実クラウド、実provider、iOS/Android/Pi実機はこの変更の検証範囲に含まない。
