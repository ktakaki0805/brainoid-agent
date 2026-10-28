import type { Client, ClientMeta, Options as Options2, RequestResult, ServerSentEventsResult, TDataShape } from './client/index.js';
import type { AcceptInvitationData, AcceptInvitationErrors, AcceptInvitationResponses, AckNotificationData, AckNotificationErrors, AckNotificationResponses, CancelDeviceCommandData, CancelDeviceCommandErrors, CancelDeviceCommandResponses, CancelRunData, CancelRunErrors, CancelRunResponses, CompleteGoogleCalendarAuthorizationData, CompleteGoogleCalendarAuthorizationErrors, CompleteGoogleCalendarAuthorizationResponses, ConfirmCalendarProposalData, ConfirmCalendarProposalErrors, ConfirmCalendarProposalResponses, ConfirmDevicePairingData, ConfirmDevicePairingErrors, ConfirmDevicePairingResponses, ConnectDeviceData, ConnectDeviceErrors, ConnectDeviceResponses, CreateAccountOperationData, CreateAccountOperationErrors, CreateAccountOperationResponses, CreateCalendarProposalData, CreateCalendarProposalErrors, CreateCalendarProposalResponses, CreateConversationData, CreateConversationErrors, CreateConversationResponses, CreateDeviceCommandData, CreateDeviceCommandErrors, CreateDeviceCommandResponses, CreateDevicePairingData, CreateDevicePairingErrors, CreateDevicePairingResponses, CreateEntityData, CreateEntityErrors, CreateEntityResponses, CreateHomeAssistantIntegrationData, CreateHomeAssistantIntegrationErrors, CreateHomeAssistantIntegrationResponses, CreateInvitationData, CreateInvitationErrors, CreateInvitationResponses, CreateMemoryData, CreateMemoryErrors, CreateMemoryResponses, CreateNoteData, CreateNoteErrors, CreateNoteResponses, CreatePrivateSessionData, CreatePrivateSessionErrors, CreatePrivateSessionResponses, CreateReminderData, CreateReminderErrors, CreateReminderResponses, CreateTaskData, CreateTaskErrors, CreateTaskResponses, CreateTenantData, CreateTenantErrors, CreateTenantOperationData, CreateTenantOperationErrors, CreateTenantOperationResponses, CreateTenantResponses, CreateTurnData, CreateTurnErrors, CreateTurnResponses, DownloadExportData, DownloadExportErrors, DownloadExportResponses, EndPrivateSessionData, EndPrivateSessionErrors, EndPrivateSessionResponses, ExchangeDevicePairingData, ExchangeDevicePairingErrors, ExchangeDevicePairingResponses, GetAccountOperationData, GetAccountOperationErrors, GetAccountOperationResponses, GetAgentData, GetAgentErrors, GetAgentResponses, GetCalendarProposalData, GetCalendarProposalErrors, GetCalendarProposalResponses, GetConversationData, GetConversationErrors, GetConversationResponses, GetCurrentUserData, GetCurrentUserErrors, GetCurrentUserResponses, GetDeviceCommandData, GetDeviceCommandErrors, GetDeviceCommandResponses, GetDeviceCommandTargetData, GetDeviceCommandTargetErrors, GetDeviceCommandTargetResponses, GetDeviceData, GetDeviceErrors, GetDeviceResponses, GetDeviceSnapshotData, GetDeviceSnapshotErrors, GetDeviceSnapshotResponses, GetEntityData, GetEntityErrors, GetEntityResponses, GetHomeAssistantIntegrationData, GetHomeAssistantIntegrationErrors, GetHomeAssistantIntegrationResponses, GetIntegrationData, GetIntegrationErrors, GetIntegrationResponses, GetLivenessData, GetLivenessResponses, GetMemoryData, GetMemoryErrors, GetMemoryResponses, GetNoteData, GetNoteErrors, GetNoteResponses, GetNotificationData, GetNotificationErrors, GetNotificationResponses, GetPrivateSessionData, GetPrivateSessionErrors, GetPrivateSessionResponses, GetReadinessData, GetReadinessErrors, GetReadinessResponses, GetReminderData, GetReminderErrors, GetReminderResponses, GetRunData, GetRunErrors, GetRunResponses, GetSpeechAudioData, GetSpeechAudioErrors, GetSpeechAudioResponses, GetTaskData, GetTaskErrors, GetTaskResponses, GetTenantData, GetTenantErrors, GetTenantOperationData, GetTenantOperationErrors, GetTenantOperationResponses, GetTenantResponses, GetUsageData, GetUsageErrors, GetUsageResponses, ListAgentsData, ListAgentsErrors, ListAgentsResponses, ListCalendarEventsData, ListCalendarEventsErrors, ListCalendarEventsResponses, ListConversationsData, ListConversationsErrors, ListConversationsResponses, ListDevicesData, ListDevicesErrors, ListDevicesResponses, ListEntitiesData, ListEntitiesErrors, ListEntitiesResponses, ListIntegrationsData, ListIntegrationsErrors, ListIntegrationsResponses, ListMembershipsData, ListMembershipsErrors, ListMembershipsResponses, ListMemoriesData, ListMemoriesErrors, ListMemoriesResponses, ListMessagesData, ListMessagesErrors, ListMessagesResponses, ListNotesData, ListNotesErrors, ListNotesResponses, ListNotificationsData, ListNotificationsErrors, ListNotificationsResponses, ListRemindersData, ListRemindersErrors, ListRemindersResponses, ListTasksData, ListTasksErrors, ListTasksResponses, ListTenantsData, ListTenantsErrors, ListTenantsResponses, RefreshDeviceTokenData, RefreshDeviceTokenErrors, RefreshDeviceTokenResponses, RefreshPushInstallationData, RefreshPushInstallationErrors, RefreshPushInstallationResponses, RegisterPushInstallationData, RegisterPushInstallationErrors, RegisterPushInstallationResponses, RejectCalendarProposalData, RejectCalendarProposalErrors, RejectCalendarProposalResponses, RemovePushInstallationData, RemovePushInstallationErrors, RemovePushInstallationResponses, ReviseMemoryData, ReviseMemoryErrors, ReviseMemoryResponses, RevokeDeviceData, RevokeDeviceErrors, RevokeDeviceResponses, RevokeHomeAssistantIntegrationData, RevokeHomeAssistantIntegrationErrors, RevokeHomeAssistantIntegrationResponses, RevokeIntegrationData, RevokeIntegrationErrors, RevokeIntegrationResponses, RevokeMembershipByUidData, RevokeMembershipByUidErrors, RevokeMembershipByUidResponses, RevokeMembershipData, RevokeMembershipErrors, RevokeMembershipResponses, SearchMemoriesData, SearchMemoriesErrors, SearchMemoriesResponses, SnoozeReminderData, SnoozeReminderErrors, SnoozeReminderResponses, StartGoogleCalendarAuthorizationData, StartGoogleCalendarAuthorizationErrors, StartGoogleCalendarAuthorizationResponses, StreamRunEventsData, StreamRunEventsResponse, StreamRunEventsResponses, SyncHomeAssistantEntitiesData, SyncHomeAssistantEntitiesErrors, SyncHomeAssistantEntitiesResponses, TranscribeAudioData, TranscribeAudioErrors, TranscribeAudioResponses, TransferOwnerData, TransferOwnerErrors, TransferOwnerResponses, UpdateAgentData, UpdateAgentErrors, UpdateAgentResponses, UpdateConversationData, UpdateConversationErrors, UpdateConversationResponses, UpdateDeviceData, UpdateDeviceErrors, UpdateDeviceResponses, UpdateEntityData, UpdateEntityErrors, UpdateEntityResponses, UpdateIntegrationData, UpdateIntegrationErrors, UpdateIntegrationResponses, UpdateMembershipByUidData, UpdateMembershipByUidErrors, UpdateMembershipByUidResponses, UpdateMembershipData, UpdateMembershipErrors, UpdateMembershipResponses, UpdateNoteData, UpdateNoteErrors, UpdateNoteResponses, UpdateReminderData, UpdateReminderErrors, UpdateReminderResponses, UpdateTaskData, UpdateTaskErrors, UpdateTaskResponses, UpdateTenantData, UpdateTenantErrors, UpdateTenantResponses, ValidateDeviceSnapshotData, ValidateDeviceSnapshotErrors, ValidateDeviceSnapshotResponses } from './types.gen.js';
export type Options<TData extends TDataShape = TDataShape, ThrowOnError extends boolean = boolean, TResponse = unknown> = Options2<TData, ThrowOnError, TResponse> & {
    /**
     * You can provide a client instance returned by `createClient()` instead of
     * individual options. This might be also useful if you want to implement a
     * custom client.
     */
    client?: Client;
    /**
     * You can pass arbitrary values through the `meta` object. This can be
     * used to access values that aren't defined as part of the SDK function.
     */
    meta?: keyof ClientMeta extends never ? Record<string, unknown> : ClientMeta;
};
/**
 * APIプロセスの生存確認
 *
 * 認証・依存先への接続を行わず応答する。依存先の確認はreadinessを使う。
 */
