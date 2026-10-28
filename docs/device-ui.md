# スマホからのPi登録・許可設定・失効

BA-UI-09、FR-003、T-003/004。[共有画面](../packages/ui/src/devices/view.tsx)と[controller](../packages/ui/src/devices/controller.ts)を認証後のスマホに接続する。displayにはスマホで承認する案内を表示する。既存の[ペアリング](device-pairing.md)・[端末管理](devices.md) APIを利用し、新しい依存、権限、スキーマ、保存領域は追加しない。

## 操作と現在の認可

Pi側の登録画面に表示された8文字の公開コードを手入力し、端末名と許可する対象のUUIDを指定する。QR読取の代替として手入力を採用し、カメラ権限は不要。確認画面に現在のテナント名/ID、コード、端末名、対象IDを固定表示し、登録先を確認したcheckboxを選ぶまで承認できない。コードの期限はPiに表示された発行から5分で、期限判定はAPIを正本とする。管理APIにはコードの有効性やPiの秘密を事前照会する機能はない。

現在の `getTenant` の `my_role` がowner/adminの場合だけ管理画面を表示する。一覧/詳細を含む管理APIを呼ぶ前と、登録・設定・失効・再送の直前に現在の所属を確認する。memberには管理操作を表示せず、controllerを直接呼んでも管理APIへ送らない。API自身の現在認可も毎回必要であり、ブラウザーのroleは許可の代替にならない。SessionContextのUID・tenant・世代、controller破棄を送受信で検査し、pagehideでは確認コード・下書き・metadataを消す。bfcache復帰は所属から確認し直す。

端末名、状態、申告された能力、許可する機能・対象ID・部屋IDを表示する。対象/部屋は同じテナントの有効なUUIDを入力し、部屋のkindを含めAPIが検査する。設定は機器の実在や接続性の証明ではない。許可範囲の変更は資格情報の認可版を進めるため、接続再開には明示失効後の再登録が必要になる。

## 応答不明・競合・失効

変更は一操作ずつ送信する。設定/失効は取得済みのversionを引用符付き `If-Match` として送り、新しい操作に新しい `Idempotency-Key` を付ける。応答喪失、5xx、成功応答の不正なDTOは成功表示せず、同一本文・対象・key・versionでのみ再確認する。未確定の間は新しい変更と下書き変更を禁止する。再確認が429で制限されても元のkey/bodyを維持し、時間をおいて同じ操作を照合する。初回の確定拒否とは区別する。同じ本人・テナントでowner/admin間の役割、名前、版が変わっても、送信済みの操作を新しいkeyに置き換えない。現在の管理権限を再確認し、同じ登録先IDの元の操作を照合する。期限切れ等で照合できない場合も未確認を維持し、所属を選び直して端末一覧とPiの状態を確認する。新しいコードでの再実行を復旧とみなさない。これらの情報はメモリだけに保持し、画面を閉じた場合は現在の一覧を再取得して管理者が結果を照合する。

412では変更前と再取得した現在の内容を表示する。現在の設定を確認してから新しい操作を選び直し、古い編集を自動適用しない。一覧と詳細は同じcontroller内で直列に更新し、古いversionや不一致のETag、別tenantの応答で上書きしない。台帳のintentやcommit障害による現在の停止状態は通常versionが進む前でも取り込む。認可版・接続generationの進行を通常versionから分離し、停止を観測した後に同じ版・認可版・generationの旧active応答で復活させない。複数pageの後続取得が失敗しても、先に確認した既存端末の停止は取り消さない。API拒否・使用済みコード・期限切れ・別tenantへの登録拒否を成功表示しない。

204で確認できた失効は「クラウドで失効」と表示する。Pi内の秘密の消去は常に未確認で、オフライン端末の物理消去を推定しない。同じPiの再登録は失効済み旧device IDを再取得して明示指定し、新しい公開コードで承認する。後継は別UUIDになり、旧資格情報や旧コマンドを引き継がない。Piのpoll secret、access/refresh credentialを取得するAPIをスマホから呼ばず、画面・storage・ログへ渡さない。

## 検証と限界

`mise run test` に[controller/DTO試験](../tests/test_ui_devices.mjs)を含む。このNode試験の一部は既存uv/Python環境の実repositoryと合成transaction/台帳object storeからintent/commit障害の公開DTOを取得し、同じ通常versionでの停止、旧activeの拒否、復旧を検査する。HTTP TestClientは起動せず、新しい依存は不要。 [Chromium試験](../tests/ui/devices_browser.mjs)は本番用コンポーネントをfake transportと組み合わせ、スマホ/ディスプレイ、明示確認、拒否、応答喪失、競合、失効・再登録、member、tenant切替、pagehide/bfcache消去を確認する。

[同等device service試験](../tests/emulator/test_ui_devices_integration.py)は既存の実DeviceApplication/Runtime/CloudClient/CredentialStore/SQLiteを隔離directoryで起動し、実loopback HTTPのsession/CSRF経由で公開コードと期限だけを取得する。実mobile emulator buildの新画面をChromiumで操作し、Firebase Auth emulatorのowner認証、API、公式Firestore emulatorを通して承認する。成功応答の喪失と再確認の429をbrowser transportで注入し、元のkey/bodyで実APIへ再確認して同じ登録結果を取得する。serviceの自動exchangeと保護保存を観察した後、新画面から失効し、旧資格情報の拒否を確認する。fixtureからの直接confirmやASGI内だけの通信をこの登録証拠に使わない。

~~~bash
# mise環境で、専用のローカルEmulator設定を明示loadする。
set -a
source .env.emulator.example
set +a
export UI_CHROMIUM=/absolute/path/to/chromium
export UI_ARTIFACTS=/tmp/brainoid-device-ui-evidence
pnpm exec firebase emulators:exec --only auth,firestore --project demo-brainoid --config firebase.json --non-interactive 'PYTHONPATH=tests:tests/emulator uv run --all-packages --locked --offline python -B -m unittest discover -s tests/emulator -p test_ui_devices_integration.py -v'
node tests/ui/devices_browser.mjs
~~~

ローカルAPIの8000、browser previewの5195/5196と既定Emulator portを専用使用する。各試験は所有するbrowser/serverと一時profileを終了・破棄する。実Raspberry Pi、物理スマホ、systemd/Cage、実電源断・媒体消去、staging/providerは未実施。UI09の同等service条件としての検証であり、[Pi導入の実機gate](device-setup.md#検証記録と実機gate)を完了扱いしない。切戻しはUIを以前の互換版へ戻すのみで、登録・失効済み台帳や端末資格情報を巻き戻さない。
