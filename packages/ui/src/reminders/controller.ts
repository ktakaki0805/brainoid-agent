import {
  type Client,
  createReminder,
  getReminder,
  listReminders,
  type Reminder,
  type ReminderCreate,
  type ReminderPatch,
  type ReminderSnooze,
  snoozeReminder,
  updateReminder,
} from "@brainoid-internal/api-client";
import { isId } from "../chat/events.ts";
import { policies } from "../memories/controller.ts";
import { RequestFailure, type SessionContext, StaleContext } from "../session.ts";

import { observeReminders } from "./canonical.ts";

export { policies };
export type ReminderScope = "all" | "private" | "shared";
export type ReminderFilter = "all" | Reminder["status"];
export const statuses: Record<Reminder["status"], string> = {
  scheduled: "予定済み",
  dispatched: "配信処理済み",
  acknowledged: "本人確認済み",
  expired: "期限切れ",
  cancelled: "取消済み",
};
export interface ReminderDraft {
  title: string;
  status: Reminder["status"];
  due_at: string;
  timezone: string;
  delivery_target: Reminder["delivery_target"];
  task_id: string;
  visibility: Reminder["visibility"];
  display_policy: Reminder["display_policy"];
}
export interface ReminderEditor {
  original?: Reminder;
  etag?: string;
  draft: ReminderDraft;
  action?: "update" | "snooze";
}
export interface ReminderState {
  items: readonly Reminder[];
  scope: ReminderScope;
  filter: ReminderFilter;
  busy: boolean;
  pending: boolean;
  closed: boolean;
  editor?: ReminderEditor | undefined;
  conflict?: { before: Reminder; current?: Reminder; etag?: string } | undefined;
  notice?: string | undefined;
}
const empty = (): ReminderState => ({
  items: [],
  scope: "all",
  filter: "all",
  busy: false,
  pending: false,
  closed: false,
});

import { validTimestamp, validTimezone } from "../tasks/controller.ts";