export declare const getLiveness: <ThrowOnError extends boolean = false>(options?: Options<GetLivenessData, ThrowOnError>) => RequestResult<GetLivenessResponses, unknown, ThrowOnError>;
/**
 * 起動完了と注入された依存先の可用性確認
 *
 * 現時点の標準構成には外部adapterがない。組込時にprobeを登録し、false・例外・設定時間内の応答なしを503にする。依存先の詳細は公開しない。
 */
export declare const getReadiness: <ThrowOnError extends boolean = false>(options?: Options<GetReadinessData, ThrowOnError>) => RequestResult<GetReadinessResponses, GetReadinessErrors, ThrowOnError>;
/**
 * 本人プロフィール
 *
 * 検証済みFirebase UIDに一致する保存済みの本人プロフィール。未作成は404、削除中は403。
 */
export declare const getCurrentUser: <ThrowOnError extends boolean = false>(options?: Options<GetCurrentUserData, ThrowOnError>) => RequestResult<GetCurrentUserResponses, GetCurrentUserErrors, ThrowOnError>;
/**
 * 所属テナント一覧
 *
 * 本人の現在有効な所属のみ。tenant ID昇順。cursorは現在の所属集合・limitに束縛し、権限変更後は400。
 */
export declare const listTenants: <ThrowOnError extends boolean = false>(options?: Options<ListTenantsData, ThrowOnError>) => RequestResult<ListTenantsResponses, ListTenantsErrors, ThrowOnError>;
/**
 * テナント作成・本人をownerに登録
 *
 * tenant・本人owner・初期agentを原子的に作成。profile未作成も利用可能。user単位の冪等キーは24時間有効。再送は現在ownerを再確認し、現在のTenantを201で返す。
 */
export declare const createTenant: <ThrowOnError extends boolean = false>(options: Options<CreateTenantData, ThrowOnError>) => RequestResult<CreateTenantResponses, CreateTenantErrors, ThrowOnError>;
/**
 * テナント取得
 *
 * 選択したtenantの現在membershipを照合して取得。未所属・失効・削除中は404。
 */
export declare const getTenant: <ThrowOnError extends boolean = false>(options: Options<GetTenantData, ThrowOnError>) => RequestResult<GetTenantResponses, GetTenantErrors, ThrowOnError>;
/**
 * テナント設定更新
 *
 * 現在のowner/adminのみ。正規化patchとIf-Match版に冪等キーを束縛。再送は二重更新せず現在のTenantを返す。
 */
export declare const updateTenant: <ThrowOnError extends boolean = false>(options: Options<UpdateTenantData, ThrowOnError>) => RequestResult<UpdateTenantResponses, UpdateTenantErrors, ThrowOnError>;
/**
 * opaque UIDをqueryで指定する会員失効
 *
 * owner/adminと直近5分の再認証が必要。owner失効はownerのみ。本人退出はoperationsを使う。最後の有効ownerは失効不可。Idempotency-Keyと対象のIf-Matchを固定して再送する。本文やFirebase accountを削除する権限は与えず、このAPIは当該所属の失効だけを行う。user_idは必須queryで、`.`・`..`・slashを含むUIDをそのまま指定できる。
 */
export declare const revokeMembershipByUid: <ThrowOnError extends boolean = false>(options: Options<RevokeMembershipByUidData, ThrowOnError>) => RequestResult<RevokeMembershipByUidResponses, RevokeMembershipByUidErrors, ThrowOnError>;
/**
 * 会員一覧
 *
 * 現在のowner/adminのみ。会員ID・role・status・整数認可版だけを返す。cursorは現在の認可と一覧状態へ束縛する。
 */
export declare const listMemberships: <ThrowOnError extends boolean = false>(options: Options<ListMembershipsData, ThrowOnError>) => RequestResult<ListMembershipsResponses, ListMembershipsErrors, ThrowOnError>;
/**
 * opaque UIDをqueryで指定する会員role更新
 *
 * owner/adminのみ。owner付与/剥奪はownerのみ。本人ownerの降格は別の有効ownerが必要。Idempotency-Keyと対象のIf-Matchを固定して再送する。tenant予約から台帳確定まで直列化し、未確定変更を同じ要求で再開する。user_idを必須queryとして渡し、`.`・`..`・slashを含むFirebase opaque UIDをURL pathの正規化から保護する。
 */
