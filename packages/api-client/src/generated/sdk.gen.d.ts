import type { Client, ClientMeta, Options as Options2, RequestResult, ServerSentEventsResult, TDataShape } from './client/index.js';
import type { AcceptInvitationData, AcceptInvitationErrors, AcceptInvitationResponses, AckNotificationData, AckNotificationErrors, AckNotificationResponses, CancelDeviceCommandData, CancelDeviceCommandErrors, CancelDeviceCommandResponses, CancelRunData, CancelRunErrors, CancelRunResponses, CompleteGoogleCalendarAuthorizationData, CompleteGoogleCalendarAuthorizationErrors, CompleteGoogleCalendarAuthorizationResponses, ConfirmCalendarProposalData, ConfirmCalendarProposalErrors, ConfirmCalendarProposalResponses, ConfirmDevicePairingData, ConfirmDevicePairingErrors, ConfirmDevicePairingResponses, ConnectDeviceData, ConnectDeviceErrors, ConnectDeviceResponses, CreateAccountOperationData, CreateAccountOperationErrors, CreateAccountOperationResponses, CreateCalendarProposalData, CreateCalendarProposalErrors, CreateCalendarProposalResponses, CreateConversationData, CreateConversationErrors, CreateConversationResponses, CreateDeviceCommandData, CreateDeviceCommandErrors, CreateDeviceCommandResponses, CreateDevicePairingData, CreateDevicePairingErrors, CreateDevicePairingResponses, CreateEntityData, CreateEntityErrors, CreateEntityResponses, CreateHomeAssistantIntegrationData, CreateHomeAssistantIntegrationErrors, CreateHomeAssistantIntegrationResponses, CreateInvitationData, CreateInvitationErrors, CreateInvitationResponses, CreateMemoryData, CreateMemoryErrors, CreateMemoryResponses, CreateNoteData, CreateNoteErrors, CreateNoteResponses, CreatePrivateSessionData, CreatePrivateSessionErrors, CreatePrivateSessionResponses, CreateReminderData, CreateReminderErrors, CreateReminderResponses, CreateTaskData, CreateTaskErrors, CreateTaskResponses, CreateTenantData, CreateTenantErrors, CreateTenantOperationData, CreateTenantOperationErrors, CreateTenantOperationResponses, CreateTenantResponses, CreateTurnData, CreateTurnErrors, CreateTurnResponses, DownloadExportData, DownloadExportErrors, DownloadExportResponses, EndPrivateSessionData, EndPrivateSessionErrors, EndPrivateSessionResponses, ExchangeDevicePairingData, ExchangeDevicePairingErrors, ExchangeDevicePairingResponses, GetAccountOperationData, GetAccountOperationErrors, GetAccountOperationResponses, GetAgentData, GetAgentErrors, GetAgentResponses, GetCalendarProposalData, GetCalendarProposalErrors, GetCalendarProposalResponses, GetConversationData, GetConversationErrors, GetConversationResponses, GetCurrentUserData, GetCurrentUserErrors, GetCurrentUserResponses, GetDeviceCommandData, GetDeviceCommandErrors, GetDeviceCommandResponses, GetDeviceData, GetDeviceErrors, GetDeviceResponses, GetDeviceSnapshotData, GetDeviceSnapshotErrors, GetDeviceSnapshotResponses, GetEntityData, GetEntityErrors, GetEntityResponses, GetLivenessData, GetLivenessResponses, GetMemoryData, GetMemoryErrors, GetMemoryResponses, GetNoteData, GetNoteErrors, GetNoteResponses, GetReadinessData, GetReadinessErrors, GetReadinessResponses, GetReminderData, GetReminderErrors, GetReminderResponses, GetRunData, GetRunErrors, GetRunResponses, GetSpeechAudioData, GetSpeechAudioErrors, GetSpeechAudioResponses, GetTaskData, GetTaskErrors, GetTaskResponses, GetTenantData, GetTenantErrors, GetTenantOperationData, GetTenantOperationErrors, GetTenantOperationResponses, GetTenantResponses, GetUsageData, GetUsageErrors, GetUsageResponses, ListAgentsData, ListAgentsErrors, ListAgentsResponses, ListCalendarEventsData, ListCalendarEventsErrors, ListCalendarEventsResponses, ListConversationsData, ListConversationsErrors, ListConversationsResponses, ListDevicesData, ListDevicesErrors, ListDevicesResponses, ListEntitiesData, ListEntitiesErrors, ListEntitiesResponses, ListIntegrationsData, ListIntegrationsErrors, ListIntegrationsResponses, ListMembershipsData, ListMembershipsErrors, ListMembershipsResponses, ListMemoriesData, ListMemoriesErrors, ListMemoriesResponses, ListMessagesData, ListMessagesErrors, ListMessagesResponses, ListNotesData, ListNotesErrors, ListNotesResponses, ListNotificationsData, ListNotificationsErrors, ListNotificationsResponses, ListRemindersData, ListRemindersErrors, ListRemindersResponses, ListTasksData, ListTasksErrors, ListTasksResponses, ListTenantsData, ListTenantsErrors, ListTenantsResponses, RefreshDeviceTokenData, RefreshDeviceTokenErrors, RefreshDeviceTokenResponses, RefreshPushInstallationData, RefreshPushInstallationErrors, RefreshPushInstallationResponses, RegisterPushInstallationData, RegisterPushInstallationErrors, RegisterPushInstallationResponses, RejectCalendarProposalData, RejectCalendarProposalErrors, RejectCalendarProposalResponses, RemovePushInstallationData, RemovePushInstallationErrors, RemovePushInstallationResponses, ReviseMemoryData, ReviseMemoryErrors, ReviseMemoryResponses, RevokeDeviceData, RevokeDeviceErrors, RevokeDeviceResponses, RevokeIntegrationData, RevokeIntegrationErrors, RevokeIntegrationResponses, RevokeMembershipData, RevokeMembershipErrors, RevokeMembershipResponses, SearchMemoriesData, SearchMemoriesErrors, SearchMemoriesResponses, SnoozeReminderData, SnoozeReminderErrors, SnoozeReminderResponses, StartGoogleCalendarAuthorizationData, StartGoogleCalendarAuthorizationErrors, StartGoogleCalendarAuthorizationResponses, StreamRunEventsData, StreamRunEventsResponse, StreamRunEventsResponses, SyncHomeAssistantEntitiesData, SyncHomeAssistantEntitiesErrors, SyncHomeAssistantEntitiesResponses, TranscribeAudioData, TranscribeAudioErrors, TranscribeAudioResponses, UpdateAgentData, UpdateAgentErrors, UpdateAgentResponses, UpdateConversationData, UpdateConversationErrors, UpdateConversationResponses, UpdateDeviceData, UpdateDeviceErrors, UpdateDeviceResponses, UpdateEntityData, UpdateEntityErrors, UpdateEntityResponses, UpdateIntegrationData, UpdateIntegrationErrors, UpdateIntegrationResponses, UpdateMembershipData, UpdateMembershipErrors, UpdateMembershipResponses, UpdateNoteData, UpdateNoteErrors, UpdateNoteResponses, UpdateReminderData, UpdateReminderErrors, UpdateReminderResponses, UpdateTaskData, UpdateTaskErrors, UpdateTaskResponses, UpdateTenantData, UpdateTenantErrors, UpdateTenantResponses } from './types.gen.js';
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
 * 所属テナント一覧
 */