const draftFrom = (reminder: Reminder): ReminderDraft => ({
  title: reminder.title,
  status: reminder.status,
  due_at: reminder.due_at ?? "",
  timezone: reminder.timezone ?? "",
  delivery_target: { ...reminder.delivery_target },
  task_id: reminder.task_id ?? "",
  visibility: reminder.visibility,
  display_policy: reminder.display_policy,
});
const data = <T>(result: { data?: T; response?: Response }): NonNullable<T> => {
  if (!result.response?.ok || result.data == null)
    throw new RequestFailure(result.response?.status ?? 0);
  return result.data;
};
export function validateDraft(
  value: ReminderDraft,
  original?: Reminder,
  action = "update",
  now = Date.now(),
): string | undefined {
  if (typeof value.title !== "string" || !value.title.length || [...value.title].length > 200)
    return "題名を1〜200文字で入力してください。";
  if (!validTimestamp(value.due_at))
    return "日時はUTCオフセットを秒まで指定してください（例: 2027-01-01T18:00:00+09:00）。";
  if (!validTimezone(value.timezone))
    return "タイムゾーンはAsia/TokyoなどのIANA名で指定してください。";
  if (
    (!original ||
      action === "snooze" ||
      value.due_at !== original.due_at ||
      value.timezone !== original.timezone) &&
    Date.parse(value.due_at) <= now
  )
    return "新しい予定は未来の日時を指定してください。";
  if (
    !value.delivery_target ||
    !["user", "device"].includes(value.delivery_target.kind) ||
    typeof value.delivery_target.id !== "string" ||
    !value.delivery_target.id ||
    [...value.delivery_target.id].length > 128 ||
    (value.delivery_target.kind === "device" && !isId(value.delivery_target.id))
  )
    return "配信先の種類とIDを明示してください。";
  if (typeof value.task_id !== "string" || (value.task_id && !isId(value.task_id)))
    return "関連リマインダーはUUIDで指定してください。";
  if (
    !["private", "shared"].includes(value.visibility) ||
    !Object.hasOwn(policies, value.display_policy)
  )
    return "公開範囲と表示先を確認してください。";
  if (
    value.visibility === "private" &&
    (value.display_policy === "shared_display" ||
      (value.delivery_target.kind === "device" && value.display_policy !== "authenticated_session"))
  )
    return "個人の予定は共有画面に表示できません。端末への配信は本人確認した個人セッションが必要です。";
  if (
    original &&
    (JSON.stringify(value.delivery_target) !== JSON.stringify(original.delivery_target) ||
      value.task_id !== (original.task_id ?? "") ||
      value.visibility !== original.visibility ||
      value.display_policy !== original.display_policy ||
      ![original.status, "cancelled"].includes(value.status))
  )
    return "編集できる項目は題名・日時・タイムゾーン・取消です。";
  if (!original && value.status !== "scheduled")
    return "新しいリマインダーは予定済みで作成します。";
  return undefined;
}
export function validateReminder(value: Reminder, context: SessionContext): Reminder {
  if (
    !value ||
    !isId(value.id) ||
    value.tenant_id !== context.tenantId ||
    !Number.isSafeInteger(value.version) ||
    value.version < 1 ||
    !Number.isSafeInteger(value.schedule_version) ||
    value.schedule_version < 1 ||
    !Object.hasOwn(statuses, value.status) ||
    validateDraft(draftFrom(value), value) ||
    !validTimestamp(value.created_at) ||
    !validTimestamp(value.updated_at) ||
    (value.visibility === "private" && value.owner_user_id !== context.uid)
  )
    throw new Error("Invalid reminder");
  return value;
}
type Mutation = { key: string } & (
  | { kind: "create"; body: ReminderCreate }
  | { kind: "update"; body: ReminderPatch; original: Reminder; etag: string }
  | { kind: "snooze"; body: ReminderSnooze; original: Reminder }
);
/** One tenant/identity generation; drafts and exact retries live in memory only. */
export class RemindersController {
  private state = empty();
  private listeners = new Set<() => void>();
  private closed = false;
  private abort = new AbortController();
  private release: () => void;
  private mutation?: Mutation | undefined;
  private stopObserving: () => void;
  private revision = 0;
  private observations = new Map<string, { value: Reminder; revision: number }>();
  private versions = new Map<string, Reminder>();
  readonly context: SessionContext;
  readonly surface: "mobile" | "display";
  constructor(context: SessionContext, surface: "mobile" | "display") {
    if (!context.tenantId) throw new Error("Tenant required");
    this.context = context;
    this.surface = surface;
    if (surface === "display") this.state.scope = "shared";
    this.release = context.own(() => this.dispose());
    this.stopObserving = observeReminders(context, (value) => this.reconcile(value));
  }
  snapshot = () => this.state;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  private publish(state: ReminderState) {
    this.state = state;
    for (const listener of this.listeners) listener();
  }
  dispose() {
    if (this.closed) return;
    this.closed = true;
    this.abort.abort();
    this.mutation = undefined;
    this.stopObserving?.();
    this.observations.clear();
    this.versions.clear();
    this.publish({ ...empty(), closed: true });
    this.release?.();
  }
  private assert() {
    this.context.assertCurrent();
    if (this.closed) throw new StaleContext();
  }
  private async request<T>(
    work: (client: Client, signal: AbortSignal, tenant: string) => Promise<T>,
  ): Promise<T> {
    this.assert();
    const signal = AbortSignal.any([
      this.abort.signal,
      this.context.signal,
      AbortSignal.timeout(15_000),
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
      signal.throwIfAborted();
      return result;
    } finally {
      detach();
    }
  }
  private fail(error: unknown) {
    if (this.closed || this.context.signal.aborted) return;
    const status = error instanceof RequestFailure ? error.status : 0;
    this.publish({
      ...this.state,
      busy: false,
      pending: !!this.mutation,
      notice: this.mutation
        ? "受付結果を確認できません。同じ内容で再確認してください。"
        : [400, 422].includes(status)
          ? "入力または表示先がサーバーで拒否されました。内容を確認してください。"
          : status === 409
            ? "状態が変わりました。一覧を再取得して確認してください。"
            : "通信または応答を確認できません。もう一度取得してください。",
    });
  }
  private visible(reminder: Reminder, filter = true) {
    return (
      (this.surface !== "display" ||
        (reminder.visibility === "shared" && reminder.display_policy === "shared_display")) &&
      (this.state.scope === "all" || reminder.visibility === this.state.scope) &&
      (!filter || this.state.filter === "all" || reminder.status === this.state.filter)
    );
  }
  private remember(value: Reminder) {
    const known = this.versions.get(value.id);
    if (known && (value.version < known.version || value.schedule_version < known.schedule_version))
      return false;
    this.versions.set(value.id, value);
    return true;
  }
  private latest(value: Reminder): Reminder {
    return this.remember(value) ? value : (this.versions.get(value.id) as Reminder);
  }
  private reconcile(value: Reminder) {
    if (this.closed) return;
    this.assert();
    validateReminder(value, this.context);
    if (!this.remember(value)) return;
    this.observations.set(value.id, { value, revision: ++this.revision });
    const editor = this.state.editor;
    const changedEditor =
      !this.mutation &&
      editor?.original?.id === value.id &&
      editor.original.version < value.version;
    this.publish({
      ...this.state,
      items: [
        ...this.state.items.filter((row) => row.id !== value.id),
        ...(this.visible(value) ? [value] : []),
      ],
      ...(changedEditor
        ? {
            conflict: {
              before: editor.original as Reminder,
              current: value,
              etag: `"${value.version}"`,
            },
            notice:
              "予定の現在状態が更新されました。入力は保持しています。最新版を確認してください。",
          }
        : {}),
    });
  }
  private reconcilePage(items: Reminder[], since: number) {
    const rows = new Map(items.map((value) => [value.id, value]));
    for (const [id, observation] of this.observations) {
      const old = rows.get(id),
        value = observation.value;
      if ((old && value.version > old.version) || (!old && observation.revision > since)) {
        if (value.version < (this.versions.get(id)?.version ?? 0)) continue;
        if (this.visible(value)) rows.set(id, value);
        else rows.delete(id);
      }
    }
    return [...rows.values()].filter((value) => this.visible(value));
  }
  private async readItems() {
    const revision = this.revision;
    const items: Reminder[] = [],
      ids = new Set<string>(),
      cursors = new Set<string>();
    let cursor: string | undefined;
    for (let pageIndex = 0; pageIndex < 100; pageIndex++) {
      const page = await this.request(async (client, signal, tenant) =>
        data(
          await listReminders({
            client,
            signal,
            path: { tenant_id: tenant },
            query: {
              ...(this.state.filter === "all" ? {} : { status: this.state.filter }),
              limit: 100,
              ...(cursor ? { cursor } : {}),
            },
          }),
        ),
      );
      if (!Array.isArray(page.items) || page.items.length > 100) throw new Error("Invalid page");
      for (const value of page.items) {
        const reminder = validateReminder(value, this.context);
        if (this.state.filter !== "all" && reminder.status !== this.state.filter)
          throw new Error("Status mismatch");
        if (ids.has(reminder.id)) throw new Error("Duplicate reminder");
        ids.add(reminder.id);
        const current = this.latest(reminder);
        if (this.visible(current)) items.push(current);
      }
      if (page.next_cursor === null) return this.reconcilePage(items, revision);
      if (
        typeof page.next_cursor !== "string" ||
        !page.next_cursor ||
        page.next_cursor.length > 2048 ||
        cursors.has(page.next_cursor)
      )
        throw new Error("Invalid cursor");
      cursors.add(page.next_cursor);
      cursor = page.next_cursor;
    }
    throw new Error("Too many pages");
  }
  async load() {
    if (this.closed || this.state.busy || this.mutation) return;
    this.publish({ ...this.state, busy: true, items: [], notice: undefined });
    try {
      const items = await this.readItems();
      this.assert();
      this.publish({ ...this.state, items, busy: false });
    } catch (error) {
      this.fail(error);
    }
  }
  async scope(scope: ReminderScope) {
    if (
      this.closed ||
      this.surface !== "mobile" ||
      this.state.busy ||
      this.mutation ||
      this.state.editor ||
      !["all", "shared", "private"].includes(scope)
    )
      return;
    this.assert();
    this.publish({ ...this.state, scope, items: [], conflict: undefined, notice: undefined });
    await this.load();
  }
  async filter(filter: ReminderFilter) {
    if (
      this.closed ||
      this.state.busy ||
      this.mutation ||
      this.state.editor ||
      !["all", ...Object.keys(statuses)].includes(filter)
    )
      return;
    this.assert();
    this.publish({ ...this.state, filter, items: [], conflict: undefined, notice: undefined });
    await this.load();
  }
  private canSelect() {
    return (
      !this.closed &&
      this.surface === "mobile" &&
      !this.state.busy &&
      !this.mutation &&
      !this.state.editor
    );
  }
  create() {
    if (!this.canSelect()) return;
    this.assert();
    this.publish({
      ...this.state,
      conflict: undefined,
      notice: undefined,
      editor: {
        draft: {
          title: "",
          status: "scheduled",
          due_at: "",
          timezone: "",
          delivery_target: { kind: "user", id: this.context.uid },
          task_id: "",
          visibility: this.state.scope === "shared" ? "shared" : "private",
          display_policy: "never",
        },
      },
    });
  }
  draft(draft: ReminderDraft) {
    if (this.closed || this.state.busy || this.mutation || !this.state.editor) return;
    this.assert();
    this.publish({
      ...this.state,
      editor: { ...this.state.editor, draft: structuredClone(draft) },
      notice: undefined,
    });
  }
  closeEditor() {
    if (this.closed || this.state.busy || this.mutation) return;
    this.publish({
      ...this.state,
      editor: undefined,
      conflict: undefined,
      notice: undefined,
    });
  }
  private async get(id: string) {
    const result = await this.request((client, signal, tenant) =>
      getReminder({ client, signal, path: { tenant_id: tenant, reminder_id: id } }),
    );
    const reminder = validateReminder(data(result), this.context),
      etag = result.response?.headers.get("ETag");
    if (reminder.id !== id || etag !== `"${reminder.version}"` || !this.visible(reminder, false))
      throw new Error("Invalid selected reminder");
    const current = this.latest(reminder);
    return { reminder: current, etag: `"${current.version}"` };
  }
  async select(id: string, action: "update" | "snooze" = "update") {
    if (!this.canSelect() || !this.state.items.some((reminder) => reminder.id === id)) return;
    this.publish({ ...this.state, busy: true, notice: undefined, conflict: undefined });
    try {
      const { reminder, etag } = await this.get(id);
      this.publish({
        ...this.state,
        busy: false,
        items: this.state.items
          .map((row) => (row.id === reminder.id ? reminder : row))
          .filter((row) => this.visible(row)),
        editor: { original: reminder, etag, draft: draftFrom(reminder), action },
      });
    } catch (error) {
      this.fail(error);
    }
  }
  async save() {
    const editor = this.state.editor;
    if (
      this.closed ||
      this.surface !== "mobile" ||
      this.state.busy ||
      this.mutation ||
      !editor ||
      this.state.conflict
    )
      return;
    const warning = validateDraft(editor.draft, editor.original, editor.action);
    if (warning || (editor.original && editor.draft.visibility !== editor.original.visibility)) {
      this.publish({ ...this.state, notice: warning ?? "編集では公開範囲を変更できません。" });
      return;
    }
    const draft = editor.draft;
    if (
      draft.visibility === "private" &&
      draft.delivery_target.kind === "user" &&
      draft.delivery_target.id !== this.context.uid
    ) {
      this.publish({ ...this.state, notice: "個人の配信先は自分のユーザーIDを指定してください。" });
      return;
    }
    this.mutation = editor.original
      ? editor.action === "snooze"
        ? {
            kind: "snooze",
            key: crypto.randomUUID(),
            original: editor.original,
            body: {
              due_at: draft.due_at,
              timezone: draft.timezone,
              expected_version: editor.original.version,
            },
          }
        : {
            kind: "update",
            key: crypto.randomUUID(),
            original: editor.original,
            etag: editor.etag ?? "",
            body: {
              title: draft.title,
              ...(draft.due_at !== editor.original.due_at ? { due_at: draft.due_at } : {}),
              ...(draft.timezone !== editor.original.timezone ? { timezone: draft.timezone } : {}),
              ...(draft.status === "cancelled" ? { status: "cancelled" as const } : {}),
            },
          }
      : {
          kind: "create",
          key: crypto.randomUUID(),
          body: {
            title: draft.title,
            due_at: draft.due_at,
            timezone: draft.timezone,
            delivery_target: { ...draft.delivery_target },
            visibility: draft.visibility,
            display_policy: draft.display_policy,
            ...(draft.task_id ? { task_id: draft.task_id } : {}),
          },
        };
    await this.retry();
  }
  async refreshConflict() {
    const conflict = this.state.conflict;
    if (!conflict || this.closed || this.state.busy || this.mutation) return;
    this.publish({ ...this.state, busy: true });
    try {
      const { reminder, etag } = await this.get(conflict.before.id);
      this.publish({
        ...this.state,
        busy: false,
        conflict: { before: conflict.before, current: reminder, etag },
        items: this.state.items
          .map((row) => (row.id === reminder.id ? reminder : row))
          .filter((row) => this.visible(row)),
      });
    } catch (error) {
      this.fail(error);
    }
  }
  continueEditing() {
    const { conflict, editor } = this.state;
    if (
      this.closed ||
      this.state.busy ||
      this.mutation ||
      !conflict?.current ||
      !conflict.etag ||
      !editor
    )
      return;
    this.assert();
    this.publish({
      ...this.state,
      editor: {
        ...editor,
        original: conflict.current,
        etag: conflict.etag,
        draft: {
          ...editor.draft,
          status: editor.draft.status === "cancelled" ? "cancelled" : conflict.current.status,
        },
      },
      conflict: undefined,
      notice: "入力を保持しています。最新版との差分を反映してから保存してください。",
    });
  }
  async retry() {
    const mutation = this.mutation;
    if (!mutation || this.closed || this.state.busy || this.surface !== "mobile") return;
    this.publish({ ...this.state, busy: true, pending: true, notice: undefined });
    try {
      const result = await this.request((client, signal, tenant) =>
        mutation.kind === "create"
          ? createReminder({
              client,
              signal,
              path: { tenant_id: tenant },
              body: mutation.body,
              headers: { "Idempotency-Key": mutation.key },
            })
          : mutation.kind === "snooze"
            ? snoozeReminder({
                client,
                signal,
                path: { tenant_id: tenant, reminder_id: mutation.original.id },
                body: mutation.body,
                headers: { "Idempotency-Key": mutation.key },
              })
            : updateReminder({
                client,
                signal,
                path: { tenant_id: tenant, reminder_id: mutation.original.id },
                body: mutation.body,
                headers: { "Idempotency-Key": mutation.key, "If-Match": mutation.etag },
              }),
      );
      let reminder = validateReminder(data(result), this.context);
      if (
        result.response?.status !== (mutation.kind === "create" ? 201 : 200) ||
        result.response?.headers.get("ETag") !== `"${reminder.version}"` ||
        (mutation.kind !== "create" &&
          (reminder.id !== mutation.original.id ||
            reminder.version <= mutation.original.version ||
            reminder.schedule_version < mutation.original.schedule_version ||
            (mutation.kind === "snooze" &&
              reminder.schedule_version === mutation.original.schedule_version) ||
            reminder.visibility !== mutation.original.visibility)) ||
        (mutation.kind === "create" && reminder.visibility !== mutation.body.visibility)
      )
        throw new Error("Invalid saved reminder");
      reminder = this.latest(reminder);
      this.mutation = undefined;
      // A replay returns the current canonical version, possibly edited by someone else.
      this.publish({
        ...this.state,
        busy: true,
        pending: false,
        editor: undefined,
        conflict: undefined,
        items: this.visible(reminder) ? [reminder] : [],
        notice: `保存の受付を確認しました。現在の状態: ${statuses[reminder.status]} (${reminder.status})。一覧の条件に合うリマインダーを表示しています。`,
      });
      try {
        const items = await this.readItems();
        this.assert();
        this.publish({ ...this.state, busy: false, items });
      } catch {
        if (!this.closed && !this.context.signal.aborted)
          this.publish({
            ...this.state,
            busy: false,
            notice:
              "保存の受付を確認しました。一覧全体は取得できていません。一覧を再取得してください。",
          });
      }
    } catch (error) {
      if (this.closed || this.context.signal.aborted) return;
      const status = error instanceof RequestFailure ? error.status : 0;
      if (status === 412 && mutation.kind !== "create") {
        this.mutation = undefined;
        this.publish({
          ...this.state,
          busy: false,
          pending: false,
          conflict: { before: mutation.original },
          notice: "別の更新がありました。入力は保持しています。最新版を確認してください。",
        });
        await this.refreshConflict();
      } else {
        if ([400, 422].includes(status)) this.mutation = undefined;
        this.fail(error);
      }
    }
  }
}
