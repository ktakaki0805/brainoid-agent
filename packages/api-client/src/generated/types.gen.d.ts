export type ClientOptions = {
    baseUrl: `${string}://${string}/v1` | `${string}://${string}` | `${string}://${string}` | (string & {});
};
/**
 * サーバー発行のUUID。ハイフン付き表記を受理し、小文字に正規化する。
 */
export type ResourceId = string;
/**
 * Firebase UID。UUID制約を課さず、大文字小文字や空白を変換しない。
 */
export type FirebaseUid = string;
/**
 * providerのopaque ID。Calendarのcalendar/event ID等。フィールド固有の上限がある場合はさらに制限する。
 */
export type ProviderId = string;
/**
 * offset必須のRFC 3339。保存・応答はUTCのZ表記。小数秒は最大6桁。未知offsetの-00:00、うるう秒、UTC変換後の年1〜9999範囲外を拒否する。
 */
export type UtcTimestamp = string;
/**
 * IANA名の存在をサーバーで検証する。Asia/Tokyo、UTC等。OS固有localtime、posix/、right/を許可しない。
 */
export type IanaTimezone = string;
/**
 * 1から始まる更新版。文字列、boolean、浮動小数点からの型変換を行わない。
 */
export type Version = number;
export type Visibility = 'private' | 'shared';
export type DisplayPolicy = 'never' | 'authenticated_session' | 'shared_display';
export type Error = {
    error: {
        code: string;
        message: string;
        request_id: ResourceId;
        retryable: boolean;
        fields?: Array<string>;
    };
};
export type Scope = unknown & {
    visibility: Visibility;
    display_policy: DisplayPolicy;
};
export type User = {
    user_id: FirebaseUid;
    display_name: string;
    timezone: IanaTimezone;
    status: 'active' | 'deleting';
    version: Version;
};
export type TenantCreate = {
    name: string;
    timezone?: IanaTimezone;
};
export type Tenant = {
    id: ResourceId;
    tenant_id: ResourceId;
    version: Version;
    created_at: UtcTimestamp;
    updated_at: UtcTimestamp;
    name: string;
    timezone: IanaTimezone;
    status: 'active' | 'deleting';
    my_role: 'owner' | 'admin' | 'member';
};
export type TenantPage = {
    items: Array<Tenant>;
    next_cursor: string | null;
};
export type TenantPatch = {
    name?: string;
    timezone?: IanaTimezone;
};
export type Membership = {
    user_id: FirebaseUid;
    role: 'owner' | 'admin' | 'member';
    status: 'active' | 'revoked' | 'suspended';
    version: Version;
};
export type MembershipPage = {
    items: Array<Membership>;
    next_cursor: string | null;
};
export type MembershipPatch = {
    role: 'owner' | 'admin' | 'member';
};
export type OwnerTransfer = {
    target_user_id: FirebaseUid;
    target_version: Version;
};
export type InvitationCreate = {
    role: 'admin' | 'member';
};
export type Invitation = {
    id: ResourceId;
    code: string;
    expires_at: UtcTimestamp;
    role: 'admin' | 'member';
};
export type InvitationAccept = {
    code: string;
};
export type Agent = {
    id: ResourceId;
    tenant_id: ResourceId;
    version: Version;
    created_at: UtcTimestamp;
    updated_at: UtcTimestamp;
    name: string;
    persona: string;
    allowed_tools: Array<string>;
};
export type AgentPage = {
    items: Array<Agent>;
    next_cursor: string | null;
};
export type AgentPatch = {
    name?: string;
    persona?: string;
};
export type Device = {
    id: ResourceId;
    tenant_id: ResourceId;
    version: Version;
    created_at: UtcTimestamp;
    updated_at: UtcTimestamp;
    name: string;
    status: 'active' | 'revoked' | 'offline' | 'suspended';
    capabilities: Array<string>;
    allowed_entity_ids: Array<ResourceId>;
    connection_generation: number;
    authorization_version?: Version;
    allowed_scopes?: Array<DeviceOperationScope>;
    allowed_room_ids?: Array<ResourceId>;
};
export type DevicePage = {
    items: Array<Device>;
    next_cursor: string | null;
};
export type DevicePatch = {
    name?: string;
    allowed_entity_ids?: Array<ResourceId>;
    allowed_room_ids?: Array<ResourceId>;
    allowed_scopes?: Array<DeviceOperationScope>;
};
export type DeviceOperationScope = 'shared' | 'conversation' | 'memory.read' | 'memory.write' | 'notes' | 'tasks' | 'reminders' | 'calendar.read' | 'calendar.propose' | 'home.control';
/**
 * Piサービス専用。installation_nonceは起動ごとに変更せず、初回deviceとtenantへ束縛される。
 */
export type PairingCreate = {
    device_name: string;
    installation_nonce: string;
};
/**
 * Piサービスだけが受け取る。画面へ渡すのはuser_codeとexpires_atのみ。poll_secretをChromiumやURLへ渡さない。
 */
export type Pairing = {
    pairing_id: ResourceId;
    user_code: string;
    poll_secret: string;
    expires_at: UtcTimestamp;
};
/**
 * user_codeは4文字selectorと4文字secret。selector既知時のsecretは36^4通りで5回まで照合できる。
 */
export type PairingConfirm = {
    user_code: string;
    device_name: string;
    allowed_entity_ids: Array<ResourceId>;
    /**
     * 明示再登録。最新台帳で失効済みの同installation・同tenantの旧device ID。旧credentialは使わず新device IDを発行する。
     */
    replaces_device_id?: ResourceId | null;
};
export type PairingExchange = {
    poll_secret: string;
};
export type DeviceCredentials = {
    device_id: ResourceId;
    tenant_id: ResourceId;
    access_token: string;
    refresh_token: string;
    expires_in: 900;
    token_type: 'Bearer';
};
export type DeviceRefresh = {
    device_id: ResourceId;
    refresh_token: string;
    refresh_request_id: ResourceId;
};
export type PrivateSessionCreate = {
    allowed_scopes: Array<'memory.read' | 'memory.write' | 'conversation' | 'notes' | 'tasks' | 'reminders' | 'calendar.read' | 'calendar.propose' | 'home.control'>;
    acknowledge_shared_space: true;
};
export type PrivateSession = {
    id: ResourceId;
    tenant_id: ResourceId;
    version: Version;
    created_at: UtcTimestamp;
    updated_at: UtcTimestamp;
    user_id: FirebaseUid;
    device_id: ResourceId;
    allowed_scopes: Array<string>;
    expires_at: UtcTimestamp;
    idle_expires_at: UtcTimestamp;
    status: 'active' | 'ended' | 'expired';
};
export type PrivateSessionPage = {
    items: Array<PrivateSession>;
    next_cursor: string | null;
};
export type RunAssignment = {
    run_id: ResourceId;
    conversation_id: ResourceId;
    output_mode: 'text' | 'speech';
    assignment_generation: number;
    last_event_seq: number;
    private_session_id?: ResourceId;
};
/**
 * 現在の権限で再計算。失効したprivate session/runは返さない。next_cursorがある間は続頁取得。
 */
export type DeviceSnapshot = {
    device_id: ResourceId;
    tenant_id: ResourceId;
    connection_generation: number;
    active_private_session: PrivateSession | null;
    assigned_runs: Array<RunAssignment>;
    pending_command_ids: Array<ResourceId>;
    pending_notification_ids: Array<ResourceId>;
    delivery_cursor: number;
    next_cursor: string | null;
};
export type ConversationCreate = unknown & {
    agent_id: ResourceId;
    title?: string;
    visibility: Visibility;
    display_policy: DisplayPolicy;
};
export type Conversation = unknown & {
    id: ResourceId;
    tenant_id: ResourceId;
    version: Version;
    created_at: UtcTimestamp;
    updated_at: UtcTimestamp;
    agent_id: ResourceId;
    title: string;
    visibility: Visibility;
    display_policy: DisplayPolicy;
    owner_user_id?: FirebaseUid;
};
export type ConversationPage = {
    items: Array<Conversation>;
    next_cursor: string | null;
};
/**
 * visibilityの変更は専用の明示共有フローを将来定義する。この版では変更不可。
 */
export type ConversationPatch = {
    title?: string;
    display_policy?: DisplayPolicy;
};
export type SourceRef = {
    kind: 'message' | 'note' | 'import';
    resource_id: ResourceId;
    version: Version;
};
export type Message = {
    id: ResourceId;
    role: 'user' | 'assistant' | 'tool';
    text: string;
    run_id: ResourceId;
    created_at: UtcTimestamp;
    source_refs: Array<SourceRef>;
};
export type MessagePage = {
    items: Array<Message>;
    next_cursor: string | null;
};
export type TurnCreate = {
    text: string;
    output_mode: 'text' | 'speech';
    output_device_id?: ResourceId;
} | {
    audio_input_id: ResourceId;
    output_mode: 'text' | 'speech';
    output_device_id?: ResourceId;
};
export type RunAccepted = {
    run_id: ResourceId;
    status: 'queued';
    events_path: string;
    status_path: string;
};
export type Run = {
    run_id: ResourceId;
    conversation_id: ResourceId;
    status: 'queued' | 'running' | 'waiting_confirmation' | 'completed' | 'failed' | 'cancelled';
    version: Version;
    last_event_seq: number;
    created_at: UtcTimestamp;
    final_message_id?: ResourceId;
    error_code?: string;
    waiting_proposal_id?: ResourceId;
    checkpoint_version?: number;
};
export type AudioInput = {
    audio_input_id: ResourceId;
    transcript: string;
    language: 'ja-JP';
    expires_at: UtcTimestamp;
};
/**
 * payloadのtype別定義はrun-events.schema.jsonを正本とする。
 */