export declare const updateMembershipByUid: <ThrowOnError extends boolean = false>(options: Options<UpdateMembershipByUidData, ThrowOnError>) => RequestResult<UpdateMembershipByUidResponses, UpdateMembershipByUidErrors, ThrowOnError>;
/**
 * 会員失効
 *
 * owner/adminと直近5分の再認証が必要。owner失効はownerのみ。本人退出はoperationsを使う。最後の有効ownerは失効不可。Idempotency-Keyと対象のIf-Matchを固定して再送する。本文やFirebase accountを削除する権限は与えず、このAPIは当該所属の失効だけを行う。既存path互換API。任意opaque UIDにはrevokeMembershipByUidのquery経路を使う。
 */
export declare const revokeMembership: <ThrowOnError extends boolean = false>(options: Options<RevokeMembershipData, ThrowOnError>) => RequestResult<RevokeMembershipResponses, RevokeMembershipErrors, ThrowOnError>;
/**
 * 会員role更新
 *
 * owner/adminのみ。owner付与/剥奪はownerのみ。本人ownerの降格は別の有効ownerが必要。Idempotency-Keyと対象のIf-Matchを固定して再送する。tenant予約から台帳確定まで直列化し、未確定変更を同じ要求で再開する。既存path互換API。`.`・`..`などURLで正規化されるUIDにはupdateMembershipByUidのquery経路を使う。
 */
export declare const updateMembership: <ThrowOnError extends boolean = false>(options: Options<UpdateMembershipData, ThrowOnError>) => RequestResult<UpdateMembershipResponses, UpdateMembershipErrors, ThrowOnError>;
/**
 * 本人から別の会員へownerを移譲
 *
 * 現在owner本人と直近5分の再認証が必要。If-Matchは本人の認可版、target_versionは対象の認可版。対象のowner grantを確定してから本人をmemberへ変更し、途中失敗でも有効ownerを保持する。Idempotency-Keyと同じ本文・If-Matchで再送する。成功時は対象のMembershipとETagを返す。
 */
export declare const transferOwner: <ThrowOnError extends boolean = false>(options: Options<TransferOwnerData, ThrowOnError>) => RequestResult<TransferOwnerResponses, TransferOwnerErrors, ThrowOnError>;
/**
 * 招待コード作成
 *
 * owner/admin が24時間有効な一回利用コードを発行する。owner付与不可。Idempotency-Key再送は現在の管理権限・未使用・期限を再確認して同じコードを返す。membership減権限で同テナントの既存招待は無効になる。
 */
export declare const createInvitation: <ThrowOnError extends boolean = false>(options: Options<CreateInvitationData, ThrowOnError>) => RequestResult<CreateInvitationResponses, CreateInvitationErrors, ThrowOnError>;
/**
 * 本人が招待を受理
 *
 * 検証済み本人がコードを受諾する。UIDごと15分5回まで照合し、期限と一回性を原子的にclaimする。同じkey/codeは固定operationを再試行し、最新台帳のDB適用確認済みgrantのみで所属を有効にする。失効後は新しく発行された招待と別keyが必要。
 */
export declare const acceptInvitation: <ThrowOnError extends boolean = false>(options: Options<AcceptInvitationData, ThrowOnError>) => RequestResult<AcceptInvitationResponses, AcceptInvitationErrors, ThrowOnError>;
/**
 * 利用可能な人格一覧
 *
 * 現在の会員権限または端末credential・任意private sessionを再確認し、同じ共有agent設定を返す。cursorは現在認可と設定版に束縛する。
 */
export declare const listAgents: <ThrowOnError extends boolean = false>(options: Options<ListAgentsData, ThrowOnError>) => RequestResult<ListAgentsResponses, ListAgentsErrors, ThrowOnError>;
/**
 * 人格取得
 *
 * 現在の会員権限または端末credentialを再確認し、保存済みagentを返す。ETagは現在版。旧版の履歴取得は提供しない。
 */
export declare const getAgent: <ThrowOnError extends boolean = false>(options: Options<GetAgentData, ThrowOnError>) => RequestResult<GetAgentResponses, GetAgentErrors, ThrowOnError>;
/**
 * 共有人格更新
 *
 * owner/adminのuser bearer限定。If-Matchで現在版を比較し、name/personaのみ更新する。allowed_toolsやprompt文は実行権限を付与しない。冪等再送も現在管理権限を再確認し現在のAgentを返す。
 */
export declare const updateAgent: <ThrowOnError extends boolean = false>(options: Options<UpdateAgentData, ThrowOnError>) => RequestResult<UpdateAgentResponses, UpdateAgentErrors, ThrowOnError>;
/**
 * 未登録Piが短命ペアリングを開始
 *
 * Piサービス用の未認証endpoint。IP/installation rate limit、5分失効。同installation_nonceとIdempotency-Key・本文の再送は期限内に同じ秘密応答を返す。poll_secretはPiサービスだけで保持し、ブラウザ・ログへ出さない。
 */
export declare const createDevicePairing: <ThrowOnError extends boolean = false>(options: Options<CreateDevicePairingData, ThrowOnError>) => RequestResult<CreateDevicePairingResponses, CreateDevicePairingErrors, ThrowOnError>;
/**
 * スマホ管理者が物理端末を確認して登録
 *
 * owner/adminのみ。8文字codeの前半4文字で対象を検索し後半を照合する。対象ごとに5回の照合失敗で失効。未知selectorもIP/actor制限。同一確認再送のみ同deviceを返し、既登録installationの再登録・他tenant移管は409。応答に秘密は含めない。
 */
export declare const confirmDevicePairing: <ThrowOnError extends boolean = false>(options: Options<ConfirmDevicePairingData, ThrowOnError>) => RequestResult<ConfirmDevicePairingResponses, ConfirmDevicePairingErrors, ThrowOnError>;
/**
 * Piがpoll_secretで一回だけ資格情報を受取る
 *
 * 未承認は409 pairing_pending。poll_secret不一致は404。期限切れ410。一度だけ発行し、同じIdempotency-Keyとpoll_secretの再送は発行後60秒かつpairing期限内だけ同じ資格情報を返す。refresh後の旧exchangeは410。新しいキーで再発行しない。
 */
