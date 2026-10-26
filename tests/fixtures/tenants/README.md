# ローカル専用の2テナントfixture

[demo.json](demo.json) がID・本文・時刻の正本。[起動手順](../../../docs/05-development-and-testing.md#52-authfirestore-emulatorの起動と初期化)に従い、専用 `demo-brainoid` のAuth/Firestore emulatorへ投入する。実ユーザー、メール、password、token、外部credentialは含めない。本文は検索漏えい確認用の架空の `FIXTURE_A_PRIVATE` 等の識別語だけを使う。

| テナント | 固定tenant UUID | Auth UIDと会員role |
| --- | --- | --- |
| A | `51cea16f-85c3-5b21-a3bf-5fe62e091124` | `demo-a-owner` / `demo-a-admin` / `demo-a-member` |
| B | `257e4adc-6163-518b-b46d-d8500e7c569d` | `demo-b-owner` / `demo-b-admin` / `demo-b-member` |

Authユーザー6件、Firestore文書36件を投入する。各テナントはowner/admin/memberのactive membership、agent1件、active/revokedのPi各1件、private/sharedの会話・メッセージ・ノート各1件を持つ。`users/{uid}` はglobal profile、会員・端末・会話・ノートは `tenants/{tenant_id}` 配下。privateはmember所有で `authenticated_session`、sharedはowner作成で `shared_display` とし、adminによる他人privateの拒否ケースを後続で検証できる。

保存用fieldは[データ設計](../../../docs/03-data-and-authorization.md)に従い、APIのresponse DTOそのものではない。UIDは意図的にUUID形式にせず、tenant/resource IDは固定UUIDとする。Authの `createdAt` / `lastLoginAt` と全アプリ文書の `created_at` / `updated_at` は `2026-10-25T10:00:00Z` に固定。Firestoreのserver metadata `createTime` / `updateTime` と、Authが管理する内部 `validSince` は実行時刻になり得るため、アプリのfixture時刻として使用しない。

seedは同じIDのFirestore文書を置き換え、同じfieldのAuthユーザーは変更しない。fixture外の手動追加データは残る。完全な初期状態は `reset-demo` → `seed-demo` で再作成する。Authユーザーは識別・会員照合用で、ログイン用passwordやtokenは配らない。Firebaseの実認証・役割認可・private検索・端末失効の検証はBA-IAM-01/02以降で追加する。

通常テストはfixture構成・scope・時刻と設定拒否を、`test-emulator` は実REST経由の二重seed・件数/ID/field一致・nested reset・再seed・prod拒否・直接client読取拒否を確認する。Firestoreの複合索引、全transaction挙動・本番制限はstaging（BA-OPS-02等）で別途検証する。

BA-RUN-04の保存形に合わせ、会話のhistory revision/next sequence/active slotとmessageの親・sequence・run・source_refsを固定する。各元messageには `failed` / `fixture_history_only` のrunを対応させ、架空provider成功を作らない。reset/seed後はshared/private会話のmessage取得と新しいturnをそのまま実行できる。