export declare const listTenants: <ThrowOnError extends boolean = false>(options?: Options<ListTenantsData, ThrowOnError>) => RequestResult<ListTenantsResponses, ListTenantsErrors, ThrowOnError>;
/**
 * テナント作成・本人をownerに登録
 *
 * テナント作成・本人をownerに登録
 */
export declare const createTenant: <ThrowOnError extends boolean = false>(options: Options<CreateTenantData, ThrowOnError>) => RequestResult<CreateTenantResponses, CreateTenantErrors, ThrowOnError>;
/**
 * テナント取得
 *
 * テナント取得
 */
export declare const getTenant: <ThrowOnError extends boolean = false>(options: Options<GetTenantData, ThrowOnError>) => RequestResult<GetTenantResponses, GetTenantErrors, ThrowOnError>;
/**
 * テナント設定更新
 *
 * テナント設定更新
 */
export declare const updateTenant: <ThrowOnError extends boolean = false>(options: Options<UpdateTenantData, ThrowOnError>) => RequestResult<UpdateTenantResponses, UpdateTenantErrors, ThrowOnError>;
/**
 * 会員一覧
 *
 * 会員一覧
 */
export declare const listMemberships: <ThrowOnError extends boolean = false>(options: Options<ListMembershipsData, ThrowOnError>) => RequestResult<ListMembershipsResponses, ListMembershipsErrors, ThrowOnError>;
/**
 * 会員失効
 *
 * owner/adminのみ。本人退出はoperationsを使う。最後のownerは失効不可。
 */
