# 共有UIとモバイル・ディスプレイの起動

BA-UI-01 / FR-004 の画面基盤。Reactの状態・エラー・確認カードは `packages/ui`、独立したVite entryは `apps/mobile` と `apps/display` にある。認証・tenant選択は [BA-UI-02](ui-authentication.md) が提供する。会話、録音、個人／共有の表示、SSE再接続は後続チケットであり、この基盤はそれらの完了を表さない。AC-004全体の受入には後続機能の検証が必要。

## 起動とビルド

ルートで `mise run setup` を実行する。既存の固定依存だけを使う。追加した依存は共有UIから既存workspace内のAPI clientへの参照のみで、外部パッケージ・ライセンス・OS権限は増えない。

| コマンド | 内容 |
| --- | --- |
| `mise run dev:mobile` | localhost:5173、明示的なfakeモードのモバイル画面 |
| `mise run dev:display` | localhost:5174、明示的なfakeモードの大画面 |
| `mise exec -- pnpm --filter @brainoid-internal/mobile build` | モバイルの通常静的bundle、`apps/mobile/dist` |
| `mise exec -- pnpm --filter @brainoid-internal/display build` | kioskの通常静的bundle、`apps/display/dist` |
| `mise exec -- pnpm --filter @brainoid-internal/mobile build:fake` | モバイルの体験用bundle、`apps/mobile/dist-fake` |
| `mise exec -- pnpm --filter @brainoid-internal/display build:fake` | kioskの体験用bundle、`apps/display/dist-fake` |
| `mise run check` | 既存の検査に加え `check:ui` で両通常bundleをビルド |
| `mise run test` | 既存の回帰検査と、注入client・fake失敗／取消・未対応機能の検証 |

開発サーバーはCtrl-Cで終了する。buildは型検査を兼ねないため、変更後は `mise run check` も実行する。fake bundleをローカル確認する場合は、対象packageで `preview --outDir dist-fake --port 4173`（displayは4174）を実行する。fake成果物を配布用 `dist` へコピーしない。

BA-UI-01時点の通常buildは接続先未設定の画面で停止する。現在のmobile通常entryは [認証設定](ui-authentication.md#設定と実接続)を使用し、display通常entryは復旧画面を提供する。`--mode fake` の場合だけ体験画面とfake transportを取り込む。tokenやfake切替をquery stringから読まない。現在のfake entryは [認証と切替の検証画面](ui-authentication.md)であり、以下の旧状態プレビュー部品も `/preview` として保持する。画面の「表示の確認」から空・読み込み・エラー、長い確認内容を操作できる。読み込みは10秒でエラーに移り、再試行できる。体験画面の確認ボタンはローカル表示だけを変え、業務データの保存や送信はしない。

## 接続と端末の境界

`apps/mobile/src/index.tsx` の `mountMobile(element, dependencies)`、display側の `mountDisplay` は `{ api, platform }` を受け取り、unmount関数を返す。mobile実接続のcompositionは BA-UI-02 が `src/main.tsx` に接続する。APIは `createBrainoidClient({ baseUrl, auth, fetch })` の既存clientを注入する。共通UIは生成済み `getReadiness` を使い、別のHTTP契約を定義しない。health成功は認証・権限・会話可能状態を意味しない。

`packages/ui/platform/index.ts` の `ClientPlatformAdapter` は表示面と音声の未対応理由を持つ。現段階では音声は必ず未対応で、録音APIを提供しない。今後の資格情報保管・通知・録音の実装はこの境界を拡張し、共通React部品からネイティブAPIを直接呼ばない。表示面の指定は認可境界ではない。Piの資格情報やHA tokenをブラウザーへ渡さず、device service経由の接続はBA-DEV-10等で扱う。

`@brainoid-internal/ui` はNodeでもimportできる型と接続関数、`/react` は共有React部品、`/platform` はadapter、`/styles.css` は共通CSS。`/fake` と `/preview` は明示的な検証用composition。`ConfirmationCard` は制御型のインライン確認で、呼出元が操作・pending・error・閉じた後のfocusを管理する。pending中は両ボタンを無効化する。通信はcomponent破棄・client変更・再試行時にabortし、古い応答を表示に反映しない。

## 静的配信とCapacitor

両buildの `base: "./"` により、ルートとサブディレクトリに置ける。history routerやSPA fallbackは不要。外部font、inline script/style、source mapを使わず、[device service](device-service.md)の拡張子制限と `script-src 'self'; style-src 'self'` に対応する。開発中のVite HMRは本番CSP検証の代用にならない。

mobileの `capacitor.config.json` は `webDir: "dist"` を指定する。`dev.brainoid.agent` は開発用識別子で、公開識別子は後続配布チケットで確定する。Capacitor CLI・Android/iOS platform・署名鍵は追加しない。web buildとブラウザー確認は、native buildや実Piの描画・音声検証を意味しない。

配布前には通常buildを選び、認証を含む実compositionと実機を検証する。切戻しは以前の静的bundleへ戻す。データ移行・サーバー契約の変更はない。Firestore・API・worker・device実装を変更しない本チケットでは、Emulator再実行をUI確認の代用にしない。

## ブラウザー確認

390×844のスマホ幅、1280×800のPi相当幅で両entryを開く。空・loading・error・再試行・確認／取消・長い説明を確認し、Tab/Enterとタップで操作する。320px幅と拡大相当幅も横overflowがないことを確認する。スクリーンショットを目視し、文字の読みやすさ、focus、console/network/CSPエラーを確認する。通常buildがfakeを含まず設定待ちになること、静的ファイルが相対パスから読み込めることも確認する。ブラウザーやOSの実機差は別途検証する。