export declare const exchangeDevicePairing: <ThrowOnError extends boolean = false>(options: Options<ExchangeDevicePairingData, ThrowOnError>) => RequestResult<ExchangeDevicePairingResponses, ExchangeDevicePairingErrors, ThrowOnError>;
/**
 * Pi専用credentialを更新
 *
 * refresh_token自体を検証しtenant/deviceへ束縛。accessは15分、refresh familyは初回発行から30日。各rotationは新しいrefresh_request_idとIdempotency-Keyを生成し、応答喪失時は同じ値・同じ本文を60秒未満だけ再送する。旧tokenの別要求または猶予後の再利用はfamily全体を停止する。同request-idの別本文は409。並行した別rotationは再利用として停止するためPiは直列更新する。
 */
export declare const refreshDeviceToken: <ThrowOnError extends boolean = false>(options: Options<RefreshDeviceTokenData, ThrowOnError>) => RequestResult<RefreshDeviceTokenResponses, RefreshDeviceTokenErrors, ThrowOnError>;
/**
 * 登録端末一覧
 *
 * owner/admin専用。現在の台帳状態で認可し、cursorを現在の認可と一覧内容へ束縛する。
 */
export declare const listDevices: <ThrowOnError extends boolean = false>(options: Options<ListDevicesData, ThrowOnError>) => RequestResult<ListDevicesResponses, ListDevicesErrors, ThrowOnError>;
/**
 * 端末失効
 *
 * owner/admin専用。同一Idempotency-KeyとIf-Matchで途中失敗を再送。台帳先行deny後は旧access/refresh/保存commandを拒否。
 */
export declare const revokeDevice: <ThrowOnError extends boolean = false>(options: Options<RevokeDeviceData, ThrowOnError>) => RequestResult<RevokeDeviceResponses, RevokeDeviceErrors, ThrowOnError>;
/**
 * 端末情報
 *
 * 端末情報
 */
export declare const getDevice: <ThrowOnError extends boolean = false>(options: Options<GetDeviceData, ThrowOnError>) => RequestResult<GetDeviceResponses, GetDeviceErrors, ThrowOnError>;
/**
 * 端末表示名・操作scope変更
 *
 * owner/admin専用。名前だけの変更は通常versionのみ更新。scope/entity/room変更は台帳と認可版・generationを更新し、旧credentialを失効する。再接続は明示失効と再登録が必要。
 */
export declare const updateDevice: <ThrowOnError extends boolean = false>(options: Options<UpdateDeviceData, ThrowOnError>) => RequestResult<UpdateDeviceResponses, UpdateDeviceErrors, ThrowOnError>;
/**
 * 本人のPi個人セッション開始
 *
 * 本人の明示同意で開始。1台につきactiveは1件。同一キー再送は同じIDの現在状態を返し、ended/expiredから復活しない。最長5分・無操作60秒をサーバー時刻で検査。
 */
export declare const createPrivateSession: <ThrowOnError extends boolean = false>(options: Options<CreatePrivateSessionData, ThrowOnError>) => RequestResult<CreatePrivateSessionResponses, CreatePrivateSessionErrors, ThrowOnError>;
/**
 * Pi個人セッション終了
 *
 * 本人スマホのみ。If-Matchは同意・終了状態の版と照合。終了・失効後の再送でセッションを復活させない。
 */
export declare const endPrivateSession: <ThrowOnError extends boolean = false>(options: Options<EndPrivateSessionData, ThrowOnError>) => RequestResult<EndPrivateSessionResponses, EndPrivateSessionErrors, ThrowOnError>;
/**
 * 本人がPi個人セッションの現在状態を取得
 *
 * 開始した本人のみ。現在の権限とサーバー時刻でended/expiredを反映し、無操作期限は更新しない。
 */
export declare const getPrivateSession: <ThrowOnError extends boolean = false>(options: Options<GetPrivateSessionData, ThrowOnError>) => RequestResult<GetPrivateSessionResponses, GetPrivateSessionErrors, ThrowOnError>;
/**
 * Piが現在のsession・出力run・未完了配送を同期
 *
 * 現在のdevice credentialで認可し最大20候補ずつ走査するsnapshot。全頁取得後snapshot:validateで現在権限を再検証する。cursorは5分期限で同じdevice・認可・sessionだけに有効。
 */
export declare const getDeviceSnapshot: <ThrowOnError extends boolean = false>(options: Options<GetDeviceSnapshotData, ThrowOnError>) => RequestResult<GetDeviceSnapshotResponses, GetDeviceSnapshotErrors, ThrowOnError>;
/**
 * 全頁の候補IDを現在権限で再検証
 *
 * 合計128件以内の一意なIDのみを現在のdevice・family・sessionと各出典で再認可する読み取り。任意のID指定はアクセス権を与えない。scope変更時は400で全頁を再取得する。
 */
export declare const validateDeviceSnapshot: <ThrowOnError extends boolean = false>(options: Options<ValidateDeviceSnapshotData, ThrowOnError>) => RequestResult<ValidateDeviceSnapshotResponses, ValidateDeviceSnapshotErrors, ThrowOnError>;
/**
 * Pi WebSocket upgrade
 *
 * フレームはdevice-protocol.schema.json。Authorization header必須。tokenをqueryへ含めない。
 */
export declare const connectDevice: <ThrowOnError extends boolean = false>(options: Options<ConnectDeviceData, ThrowOnError>) => RequestResult<ConnectDeviceResponses, ConnectDeviceErrors, ThrowOnError>;
/**
 * 閲覧可能な会話一覧
 *
 * 閲覧可能な会話一覧
 */
export declare const listConversations: <ThrowOnError extends boolean = false>(options: Options<ListConversationsData, ThrowOnError>) => RequestResult<ListConversationsResponses, ListConversationsErrors, ThrowOnError>;
/**
 * 会話作成
 *
 * 会話作成
 */
export declare const createConversation: <ThrowOnError extends boolean = false>(options: Options<CreateConversationData, ThrowOnError>) => RequestResult<CreateConversationResponses, CreateConversationErrors, ThrowOnError>;
/**
 * 会話取得
 *
 * 会話取得
 */
export declare const getConversation: <ThrowOnError extends boolean = false>(options: Options<GetConversationData, ThrowOnError>) => RequestResult<GetConversationResponses, GetConversationErrors, ThrowOnError>;
/**
 * 会話名・表示方針更新
 *
 * 会話の編集権限とIf-Matchを確認して更新する。visibilityは変更不可。更新中のrunは旧source版のまま失効し、新しい設定で自動再生成しない。
 */
export declare const updateConversation: <ThrowOnError extends boolean = false>(options: Options<UpdateConversationData, ThrowOnError>) => RequestResult<UpdateConversationResponses, UpdateConversationErrors, ThrowOnError>;
/**
 * 会話メッセージ一覧
 *
 * 会話メッセージ一覧
 */