export declare const revokeMembership: <ThrowOnError extends boolean = false>(options: Options<RevokeMembershipData, ThrowOnError>) => RequestResult<RevokeMembershipResponses, RevokeMembershipErrors, ThrowOnError>;
/**
 * 会員role更新
 *
 * owner/adminのみ。owner付与/剥奪はownerのみ。最後のownerは削除不可。
 */
export declare const updateMembership: <ThrowOnError extends boolean = false>(options: Options<UpdateMembershipData, ThrowOnError>) => RequestResult<UpdateMembershipResponses, UpdateMembershipErrors, ThrowOnError>;
/**
 * 招待コード作成
 *
 * 招待コード作成
 */
export declare const createInvitation: <ThrowOnError extends boolean = false>(options: Options<CreateInvitationData, ThrowOnError>) => RequestResult<CreateInvitationResponses, CreateInvitationErrors, ThrowOnError>;
/**
 * 本人が招待を受理
 *
 * 本人が招待を受理
 */
export declare const acceptInvitation: <ThrowOnError extends boolean = false>(options: Options<AcceptInvitationData, ThrowOnError>) => RequestResult<AcceptInvitationResponses, AcceptInvitationErrors, ThrowOnError>;
/**
 * 利用可能な人格一覧
 *
 * 利用可能な人格一覧
 */
export declare const listAgents: <ThrowOnError extends boolean = false>(options: Options<ListAgentsData, ThrowOnError>) => RequestResult<ListAgentsResponses, ListAgentsErrors, ThrowOnError>;
/**
 * 人格取得
 *
 * 人格取得
 */
export declare const getAgent: <ThrowOnError extends boolean = false>(options: Options<GetAgentData, ThrowOnError>) => RequestResult<GetAgentResponses, GetAgentErrors, ThrowOnError>;
/**
 * 共有人格更新
 *
 * 共有人格更新
 */
export declare const updateAgent: <ThrowOnError extends boolean = false>(options: Options<UpdateAgentData, ThrowOnError>) => RequestResult<UpdateAgentResponses, UpdateAgentErrors, ThrowOnError>;
/**
 * 未登録Piが短命ペアリングを開始
 *
 * 未認証endpoint。IP/installation rate limit、5分失効。poll_secretをログへ出さない。
 */
export declare const createDevicePairing: <ThrowOnError extends boolean = false>(options: Options<CreateDevicePairingData, ThrowOnError>) => RequestResult<CreateDevicePairingResponses, CreateDevicePairingErrors, ThrowOnError>;
/**
 * スマホ管理者が物理端末を確認して登録
 *
 * スマホ管理者が物理端末を確認して登録
 */
export declare const confirmDevicePairing: <ThrowOnError extends boolean = false>(options: Options<ConfirmDevicePairingData, ThrowOnError>) => RequestResult<ConfirmDevicePairingResponses, ConfirmDevicePairingErrors, ThrowOnError>;
/**
 * Piがpoll_secretで一回だけ資格情報を受取る
 *
 * 未承認は409 pairing_pending。poll_secret不一致は404。期限切れ410。一回利用。
 */