export type RunEvent = {
    run_id: ResourceId;
    seq: number;
    type: 'run.started' | 'text.delta' | 'speech.ready' | 'tool.status' | 'confirmation.required' | 'run.completed' | 'run.failed' | 'run.cancelled';
    occurred_at: UtcTimestamp;
    payload: {
        [key: string]: unknown;
    };
};
export type EntityCreate = unknown & {
    kind: 'person' | 'room' | 'appliance' | 'activity' | 'topic';
    label: string;
    aliases?: Array<string>;
    visibility: Visibility;
    display_policy: DisplayPolicy;
};
export type Entity = unknown & {
    id: ResourceId;
    tenant_id: ResourceId;
    version: Version;
    created_at: UtcTimestamp;
    updated_at: UtcTimestamp;
    kind: 'person' | 'room' | 'appliance' | 'activity' | 'topic';
    label: string;
    aliases?: Array<string>;
    visibility: Visibility;
    display_policy: DisplayPolicy;
    owner_user_id?: FirebaseUid;
    external_refs?: Array<{
        provider: 'home_assistant';
        integration_id: ResourceId;
        external_id: string;
    }>;
};
export type EntityPage = {
    items: Array<Entity>;
    next_cursor: string | null;
};
export type EntityPatch = {
    label?: string;
    aliases?: Array<string>;
};
/**
 * source_refs省略時は空配列、valid_from省略時はサーバー記録時刻。既存出典のない直接入力はcreated_byとrecorded_atを出典として示す。利用者の明示保存はuser_explicit/confirmedをサーバーが設定。private＋shared_displayを拒否。qualifiersのbrightness_pct/temperature_cはprefers_settingのみ、contextは全predicateで使用可能。構造schemaの成功だけで保存可能とは判断せず、保存モデルでpredicate別の業務制約を検証する。
 */
export type MemoryCreate = unknown & {
    subject_entity_id: ResourceId;
    predicate: 'located_in' | 'prefers_setting' | 'prefers' | 'related_to' | 'has_attribute';
    object_entity_id?: ResourceId;
    value?: string | number | boolean;
    qualifiers?: {
        context?: string;
        brightness_pct?: number;
        temperature_c?: number;
    };
    visibility: Visibility;
    display_policy: DisplayPolicy;
    source_refs?: Array<SourceRef>;
    valid_from?: UtcTimestamp;
    valid_to?: string | null;
};
export type Memory = unknown & {
    id: ResourceId;
    tenant_id: ResourceId;
    version: Version;
    created_at: UtcTimestamp;
    updated_at: UtcTimestamp;
    subject_entity_id: ResourceId;
    predicate: 'located_in' | 'prefers_setting' | 'prefers' | 'related_to' | 'has_attribute';
    object_entity_id?: ResourceId;
    value?: string | number | boolean;
    qualifiers?: {
        context?: string;
        brightness_pct?: number;
        temperature_c?: number;
    };
    visibility: Visibility;
    display_policy: DisplayPolicy;
    source_refs: Array<SourceRef>;
    valid_from: UtcTimestamp;
    valid_to?: string | null;
    owner_user_id?: FirebaseUid;
    origin: 'user_explicit' | 'imported' | 'inferred';
    status: 'candidate' | 'confirmed' | 'superseded' | 'retracted';
    recorded_at: UtcTimestamp;
    /**
     * 記録した本人のUID。出典のない直接入力ではrecorded_atとともに出典として示す。
     */
    created_by?: string;
    deletion_epoch: number;
    supersedes_fact_id?: ResourceId;
};
export type MemoryPage = {
    items: Array<Memory>;
    next_cursor: string | null;
};
/**
 * 旧factをsupersededにして新factを作成。戻り値は新fact。旧factのvisibility・display_policy・ownerを継承し、訂正で公開範囲を変更しない。reasonは要求の冪等性照合に用い、本文やジョブへ保存しない。
 */
export type MemoryRevision = {
    replacement: MemoryCreate;
    reason?: string;
};
export type MemorySearch = {
    query: string;
    limit?: number;
};
export type NoteCreate = unknown & {
    title: string;
    body: string;
    visibility: Visibility;
    display_policy: DisplayPolicy;
};
export type Note = unknown & {
    id: ResourceId;
    tenant_id: ResourceId;
    version: Version;
    created_at: UtcTimestamp;
    updated_at: UtcTimestamp;
    title: string;
    body: string;
    visibility: Visibility;
    display_policy: DisplayPolicy;
    owner_user_id?: FirebaseUid;
};
export type NotePage = {
    items: Array<Note>;
    next_cursor: string | null;
};
export type NotePatch = {
    title?: string;
    body?: string;
    display_policy?: DisplayPolicy;
};
export type TaskCreate = unknown & {
    title: string;
    due_at?: UtcTimestamp;
    timezone?: IanaTimezone;
    assignee_user_id?: FirebaseUid;
    visibility: Visibility;
    display_policy: DisplayPolicy;
};
export type Task = unknown & {
    id: ResourceId;
    tenant_id: ResourceId;
    version: Version;
    created_at: UtcTimestamp;
    updated_at: UtcTimestamp;
    title: string;
    due_at?: UtcTimestamp;
    timezone?: IanaTimezone;
    assignee_user_id?: FirebaseUid;
    visibility: Visibility;
    display_policy: DisplayPolicy;
    status: 'open' | 'done' | 'cancelled';
    owner_user_id?: FirebaseUid;
};
export type TaskPage = {
    items: Array<Task>;
    next_cursor: string | null;
};
export type TaskPatch = {
    title?: string;
    status?: 'open' | 'done' | 'cancelled';
    due_at?: string | null;
};
export type ReminderCreate = unknown & {
    title: string;
    due_at: UtcTimestamp;
    timezone: IanaTimezone;
    delivery_target: {
        kind: 'user' | 'device';
        id: string;
    };
    task_id?: ResourceId;
    visibility: Visibility;
    display_policy: DisplayPolicy;
};
export type Reminder = unknown & {
    id: ResourceId;
    tenant_id: ResourceId;
    version: Version;
    created_at: UtcTimestamp;
    updated_at: UtcTimestamp;
    title: string;
    due_at: UtcTimestamp;
    timezone: IanaTimezone;
    delivery_target: {
        kind: 'user' | 'device';
        id: string;
    };
    task_id?: ResourceId;
    visibility: Visibility;
    display_policy: DisplayPolicy;
    status: 'scheduled' | 'dispatched' | 'acknowledged' | 'cancelled' | 'expired';
    owner_user_id?: FirebaseUid;
    schedule_version: number;
};
export type ReminderPage = {
    items: Array<Reminder>;
    next_cursor: string | null;
};
export type ReminderPatch = {
    title?: string;
    due_at?: UtcTimestamp;
    timezone?: IanaTimezone;
    status?: 'cancelled';
};
export type ReminderSnooze = {
    due_at: UtcTimestamp;
    timezone: IanaTimezone;
    expected_version: number;
};
export type PushInstallationCreate = {
    fcm_token: string;
    platform: 'ios' | 'android';
    app_version: string;
};
export type PushInstallation = {
    id: ResourceId;
    platform: 'ios' | 'android';
    version: Version;
    created_at: UtcTimestamp;
};
export type PushInstallationPatch = {
    fcm_token: string;
    app_version?: string;
};
export type Notification = unknown & {
    id: ResourceId;
    tenant_id: ResourceId;
    /**
     * 初回は1。現在Reminderの編集と配送receiptの更新で単調増加する公開版。固定source版とは別でGET・ackのETagに対応する。
     */
    version: Version;
    created_at: UtcTimestamp;
    /**
     * 現在Reminderと配送receiptの更新時刻の新しい方。同時刻の更新はversionで識別する。
     */
    updated_at: UtcTimestamp;
    reminder_id: ResourceId;
    title: string;
    status: 'pending' | 'sent' | 'displayed' | 'acknowledged' | 'expired';
    target_kind: 'user' | 'device';
    target_id: string;
    expires_at: UtcTimestamp;
    visibility: Visibility;
    display_policy: DisplayPolicy;
};
export type NotificationPage = {
    items: Array<Notification>;
    next_cursor: string | null;
};
export type NotificationAck = {
    action: 'displayed' | 'acknowledged';
};
export type OAuthStart = {
    scope_profile: 'read' | 'read_write';
    return_target: 'mobile' | 'web';
};
export type OAuthAuthorization = {
    authorization_url: string;
    expires_at: UtcTimestamp;
};
export type Integration = {
    id: ResourceId;
    tenant_id: ResourceId;
    version: Version;
    created_at: UtcTimestamp;
    updated_at: UtcTimestamp;
    provider: 'google_calendar' | 'home_assistant';
    status: 'active' | 'reauth_required' | 'revoked';
    owner_user_id?: FirebaseUid;
    device_id?: ResourceId;
    allowed_calendar_ids?: Array<ProviderId>;
};
export type IntegrationPage = {
    items: Array<Integration>;
    next_cursor: string | null;
};
/**
 * 本人のGoogle Calendar連携のみ。付与済みscopeとproviderの閲覧権を検査。
 */
export type IntegrationPatch = {
    allowed_calendar_ids: Array<ProviderId>;
};
/**
 * HA URL/秘密はPiのローカル設定で管理。クラウドに任意URLを渡さない。
 */
export type HomeIntegrationCreate = {
    device_id: ResourceId;
    name: string;
};
/**
 * そのintegrationに束縛されたPiだけが送信。新規家電はadminがallowlistへ追加するまで実行不可。
 */