export declare const listMessages: <ThrowOnError extends boolean = false>(options: Options<ListMessagesData, ThrowOnError>) => RequestResult<ListMessagesResponses, ListMessagesErrors, ThrowOnError>;
/**
 * 会話実行を永続化して受け付ける
 *
 * 現在認可後、user message・queued run・pending jobを同一transactionで保存する。現在はtext入力とtext出力のみ受け付け、audio_input_idとspeech出力は422。同じ冪等キーは同じrunを返す。会話ごとの実行を直列化し、別のturnが進行中なら409。202後は独立workerが処理し、run GETで結果を取得する。events_pathのSSEは後続実装。
 */
export declare const createTurn: <ThrowOnError extends boolean = false>(options: Options<CreateTurnData, ThrowOnError>) => RequestResult<CreateTurnResponses, CreateTurnErrors, ThrowOnError>;
/**
 * 実行状態を再取得
 *
 * 現在の親会話権限で取得する。内部checkpoint・provider署名は返さない。終端jobの失敗/失効をfailedへ反映する。content_filter、空応答、未対応tool呼出しはfailedとなり成功messageを保存しない。
 */
export declare const getRun: <ThrowOnError extends boolean = false>(options: Options<GetRunData, ThrowOnError>) => RequestResult<GetRunResponses, GetRunErrors, ThrowOnError>;
/**
 * SSEで実行イベントを取得
 *
 * 認証header付きfetch。idはrun内seq、eventはtype、dataはrun-events.schema.json。Last-Event-IDの次から再開し既読seqを再適用しない。run作成後24時間の固定期限または履歴欠損は410、未来seqは409、不正cursorは400。410ではrunと最終messageを取得する。各batch直前に現在の会話・出力先・token期限を確認し、headers送信後の失効は接続終了とする。heartbeatはseqを消費しないコメント。
 */
export declare const streamRunEvents: <ThrowOnError extends boolean = false>(options: Options<StreamRunEventsData, ThrowOnError, StreamRunEventsResponse>) => Promise<ServerSentEventsResult<StreamRunEventsResponses>>;
/**
 * 生成と未実行ツールを停止
 *
 * 現在の会話POST権限と保存された開始者の一致を検査し、生成と未実行処理を停止する。本人ユーザーは本人端末から停止可能。Piは同一device/sessionを要求する。冪等再送にも現在認可を要求し、終端runは維持する。実行済み外部副作用の取消しは保証しない。
 */
export declare const cancelRun: <ThrowOnError extends boolean = false>(options: Options<CancelRunData, ThrowOnError>) => RequestResult<CancelRunResponses, CancelRunErrors, ThrowOnError>;
/**
 * タップ音声を文字起こし
 *
 * タップ音声を文字起こし
 */
export declare const transcribeAudio: <ThrowOnError extends boolean = false>(options: Options<TranscribeAudioData, ThrowOnError>) => RequestResult<TranscribeAudioResponses, TranscribeAudioErrors, ThrowOnError>;
/**
 * 確定文のTTSを認可付きで取得
 *
 * speech.readyのspeech_id。5分期限、run/current sessionを再検査。音声自体は保存しない。
 */
export declare const getSpeechAudio: <ThrowOnError extends boolean = false>(options: Options<GetSpeechAudioData, ThrowOnError>) => RequestResult<GetSpeechAudioResponses, GetSpeechAudioErrors, ThrowOnError>;
/**
 * 閲覧可能なentities一覧
 *
 * 現在閲覧可能な候補のみを返す。queryはlabel/aliasesのcasefold部分一致。同名でも統合・自動選択せず、不可視候補をcursorや件数に含めない。
 */
export declare const listEntities: <ThrowOnError extends boolean = false>(options: Options<ListEntitiesData, ThrowOnError>) => RequestResult<ListEntitiesResponses, ListEntitiesErrors, ThrowOnError>;
/**
 * Entity作成
 *
 * deviceは本人private_sessionとmemory.writeが必須。person/activity/topicはscope・表示方針内で作成可能。appliance/roomは現在のdevice allowlistも必要で、新規IDへ自動付与しない。external_refsの申告は受け付けない。
 */
export declare const createEntity: <ThrowOnError extends boolean = false>(options: Options<CreateEntityData, ThrowOnError>) => RequestResult<CreateEntityResponses, CreateEntityErrors, ThrowOnError>;
/**
 * Entity取得
 *
 * 現在のscope・所有者・表示方針で取得。deviceのappliance/roomは対応allowlistも検証する。
 */
export declare const getEntity: <ThrowOnError extends boolean = false>(options: Options<GetEntityData, ThrowOnError>) => RequestResult<GetEntityResponses, GetEntityErrors, ThrowOnError>;
/**
 * Entity更新
 *
 * label/aliasesのみ更新。kind・scope・外部参照は変更不可。deviceは本人sessionと現在のmemory.write・表示方針・対象allowlistが必要。
 */
export declare const updateEntity: <ThrowOnError extends boolean = false>(options: Options<UpdateEntityData, ThrowOnError>) => RequestResult<UpdateEntityResponses, UpdateEntityErrors, ThrowOnError>;
/**
 * 閲覧可能なmemories一覧
 *
 * 現在閲覧可能なconfirmed記憶の一覧。訂正済み履歴はID取得でstatusを確認できる。
 */
export declare const listMemories: <ThrowOnError extends boolean = false>(options: Options<ListMemoriesData, ThrowOnError>) => RequestResult<ListMemoriesResponses, ListMemoriesErrors, ThrowOnError>;
/**
 * Memory作成
 *
 * deviceは本人private_sessionの対応write scopeが必須。未認証話者の共有Piからの書込みは禁止。
 */
export declare const createMemory: <ThrowOnError extends boolean = false>(options: Options<CreateMemoryData, ThrowOnError>) => RequestResult<CreateMemoryResponses, CreateMemoryErrors, ThrowOnError>;
/**
 * Memory取得
 *
 * 現在の出典・参照先権限を再確認して取得。訂正済みの旧IDはsupersededとして返す。
 */
export declare const getMemory: <ThrowOnError extends boolean = false>(options: Options<GetMemoryData, ThrowOnError>) => RequestResult<GetMemoryResponses, GetMemoryErrors, ThrowOnError>;
/**
 * 旧記憶を置換する新記憶を作成
 *
 * 旧記憶を置換する新記憶を作成
 */
export declare const reviseMemory: <ThrowOnError extends boolean = false>(options: Options<ReviseMemoryData, ThrowOnError>) => RequestResult<ReviseMemoryResponses, ReviseMemoryErrors, ThrowOnError>;
/**
 * 閲覧可能なnotes一覧
 *
 * 閲覧可能なnotes一覧
 */