export declare const exchangeDevicePairing: <ThrowOnError extends boolean = false>(options: Options<ExchangeDevicePairingData, ThrowOnError>) => RequestResult<ExchangeDevicePairingResponses, ExchangeDevicePairingErrors, ThrowOnError>;
/**
 * Pi専用credentialを更新
 *
 * refresh_token自体を検証。短い同一refresh_request_id再送猶予とreuse検知を実装。
 */
export declare const refreshDeviceToken: <ThrowOnError extends boolean = false>(options: Options<RefreshDeviceTokenData, ThrowOnError>) => RequestResult<RefreshDeviceTokenResponses, RefreshDeviceTokenErrors, ThrowOnError>;
/**
 * 登録端末一覧
 *
 * 登録端末一覧
 */
export declare const listDevices: <ThrowOnError extends boolean = false>(options: Options<ListDevicesData, ThrowOnError>) => RequestResult<ListDevicesResponses, ListDevicesErrors, ThrowOnError>;
/**
 * 端末失効
 *
 * 端末失効
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
 * 端末表示名・操作scope変更
 */
export declare const updateDevice: <ThrowOnError extends boolean = false>(options: Options<UpdateDeviceData, ThrowOnError>) => RequestResult<UpdateDeviceResponses, UpdateDeviceErrors, ThrowOnError>;
/**
 * 本人のPi個人セッション開始
 *
 * 本人のPi個人セッション開始
 */
export declare const createPrivateSession: <ThrowOnError extends boolean = false>(options: Options<CreatePrivateSessionData, ThrowOnError>) => RequestResult<CreatePrivateSessionResponses, CreatePrivateSessionErrors, ThrowOnError>;
/**
 * Pi個人セッション終了
 *
 * Pi個人セッション終了
 */
export declare const endPrivateSession: <ThrowOnError extends boolean = false>(options: Options<EndPrivateSessionData, ThrowOnError>) => RequestResult<EndPrivateSessionResponses, EndPrivateSessionErrors, ThrowOnError>;
/**
 * Piが現在のsession・出力run・未完了配送を同期
 *
 * Piが現在のsession・出力run・未完了配送を同期
 */
export declare const getDeviceSnapshot: <ThrowOnError extends boolean = false>(options: Options<GetDeviceSnapshotData, ThrowOnError>) => RequestResult<GetDeviceSnapshotResponses, GetDeviceSnapshotErrors, ThrowOnError>;
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
 * 会話名・表示方針更新
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
 * 会話実行を永続化して受け付ける
 */
export declare const createTurn: <ThrowOnError extends boolean = false>(options: Options<CreateTurnData, ThrowOnError>) => RequestResult<CreateTurnResponses, CreateTurnErrors, ThrowOnError>;
/**
 * 実行状態を再取得
 *
 * 実行状態を再取得
 */
export declare const getRun: <ThrowOnError extends boolean = false>(options: Options<GetRunData, ThrowOnError>) => RequestResult<GetRunResponses, GetRunErrors, ThrowOnError>;
/**
 * SSEで実行イベントを取得
 *
 * 認証header付きfetch。idはrun内のseq。24時間内は再送可能。イベント履歴消失は410。run-events.schema.json参照。
 */
export declare const streamRunEvents: <ThrowOnError extends boolean = false>(options: Options<StreamRunEventsData, ThrowOnError, StreamRunEventsResponse>) => Promise<ServerSentEventsResult<StreamRunEventsResponses>>;
/**
 * 生成と未実行ツールを停止
 *
 * 実行済み外部副作用は取消しを保証しない。別途状態確認・補償が必要。
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
 * 閲覧可能なentities一覧
 */
export declare const listEntities: <ThrowOnError extends boolean = false>(options: Options<ListEntitiesData, ThrowOnError>) => RequestResult<ListEntitiesResponses, ListEntitiesErrors, ThrowOnError>;
/**
 * Entity作成
 *
 * deviceは本人private_sessionの対応write scopeが必須。未認証話者の共有Piからの書込みは禁止。
 */
export declare const createEntity: <ThrowOnError extends boolean = false>(options: Options<CreateEntityData, ThrowOnError>) => RequestResult<CreateEntityResponses, CreateEntityErrors, ThrowOnError>;
/**
 * Entity取得
 *
 * Entity取得
 */
