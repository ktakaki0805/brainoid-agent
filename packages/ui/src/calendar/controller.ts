import {
  type CalendarEvent,
  type CalendarProposal,
  type CalendarProposalCreate,
  type Client,
  type Integration,
  type Operation,
  confirmCalendarProposal,
  createCalendarProposal,
  getCalendarProposal,
  getIntegration,
  getTenantOperation,
  listCalendarEvents,
  listIntegrations,
  rejectCalendarProposal,
  revokeIntegration,
  startGoogleCalendarAuthorization,
  updateIntegration,
} from "@brainoid-internal/api-client";
import { isId } from "../chat/events.ts";
import { RequestFailure, type SessionContext, StaleContext } from "../session.ts";
import { validTimestamp } from "../tasks/controller.ts";
import {
  type CalendarAuthorityChange,
  assertCalendarScope,
  calendarAuthorityGuard,
  calendarListPublication,
  observeCalendarAuthority,
  observeCalendarProposal,
  publishCalendarAuthority,
} from "./authority.ts";
import {
  type CalendarOAuth,
  calendarIds,
  event,
  integration,
  operation,
  proposal,
  validChange,
} from "./contracts.ts";

export interface CalendarState {
  integrations: readonly Integration[];
  selected?: Integration | undefined;
  calendar: string;
  events: readonly CalendarEvent[];
  range: { time_min: string; time_max: string };
  draft?: CalendarProposalCreate | undefined;
  proposal?: CalendarProposal | undefined;
  operation?: Operation | undefined;
  selectionDraft?: string[] | undefined;
  conflict?: { before: Integration; current?: Integration } | undefined;
  busy: boolean;
  pending: boolean;
  oauthWaiting: boolean;
  closed: boolean;
  notice?: string | undefined;
}
type Mutation = { key: string } & (
  | { kind: "oauth"; body: { scope_profile: "read" | "read_write"; return_target: "mobile" } }
  | { kind: "selection"; original: Integration; body: { allowed_calendar_ids: string[] } }
  | { kind: "disconnect"; original: Integration }
  | { kind: "proposal"; integration: string; body: CalendarProposalCreate }
  | { kind: "confirm"; original: CalendarProposal; body: { args_hash: string; confirmed: true } }
  | { kind: "reject"; original: CalendarProposal }
);
const empty = (): CalendarState => ({
  integrations: [],
  calendar: "",
  events: [],
  range: {
    time_min: new Date().toISOString(),
    time_max: new Date(Date.now() + 7 * 86400000).toISOString(),
  },
  busy: false,
  pending: false,
  oauthWaiting: false,
  closed: false,
});
const data = <T>(r: { data?: T; response?: Response }): NonNullable<T> => {
  if (!r.response?.ok || r.data == null) throw new RequestFailure(r.response?.status ?? 0);
  return r.data;
};
class CalendarAuthorityLost extends Error {}
function freeze<T>(value: T): T {
  if (value && typeof value === "object") {
    for (const child of Object.values(value)) freeze(child);
    Object.freeze(value);
  }
  return value;
}
/** All private data and exact retries are bound to one live identity/tenant generation. */
export class CalendarController {
  private state = empty();
  private listeners = new Set<() => void>();
  private closed = false;
  private abort = new AbortController();
  private release: () => void;
  private releaseAuthority: () => void;
  private activeRequest?:
    | { integration: string | null | undefined; cancelled: boolean; abort: AbortController }
    | undefined;
  private mutation?: Mutation | undefined;
  private lease?: ReturnType<CalendarOAuth["open"]> | undefined;
  private oauthExpiry = 0;
  private returned = false;
  readonly context: SessionContext;
  readonly surface: "mobile" | "display";
  readonly oauth: CalendarOAuth | undefined;
  constructor(context: SessionContext, surface: "mobile" | "display", oauth?: CalendarOAuth) {
    this.context = context;
    this.surface = surface;
    this.oauth = oauth;
    this.releaseAuthority = observeCalendarAuthority(context, (change) =>
      this.authorityChanged(change),
    );
    this.release = context.own(() => this.dispose());
  }
  snapshot = () => this.state;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  private publish(value: CalendarState) {
    this.state = freeze(value);
    for (const listener of this.listeners) listener();
  }
  dispose() {
    if (this.closed) return;
    this.closed = true;
    this.abort.abort();
    this.releaseAuthority();
    this.mutation = undefined;
    this.lease?.dispose();
    this.lease = undefined;
    this.publish({ ...empty(), closed: true });
    this.release?.();
  }
  private assert() {
    this.context.assertCurrent();
    if (this.closed) throw new StaleContext();
  }
  private mutationIntegration(m = this.mutation): string | undefined {
    if (!m || m.kind === "oauth") return undefined;
    if (m.kind === "proposal") return m.integration;
    return m.kind === "selection" || m.kind === "disconnect"
      ? m.original.id
      : m.original.integration_id;
  }
  private authorityChanged(change: CalendarAuthorityChange) {
    if (this.closed) return;
    if (change.kind === "collection") {
      if (this.activeRequest?.integration === null) {
        this.activeRequest.cancelled = true;
        this.activeRequest.abort.abort();
        this.publish({
          ...this.state,
          busy: false,
          notice: "連携一覧を更新したため、確認内容を取得し直してください。",
        });
      }
      return;
    }
    const { id, current, previous, incompatible } = change;
    const selected = this.state.selected?.id === id;
    const fixed = this.state.proposal?.integration_id === id;
    const denied = current?.status !== "active";
    const proposalLost =
      fixed &&
      (incompatible ||
        denied ||
        !current?.allowed_calendar_ids?.includes(
          this.state.proposal?.normalized_request.calendar_id ?? "",
        ));
    const selectionLost =
      selected &&
      (incompatible ||
        denied ||
        (!!this.state.calendar && !current?.allowed_calendar_ids?.includes(this.state.calendar)));
    const m = this.mutation;
    const mutationCalendar =
      m?.kind === "proposal"
        ? m.body.calendar_id
        : m?.kind === "confirm" || m?.kind === "reject"
          ? m.original.normalized_request.calendar_id
          : undefined;
    const mutationLost =
      this.mutationIntegration() === id &&
      (incompatible ||
        denied ||
        (!!mutationCalendar && !current?.allowed_calendar_ids?.includes(mutationCalendar)));
    const readLost =
      !!this.activeRequest &&
      (this.activeRequest.integration === id || this.activeRequest.integration === null) &&
      (incompatible || denied || proposalLost || selectionLost || mutationLost || !previous);
    if (readLost && this.activeRequest) {
      this.activeRequest.cancelled = true;
      this.activeRequest.abort.abort();
    }
    if (mutationLost) this.mutation = undefined;
    this.publish({
      ...this.state,
      integrations: this.state.integrations.flatMap((v) =>
        v.id === id ? (current ? [current] : []) : [v],
      ),
      ...(selected ? { selected: current } : {}),
      ...(selectionLost ? { events: [], calendar: "", draft: undefined } : {}),
      ...(proposalLost ? { proposal: undefined, operation: undefined } : {}),
      ...(selectionLost || proposalLost || mutationLost || readLost
        ? {
            busy:
              !mutationLost &&
              !readLost &&
              !!(this.mutation || this.activeRequest) &&
              this.state.busy,
            pending: !!this.mutation,
            notice:
              "連携の権限・選択・版が変更されたため、以前の予定と確認内容を閉じました。現在の状態を取得してください。",
          }
        : {}),
    });
  }
  private available() {
    return (
      !this.closed &&
      this.surface === "mobile" &&
      !this.state.busy &&
      !this.mutation &&
      !this.state.oauthWaiting
    );
  }
  private async request<T>(
    work: (client: Client, signal: AbortSignal, tenant: string) => Promise<T>,
    integrationId: string | null | undefined = this.mutationIntegration(),
  ): Promise<T> {
    this.assert();
    const owned = { integration: integrationId, cancelled: false, abort: new AbortController() };
    this.activeRequest = owned;
    const signal = AbortSignal.any([
      this.abort.signal,
      owned.abort.signal,
      this.context.signal,
      AbortSignal.timeout(15000),
    ]);
    let detach = () => {};
    try {
      const aborted = new Promise<never>((_resolve, reject) => {
        const fail = () => reject(new StaleContext());
        signal.addEventListener("abort", fail, { once: true });
        detach = () => signal.removeEventListener("abort", fail);
        if (signal.aborted) fail();
      });
      const result = await Promise.race([
        this.context.run((client, _signal, tenant) => work(client, signal, tenant ?? "")),
        aborted,
      ]);
      this.assert();
      if (owned.cancelled) throw new CalendarAuthorityLost();
      signal.throwIfAborted();
      return result;
    } catch (error) {
      if (owned.cancelled) throw new CalendarAuthorityLost();
      throw error;
    } finally {
      detach();
      if (this.activeRequest === owned) this.activeRequest = undefined;
    }
  }
  private fail(error: unknown) {
    if (this.closed || this.context.signal.aborted || error instanceof CalendarAuthorityLost)
      return;
    const status = error instanceof RequestFailure ? error.status : 0;
    this.publish({
      ...this.state,
      busy: false,
      pending: !!this.mutation,
      notice: this.mutation
        ? "受付結果を確認できません。同じ内容で再確認してください。新しい操作はまだ送信しません。"
        : status === 409
          ? "状態・確認期限・予定の版が変わりました。現在の内容を再取得してください。"
          : status === 503
            ? "Calendarを利用できません。設定または接続を確認してから再取得してください。"
            : "取得または入力を確認できません。内容を確認して再取得してください。",
    });
  }
  private async readIntegration(id: string) {
    const guard = calendarAuthorityGuard(this.context, id);
    const r = await this.request(
      (client, signal, tenant) =>
        getIntegration({ client, signal, path: { tenant_id: tenant, integration_id: id } }),
      id,
    );
    const value = integration(data(r), this.context);
    if (value.id !== id || r.response?.headers.get("ETag") !== `"${value.version}"`)
      throw new Error("Invalid integration version");
    guard();
    publishCalendarAuthority(this.context, id, value);
    return value;
  }
  async load() {
    if (!this.available()) return;
    const publishList = calendarListPublication(this.context);
    this.publish({
      ...this.state,
      busy: true,
      integrations: [],
      selected: undefined,
      draft: undefined,
      selectionDraft: undefined,
      conflict: undefined,
      events: [],
      calendar: "",
      notice: undefined,
    });
    try {
      const items: Integration[] = [],
        ids = new Set<string>(),
        cursors = new Set<string>();
      let cursor: string | undefined;
      for (let n = 0; n < 100; n++) {
        const page = data(
          await this.request((client, signal, tenant) =>
            listIntegrations({
              client,
              signal,
              path: { tenant_id: tenant },
              query: { limit: 100, ...(cursor ? { cursor } : {}) },
            }),
          ),
        );
        if (!Array.isArray(page.items) || page.items.length > 100) throw new Error("Invalid page");
        for (const row of page.items) {
          if (row.provider === "home_assistant") continue;
          const value = integration(row, this.context);
          if (ids.has(value.id)) throw new Error("Duplicate integration");
          ids.add(value.id);
          items.push(value);
        }
        if (page.next_cursor === null) {
          publishList(items);
          this.publish({ ...this.state, integrations: items, busy: false });
          return;
        }
        cursor = this.cursor(page.next_cursor, cursors);
      }
      throw new Error("Too many pages");
    } catch (error) {
      this.fail(error);
    }
  }
  private cursor(value: unknown, seen: Set<string>) {
    if (typeof value !== "string" || !value || value.length > 2048 || seen.has(value))
      throw new Error("Invalid cursor");
    seen.add(value);
    return value;
  }
  async select(id: string) {
    if (!this.available() || !this.state.integrations.some((v) => v.id === id)) return;
    this.publish({
      ...this.state,
      busy: true,
      selected: undefined,
      events: [],
      calendar: "",
      draft: undefined,
      selectionDraft: undefined,
      conflict: undefined,
      notice: undefined,
    });
    try {
      const selected = await this.readIntegration(id);
      this.publish({
        ...this.state,
        selected,
        integrations: this.state.integrations.map((v) => (v.id === selected.id ? selected : v)),
        busy: false,
        selectionDraft: [...(selected.allowed_calendar_ids ?? [])],
      });
    } catch (error) {
      this.fail(error);
    }
  }
  selectionDraft(ids: string[]) {
    if (!this.available() || !this.state.selected) return;
    this.assert();
    this.publish({ ...this.state, selectionDraft: [...ids] });
  }
  async saveSelection() {
    if (!this.available() || !this.state.selected || this.state.conflict) return;
    if (!calendarIds(this.state.selectionDraft)) {
      this.publish({
        ...this.state,
        notice:
          "カレンダーIDは空でないIDを最大10個、重複なく入力してください。全て外す場合は空欄にしてください。",
      });
      return;
    }
    this.mutation = {
      kind: "selection",
      key: crypto.randomUUID(),
      original: this.state.selected,
      body: { allowed_calendar_ids: [...this.state.selectionDraft] },
    };
    await this.retry();
  }
  async disconnect() {
    if (!this.available() || !this.state.selected || this.state.conflict) return;
    this.mutation = { kind: "disconnect", key: crypto.randomUUID(), original: this.state.selected };
    await this.retry();
  }
  async refreshIntegration() {
    const selected = this.state.conflict?.before ?? this.state.selected;
    if (!this.available() || !selected) return;
    this.publish({ ...this.state, busy: true, events: [], calendar: "" });
    try {
      const current = await this.readIntegration(selected.id);
      this.publish({
        ...this.state,
        busy: false,
        selected: current,
        integrations: this.state.integrations.map((v) => (v.id === current.id ? current : v)),
        ...(this.state.conflict ? { conflict: { before: selected, current } } : {}),
      });
    } catch (error) {
      this.fail(error);
    }
  }
  acceptCurrentSelection() {
    if (!this.available() || !this.state.conflict?.current) return;
    this.publish({
      ...this.state,
      selected: this.state.conflict.current,
      conflict: undefined,
      notice: "入力したIDを保持しています。最新版と比較してから保存してください。",
    });
  }
  async events(calendar: string, range = this.state.range) {
    const selected = this.state.selected;
    if (
      !this.available() ||
      !selected ||
      selected.status !== "active" ||
      !selected.allowed_calendar_ids?.includes(calendar)
    )
      return;
    if (
      !validTimestamp(range.time_min) ||
      !validTimestamp(range.time_max) ||
      Date.parse(range.time_max) <= Date.parse(range.time_min) ||
      Date.parse(range.time_max) - Date.parse(range.time_min) > 31 * 86400000
    ) {
      this.publish({
        ...this.state,
        notice: "取得期間は明示オフセット付き日時で、正の31日以内にしてください。",
      });
      return;
    }
    const guard = calendarAuthorityGuard(this.context, selected.id);
    this.publish({
      ...this.state,
      busy: true,
      events: [],
      calendar,
      range: { ...range },
      draft: undefined,
      notice: undefined,
    });
    try {
      const items: CalendarEvent[] = [],
        ids = new Set<string>(),
        cursors = new Set<string>();
      let cursor: string | undefined;
      for (let n = 0; n < 10; n++) {
        const page = data(
          await this.request(
            (client, signal, tenant) =>
              listCalendarEvents({
                client,
                signal,
                path: { tenant_id: tenant, integration_id: selected.id },
                query: {
                  calendar_id: calendar,
                  ...range,
                  limit: 100,
                  ...(cursor ? { cursor } : {}),
                },
              }),
            selected.id,
          ),
        );
        guard();
        if (!Array.isArray(page.items) || page.items.length > 100) throw new Error("Invalid page");
        for (const row of page.items) {
          const value = event(row, calendar);
          if (ids.has(value.id)) throw new Error("Duplicate event");
          ids.add(value.id);
          items.push(value);
        }
        if (page.next_cursor === null) {
          this.publish({ ...this.state, busy: false, events: items });
          return;
        }
        cursor = this.cursor(page.next_cursor, cursors);
      }
      throw new Error("Too many pages");
    } catch (error) {
      this.fail(error);
    }
  }
  edit(action: "create" | "update" | "delete", id?: string) {
    if (
      !this.available() ||
      this.state.selected?.status !== "active" ||
      !this.state.calendar ||
      (this.state.proposal &&
        ["confirmed", "executing", "outcome_unknown"].includes(this.state.proposal.status)) ||
      (this.state.operation &&
        ["pending", "running", "outcome_unknown"].includes(this.state.operation.status))
    )
      return;
    const target = this.state.events.find((v) => v.id === id);
    if (action !== "create" && !target) return;
    const content = {
      calendar_id: this.state.calendar,
      summary: target?.summary ?? "",
      start: target?.start ?? { date_time: this.state.range.time_min, timezone: "Asia/Tokyo" },
      end: target?.end ?? {
        date_time: new Date(Date.parse(this.state.range.time_min) + 3600000).toISOString(),
        timezone: "Asia/Tokyo",
      },
      ...(target?.description !== undefined ? { description: target.description } : {}),
    };
    const draft: CalendarProposalCreate =
      action === "delete" && target
        ? {
            action,
            calendar_id: target.calendar_id,
            event_id: target.id,
            provider_etag: target.etag,
          }
        : action === "update" && target
          ? { ...content, action, event_id: target.id, provider_etag: target.etag }
          : { ...content, action: "create" };
    this.publish({ ...this.state, draft, notice: undefined });
  }
  draft(value: CalendarProposalCreate) {
    if (
      !this.available() ||
      !this.state.draft ||
      value.action !== this.state.draft.action ||
      value.calendar_id !== this.state.draft.calendar_id
    )
      return;
    if (
      value.action !== "create" &&
      this.state.draft.action !== "create" &&
      (value.event_id !== this.state.draft.event_id ||
        value.provider_etag !== this.state.draft.provider_etag)
    )
      return;
    this.publish({ ...this.state, draft: structuredClone(value) });
  }
  closeDraft() {
    if (this.available()) this.publish({ ...this.state, draft: undefined });
  }
  async propose() {
    if (
      !this.available() ||
      !this.state.draft ||
      !this.state.selected ||
      !validChange(this.state.draft)
    ) {
      if (this.available())
        this.publish({
          ...this.state,
          notice: "題名・日時・終日区間・タイムゾーン・対象を確認してください。",
        });
      return;
    }
    this.mutation = {
      kind: "proposal",
      key: crypto.randomUUID(),
      integration: this.state.selected.id,
      body: structuredClone(this.state.draft),
    };
    await this.retry();
  }
  async openProposal(id: string) {
    if (!this.available() || !isId(id)) return;
    const guard = calendarAuthorityGuard(this.context);
    this.publish({
      ...this.state,
      busy: true,
      proposal: undefined,
      operation: undefined,
      draft: undefined,
      notice: undefined,
    });
    try {
      const value = proposal(
        data(
          await this.request(
            (client, signal, tenant) =>
              getCalendarProposal({ client, signal, path: { tenant_id: tenant, proposal_id: id } }),
            null,
          ),
        ),
        id,
      );
      guard(value.integration_id);
      assertCalendarScope(this.context, value.integration_id, value.normalized_request.calendar_id);
      observeCalendarProposal(this.context, value.integration_id);
      this.publish({ ...this.state, busy: false, proposal: value });
      if (value.operation_id) await this.refreshProposal();
    } catch (error) {
      this.fail(error);
    }
  }
  async refreshProposal() {
    const before = this.state.proposal;
    if (!this.available() || !before) return;
    const guard = calendarAuthorityGuard(this.context, before.integration_id);
    this.publish({ ...this.state, busy: true, notice: undefined });
    try {
      const value = proposal(
        data(
          await this.request(
            (client, signal, tenant) =>
              getCalendarProposal({
                client,
                signal,
                path: { tenant_id: tenant, proposal_id: before.proposal_id },
              }),
            before.integration_id,
          ),
        ),
        before.proposal_id,
      );
      if (
        value.args_hash !== before.args_hash ||
        JSON.stringify(value.normalized_request) !== JSON.stringify(before.normalized_request) ||
        value.integration_id !== before.integration_id ||
        value.expires_at !== before.expires_at
      )
        throw new Error("Changed immutable proposal");
      const opId = value.operation_id ?? this.state.operation?.operation_id;
      guard();
      const op = opId
        ? operation(
            data(
              await this.request(
                (client, signal, tenant) =>
                  getTenantOperation({
                    client,
                    signal,
                    path: { tenant_id: tenant, operation_id: opId },
                  }),
                before.integration_id,
              ),
            ),
            opId,
          )
        : undefined;
      guard();
      assertCalendarScope(this.context, value.integration_id, value.normalized_request.calendar_id);
      observeCalendarProposal(this.context, value.integration_id);
      this.publish({ ...this.state, busy: false, proposal: value, operation: op });
    } catch (error) {
      this.fail(error);
    }
  }
  async decide(action: "confirm" | "reject") {
    const original = this.state.proposal;
    if (
      !this.available() ||
      !original ||
      original.status !== "pending" ||
      this.state.operation ||
      this.state.draft
    )
      return;
    if (Date.now() >= Date.parse(original.expires_at)) {
      this.publish({
        ...this.state,
        notice: "確認期限を過ぎています。現在の状態を確認してください。",
      });
      await this.refreshProposal();
      return;
    }
    this.mutation =
      action === "confirm"
        ? {
            kind: action,
            key: crypto.randomUUID(),
            original,
            body: { args_hash: original.args_hash, confirmed: true },
          }
        : { kind: action, key: crypto.randomUUID(), original };
    await this.retry();
  }
  async authorize(scope: "read" | "read_write") {
    if (!this.available() || !this.oauth || !["read", "read_write"].includes(scope)) return;
    this.mutation = {
      kind: "oauth",
      key: crypto.randomUUID(),
      body: { scope_profile: scope, return_target: "mobile" },
    };
    await this.retry();
  }
  finishOAuth() {
    if (this.closed) return;
    this.lease?.dispose();
    this.lease = undefined;
    this.returned = false;
    this.publish({
      ...this.state,
      oauthWaiting: false,
      notice:
        "ブラウザーから戻りました。連携状態を再取得してください。取消や途中終了では接続は完了しません。",
    });
  }
  private async returnedOAuth() {
    if (this.closed || !this.state.oauthWaiting || Date.now() >= this.oauthExpiry) return;
    this.assert();
    this.returned = true;
    if (this.state.busy) return;
    this.finishOAuth();
    await this.load();
  }
  async retry() {
    const m = this.mutation;
    if (!m || this.closed || this.state.busy || this.surface !== "mobile") return;
    const guard = calendarAuthorityGuard(this.context, this.mutationIntegration(m));
    this.publish({ ...this.state, busy: true, pending: true, notice: undefined });
    try {
      const headers = { "Idempotency-Key": m.key };
      if (m.kind === "oauth") {
        if (!this.oauth) throw new Error("OAuth unavailable");
        this.lease?.dispose();
        this.lease = this.oauth.open(
          () => {
            void this.returnedOAuth();
          },
          () => this.finishOAuth(),
        );
        const r = await this.request((client, signal, tenant) =>
          startGoogleCalendarAuthorization({
            client,
            signal,
            path: { tenant_id: tenant },
            body: m.body,
            headers,
          }),
        );
        const value = data(r);
        if (
          r.response?.status !== 201 ||
          !validTimestamp(value.expires_at) ||
          Date.parse(value.expires_at) <= Date.now()
        )
          throw new Error("Invalid authorization");
        this.oauthExpiry = Date.parse(value.expires_at);
        this.returned = false;
        this.publish({ ...this.state, oauthWaiting: true });
        await this.lease.launch(value.authorization_url);
        this.assert();
        this.mutation = undefined;
        this.publish({ ...this.state, busy: false, pending: false });
        if (this.returned) await this.returnedOAuth();
        return;
      }
      if (m.kind === "selection" || m.kind === "disconnect") {
        const r = await this.request((client, signal, tenant) =>
          m.kind === "selection"
            ? updateIntegration({
                client,
                signal,
                path: { tenant_id: tenant, integration_id: m.original.id },
                body: m.body,
                headers: { ...headers, "If-Match": `"${m.original.version}"` },
              })
            : revokeIntegration({
                client,
                signal,
                path: { tenant_id: tenant, integration_id: m.original.id },
                headers: { ...headers, "If-Match": `"${m.original.version}"` },
              }),
        );
        if (!r.response?.ok) throw new RequestFailure(r.response?.status ?? 0);
        const selected =
          r.response.status === 204 && m.kind === "disconnect"
            ? undefined
            : integration(data(r) as Integration, this.context);
        if (
          selected &&
          (selected.id !== m.original.id ||
            r.response.headers.get("ETag") !== `"${selected.version}"` ||
            (m.kind === "disconnect" &&
              (r.response.status !== 202 || selected.status !== "revoked")))
        )
          throw new Error("Invalid integration mutation");
        guard();
        this.mutation = undefined;
        publishCalendarAuthority(this.context, m.original.id, selected);
        this.publish({
          ...this.state,
          busy: false,
          pending: false,
          selected,
          events: [],
          calendar: "",
          draft: undefined,
          conflict: undefined,
          selectionDraft: selected?.allowed_calendar_ids,
          integrations: this.state.integrations
            .map((v) => (v.id === selected?.id ? selected : v))
            .filter((v) => selected || v.id !== m.original.id),
          notice:
            m.kind === "disconnect"
              ? selected
                ? "連携を停止しました。秘密・取得済みデータの消去状況を確認してください。実行済みの予定は取り消されません。"
                : "連携の消去完了を確認しました。"
              : "参照するカレンダーを保存しました。",
        });
        return;
      }
      if (m.kind === "proposal") {
        const r = await this.request((client, signal, tenant) =>
          createCalendarProposal({
            client,
            signal,
            path: { tenant_id: tenant, integration_id: m.integration },
            body: m.body,
            headers,
          }),
        );
        const value = proposal(data(r));
        guard();
        assertCalendarScope(
          this.context,
          value.integration_id,
          value.normalized_request.calendar_id,
        );
        if (
          r.response?.status !== 201 ||
          value.integration_id !== m.integration ||
          value.action !== m.body.action
        )
          throw new Error("Invalid created proposal");
        observeCalendarProposal(this.context, value.integration_id);
        this.mutation = undefined;
        this.publish({
          ...this.state,
          busy: false,
          pending: false,
          draft: undefined,
          proposal: value,
          operation: undefined,
        });
        return;
      }
      if (m.kind === "confirm") {
        const r = await this.request((client, signal, tenant) =>
          confirmCalendarProposal({
            client,
            signal,
            path: { tenant_id: tenant, proposal_id: m.original.proposal_id },
            body: m.body,
            headers,
          }),
        );
        const value = operation(data(r), m.original.proposal_id);
        guard();
        assertCalendarScope(
          this.context,
          m.original.integration_id,
          m.original.normalized_request.calendar_id,
        );
        if (r.response?.status !== 202) throw new Error("Invalid acceptance");
        observeCalendarProposal(this.context, m.original.integration_id);
        this.mutation = undefined;
        this.publish({
          ...this.state,
          busy: false,
          pending: false,
          operation: value,
          notice: "確認を受け付けました。Googleへの書込完了はまだ確認できていません。",
        });
        return;
      }
      const r = await this.request((client, signal, tenant) =>
        rejectCalendarProposal({
          client,
          signal,
          path: { tenant_id: tenant, proposal_id: m.original.proposal_id },
          headers,
        }),
      );
      const value = proposal(data(r), m.original.proposal_id);
      guard();
      assertCalendarScope(this.context, value.integration_id, value.normalized_request.calendar_id);
      if (
        value.args_hash !== m.original.args_hash ||
        value.integration_id !== m.original.integration_id ||
        value.expires_at !== m.original.expires_at ||
        JSON.stringify(value.normalized_request) !== JSON.stringify(m.original.normalized_request)
      )
        throw new Error("Changed proposal");
      observeCalendarProposal(this.context, value.integration_id);
      this.mutation = undefined;
      this.publish({ ...this.state, busy: false, pending: false, proposal: value });
    } catch (error) {
      if (this.closed || this.context.signal.aborted || error instanceof CalendarAuthorityLost)
        return;
      const status = error instanceof RequestFailure ? error.status : 0;
      if (m.kind === "oauth") {
        this.lease?.dispose();
        this.lease = undefined;
        this.publish({ ...this.state, oauthWaiting: false });
      }
      if (status === 412 && (m.kind === "selection" || m.kind === "disconnect")) {
        this.mutation = undefined;
        this.publish({
          ...this.state,
          busy: false,
          pending: false,
          conflict: { before: m.original },
          notice: "連携に別の更新があります。入力と最新版を比較してください。",
        });
        await this.refreshIntegration();
        return;
      }
      if ([400, 409, 410, 422].includes(status)) this.mutation = undefined;
      this.fail(error);
      if ([409, 410].includes(status) && (m.kind === "confirm" || m.kind === "reject"))
        await this.refreshProposal();
    }
  }
}