export declare const listNotes: <ThrowOnError extends boolean = false>(options: Options<ListNotesData, ThrowOnError>) => RequestResult<ListNotesResponses, ListNotesErrors, ThrowOnError>;
/**
 * Note作成
 *
 * deviceは本人private_sessionの対応write scopeが必須。未認証話者の共有Piからの書込みは禁止。
 */
export declare const createNote: <ThrowOnError extends boolean = false>(options: Options<CreateNoteData, ThrowOnError>) => RequestResult<CreateNoteResponses, CreateNoteErrors, ThrowOnError>;
/**
 * Note取得
 *
 * Note取得
 */
export declare const getNote: <ThrowOnError extends boolean = false>(options: Options<GetNoteData, ThrowOnError>) => RequestResult<GetNoteResponses, GetNoteErrors, ThrowOnError>;
/**
 * Note更新
 *
 * Note更新
 */
export declare const updateNote: <ThrowOnError extends boolean = false>(options: Options<UpdateNoteData, ThrowOnError>) => RequestResult<UpdateNoteResponses, UpdateNoteErrors, ThrowOnError>;
/**
 * 閲覧可能なtasks一覧
 *
 * 閲覧可能なtasks一覧
 */
export declare const listTasks: <ThrowOnError extends boolean = false>(options: Options<ListTasksData, ThrowOnError>) => RequestResult<ListTasksResponses, ListTasksErrors, ThrowOnError>;
/**
 * Task作成
 *
 * deviceは本人private_sessionの対応write scopeが必須。未認証話者の共有Piからの書込みは禁止。
 */
export declare const createTask: <ThrowOnError extends boolean = false>(options: Options<CreateTaskData, ThrowOnError>) => RequestResult<CreateTaskResponses, CreateTaskErrors, ThrowOnError>;
/**
 * Task取得
 *
 * Task取得
 */
export declare const getTask: <ThrowOnError extends boolean = false>(options: Options<GetTaskData, ThrowOnError>) => RequestResult<GetTaskResponses, GetTaskErrors, ThrowOnError>;
/**
 * Task更新
 *
 * Task更新
 */
export declare const updateTask: <ThrowOnError extends boolean = false>(options: Options<UpdateTaskData, ThrowOnError>) => RequestResult<UpdateTaskResponses, UpdateTaskErrors, ThrowOnError>;
/**
 * 閲覧可能なreminders一覧
 *
 * 閲覧可能なreminders一覧
 */
export declare const listReminders: <ThrowOnError extends boolean = false>(options: Options<ListRemindersData, ThrowOnError>) => RequestResult<ListRemindersResponses, ListRemindersErrors, ThrowOnError>;
/**
 * Reminder作成
 *
 * deviceは本人private_sessionの対応write scopeが必須。未認証話者の共有Piからの書込みは禁止。
 */
export declare const createReminder: <ThrowOnError extends boolean = false>(options: Options<CreateReminderData, ThrowOnError>) => RequestResult<CreateReminderResponses, CreateReminderErrors, ThrowOnError>;
/**
 * Reminder取得
 *
 * Reminder取得
 */
export declare const getReminder: <ThrowOnError extends boolean = false>(options: Options<GetReminderData, ThrowOnError>) => RequestResult<GetReminderResponses, GetReminderErrors, ThrowOnError>;
/**
 * Reminder更新
 *
 * Reminder更新
 */
export declare const updateReminder: <ThrowOnError extends boolean = false>(options: Options<UpdateReminderData, ThrowOnError>) => RequestResult<UpdateReminderResponses, UpdateReminderErrors, ThrowOnError>;
/**
 * scopeを絞ったグラフ・意味検索
 *
 * 検索のPOSTは副作用なし。共通Idempotency-Keyは要求するが副作用記録は作らない。
 */
export declare const searchMemories: <ThrowOnError extends boolean = false>(options: Options<SearchMemoriesData, ThrowOnError>) => RequestResult<SearchMemoriesResponses, SearchMemoriesErrors, ThrowOnError>;
/**
 * リマインダーを新しい予定版で再設定
 *
 * expected_versionを検査しschedule_versionを増加、status=scheduled、旧通知/旧jobを無効化。本人sessionまたは本人ユーザーが必要。
 */
export declare const snoozeReminder: <ThrowOnError extends boolean = false>(options: Options<SnoozeReminderData, ThrowOnError>) => RequestResult<SnoozeReminderResponses, SnoozeReminderErrors, ThrowOnError>;
/**
 * 本人スマホのpush送信先登録
 *
 * 本人専用。有効登録は最大20件。同tokenの再登録は旧登録を停止し、別本人へのtoken移動も旧経路を失効する。秘密tokenは応答しない。
 */
export declare const registerPushInstallation: <ThrowOnError extends boolean = false>(options: Options<RegisterPushInstallationData, ThrowOnError>) => RequestResult<RegisterPushInstallationResponses, RegisterPushInstallationErrors, ThrowOnError>;
/**
 * push登録解除
 *
 * push登録解除
 */
export declare const removePushInstallation: <ThrowOnError extends boolean = false>(options: Options<RemovePushInstallationData, ThrowOnError>) => RequestResult<RemovePushInstallationResponses, RemovePushInstallationErrors, ThrowOnError>;
/**
 * FCM token更新
 *
 * FCM token更新
 */
export declare const refreshPushInstallation: <ThrowOnError extends boolean = false>(options: Options<RefreshPushInstallationData, ThrowOnError>) => RequestResult<RefreshPushInstallationResponses, RefreshPushInstallationErrors, ThrowOnError>;
/**
 * 本人・端末に許可された通知一覧
 *
 * 本人・端末に許可された通知一覧
 */
export declare const listNotifications: <ThrowOnError extends boolean = false>(options: Options<ListNotificationsData, ThrowOnError>) => RequestResult<ListNotificationsResponses, ListNotificationsErrors, ThrowOnError>;
/**
 * 現在の本人・端末権限で通知本文を取得
 *
 * 現在の予定世代・受理主体・宛先所属・表示方針を検査する。旧通知は本文を返さない。
 */
export declare const getNotification: <ThrowOnError extends boolean = false>(options: Options<GetNotificationData, ThrowOnError>) => RequestResult<GetNotificationResponses, GetNotificationErrors, ThrowOnError>;
/**
 * 通知表示・本人確認を記録
 *
 * 通知表示・本人確認を記録
 */
export declare const ackNotification: <ThrowOnError extends boolean = false>(options: Options<AckNotificationData, ThrowOnError>) => RequestResult<AckNotificationResponses, AckNotificationErrors, ThrowOnError>;
/**
 * 連携一覧（秘密を返さない）
 *
 * 本人のGoogle Calendar連携metadataを一覧する。Calendar service未構成時は503。秘密は返さない。
 */