export declare const getEntity: <ThrowOnError extends boolean = false>(options: Options<GetEntityData, ThrowOnError>) => RequestResult<GetEntityResponses, GetEntityErrors, ThrowOnError>;
/**
 * Entity更新
 *
 * Entity更新
 */
export declare const updateEntity: <ThrowOnError extends boolean = false>(options: Options<UpdateEntityData, ThrowOnError>) => RequestResult<UpdateEntityResponses, UpdateEntityErrors, ThrowOnError>;
/**
 * 閲覧可能なmemories一覧
 *
 * 閲覧可能なmemories一覧
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
 * Memory取得
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
 * 本人スマホのpush送信先登録
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
 * 通知表示・本人確認を記録
 *
 * 通知表示・本人確認を記録
 */
export declare const ackNotification: <ThrowOnError extends boolean = false>(options: Options<AckNotificationData, ThrowOnError>) => RequestResult<AckNotificationResponses, AckNotificationErrors, ThrowOnError>;
/**
 * 連携一覧（秘密を返さない）
 *
 * 連携一覧（秘密を返さない）
 */
export declare const listIntegrations: <ThrowOnError extends boolean = false>(options: Options<ListIntegrationsData, ThrowOnError>) => RequestResult<ListIntegrationsResponses, ListIntegrationsErrors, ThrowOnError>;
/**
 * 本人がGoogle Calendar OAuthを開始
 *
 * 本人がGoogle Calendar OAuthを開始
 */
export declare const startGoogleCalendarAuthorization: <ThrowOnError extends boolean = false>(options: Options<StartGoogleCalendarAuthorizationData, ThrowOnError>) => RequestResult<StartGoogleCalendarAuthorizationResponses, StartGoogleCalendarAuthorizationErrors, ThrowOnError>;
/**
 * Google OAuth callback
 *
 * stateにuser/tenant/nonce/期限/return_targetを束縛。codeかerrorのどちらか。固定allowlistへ303。URLはaccess logでredact。
 */
export declare const completeGoogleCalendarAuthorization: <ThrowOnError extends boolean = false>(options: Options<CompleteGoogleCalendarAuthorizationData, ThrowOnError>) => RequestResult<CompleteGoogleCalendarAuthorizationResponses, CompleteGoogleCalendarAuthorizationErrors, ThrowOnError>;
/**
 * PiローカルHA連携の登録
 *
 * PiローカルHA連携の登録
 */
export declare const createHomeAssistantIntegration: <ThrowOnError extends boolean = false>(options: Options<CreateHomeAssistantIntegrationData, ThrowOnError>) => RequestResult<CreateHomeAssistantIntegrationResponses, CreateHomeAssistantIntegrationErrors, ThrowOnError>;
/**
 * 登録Piが許可対象候補の家電情報を同期
 *
 * 対象integrationとbearer deviceが一致することを検査。外部entity IDだけで別tenantへ結合しない。
 */
export declare const syncHomeAssistantEntities: <ThrowOnError extends boolean = false>(options: Options<SyncHomeAssistantEntitiesData, ThrowOnError>) => RequestResult<SyncHomeAssistantEntitiesResponses, SyncHomeAssistantEntitiesErrors, ThrowOnError>;
/**
 * 連携失効と資格情報削除
 *
 * 連携失効と資格情報削除
 */
export declare const revokeIntegration: <ThrowOnError extends boolean = false>(options: Options<RevokeIntegrationData, ThrowOnError>) => RequestResult<RevokeIntegrationResponses, RevokeIntegrationErrors, ThrowOnError>;
/**
 * Calendar allowlist更新
 *
 * Calendar allowlist更新
 */
export declare const updateIntegration: <ThrowOnError extends boolean = false>(options: Options<UpdateIntegrationData, ThrowOnError>) => RequestResult<UpdateIntegrationResponses, UpdateIntegrationErrors, ThrowOnError>;
/**
 * 本人calendarの予定一覧
 *
 * deviceはcalendar.readを許可した本人private_session必須。区間最大31日。
 */
