# 公式参考資料と仕様の確認先

文書版0.2.0、更新日2026-10-25。管理責任は技術責任者です。各設計書に記載した外部サービスの仕様と、実装開始・公開前に再確認する一次資料をまとめています。

このプロジェクトの機能範囲、数値目標、保持期間、探索予算、role設計はプロジェクト側の設計案です。外部サービスの既定値や保証値を表していません。採用時には、公式資料、実際の利用プラン・リージョン・SDK、stagingでの挙動を併せて確認します。

## 1. クライアントと端末

| 資料 | 設計に使う内容 |
| --- | --- |
| [Capacitor公式ドキュメント](https://capacitorjs.com/docs) | Web UIとAndroid・iOSの組合せ、ネイティブ機能との境界 |
| [Raspberry Pi公式キオスク構築](https://www.raspberrypi.com/tutorials/how-to-use-a-raspberry-pi-in-kiosk-mode/) | Chromiumによる専用表示端末の構築 |
| [FCMのAppleアプリ設定](https://firebase.google.com/docs/cloud-messaging/ios/get-started) | APNs認証キー、通知許可、iOS通知の接続 |
| [TUFの概要](https://theupdateframework.io/docs/overview/) | 更新メタデータ、署名、hash、version、有効期限の検証 |

対応OS、Capacitorと各pluginの組合せ、音響機器、更新方式の採用実装はOPEN-003で固定します。資料に掲載された機能が、今回の未実装アプリで既に動作することを意味しません。

## 2. GCPの実行と非同期処理

| 資料 | 設計に使う内容 |
| --- | --- |
| [Cloud RunのWebSocket](https://docs.cloud.google.com/run/docs/triggering/websockets) | 接続時間、再接続、インスタンス間の状態共有、接続中の課金 |
| [Cloud TasksからCloud Runを呼ぶ](https://docs.cloud.google.com/run/docs/triggering/using-tasks) | 非公開workerへのOIDC認証、呼出し用identity |
| [Cloud Tasksの制限・注意事項](https://docs.cloud.google.com/tasks/docs/common-pitfalls) | 重複実行や順序に依存しない処理の必要性 |
| [GitHub ActionsのGCP向けOIDC](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-google-cloud-platform) | CIから長期鍵を配らず認証する構成 |
| [GCPの予算と通知](https://docs.cloud.google.com/billing/docs/how-to/budgets) | alerts-only budgetの用途 |
| [GCPのspend cap budget](https://docs.cloud.google.com/billing/docs/how-to/budgets-spend-caps) | 対応サービス、制約、支出制御の適用範囲 |

クラウドの予算機能と、アプリ側のテナントごとの利用上限は別の制御として扱います。価格表、提供地域、割当量は実際の構成が決まった時点で確認し、測定前の一律月額はこの版に固定していません。

## 3. Firestoreと記憶

| 資料 | 設計に使う内容 |
| --- | --- |
| [Security Rulesの条件](https://firebase.google.com/docs/firestore/security/rules-conditions) | サーバーSDKのアクセスに対してアプリ側の認可が必要なこと |
| [トランザクション](https://firebase.google.com/docs/firestore/manage-data/transactions) | 競合時の再実行を踏まえた副作用の分離 |
| [ベクトル検索](https://firebase.google.com/docs/firestore/vector-search) | 埋め込み次元、絞込み、snapshot listenerとの違い |
| [データ削除](https://firebase.google.com/docs/firestore/manage-data/delete-data) | 親documentとsubcollectionの削除範囲 |
| [TTL](https://firebase.google.com/docs/firestore/ttl) | 遅延した掃除と、アプリ側の期限判定の区別 |
| [ロケーション](https://firebase.google.com/docs/firestore/locations) | DB作成前に配置を確定する判断 |
| [バックアップと復元](https://firebase.google.com/docs/firestore/backups) | 保存先、復元先、復元後に確認する設定 |
| [エミュレーターと本番の差異](https://firebase.google.com/docs/emulator-suite/connect_firestore#how_the_cloud_firestore_emulator_differs_from_production) | stagingでも確認すべき索引・制限・挙動 |

2hop・40fact・20entityという探索上限や、バックアップ7日・制御台帳30日という保持はこのプロジェクトの初期案です。Firestoreの一律のサービス上限・既定保持期間ではありません。

## 4. 生成AIと外部連携

| 資料 | 設計に使う内容 |
| --- | --- |
| [Google Gen AI SDK](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/sdks/overview) | Google Cloudのモデル接続adapter |
| [Google Calendarの認可スコープ](https://developers.google.com/workspace/calendar/api/auth) | 操作に必要な最小の委任範囲 |
| [Google OAuthのサーバー向けフロー](https://developers.google.com/identity/protocols/oauth2/web-server) | 認可開始・callback・token更新の境界 |
| [Calendarのリソース版管理](https://developers.google.com/workspace/calendar/api/guides/version-resources) | 変更・削除時のETagと競合の検出 |
| [Calendar events.insert](https://developers.google.com/workspace/calendar/api/v3/reference/events/insert) | 作成時のevent IDと結果照合 |

モデルID・embeddingモデルと出力次元・STT/TTS設定はOPEN-005で検証して固定します。利用者向けプライバシー説明には、モデルや音声providerの保持条件、処理地域、自システムの保持条件を区別して記載します。

Home Assistant側のentity・service・報告状態は実装対象のバージョンと機器で確認します。具体的な許可リストはデバイス登録時の設定とし、会話から任意のservice名・引数を実行する構成にはしません。

## 5. 契約とOSS公開

| 資料 | 設計に使う内容 |
| --- | --- |
| [OpenAPI 3.1.1 specification](https://spec.openapis.org/oas/v3.1.1.html) | HTTP APIのスキーマ記述 |
| [JSON Schema draft 2020-12](https://json-schema.org/draft/2020-12) | イベント・コマンド・ツールの構造記述 |
| [Apache License 2.0標準本文](https://www.apache.org/licenses/LICENSE-2.0.txt) | 同梱LICENSEの出典。最終採用はOPEN-004 |
| [GitHubの非公開脆弱性報告の設定](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository) | SECURITYに記載した公開前の報告窓口設定 |

## 6. 更新の手順

1. SDK・モデル・ストア要件・リージョンを決める際に、該当する公式資料を開いて対象版・適用条件を確認する。
2. 採用する版と設定はlockfile・環境定義・対応構成表へ記録する。
3. 仕様変更が要件・データ・権限・通信・運用のどこへ影響するかを調べる。
4. 関連する文書・スキーマ・検証ケースを同じPRで変更し、設計判断の記録へ理由を残す。

リンク先が移転した場合は公式の移転先へ更新します。ブログ記事や検索結果の断片を、サービスの提供条件・セキュリティ境界の唯一の根拠にはしません。