export declare const listIntegrations: <ThrowOnError extends boolean = false>(options: Options<ListIntegrationsData, ThrowOnError>) => RequestResult<ListIntegrationsResponses, ListIntegrationsErrors, ThrowOnError>;
/**
 * 本人がGoogle Calendar OAuthを開始
 *
 * 本人がGoogle Calendar OAuthを開始する。Calendar service未構成時は503。
 */
export declare const startGoogleCalendarAuthorization: <ThrowOnError extends boolean = false>(options: Options<StartGoogleCalendarAuthorizationData, ThrowOnError>) => RequestResult<StartGoogleCalendarAuthorizationResponses, StartGoogleCalendarAuthorizationErrors, ThrowOnError>;
/**
 * Google OAuth callback
 *
 * stateにuser/tenant/nonce/期限/return_targetを束縛。codeかerrorのどちらか。固定allowlistへ303。Calendar service未構成時は503。URLはaccess logでredact。
 */
export declare const completeGoogleCalendarAuthorization: <ThrowOnError extends boolean = false>(options: Options<CompleteGoogleCalendarAuthorizationData, ThrowOnError>) => RequestResult<CompleteGoogleCalendarAuthorizationResponses, CompleteGoogleCalendarAuthorizationErrors, ThrowOnError>;
/**
 * PiローカルHA連携の登録
 *
 * owner/adminがactiveなPiへshared HA連携を登録。URLとtokenはPiのみで管理する。
 */
export declare const createHomeAssistantIntegration: <ThrowOnError extends boolean = false>(options: Options<CreateHomeAssistantIntegrationData, ThrowOnError>) => RequestResult<CreateHomeAssistantIntegrationResponses, CreateHomeAssistantIntegrationErrors, ThrowOnError>;
/**
 * 登録Piが許可対象候補の家電情報を同期
 *
 * 対象integrationとbearer deviceが一致することを検査。外部entity IDだけで別tenantへ結合しない。
 */
export declare const syncHomeAssistantEntities: <ThrowOnError extends boolean = false>(options: Options<SyncHomeAssistantEntitiesData, ThrowOnError>) => RequestResult<SyncHomeAssistantEntitiesResponses, SyncHomeAssistantEntitiesErrors, ThrowOnError>;
/**
 * owner/adminがHA連携を失効
 *
 * HA専用の管理経路。秘密はPiのみで保持し、失効後は同期とcommand受付を拒否する。
 */
export declare const revokeHomeAssistantIntegration: <ThrowOnError extends boolean = false>(options: Options<RevokeHomeAssistantIntegrationData, ThrowOnError>) => RequestResult<RevokeHomeAssistantIntegrationResponses, RevokeHomeAssistantIntegrationErrors, ThrowOnError>;
/**
 * owner/adminがHA連携metadataを取得
 *
 * HA専用の管理経路。秘密はPiのみで保持し、失効後は同期とcommand受付を拒否する。
 */
export declare const getHomeAssistantIntegration: <ThrowOnError extends boolean = false>(options: Options<GetHomeAssistantIntegrationData, ThrowOnError>) => RequestResult<GetHomeAssistantIntegrationResponses, GetHomeAssistantIntegrationErrors, ThrowOnError>;
/**
 * 連携失効と資格情報削除
 *
 * 本人のCalendarを直ちに停止する。202は資格情報・派生データの消去受付。GETのcleanup_statusがcompleteになるまで消去完了ではない。Googleのuser/project全体失効は別連携への影響を防ぐため実行せずscopeと理由を返す。
 */
export declare const revokeIntegration: <ThrowOnError extends boolean = false>(options: Options<RevokeIntegrationData, ThrowOnError>) => RequestResult<RevokeIntegrationResponses, RevokeIntegrationErrors, ThrowOnError>;
/**
 * 本人Calendarの停止理由と消去状態
 *
 * 秘密値を含まない現在状態。再接続は新しいOAuth開始から行い、新integrationを作る。owner/adminも他人の連携は取得できない。
 */
export declare const getIntegration: <ThrowOnError extends boolean = false>(options: Options<GetIntegrationData, ThrowOnError>) => RequestResult<GetIntegrationResponses, GetIntegrationErrors, ThrowOnError>;
/**
 * Calendar allowlist更新
 *
 * 本人のCalendarを明示選択。実付与scopeとprovider閲覧権、If-Matchを検査。空配列は全calendarを拒否。
 */
export declare const updateIntegration: <ThrowOnError extends boolean = false>(options: Options<UpdateIntegrationData, ThrowOnError>) => RequestResult<UpdateIntegrationResponses, UpdateIntegrationErrors, ThrowOnError>;
/**
 * 本人calendarの予定一覧
 *
 * deviceはcalendar.readを許可した本人private_session必須。区間最大31日、1頁100件以下、継続10頁以下。cursorは現在の認可と選択版・queryに束縛。未構成時503。
 */
export declare const listCalendarEvents: <ThrowOnError extends boolean = false>(options: Options<ListCalendarEventsData, ThrowOnError>) => RequestResult<ListCalendarEventsResponses, ListCalendarEventsErrors, ThrowOnError>;
/**
 * 予定書込みの確認内容を確定
 *
 * 本人privateの固定proposalを201で保存する。deviceは本人private_sessionとcalendar.propose scope必須。実付与WRITE scopeと現在calendar ACL、update/deleteのtarget GET・ETagを検査する。同key/bodyは同一proposal。本人confirm/provider書込は後続。
 */
export declare const createCalendarProposal: <ThrowOnError extends boolean = false>(options: Options<CreateCalendarProposalData, ThrowOnError>) => RequestResult<CreateCalendarProposalResponses, CreateCalendarProposalErrors, ThrowOnError>;
/**
 * 予定書込み確認内容
 *
 * 現在の本人・原producer/session/credential・出典を検査して固定内容と状態を取得する。期限到達pendingはexpiredへ収束する。
 */
export declare const getCalendarProposal: <ThrowOnError extends boolean = false>(options: Options<GetCalendarProposalData, ThrowOnError>) => RequestResult<GetCalendarProposalResponses, GetCalendarProposalErrors, ThrowOnError>;
/**
 * 本人が正規化内容を確認して実行を依頼
 *
 * 本人user、固定args_hash、初回5分期限、現在のgrant・原出典を検査し、canonical operationとjobを同一transactionで保存する。同一proposalの再送は同じoperationへ収束する。provider実行はworkerが現在権限を再確認し、安定event IDまたはIf-Matchで一回開始する。
 */