export declare const listCalendarEvents: <ThrowOnError extends boolean = false>(options: Options<ListCalendarEventsData, ThrowOnError>) => RequestResult<ListCalendarEventsResponses, ListCalendarEventsErrors, ThrowOnError>;
/**
 * 予定書込みの確認内容を確定
 *
 * deviceは本人private_sessionとcalendar.propose scope必須。実行確認はスマホのみ。
 */
export declare const createCalendarProposal: <ThrowOnError extends boolean = false>(options: Options<CreateCalendarProposalData, ThrowOnError>) => RequestResult<CreateCalendarProposalResponses, CreateCalendarProposalErrors, ThrowOnError>;
/**
 * 予定書込み確認内容
 *
 * 予定書込み確認内容
 */
export declare const getCalendarProposal: <ThrowOnError extends boolean = false>(options: Options<GetCalendarProposalData, ThrowOnError>) => RequestResult<GetCalendarProposalResponses, GetCalendarProposalErrors, ThrowOnError>;
/**
 * 本人が正規化内容を確認して実行を依頼
 *
 * 同user、args_hash、5分期限、provider ETagを検査。proposalは一回使用。
 */
export declare const confirmCalendarProposal: <ThrowOnError extends boolean = false>(options: Options<ConfirmCalendarProposalData, ThrowOnError>) => RequestResult<ConfirmCalendarProposalResponses, ConfirmCalendarProposalErrors, ThrowOnError>;
/**
 * 本人が提案を拒否
 *
 * pendingのみdeclinedへ。対応runのcheckpointに否認結果を保存しresume jobを作成。既にexecutingなら409。
 */
export declare const rejectCalendarProposal: <ThrowOnError extends boolean = false>(options: Options<RejectCalendarProposalData, ThrowOnError>) => RequestResult<RejectCalendarProposalResponses, RejectCalendarProposalErrors, ThrowOnError>;
/**
 * 照明・空調コマンドを保存
 *
 * 照明・空調コマンドを保存
 */
export declare const createDeviceCommand: <ThrowOnError extends boolean = false>(options: Options<CreateDeviceCommandData, ThrowOnError>) => RequestResult<CreateDeviceCommandResponses, CreateDeviceCommandErrors, ThrowOnError>;
/**
 * 家電操作の結果取得
 *
 * 家電操作の結果取得
 */
export declare const getDeviceCommand: <ThrowOnError extends boolean = false>(options: Options<GetDeviceCommandData, ThrowOnError>) => RequestResult<GetDeviceCommandResponses, GetDeviceCommandErrors, ThrowOnError>;
/**
 * 未実行家電操作を取り消す
 *
 * queued/delivered/acceptedかつstart claim前のみ取消し可。running以降は409 already_executing。
 */
export declare const cancelDeviceCommand: <ThrowOnError extends boolean = false>(options: Options<CancelDeviceCommandData, ThrowOnError>) => RequestResult<CancelDeviceCommandResponses, CancelDeviceCommandErrors, ThrowOnError>;
/**
 * forget/delete/export/退出を受け付ける
 *
 * delete_tenantはownerの直近5分の再認証と名称一致。削除は独立台帳の先行記録、DB tombstone保存後に202。
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
 * export成功・24時間期限内のみ。再認可し、公開URLを返さずstreamする。
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
 * 削除中の既存認証は当該status参照だけに限定。削除確定でFirebase認証も失効し、以後は401/404になる。
 */
export declare const getAccountOperation: <ThrowOnError extends boolean = false>(options: Options<GetAccountOperationData, ThrowOnError>) => RequestResult<GetAccountOperationResponses, GetAccountOperationErrors, ThrowOnError>;
/**
 * 本人に許可された利用量・残枠
 *
 * owner/adminはtenant総量、memberは本人分。価格見積りを確定請求額と表示しない。
 */
export declare const getUsage: <ThrowOnError extends boolean = false>(options: Options<GetUsageData, ThrowOnError>) => RequestResult<GetUsageResponses, GetUsageErrors, ThrowOnError>;