export type HomeInventorySync = {
    entities: Array<{
        external_id: string;
        label: string;
        domain: 'light' | 'climate';
        supported_actions: Array<'light.turn_on' | 'light.turn_off' | 'climate.set_temperature' | 'climate.set_hvac_mode'>;
    }>;
};
export type CalendarTime = {
    date_time: UtcTimestamp;
    timezone: IanaTimezone;
} | {
    date: string;
};
export type CalendarEvent = {
    id: ProviderId;
    calendar_id: ProviderId;
    summary: string;
    start: CalendarTime;
    end: CalendarTime;
    etag: string;
    fetched_at?: UtcTimestamp;
    description?: string;
};
export type CalendarEventPage = {
    items: Array<CalendarEvent>;
    next_cursor: string | null;
};
export type CalendarProposalCreate = {
    action: 'create';
    calendar_id: ProviderId;
    summary: string;
    start: CalendarTime;
    end: CalendarTime;
    description?: string;
} | {
    action: 'update';
    event_id: ProviderId;
    provider_etag: string;
    calendar_id: ProviderId;
    summary: string;
    start: CalendarTime;
    end: CalendarTime;
    description?: string;
} | {
    action: 'delete';
    calendar_id: ProviderId;
    event_id: ProviderId;
    provider_etag: string;
};
export type CalendarProposal = {
    proposal_id: ResourceId;
    integration_id: ResourceId;
    action: 'create' | 'update' | 'delete';
    normalized_request: CalendarProposalCreate;
    args_hash: string;
    expires_at: UtcTimestamp;
    status: 'pending' | 'confirmed' | 'executing' | 'succeeded' | 'failed' | 'outcome_unknown' | 'expired' | 'declined' | 'cancelled';
    run_id?: ResourceId;
    tool_call_id?: ResourceId;
    checkpoint_id?: ResourceId;
    operation_id?: ResourceId;
    error_code?: string;
};
export type CalendarConfirm = {
    args_hash: string;
    confirmed: true;
};
export type HomeAction = {
    action: 'light.turn_on';
    parameters: {
        brightness_pct?: number;
    };
} | {
    action: 'light.turn_off';
    parameters: {
        [key: string]: never;
    };
} | {
    action: 'climate.set_temperature';
    parameters: {
        temperature_c: number;
    };
} | {
    action: 'climate.set_hvac_mode';
    parameters: {
        hvac_mode: 'off' | 'heat' | 'cool' | 'auto';
    };
};
export type CommandCreate = {
    device_id: ResourceId;
    target_entity_id: ResourceId;
    operation: HomeAction;
};
export type Command = unknown & {
    id: ResourceId;
    tenant_id: ResourceId;
    version: Version;
    created_at: UtcTimestamp;
    updated_at: UtcTimestamp;
    device_id: ResourceId;
    target_entity_id: ResourceId;
    operation: HomeAction;
    status: 'queued' | 'delivered' | 'accepted' | 'running' | 'succeeded' | 'failed' | 'expired' | 'cancelled' | 'outcome_unknown';
    expires_at: UtcTimestamp;
    execution_id?: ResourceId;
    observed_state?: {
        power?: 'on' | 'off';
        brightness_pct?: number;
        temperature_c?: number;
        hvac_mode?: 'off' | 'heat' | 'cool' | 'auto';
    };
    error_code?: string;
};
export type CommandPage = {
    items: Array<Command>;
    next_cursor: string | null;
};
export type OperationCreate = {
    kind: 'forget_memory';
    target_id: ResourceId;
    expected_version: number;
    source_policy?: 'suppress_reuse' | 'delete_source';
} | {
    kind: 'delete_resource';
    target_type: 'conversation' | 'note' | 'task' | 'reminder' | 'entity';
    target_id: ResourceId;
    expected_version: number;
    include_derived_memories?: boolean;
} | {
    kind: 'export';
    scope: 'my_data' | 'visible_tenant_data';
} | {
    kind: 'delete_tenant';
    confirmation_name: string;
} | {
    kind: 'leave_tenant';
};
export type AccountOperationCreate = {
    kind: 'delete_account';
    confirm: true;
};
export type Operation = {
    operation_id: ResourceId;
    kind: 'forget_memory' | 'delete_resource' | 'export' | 'delete_tenant' | 'leave_tenant' | 'delete_account' | 'calendar_write';
    status: 'pending' | 'running' | 'succeeded' | 'failed' | 'outcome_unknown';
    created_at: UtcTimestamp;
    updated_at: UtcTimestamp;
    error_code?: string;
    download_expires_at?: UtcTimestamp;
};
export type Usage = {
    period: string;
    turns: number;
    voice_seconds: number;
    remaining_turns: number;
    quota_state: 'available' | 'limited' | 'exhausted';
};
export type GetLivenessData = {
    body?: never;
    path?: never;
    query?: never;
    url: '/health/live';
};
export type GetLivenessResponses = {
    /**
     * プロセスがHTTP要求に応答できる
     */
    200: {
        status: 'ok';
    };
};
export type GetLivenessResponse = GetLivenessResponses[keyof GetLivenessResponses];
export type GetReadinessData = {
    body?: never;
    path?: never;
    query?: never;
    url: '/health/ready';
};
export type GetReadinessErrors = {
    /**
     * 起動未完了または依存先を利用できない。retryableはtrue。
     */
    503: Error;
};
export type GetReadinessError = GetReadinessErrors[keyof GetReadinessErrors];
export type GetReadinessResponses = {
    /**
     * 起動済みで登録された全probeが正常
     */
    200: {
        status: 'ready';
    };
};
export type GetReadinessResponse = GetReadinessResponses[keyof GetReadinessResponses];
export type GetCurrentUserData = {
    body?: never;
    path?: never;
    query?: never;
    url: '/users/me';
};
export type GetCurrentUserErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type GetCurrentUserError = GetCurrentUserErrors[keyof GetCurrentUserErrors];
export type GetCurrentUserResponses = {
    /**
     * 保存済みの本人プロフィール
     */
    200: User;
};
export type GetCurrentUserResponse = GetCurrentUserResponses[keyof GetCurrentUserResponses];
export type ListTenantsData = {
    body?: never;
    path?: never;
    query?: {
        limit?: number;
        cursor?: string;
    };
    url: '/tenants';
};
export type ListTenantsErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type ListTenantsError = ListTenantsErrors[keyof ListTenantsErrors];
export type ListTenantsResponses = {
    /**
     * 現在の認可を通して取得したテナント
     */
    200: TenantPage;
};
export type ListTenantsResponse = ListTenantsResponses[keyof ListTenantsResponses];
export type CreateTenantData = {
    body: TenantCreate;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
    };
    path?: never;
    query?: never;
    url: '/tenants';
};
export type CreateTenantErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type CreateTenantError = CreateTenantErrors[keyof CreateTenantErrors];
export type CreateTenantResponses = {
    /**
     * 現在の認可を通して取得したテナント
     */
    201: Tenant;
};
export type CreateTenantResponse = CreateTenantResponses[keyof CreateTenantResponses];
export type GetTenantData = {
    body?: never;
    path: {
        tenant_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}';
};
export type GetTenantErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type GetTenantError = GetTenantErrors[keyof GetTenantErrors];
export type GetTenantResponses = {
    /**
     * 現在の認可を通して取得したテナント
     */
    200: Tenant;
};
export type GetTenantResponse = GetTenantResponses[keyof GetTenantResponses];
export type UpdateTenantData = {
    body: TenantPatch;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
        /**
         * 直前に取得したversionを引用符付きで指定。
         */
        'If-Match': string;
    };
    path: {
        tenant_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}';
};
export type UpdateTenantErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type UpdateTenantError = UpdateTenantErrors[keyof UpdateTenantErrors];
export type UpdateTenantResponses = {
    /**
     * 現在の認可を通して取得したテナント
     */
    200: Tenant;
};
export type UpdateTenantResponse = UpdateTenantResponses[keyof UpdateTenantResponses];
export type RevokeMembershipByUidData = {
    body?: never;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
        /**
         * 直前に取得したversionを引用符付きで指定。
         */
        'If-Match': string;
    };
    path: {
        tenant_id: ResourceId;
    };
    query: {
        user_id: FirebaseUid;
    };
    url: '/tenants/{tenant_id}/memberships';
};
export type RevokeMembershipByUidErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type RevokeMembershipByUidError = RevokeMembershipByUidErrors[keyof RevokeMembershipByUidErrors];
export type RevokeMembershipByUidResponses = {
    /**
     * 成功
     */
    204: void;
};
export type RevokeMembershipByUidResponse = RevokeMembershipByUidResponses[keyof RevokeMembershipByUidResponses];
export type ListMembershipsData = {
    body?: never;
    path: {
        tenant_id: ResourceId;
    };
    query?: {
        limit?: number;
        cursor?: string;
    };
    url: '/tenants/{tenant_id}/memberships';
};
export type ListMembershipsErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type ListMembershipsError = ListMembershipsErrors[keyof ListMembershipsErrors];
export type ListMembershipsResponses = {
    /**
     * 成功
     */
    200: MembershipPage;
};
export type ListMembershipsResponse = ListMembershipsResponses[keyof ListMembershipsResponses];
export type UpdateMembershipByUidData = {
    body: MembershipPatch;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
        /**
         * 直前に取得したversionを引用符付きで指定。
         */
        'If-Match': string;
    };
    path: {
        tenant_id: ResourceId;
    };
    query: {
        user_id: FirebaseUid;
    };
    url: '/tenants/{tenant_id}/memberships';
};
export type UpdateMembershipByUidErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type UpdateMembershipByUidError = UpdateMembershipByUidErrors[keyof UpdateMembershipByUidErrors];
export type UpdateMembershipByUidResponses = {
    /**
     * 成功
     */
    200: Membership;
};
export type UpdateMembershipByUidResponse = UpdateMembershipByUidResponses[keyof UpdateMembershipByUidResponses];
export type RevokeMembershipData = {
    body?: never;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
        /**
         * 直前に取得したversionを引用符付きで指定。
         */
        'If-Match': string;
    };
    path: {
        tenant_id: ResourceId;
        user_id: FirebaseUid;
    };
    query?: never;
    url: '/tenants/{tenant_id}/memberships/{user_id}';
};
export type RevokeMembershipErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type RevokeMembershipError = RevokeMembershipErrors[keyof RevokeMembershipErrors];
export type RevokeMembershipResponses = {
    /**
     * 成功
     */
    204: void;
};
export type RevokeMembershipResponse = RevokeMembershipResponses[keyof RevokeMembershipResponses];
export type UpdateMembershipData = {
    body: MembershipPatch;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
        /**
         * 直前に取得したversionを引用符付きで指定。
         */
        'If-Match': string;
    };
    path: {
        tenant_id: ResourceId;
        user_id: FirebaseUid;
    };
    query?: never;
    url: '/tenants/{tenant_id}/memberships/{user_id}';
};
export type UpdateMembershipErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type UpdateMembershipError = UpdateMembershipErrors[keyof UpdateMembershipErrors];
export type UpdateMembershipResponses = {
    /**
     * 成功
     */
    200: Membership;
};
export type UpdateMembershipResponse = UpdateMembershipResponses[keyof UpdateMembershipResponses];
export type TransferOwnerData = {
    body: OwnerTransfer;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
        /**
         * 直前に取得したversionを引用符付きで指定。
         */
        'If-Match': string;
    };
    path: {
        tenant_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/memberships:transfer-owner';
};
export type TransferOwnerErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type TransferOwnerError = TransferOwnerErrors[keyof TransferOwnerErrors];
export type TransferOwnerResponses = {
    /**
     * 移譲完了
     */
    200: Membership;
};
export type TransferOwnerResponse = TransferOwnerResponses[keyof TransferOwnerResponses];
export type CreateInvitationData = {
    body: InvitationCreate;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
    };
    path: {
        tenant_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/invitations';
};
export type CreateInvitationErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type CreateInvitationError = CreateInvitationErrors[keyof CreateInvitationErrors];
export type CreateInvitationResponses = {
    /**
     * 成功（同一operationの再送を含む）。Cache-Controlはno-store。
     */
    201: Invitation;
};
export type CreateInvitationResponse = CreateInvitationResponses[keyof CreateInvitationResponses];
export type AcceptInvitationData = {
    body: InvitationAccept;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
    };
    path?: never;
    query?: never;
    url: '/invitations:accept';
};
export type AcceptInvitationErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type AcceptInvitationError = AcceptInvitationErrors[keyof AcceptInvitationErrors];
export type AcceptInvitationResponses = {
    /**
     * 成功（同一operationの再送を含む）。Cache-Controlはno-store。
     */
    201: Membership;
};
export type AcceptInvitationResponse = AcceptInvitationResponses[keyof AcceptInvitationResponses];
export type ListAgentsData = {
    body?: never;
    headers?: {
        /**
         * device bearerに追加するセッション参照。単独では認証しない。
         */
        'X-Private-Session'?: ResourceId;
    };
    path: {
        tenant_id: ResourceId;
    };
    query?: {
        limit?: number;
        cursor?: string;
    };
    url: '/tenants/{tenant_id}/agents';
};
export type ListAgentsErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type ListAgentsError = ListAgentsErrors[keyof ListAgentsErrors];
export type ListAgentsResponses = {
    /**
     * 成功
     */
    200: AgentPage;
};
export type ListAgentsResponse = ListAgentsResponses[keyof ListAgentsResponses];
export type GetAgentData = {
    body?: never;
    headers?: {
        /**
         * device bearerに追加するセッション参照。単独では認証しない。
         */
        'X-Private-Session'?: ResourceId;
    };
    path: {
        tenant_id: ResourceId;
        agent_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/agents/{agent_id}';
};
export type GetAgentErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type GetAgentError = GetAgentErrors[keyof GetAgentErrors];
export type GetAgentResponses = {
    /**
     * 成功
     */
    200: Agent;
};
export type GetAgentResponse = GetAgentResponses[keyof GetAgentResponses];
export type UpdateAgentData = {
    body: AgentPatch;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
        /**
         * 直前に取得したversionを引用符付きで指定。
         */
        'If-Match': string;
    };
    path: {
        tenant_id: ResourceId;
        agent_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/agents/{agent_id}';
};
export type UpdateAgentErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type UpdateAgentError = UpdateAgentErrors[keyof UpdateAgentErrors];
export type UpdateAgentResponses = {
    /**
     * 成功
     */
    200: Agent;
};
export type UpdateAgentResponse = UpdateAgentResponses[keyof UpdateAgentResponses];
export type CreateDevicePairingData = {
    body: PairingCreate;
    headers: {
        /**
         * 操作と本文へ束縛。端末秘密応答の再送期限は各operationの説明に従う。汎用24時間キャッシュは使わない。
         */
        'Idempotency-Key': string;
    };
    path?: never;
    query?: never;
    url: '/device-pairings';
};
export type CreateDevicePairingErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type CreateDevicePairingError = CreateDevicePairingErrors[keyof CreateDevicePairingErrors];
export type CreateDevicePairingResponses = {
    /**
     * 成功。Cache-Controlはno-store。
     */
    201: Pairing;
};
export type CreateDevicePairingResponse = CreateDevicePairingResponses[keyof CreateDevicePairingResponses];
export type ConfirmDevicePairingData = {
    body: PairingConfirm;
    headers: {
        /**
         * 操作と本文へ束縛。端末秘密応答の再送期限は各operationの説明に従う。汎用24時間キャッシュは使わない。
         */
        'Idempotency-Key': string;
    };
    path: {
        tenant_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/device-pairings:confirm';
};
export type ConfirmDevicePairingErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type ConfirmDevicePairingError = ConfirmDevicePairingErrors[keyof ConfirmDevicePairingErrors];
export type ConfirmDevicePairingResponses = {
    /**
     * 成功。Cache-Controlはno-store。
     */
    201: Device;
};
export type ConfirmDevicePairingResponse = ConfirmDevicePairingResponses[keyof ConfirmDevicePairingResponses];
export type ExchangeDevicePairingData = {
    body: PairingExchange;
    headers: {
        /**
         * 操作と本文へ束縛。端末秘密応答の再送期限は各operationの説明に従う。汎用24時間キャッシュは使わない。
         */
        'Idempotency-Key': string;
    };
    path: {
        pairing_id: ResourceId;
    };
    query?: never;
    url: '/device-pairings/{pairing_id}:exchange';
};
export type ExchangeDevicePairingErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type ExchangeDevicePairingError = ExchangeDevicePairingErrors[keyof ExchangeDevicePairingErrors];
export type ExchangeDevicePairingResponses = {
    /**
     * 成功。Cache-Controlはno-store。
     */
    200: DeviceCredentials;
};
export type ExchangeDevicePairingResponse = ExchangeDevicePairingResponses[keyof ExchangeDevicePairingResponses];
export type RefreshDeviceTokenData = {
    body: DeviceRefresh;
    headers: {
        /**
         * 操作と本文へ束縛。端末秘密応答の再送期限は各operationの説明に従う。汎用24時間キャッシュは使わない。
         */
        'Idempotency-Key': string;
    };
    path?: never;
    query?: never;
    url: '/device-tokens:refresh';
};
export type RefreshDeviceTokenErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type RefreshDeviceTokenError = RefreshDeviceTokenErrors[keyof RefreshDeviceTokenErrors];
export type RefreshDeviceTokenResponses = {
    /**
     * 成功。Cache-Controlはno-store。
     */
    200: DeviceCredentials;
};
export type RefreshDeviceTokenResponse = RefreshDeviceTokenResponses[keyof RefreshDeviceTokenResponses];
export type ListDevicesData = {
    body?: never;
    path: {
        tenant_id: ResourceId;
    };
    query?: {
        limit?: number;
        cursor?: string;
    };
    url: '/tenants/{tenant_id}/devices';
};
export type ListDevicesErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type ListDevicesError = ListDevicesErrors[keyof ListDevicesErrors];
export type ListDevicesResponses = {
    /**
     * 成功
     */
    200: DevicePage;
};
export type ListDevicesResponse = ListDevicesResponses[keyof ListDevicesResponses];
export type RevokeDeviceData = {
    body?: never;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
        /**
         * 直前に取得したversionを引用符付きで指定。
         */
        'If-Match': string;
    };
    path: {
        tenant_id: ResourceId;
        device_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/devices/{device_id}';
};
export type RevokeDeviceErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type RevokeDeviceError = RevokeDeviceErrors[keyof RevokeDeviceErrors];
export type RevokeDeviceResponses = {
    /**
     * 成功
     */
    204: void;
};
export type RevokeDeviceResponse = RevokeDeviceResponses[keyof RevokeDeviceResponses];
export type GetDeviceData = {
    body?: never;
    path: {
        tenant_id: ResourceId;
        device_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/devices/{device_id}';
};
export type GetDeviceErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type GetDeviceError = GetDeviceErrors[keyof GetDeviceErrors];
export type GetDeviceResponses = {
    /**
     * 成功
     */
    200: Device;
};
export type GetDeviceResponse = GetDeviceResponses[keyof GetDeviceResponses];
export type UpdateDeviceData = {
    body: DevicePatch;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
        /**
         * 直前に取得したversionを引用符付きで指定。
         */
        'If-Match': string;
    };
    path: {
        tenant_id: ResourceId;
        device_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/devices/{device_id}';
};
export type UpdateDeviceErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type UpdateDeviceError = UpdateDeviceErrors[keyof UpdateDeviceErrors];
export type UpdateDeviceResponses = {
    /**
     * 成功
     */
    200: Device;
};
export type UpdateDeviceResponse = UpdateDeviceResponses[keyof UpdateDeviceResponses];
export type CreatePrivateSessionData = {
    body: PrivateSessionCreate;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
    };
    path: {
        tenant_id: ResourceId;
        device_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/devices/{device_id}/private-sessions';
};
export type CreatePrivateSessionErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type CreatePrivateSessionError = CreatePrivateSessionErrors[keyof CreatePrivateSessionErrors];
export type CreatePrivateSessionResponses = {
    /**
     * 成功（再送時は同じIDの現在状態）
     */
    201: PrivateSession;
};
export type CreatePrivateSessionResponse = CreatePrivateSessionResponses[keyof CreatePrivateSessionResponses];
export type EndPrivateSessionData = {
    body?: never;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
        /**
         * 直前に取得したversionを引用符付きで指定。
         */
        'If-Match': string;
    };
    path: {
        tenant_id: ResourceId;
        private_session_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/private-sessions/{private_session_id}';
};
export type EndPrivateSessionErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type EndPrivateSessionError = EndPrivateSessionErrors[keyof EndPrivateSessionErrors];
export type EndPrivateSessionResponses = {
    /**
     * 成功
     */
    204: void;
};
export type EndPrivateSessionResponse = EndPrivateSessionResponses[keyof EndPrivateSessionResponses];
export type GetPrivateSessionData = {
    body?: never;
    path: {
        tenant_id: ResourceId;
        private_session_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/private-sessions/{private_session_id}';
};
export type GetPrivateSessionErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type GetPrivateSessionError = GetPrivateSessionErrors[keyof GetPrivateSessionErrors];
export type GetPrivateSessionResponses = {
    /**
     * 現在状態
     */
    200: PrivateSession;
};
export type GetPrivateSessionResponse = GetPrivateSessionResponses[keyof GetPrivateSessionResponses];
export type GetDeviceSnapshotData = {
    body?: never;
    path: {
        device_id: ResourceId;
    };
    query?: {
        cursor?: string;
    };
    url: '/devices/{device_id}/snapshot';
};
export type GetDeviceSnapshotErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type GetDeviceSnapshotError = GetDeviceSnapshotErrors[keyof GetDeviceSnapshotErrors];
export type GetDeviceSnapshotResponses = {
    /**
     * 成功（契約上の応答。実装は未提供）
     */
    200: DeviceSnapshot;
};
export type GetDeviceSnapshotResponse = GetDeviceSnapshotResponses[keyof GetDeviceSnapshotResponses];
export type ConnectDeviceData = {
    body?: never;
    path: {
        device_id: ResourceId;
    };
    query?: never;
    url: '/devices/{device_id}/connect';
};
export type ConnectDeviceErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type ConnectDeviceError = ConnectDeviceErrors[keyof ConnectDeviceErrors];
export type ConnectDeviceResponses = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type ConnectDeviceResponse = ConnectDeviceResponses[keyof ConnectDeviceResponses];
export type ListConversationsData = {
    body?: never;
    headers?: {
        /**
         * device bearerに追加するセッション参照。単独では認証しない。
         */
        'X-Private-Session'?: ResourceId;
    };
    path: {
        tenant_id: ResourceId;
    };
    query?: {
        limit?: number;
        cursor?: string;
    };
    url: '/tenants/{tenant_id}/conversations';
};
export type ListConversationsErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type ListConversationsError = ListConversationsErrors[keyof ListConversationsErrors];
export type ListConversationsResponses = {
    /**
     * 成功
     */
    200: ConversationPage;
};
export type ListConversationsResponse = ListConversationsResponses[keyof ListConversationsResponses];
export type CreateConversationData = {
    body: ConversationCreate;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
        /**
         * device bearerに追加するセッション参照。単独では認証しない。
         */
        'X-Private-Session'?: ResourceId;
    };
    path: {
        tenant_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/conversations';
};
export type CreateConversationErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type CreateConversationError = CreateConversationErrors[keyof CreateConversationErrors];
export type CreateConversationResponses = {
    /**
     * 成功
     */
    201: Conversation;
};
export type CreateConversationResponse = CreateConversationResponses[keyof CreateConversationResponses];
export type GetConversationData = {
    body?: never;
    headers?: {
        /**
         * device bearerに追加するセッション参照。単独では認証しない。
         */
        'X-Private-Session'?: ResourceId;
    };
    path: {
        tenant_id: ResourceId;
        conversation_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/conversations/{conversation_id}';
};
export type GetConversationErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type GetConversationError = GetConversationErrors[keyof GetConversationErrors];
export type GetConversationResponses = {
    /**
     * 成功
     */
    200: Conversation;
};
export type GetConversationResponse = GetConversationResponses[keyof GetConversationResponses];
export type UpdateConversationData = {
    body: ConversationPatch;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
        /**
         * 直前に取得したversionを引用符付きで指定。
         */
        'If-Match': string;
    };
    path: {
        tenant_id: ResourceId;
        conversation_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/conversations/{conversation_id}';
};
export type UpdateConversationErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type UpdateConversationError = UpdateConversationErrors[keyof UpdateConversationErrors];
export type UpdateConversationResponses = {
    /**
     * 成功
     */
    200: Conversation;
};
export type UpdateConversationResponse = UpdateConversationResponses[keyof UpdateConversationResponses];
export type ListMessagesData = {
    body?: never;
    headers?: {
        /**
         * device bearerに追加するセッション参照。単独では認証しない。
         */
        'X-Private-Session'?: ResourceId;
    };
    path: {
        tenant_id: ResourceId;
        conversation_id: ResourceId;
    };
    query?: {
        limit?: number;
        cursor?: string;
    };
    url: '/tenants/{tenant_id}/conversations/{conversation_id}/messages';
};
export type ListMessagesErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type ListMessagesError = ListMessagesErrors[keyof ListMessagesErrors];
export type ListMessagesResponses = {
    /**
     * 成功
     */
    200: MessagePage;
};
export type ListMessagesResponse = ListMessagesResponses[keyof ListMessagesResponses];
export type CreateTurnData = {
    body: TurnCreate;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
        /**
         * device bearerに追加するセッション参照。単独では認証しない。
         */
        'X-Private-Session'?: ResourceId;
    };
    path: {
        tenant_id: ResourceId;
        conversation_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/conversations/{conversation_id}/turns';
};
export type CreateTurnErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type CreateTurnError = CreateTurnErrors[keyof CreateTurnErrors];
export type CreateTurnResponses = {
    /**
     * 成功
     */
    202: RunAccepted;
};
export type CreateTurnResponse = CreateTurnResponses[keyof CreateTurnResponses];
export type GetRunData = {
    body?: never;
    headers?: {
        /**
         * device bearerに追加するセッション参照。単独では認証しない。
         */
        'X-Private-Session'?: ResourceId;
    };
    path: {
        tenant_id: ResourceId;
        run_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/runs/{run_id}';
};
export type GetRunErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type GetRunError = GetRunErrors[keyof GetRunErrors];
export type GetRunResponses = {
    /**
     * 成功
     */
    200: Run;
};
export type GetRunResponse = GetRunResponses[keyof GetRunResponses];
export type StreamRunEventsData = {
    body?: never;
    headers?: {
        'Last-Event-ID'?: string;
        /**
         * device bearerに追加するセッション参照。単独では認証しない。
         */
        'X-Private-Session'?: ResourceId;
    };
    path: {
        tenant_id: ResourceId;
        run_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/runs/{run_id}/events';
};
export type StreamRunEventsErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type StreamRunEventsError = StreamRunEventsErrors[keyof StreamRunEventsErrors];
export type StreamRunEventsResponses = {
    /**
     * 永続イベントを順序どおり配送。終端まで取得した接続は終了。
     */
    200: string;
};
export type StreamRunEventsResponse = StreamRunEventsResponses[keyof StreamRunEventsResponses];
export type CancelRunData = {
    body?: never;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
        /**
         * device bearerに追加するセッション参照。単独では認証しない。
         */
        'X-Private-Session'?: ResourceId;
    };
    path: {
        tenant_id: ResourceId;
        run_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/runs/{run_id}:cancel';
};
export type CancelRunErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type CancelRunError = CancelRunErrors[keyof CancelRunErrors];
export type CancelRunResponses = {
    /**
     * 現在のrun状態
     */
    200: Run;
};
export type CancelRunResponse = CancelRunResponses[keyof CancelRunResponses];
export type TranscribeAudioData = {
    /**
     * 16kHz/16bit/mono PCM WAV、60秒/5MiB以内。サービス自身は音声を永続化しない。
     */
    body: Blob | File;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
        /**
         * device bearerに追加するセッション参照。単独では認証しない。
         */
        'X-Private-Session'?: ResourceId;
    };
    path: {
        tenant_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/audio-inputs';
};
export type TranscribeAudioErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type TranscribeAudioError = TranscribeAudioErrors[keyof TranscribeAudioErrors];
export type TranscribeAudioResponses = {
    /**
     * 成功（契約上の応答。実装は未提供）
     */
    201: AudioInput;
};
export type TranscribeAudioResponse = TranscribeAudioResponses[keyof TranscribeAudioResponses];
export type GetSpeechAudioData = {
    body?: never;
    headers?: {
        /**
         * device bearerに追加するセッション参照。単独では認証しない。
         */
        'X-Private-Session'?: ResourceId;
    };
    path: {
        tenant_id: ResourceId;
        speech_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/speech-outputs/{speech_id}/audio';
};
export type GetSpeechAudioErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type GetSpeechAudioError = GetSpeechAudioErrors[keyof GetSpeechAudioErrors];
export type GetSpeechAudioResponses = {
    /**
     * 成功（契約上の応答。実装は未提供）
     */
    200: Blob | File;
};
export type GetSpeechAudioResponse = GetSpeechAudioResponses[keyof GetSpeechAudioResponses];
export type ListEntitiesData = {
    body?: never;
    headers?: {
        /**
         * device bearerに追加するセッション参照。単独では認証しない。
         */
        'X-Private-Session'?: ResourceId;
    };
    path: {
        tenant_id: ResourceId;
    };
    query?: {
        limit?: number;
        cursor?: string;
        query?: string;
    };
    url: '/tenants/{tenant_id}/entities';
};
export type ListEntitiesErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type ListEntitiesError = ListEntitiesErrors[keyof ListEntitiesErrors];
export type ListEntitiesResponses = {
    /**
     * 成功
     */
    200: EntityPage;
};
export type ListEntitiesResponse = ListEntitiesResponses[keyof ListEntitiesResponses];
export type CreateEntityData = {
    body: EntityCreate;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
        /**
         * device bearerに追加するセッション参照。単独では認証しない。
         */
        'X-Private-Session'?: ResourceId;
    };
    path: {
        tenant_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/entities';
};
export type CreateEntityErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type CreateEntityError = CreateEntityErrors[keyof CreateEntityErrors];
export type CreateEntityResponses = {
    /**
     * 成功
     */
    201: Entity;
};
export type CreateEntityResponse = CreateEntityResponses[keyof CreateEntityResponses];
export type GetEntityData = {
    body?: never;
    headers?: {
        /**
         * device bearerに追加するセッション参照。単独では認証しない。
         */
        'X-Private-Session'?: ResourceId;
    };
    path: {
        tenant_id: ResourceId;
        entity_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/entities/{entity_id}';
};
export type GetEntityErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type GetEntityError = GetEntityErrors[keyof GetEntityErrors];
export type GetEntityResponses = {
    /**
     * 成功
     */
    200: Entity;
};
export type GetEntityResponse = GetEntityResponses[keyof GetEntityResponses];
export type UpdateEntityData = {
    body: EntityPatch;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
        /**
         * 直前に取得したversionを引用符付きで指定。
         */
        'If-Match': string;
        /**
         * device bearerに追加するセッション参照。単独では認証しない。
         */
        'X-Private-Session'?: ResourceId;
    };
    path: {
        tenant_id: ResourceId;
        entity_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/entities/{entity_id}';
};
export type UpdateEntityErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type UpdateEntityError = UpdateEntityErrors[keyof UpdateEntityErrors];
export type UpdateEntityResponses = {
    /**
     * 成功
     */
    200: Entity;
};
export type UpdateEntityResponse = UpdateEntityResponses[keyof UpdateEntityResponses];
export type ListMemoriesData = {
    body?: never;
    headers?: {
        /**
         * device bearerに追加するセッション参照。単独では認証しない。
         */
        'X-Private-Session'?: ResourceId;
    };
    path: {
        tenant_id: ResourceId;
    };
    query?: {
        limit?: number;
        cursor?: string;
    };
    url: '/tenants/{tenant_id}/memories';
};
export type ListMemoriesErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type ListMemoriesError = ListMemoriesErrors[keyof ListMemoriesErrors];
export type ListMemoriesResponses = {
    /**
     * 成功
     */
    200: MemoryPage;
};
export type ListMemoriesResponse = ListMemoriesResponses[keyof ListMemoriesResponses];
export type CreateMemoryData = {
    body: MemoryCreate;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
        /**
         * device bearerに追加するセッション参照。単独では認証しない。
         */
        'X-Private-Session'?: ResourceId;
    };
    path: {
        tenant_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/memories';
};
export type CreateMemoryErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type CreateMemoryError = CreateMemoryErrors[keyof CreateMemoryErrors];
export type CreateMemoryResponses = {
    /**
     * 成功
     */
    201: Memory;
};
export type CreateMemoryResponse = CreateMemoryResponses[keyof CreateMemoryResponses];
export type GetMemoryData = {
    body?: never;
    headers?: {
        /**
         * device bearerに追加するセッション参照。単独では認証しない。
         */
        'X-Private-Session'?: ResourceId;
    };
    path: {
        tenant_id: ResourceId;
        memory_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/memories/{memory_id}';
};
export type GetMemoryErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type GetMemoryError = GetMemoryErrors[keyof GetMemoryErrors];
export type GetMemoryResponses = {
    /**
     * 成功
     */
    200: Memory;
};
export type GetMemoryResponse = GetMemoryResponses[keyof GetMemoryResponses];
export type ReviseMemoryData = {
    body: MemoryRevision;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
        /**
         * 直前に取得したversionを引用符付きで指定。
         */
        'If-Match': string;
        /**
         * device bearerに追加するセッション参照。単独では認証しない。
         */
        'X-Private-Session'?: ResourceId;
    };
    path: {
        tenant_id: ResourceId;
        memory_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/memories/{memory_id}';
};
export type ReviseMemoryErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type ReviseMemoryError = ReviseMemoryErrors[keyof ReviseMemoryErrors];
export type ReviseMemoryResponses = {
    /**
     * 成功
     */
    200: Memory;
};
export type ReviseMemoryResponse = ReviseMemoryResponses[keyof ReviseMemoryResponses];
export type ListNotesData = {
    body?: never;
    headers?: {
        /**
         * device bearerに追加するセッション参照。単独では認証しない。
         */
        'X-Private-Session'?: ResourceId;
    };
    path: {
        tenant_id: ResourceId;
    };
    query?: {
        limit?: number;
        cursor?: string;
        query?: string;
    };
    url: '/tenants/{tenant_id}/notes';
};
export type ListNotesErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type ListNotesError = ListNotesErrors[keyof ListNotesErrors];
export type ListNotesResponses = {
    /**
     * 成功
     */
    200: NotePage;
};
export type ListNotesResponse = ListNotesResponses[keyof ListNotesResponses];
export type CreateNoteData = {
    body: NoteCreate;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
        /**
         * device bearerに追加するセッション参照。単独では認証しない。
         */
        'X-Private-Session'?: ResourceId;
    };
    path: {
        tenant_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/notes';
};
export type CreateNoteErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type CreateNoteError = CreateNoteErrors[keyof CreateNoteErrors];
export type CreateNoteResponses = {
    /**
     * 成功
     */
    201: Note;
};
export type CreateNoteResponse = CreateNoteResponses[keyof CreateNoteResponses];
export type GetNoteData = {
    body?: never;
    headers?: {
        /**
         * device bearerに追加するセッション参照。単独では認証しない。
         */
        'X-Private-Session'?: ResourceId;
    };
    path: {
        tenant_id: ResourceId;
        note_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/notes/{note_id}';
};
export type GetNoteErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type GetNoteError = GetNoteErrors[keyof GetNoteErrors];
export type GetNoteResponses = {
    /**
     * 成功
     */
    200: Note;
};
export type GetNoteResponse = GetNoteResponses[keyof GetNoteResponses];
export type UpdateNoteData = {
    body: NotePatch;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
        /**
         * 直前に取得したversionを引用符付きで指定。
         */
        'If-Match': string;
        /**
         * device bearerに追加するセッション参照。単独では認証しない。
         */
        'X-Private-Session'?: ResourceId;
    };
    path: {
        tenant_id: ResourceId;
        note_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/notes/{note_id}';
};
export type UpdateNoteErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type UpdateNoteError = UpdateNoteErrors[keyof UpdateNoteErrors];
export type UpdateNoteResponses = {
    /**
     * 成功
     */
    200: Note;
};
export type UpdateNoteResponse = UpdateNoteResponses[keyof UpdateNoteResponses];
export type ListTasksData = {
    body?: never;
    headers?: {
        /**
         * device bearerに追加するセッション参照。単独では認証しない。
         */
        'X-Private-Session'?: ResourceId;
    };
    path: {
        tenant_id: ResourceId;
    };
    query?: {
        limit?: number;
        cursor?: string;
        status?: 'open' | 'done' | 'cancelled';
    };
    url: '/tenants/{tenant_id}/tasks';
};
export type ListTasksErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type ListTasksError = ListTasksErrors[keyof ListTasksErrors];
export type ListTasksResponses = {
    /**
     * 現在の権限で閲覧可能なタスク一覧。
     */
    200: TaskPage;
};
export type ListTasksResponse = ListTasksResponses[keyof ListTasksResponses];
export type CreateTaskData = {
    body: TaskCreate;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
        /**
         * device bearerに追加するセッション参照。単独では認証しない。
         */
        'X-Private-Session'?: ResourceId;
    };
    path: {
        tenant_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/tasks';
};
export type CreateTaskErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type CreateTaskError = CreateTaskErrors[keyof CreateTaskErrors];
export type CreateTaskResponses = {
    /**
     * タスクを保存済み。現在のTaskとETagを返す。
     */
    201: Task;
};
export type CreateTaskResponse = CreateTaskResponses[keyof CreateTaskResponses];
export type GetTaskData = {
    body?: never;
    headers?: {
        /**
         * device bearerに追加するセッション参照。単独では認証しない。
         */
        'X-Private-Session'?: ResourceId;
    };
    path: {
        tenant_id: ResourceId;
        task_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/tasks/{task_id}';
};
export type GetTaskErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type GetTaskError = GetTaskErrors[keyof GetTaskErrors];
export type GetTaskResponses = {
    /**
     * 現在の権限で閲覧可能なTask。
     */
    200: Task;
};
export type GetTaskResponse = GetTaskResponses[keyof GetTaskResponses];
export type UpdateTaskData = {
    body: TaskPatch;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
        /**
         * 直前に取得したversionを引用符付きで指定。
         */
        'If-Match': string;
        /**
         * device bearerに追加するセッション参照。単独では認証しない。
         */
        'X-Private-Session'?: ResourceId;
    };
    path: {
        tenant_id: ResourceId;
        task_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/tasks/{task_id}';
};
export type UpdateTaskErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type UpdateTaskError = UpdateTaskErrors[keyof UpdateTaskErrors];
export type UpdateTaskResponses = {
    /**
     * タスクを更新済み。同じ冪等キーの再送は重複更新しない。
     */
    200: Task;
};
export type UpdateTaskResponse = UpdateTaskResponses[keyof UpdateTaskResponses];
export type ListRemindersData = {
    body?: never;
    headers?: {
        /**
         * device bearerに追加するセッション参照。単独では認証しない。
         */
        'X-Private-Session'?: ResourceId;
    };
    path: {
        tenant_id: ResourceId;
    };
    query?: {
        limit?: number;
        cursor?: string;
        status?: 'scheduled' | 'dispatched' | 'acknowledged' | 'cancelled' | 'expired';
    };
    url: '/tenants/{tenant_id}/reminders';
};
export type ListRemindersErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type ListRemindersError = ListRemindersErrors[keyof ListRemindersErrors];
export type ListRemindersResponses = {
    /**
     * 成功（永続化済みの現在のリマインダー）
     */
    200: ReminderPage;
};
export type ListRemindersResponse = ListRemindersResponses[keyof ListRemindersResponses];
export type CreateReminderData = {
    body: ReminderCreate;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
        /**
         * device bearerに追加するセッション参照。単独では認証しない。
         */
        'X-Private-Session'?: ResourceId;
    };
    path: {
        tenant_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/reminders';
};
export type CreateReminderErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type CreateReminderError = CreateReminderErrors[keyof CreateReminderErrors];
export type CreateReminderResponses = {
    /**
     * 成功（永続化済みの現在のリマインダー）
     */
    201: Reminder;
};
export type CreateReminderResponse = CreateReminderResponses[keyof CreateReminderResponses];
export type GetReminderData = {
    body?: never;
    headers?: {
        /**
         * device bearerに追加するセッション参照。単独では認証しない。
         */
        'X-Private-Session'?: ResourceId;
    };
    path: {
        tenant_id: ResourceId;
        reminder_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/reminders/{reminder_id}';
};
export type GetReminderErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type GetReminderError = GetReminderErrors[keyof GetReminderErrors];
export type GetReminderResponses = {
    /**
     * 成功（永続化済みの現在のリマインダー）
     */
    200: Reminder;
};
export type GetReminderResponse = GetReminderResponses[keyof GetReminderResponses];
export type UpdateReminderData = {
    body: ReminderPatch;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
        /**
         * 直前に取得したversionを引用符付きで指定。
         */
        'If-Match': string;
        /**
         * device bearerに追加するセッション参照。単独では認証しない。
         */
        'X-Private-Session'?: ResourceId;
    };
    path: {
        tenant_id: ResourceId;
        reminder_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/reminders/{reminder_id}';
};
export type UpdateReminderErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type UpdateReminderError = UpdateReminderErrors[keyof UpdateReminderErrors];
export type UpdateReminderResponses = {
    /**
     * 成功（永続化済みの現在のリマインダー）
     */
    200: Reminder;
};
export type UpdateReminderResponse = UpdateReminderResponses[keyof UpdateReminderResponses];
export type SearchMemoriesData = {
    body: MemorySearch;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
        /**
         * device bearerに追加するセッション参照。単独では認証しない。
         */
        'X-Private-Session'?: ResourceId;
    };
    path: {
        tenant_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/memories:search';
};
export type SearchMemoriesErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type SearchMemoriesError = SearchMemoriesErrors[keyof SearchMemoriesErrors];
export type SearchMemoriesResponses = {
    /**
     * 成功（契約上の応答。実装は未提供）
     */
    200: MemoryPage;
};
export type SearchMemoriesResponse = SearchMemoriesResponses[keyof SearchMemoriesResponses];
export type SnoozeReminderData = {
    body: ReminderSnooze;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
        /**
         * device bearerに追加するセッション参照。単独では認証しない。
         */
        'X-Private-Session'?: ResourceId;
    };
    path: {
        tenant_id: ResourceId;
        reminder_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/reminders/{reminder_id}:snooze';
};
export type SnoozeReminderErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type SnoozeReminderError = SnoozeReminderErrors[keyof SnoozeReminderErrors];
export type SnoozeReminderResponses = {
    /**
     * 成功（永続化済みの現在のリマインダー）
     */
    200: Reminder;
};
export type SnoozeReminderResponse = SnoozeReminderResponses[keyof SnoozeReminderResponses];
export type RegisterPushInstallationData = {
    body: PushInstallationCreate;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
    };
    path?: never;
    query?: never;
    url: '/users/me/push-installations';
};
export type RegisterPushInstallationErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type RegisterPushInstallationError = RegisterPushInstallationErrors[keyof RegisterPushInstallationErrors];
export type RegisterPushInstallationResponses = {
    /**
     * 成功
     */
    201: PushInstallation;
};
export type RegisterPushInstallationResponse = RegisterPushInstallationResponses[keyof RegisterPushInstallationResponses];
export type RemovePushInstallationData = {
    body?: never;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
        /**
         * 直前に取得したversionを引用符付きで指定。
         */
        'If-Match': string;
    };
    path: {
        installation_id: ResourceId;
    };
    query?: never;
    url: '/users/me/push-installations/{installation_id}';
};
export type RemovePushInstallationErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type RemovePushInstallationError = RemovePushInstallationErrors[keyof RemovePushInstallationErrors];
export type RemovePushInstallationResponses = {
    /**
     * 成功
     */
    204: void;
};
export type RemovePushInstallationResponse = RemovePushInstallationResponses[keyof RemovePushInstallationResponses];
export type RefreshPushInstallationData = {
    body: PushInstallationPatch;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
        /**
         * 直前に取得したversionを引用符付きで指定。
         */
        'If-Match': string;
    };
    path: {
        installation_id: ResourceId;
    };
    query?: never;
    url: '/users/me/push-installations/{installation_id}';
};
export type RefreshPushInstallationErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type RefreshPushInstallationError = RefreshPushInstallationErrors[keyof RefreshPushInstallationErrors];
export type RefreshPushInstallationResponses = {
    /**
     * 成功
     */
    200: PushInstallation;
};
export type RefreshPushInstallationResponse = RefreshPushInstallationResponses[keyof RefreshPushInstallationResponses];
export type ListNotificationsData = {
    body?: never;
    headers?: {
        /**
         * device bearerに追加するセッション参照。単独では認証しない。
         */
        'X-Private-Session'?: ResourceId;
    };
    path: {
        tenant_id: ResourceId;
    };
    query?: {
        limit?: number;
        cursor?: string;
    };
    url: '/tenants/{tenant_id}/notifications';
};
export type ListNotificationsErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type ListNotificationsError = ListNotificationsErrors[keyof ListNotificationsErrors];
export type ListNotificationsResponses = {
    /**
     * 成功
     */
    200: NotificationPage;
};
export type ListNotificationsResponse = ListNotificationsResponses[keyof ListNotificationsResponses];
export type GetNotificationData = {
    body?: never;
    headers?: {
        /**
         * device bearerに追加するセッション参照。単独では認証しない。
         */
        'X-Private-Session'?: ResourceId;
    };
    path: {
        tenant_id: ResourceId;
        notification_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/notifications/{notification_id}';
};
export type GetNotificationErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type GetNotificationError = GetNotificationErrors[keyof GetNotificationErrors];
export type GetNotificationResponses = {
    /**
     * 成功
     */
    200: Notification;
};
export type GetNotificationResponse = GetNotificationResponses[keyof GetNotificationResponses];
export type AckNotificationData = {
    body: NotificationAck;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
        /**
         * device bearerに追加するセッション参照。単独では認証しない。
         */
        'X-Private-Session'?: ResourceId;
    };
    path: {
        tenant_id: ResourceId;
        notification_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/notifications/{notification_id}:ack';
};
export type AckNotificationErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type AckNotificationError = AckNotificationErrors[keyof AckNotificationErrors];
export type AckNotificationResponses = {
    /**
     * 成功
     */
    200: Notification;
};
export type AckNotificationResponse = AckNotificationResponses[keyof AckNotificationResponses];
export type ListIntegrationsData = {
    body?: never;
    path: {
        tenant_id: ResourceId;
    };
    query?: {
        limit?: number;
        cursor?: string;
    };
    url: '/tenants/{tenant_id}/integrations';
};
export type ListIntegrationsErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type ListIntegrationsError = ListIntegrationsErrors[keyof ListIntegrationsErrors];
export type ListIntegrationsResponses = {
    /**
     * 本人が現在参照できるCalendar連携metadata
     */
    200: IntegrationPage;
};
export type ListIntegrationsResponse = ListIntegrationsResponses[keyof ListIntegrationsResponses];
export type StartGoogleCalendarAuthorizationData = {
    body: OAuthStart;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
    };
    path: {
        tenant_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/integrations/google-calendar/authorizations';
};
export type StartGoogleCalendarAuthorizationErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type StartGoogleCalendarAuthorizationError = StartGoogleCalendarAuthorizationErrors[keyof StartGoogleCalendarAuthorizationErrors];
export type StartGoogleCalendarAuthorizationResponses = {
    /**
     * 登録済みproviderへの認可URLと有効期限
     */
    201: OAuthAuthorization;
};
export type StartGoogleCalendarAuthorizationResponse = StartGoogleCalendarAuthorizationResponses[keyof StartGoogleCalendarAuthorizationResponses];
export type CompleteGoogleCalendarAuthorizationData = {
    body?: never;
    path?: never;
    query: {
        state: string;
        code?: string;
        error?: string;
    };
    url: '/oauth/google-calendar/callback';
};
export type CompleteGoogleCalendarAuthorizationErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type CompleteGoogleCalendarAuthorizationError = CompleteGoogleCalendarAuthorizationErrors[keyof CompleteGoogleCalendarAuthorizationErrors];
export type CompleteGoogleCalendarAuthorizationResponses = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type CompleteGoogleCalendarAuthorizationResponse = CompleteGoogleCalendarAuthorizationResponses[keyof CompleteGoogleCalendarAuthorizationResponses];
export type CreateHomeAssistantIntegrationData = {
    body: HomeIntegrationCreate;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
    };
    path: {
        tenant_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/integrations/home-assistant';
};
export type CreateHomeAssistantIntegrationErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type CreateHomeAssistantIntegrationError = CreateHomeAssistantIntegrationErrors[keyof CreateHomeAssistantIntegrationErrors];
export type CreateHomeAssistantIntegrationResponses = {
    /**
     * 成功（契約上の応答。実装は未提供）
     */
    201: Integration;
};
export type CreateHomeAssistantIntegrationResponse = CreateHomeAssistantIntegrationResponses[keyof CreateHomeAssistantIntegrationResponses];
export type SyncHomeAssistantEntitiesData = {
    body: HomeInventorySync;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
    };
    path: {
        tenant_id: ResourceId;
        integration_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/integrations/{integration_id}/home-assistant/entities:sync';
};
export type SyncHomeAssistantEntitiesErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type SyncHomeAssistantEntitiesError = SyncHomeAssistantEntitiesErrors[keyof SyncHomeAssistantEntitiesErrors];
export type SyncHomeAssistantEntitiesResponses = {
    /**
     * 成功（契約上の応答。実装は未提供）
     */
    200: EntityPage;
};
export type SyncHomeAssistantEntitiesResponse = SyncHomeAssistantEntitiesResponses[keyof SyncHomeAssistantEntitiesResponses];
export type RevokeIntegrationData = {
    body?: never;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
        /**
         * 直前に取得したversionを引用符付きで指定。
         */
        'If-Match': string;
    };
    path: {
        tenant_id: ResourceId;
        integration_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/integrations/{integration_id}';
};
export type RevokeIntegrationErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type RevokeIntegrationError = RevokeIntegrationErrors[keyof RevokeIntegrationErrors];
export type RevokeIntegrationResponses = {
    /**
     * 成功（契約上の応答。実装は未提供）
     */
    204: void;
};
export type RevokeIntegrationResponse = RevokeIntegrationResponses[keyof RevokeIntegrationResponses];
export type UpdateIntegrationData = {
    body: IntegrationPatch;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
        /**
         * 直前に取得したversionを引用符付きで指定。
         */
        'If-Match': string;
    };
    path: {
        tenant_id: ResourceId;
        integration_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/integrations/{integration_id}';
};
export type UpdateIntegrationErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type UpdateIntegrationError = UpdateIntegrationErrors[keyof UpdateIntegrationErrors];
export type UpdateIntegrationResponses = {
    /**
     * 成功
     */
    200: Integration;
};
export type UpdateIntegrationResponse = UpdateIntegrationResponses[keyof UpdateIntegrationResponses];
export type ListCalendarEventsData = {
    body?: never;
    headers?: {
        /**
         * device bearerに追加するセッション参照。単独では認証しない。
         */
        'X-Private-Session'?: ResourceId;
    };
    path: {
        tenant_id: ResourceId;
        integration_id: ResourceId;
    };
    query: {
        limit?: number;
        cursor?: string;
        calendar_id: ProviderId;
        time_min: UtcTimestamp;
        time_max: UtcTimestamp;
    };
    url: '/tenants/{tenant_id}/integrations/{integration_id}/calendar-events';
};
export type ListCalendarEventsErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type ListCalendarEventsError = ListCalendarEventsErrors[keyof ListCalendarEventsErrors];
export type ListCalendarEventsResponses = {
    /**
     * 成功
     */
    200: CalendarEventPage;
};
export type ListCalendarEventsResponse = ListCalendarEventsResponses[keyof ListCalendarEventsResponses];
export type CreateCalendarProposalData = {
    body: CalendarProposalCreate;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
        /**
         * device bearerに追加するセッション参照。単独では認証しない。
         */
        'X-Private-Session'?: ResourceId;
    };
    path: {
        tenant_id: ResourceId;
        integration_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/integrations/{integration_id}/calendar-proposals';
};
export type CreateCalendarProposalErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type CreateCalendarProposalError = CreateCalendarProposalErrors[keyof CreateCalendarProposalErrors];
export type CreateCalendarProposalResponses = {
    /**
     * 固定proposalを保存した
     */
    201: CalendarProposal;
};
export type CreateCalendarProposalResponse = CreateCalendarProposalResponses[keyof CreateCalendarProposalResponses];
export type GetCalendarProposalData = {
    body?: never;
    path: {
        tenant_id: ResourceId;
        proposal_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/calendar-proposals/{proposal_id}';
};
export type GetCalendarProposalErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type GetCalendarProposalError = GetCalendarProposalErrors[keyof GetCalendarProposalErrors];
export type GetCalendarProposalResponses = {
    /**
     * 現在認可された本人の固定proposal
     */
    200: CalendarProposal;
};
export type GetCalendarProposalResponse = GetCalendarProposalResponses[keyof GetCalendarProposalResponses];
export type ConfirmCalendarProposalData = {
    body: CalendarConfirm;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
    };
    path: {
        tenant_id: ResourceId;
        proposal_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/calendar-proposals/{proposal_id}:confirm';
};
export type ConfirmCalendarProposalErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type ConfirmCalendarProposalError = ConfirmCalendarProposalErrors[keyof ConfirmCalendarProposalErrors];
export type ConfirmCalendarProposalResponses = {
    /**
     * 非同期operationを受付（現在認可された本人のみ取得可能）
     */
    202: Operation;
};
export type ConfirmCalendarProposalResponse = ConfirmCalendarProposalResponses[keyof ConfirmCalendarProposalResponses];
export type RejectCalendarProposalData = {
    body?: never;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
    };
    path: {
        tenant_id: ResourceId;
        proposal_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/calendar-proposals/{proposal_id}:reject';
};
export type RejectCalendarProposalErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type RejectCalendarProposalError = RejectCalendarProposalErrors[keyof RejectCalendarProposalErrors];
export type RejectCalendarProposalResponses = {
    /**
     * 現在認可された本人の固定proposal
     */
    200: CalendarProposal;
};
export type RejectCalendarProposalResponse = RejectCalendarProposalResponses[keyof RejectCalendarProposalResponses];
export type CreateDeviceCommandData = {
    body: CommandCreate;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
        /**
         * device bearerに追加するセッション参照。単独では認証しない。
         */
        'X-Private-Session'?: ResourceId;
    };
    path: {
        tenant_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/commands';
};
export type CreateDeviceCommandErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type CreateDeviceCommandError = CreateDeviceCommandErrors[keyof CreateDeviceCommandErrors];
export type CreateDeviceCommandResponses = {
    /**
     * 受付済み。同key再送は現在認可で同じcommandを返す。
     */
    202: Command;
};
export type CreateDeviceCommandResponse = CreateDeviceCommandResponses[keyof CreateDeviceCommandResponses];
export type GetDeviceCommandData = {
    body?: never;
    headers?: {
        /**
         * device bearerに追加するセッション参照。単独では認証しない。
         */
        'X-Private-Session'?: ResourceId;
    };
    path: {
        tenant_id: ResourceId;
        command_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/commands/{command_id}';
};
export type GetDeviceCommandErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type GetDeviceCommandError = GetDeviceCommandErrors[keyof GetDeviceCommandErrors];
export type GetDeviceCommandResponses = {
    /**
     * 現在のcommand状態
     */
    200: Command;
};
export type GetDeviceCommandResponse = GetDeviceCommandResponses[keyof GetDeviceCommandResponses];
export type CancelDeviceCommandData = {
    body?: never;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
        /**
         * device bearerに追加するセッション参照。単独では認証しない。
         */
        'X-Private-Session'?: ResourceId;
        /**
         * 任意の期待version。未指定でも現在状態とclaimを原子的に検査する。
         */
        'If-Match'?: string;
    };
    path: {
        tenant_id: ResourceId;
        command_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/commands/{command_id}:cancel';
};
export type CancelDeviceCommandErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type CancelDeviceCommandError = CancelDeviceCommandErrors[keyof CancelDeviceCommandErrors];
export type CancelDeviceCommandResponses = {
    /**
     * 取消し済みcommand
     */
    200: Command;
};
export type CancelDeviceCommandResponse = CancelDeviceCommandResponses[keyof CancelDeviceCommandResponses];
export type CreateTenantOperationData = {
    body: OperationCreate;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
    };
    path: {
        tenant_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/operations';
};
export type CreateTenantOperationErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type CreateTenantOperationError = CreateTenantOperationErrors[keyof CreateTenantOperationErrors];
export type CreateTenantOperationResponses = {
    /**
     * 非同期operation受付済み。削除は独立台帳とtombstone保存後、exportはoperationと通常jobの同一transaction保存後に返す。処理完了ではない
     */
    202: Operation;
};
export type CreateTenantOperationResponse = CreateTenantOperationResponses[keyof CreateTenantOperationResponses];
export type GetTenantOperationData = {
    body?: never;
    path: {
        tenant_id: ResourceId;
        operation_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/operations/{operation_id}';
};
export type GetTenantOperationErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type GetTenantOperationError = GetTenantOperationErrors[keyof GetTenantOperationErrors];
export type GetTenantOperationResponses = {
    /**
     * 現在の認可を検査した本人の忘却操作状態。退出・tenant削除の受付後は同じ本人の該当operation状態だけを参照できる
     */
    200: Operation;
};
export type GetTenantOperationResponse = GetTenantOperationResponses[keyof GetTenantOperationResponses];
export type DownloadExportData = {
    body?: never;
    path: {
        tenant_id: ResourceId;
        operation_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/operations/{operation_id}/download';
};
export type DownloadExportErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type DownloadExportError = DownloadExportErrors[keyof DownloadExportErrors];
export type DownloadExportResponses = {
    /**
     * 認可済みdata.jsonを含むZIP。Cache-Controlはno-store
     */
    200: Blob | File;
};
export type DownloadExportResponse = DownloadExportResponses[keyof DownloadExportResponses];
export type CreateAccountOperationData = {
    body: AccountOperationCreate;
    headers: {
        /**
         * 同一scopeで24時間。同一keyの異なるbodyは409。
         */
        'Idempotency-Key': string;
    };
    path?: never;
    query?: never;
    url: '/operations';
};
export type CreateAccountOperationErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type CreateAccountOperationError = CreateAccountOperationErrors[keyof CreateAccountOperationErrors];
export type CreateAccountOperationResponses = {
    /**
     * 成功（local-emulatorと明示fake構成。実環境の受付は未公開）
     */
    202: Operation;
};
export type CreateAccountOperationResponse = CreateAccountOperationResponses[keyof CreateAccountOperationResponses];
export type GetAccountOperationData = {
    body?: never;
    path: {
        operation_id: ResourceId;
    };
    query?: never;
    url: '/operations/{operation_id}';
};
export type GetAccountOperationErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type GetAccountOperationError = GetAccountOperationErrors[keyof GetAccountOperationErrors];
export type GetAccountOperationResponses = {
    /**
     * 成功（local-emulatorと明示fake構成。実環境の受付は未公開）
     */
    200: Operation;
};
export type GetAccountOperationResponse = GetAccountOperationResponses[keyof GetAccountOperationResponses];
export type GetUsageData = {
    body?: never;
    path: {
        tenant_id: ResourceId;
    };
    query?: never;
    url: '/tenants/{tenant_id}/usage';
};
export type GetUsageErrors = {
    /**
     * 共通エラー
     */
    default: Error;
};
export type GetUsageError = GetUsageErrors[keyof GetUsageErrors];
export type GetUsageResponses = {
    /**
     * 成功
     */
    200: Usage;
};
export type GetUsageResponse = GetUsageResponses[keyof GetUsageResponses];