export declare const confirmCalendarProposal: <ThrowOnError extends boolean = false>(options: Options<ConfirmCalendarProposalData, ThrowOnError>) => RequestResult<ConfirmCalendarProposalResponses, ConfirmCalendarProposalErrors, ThrowOnError>;
/**
 * 本人が提案を拒否
 *
 * 本人がbodyなしで否認する。pendingはdeclined、期限到達ならexpired。同一決定の再送は同じ結果。対応runのcheckpointと否認結果・resume jobを同一transactionで保存する。取消済みやexecutingは409。
 */
export declare const rejectCalendarProposal: <ThrowOnError extends boolean = false>(options: Options<RejectCalendarProposalData, ThrowOnError>) => RequestResult<RejectCalendarProposalResponses, RejectCalendarProposalErrors, ThrowOnError>;
/**
 * 照明・空調コマンドを保存
 *
 * 現在のentity・端末allowlist・信頼済みhome_targetsを検査し、30秒期限のqueued commandを冪等保存する。202は受付のみで家電の実行成功ではない。snapshot未構成時は拒否する。
 */
export declare const createDeviceCommand: <ThrowOnError extends boolean = false>(options: Options<CreateDeviceCommandData, ThrowOnError>) => RequestResult<CreateDeviceCommandResponses, CreateDeviceCommandErrors, ThrowOnError>;
/**
 * 家電操作の結果取得
 *
 * 現在認可と固定target/device bindingを再検査する。未claimの30秒期限到来はexpiredへ永続遷移しversionを進める。outcome_unknownを成功へ変更しない。
 */
export declare const getDeviceCommand: <ThrowOnError extends boolean = false>(options: Options<GetDeviceCommandData, ThrowOnError>) => RequestResult<GetDeviceCommandResponses, GetDeviceCommandErrors, ThrowOnError>;
/**
 * 家電commandの許可済み操作対象を解決
 *
 * home.controlで現在のcommand・元actor・target fingerprintを再検査し、exact HA external referenceだけを返す。一般のentity読取権限や実行grantは付与しない。
 */
export declare const getDeviceCommandTarget: <ThrowOnError extends boolean = false>(options: Options<GetDeviceCommandTargetData, ThrowOnError>) => RequestResult<GetDeviceCommandTargetResponses, GetDeviceCommandTargetErrors, ThrowOnError>;
/**
 * 未実行家電操作を取り消す
 *
 * 元のactorだけがqueued/delivered/acceptedかつstart claim前に取消し可。claimと同じ文書をtransactionで検査する。claim済みは409 conflict、期限切れは410。任意If-Matchの古い版は412。同keyで取消しを再取得できる。
 */
export declare const cancelDeviceCommand: <ThrowOnError extends boolean = false>(options: Options<CancelDeviceCommandData, ThrowOnError>) => RequestResult<CancelDeviceCommandResponses, CancelDeviceCommandErrors, ThrowOnError>;
/**
 * forget/delete/export/退出を受け付ける
 *
 * forget_memoryを実装。認可・expected_version確認、独立削除台帳の先行記録、DB tombstoneとoperation保存後に202 pending。source_policy既定はsuppress_reuse。leave_tenantとdelete_tenantはlocal-emulatorと明示fake構成で提供し、本人の直近5分再認証を要求する。tenant削除は現在のownerとconfirmation_name一致が必要。exportは明示local構成で提供。現在読める会話・メッセージ・Entity・記憶・ノートをJSON入りZIPにまとめる。my_dataは本人privateと本人の共有寄稿（共有会話の親metadataを含む）、visible_tenant_dataは現在読める共有範囲も含む。delete_resourceはtarget_type=note/task/conversationを提供。現在の作成者/admin編集権と期待版を検査し、独立削除台帳・tombstone・purge job保存後に202を返す。include_derived_memories=falseでは派生Factの物理消去を省くが、削除sourceのモデル再利用は禁止する。conversationは選択した会話と全messages・run events・音声参照を削除し、共有会話では他の参加者の発言も対象となる。派生記憶の同時削除は既定true。reminder/entityは未提供。
 */
export declare const createTenantOperation: <ThrowOnError extends boolean = false>(options: Options<CreateTenantOperationData, ThrowOnError>) => RequestResult<CreateTenantOperationResponses, CreateTenantOperationErrors, ThrowOnError>;
/**
 * 非同期処理状態
 *
 * 非同期処理状態
 */
export declare const getTenantOperation: <ThrowOnError extends boolean = false>(options: Options<GetTenantOperationData, ThrowOnError>) => RequestResult<GetTenantOperationResponses, GetTenantOperationErrors, ThrowOnError>;
/**
 * 本人のexportを取得
 *
 * 明示local構成で提供。export成功・受付から24時間以内のみ。本人の現在membership・owner・全manifestの版/削除epoch/依存権限とobject期限を各取得およびstream送信境界で再検査する。公開URLを返さない。200はdata.jsonを含むZIP。上限200対象・非圧縮4MiB、走査5000対象を超える場合はoperation失敗とし部分出力しない。実非公開一時bucketはBA-OPS-02の外部前提。
 */
export declare const downloadExport: <ThrowOnError extends boolean = false>(options: Options<DownloadExportData, ThrowOnError>) => RequestResult<DownloadExportResponses, DownloadExportErrors, ThrowOnError>;
/**
 * 本人アカウント削除を受け付ける
 *
 * 直近5分の再認証が必要。最後のownerの場合409 ownership_transfer_required。
 */
export declare const createAccountOperation: <ThrowOnError extends boolean = false>(options: Options<CreateAccountOperationData, ThrowOnError>) => RequestResult<CreateAccountOperationResponses, CreateAccountOperationErrors, ThrowOnError>;
/**
 * 本人アカウント処理状態
 *
 * 削除中の既存認証は当該status参照だけに限定。最終処理後は404となる。現在のlocal-emulatorと明示fake構成は実Firebase認証を失効させない。実provider失効adapterは未提供。
 */
export declare const getAccountOperation: <ThrowOnError extends boolean = false>(options: Options<GetAccountOperationData, ThrowOnError>) => RequestResult<GetAccountOperationResponses, GetAccountOperationErrors, ThrowOnError>;
/**
 * 本人に許可された利用量・残枠
 *
 * owner/adminはtenant総量、memberは本人分。UTC当月集計。音声秒は未確定予約を含み、残要求数は本人とtenantの枠の小さい方。queryによる他人の明細選択は不可。価格見積りを確定請求額と表示しない。
 */
export declare const getUsage: <ThrowOnError extends boolean = false>(options: Options<GetUsageData, ThrowOnError>) => RequestResult<GetUsageResponses, GetUsageErrors, ThrowOnError>;
