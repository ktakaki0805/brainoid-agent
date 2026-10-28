import {
  type Client,
  createTask,
  getTask,
  listTasks,
  type Task,
  type TaskCreate,
  type TaskPatch,
  updateTask,
} from "@brainoid-internal/api-client";
import { isId } from "../chat/events.ts";
import { policies } from "../memories/controller.ts";
import { RequestFailure, type SessionContext, StaleContext } from "../session.ts";

export { policies };
export type TaskScope = "all" | "private" | "shared";
export type TaskFilter = "all" | Task["status"];
export const statuses: Record<Task["status"], string> = {
  open: "未完了",
  done: "完了",
  cancelled: "取消済み",
};
export interface TaskDraft {
  title: string;
  status: Task["status"];
  due_at: string;
  timezone: string;
  assignee_user_id: string;
  visibility: Task["visibility"];
  display_policy: Task["display_policy"];
}
export interface TaskEditor {
  original?: Task;
  etag?: string;
  draft: TaskDraft;
}
export interface TaskState {
  items: readonly Task[];
  scope: TaskScope;
  filter: TaskFilter;
  busy: boolean;
  pending: boolean;
  closed: boolean;
  editor?: TaskEditor | undefined;
  conflict?: { before: Task; current?: Task; etag?: string } | undefined;
  notice?: string | undefined;
}
const empty = (): TaskState => ({
  items: [],
  scope: "all",
  filter: "all",
  busy: false,
  pending: false,
  closed: false,
});
export function validTimezone(value: string): boolean {
  if (!value || value.length > 64 || /^[+-]/.test(value)) return false;
  try {
    new Intl.DateTimeFormat("ja-JP", { timeZone: value }).format();
    return true;
  } catch {
    return false;
  }
}
/** Require an explicit instant; never guess a DST fold or normalize an impossible calendar date. */
export function validTimestamp(value: unknown): value is string {
  if (typeof value !== "string" || value.endsWith("-00:00")) return false;
  const match =
    /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,6})?(?:Z|([+-])(\d{2}):(\d{2}))$/.exec(
      value,
    );
  if (!match) return false;
  const year = Number(match[1]),
    month = Number(match[2]),
    day = Number(match[3]);
  const days = [
    31,
    year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0) ? 29 : 28,
    31,
    30,
    31,
    30,
    31,
    31,
    30,
    31,
    30,
    31,
  ];
  return (
    year >= 1 &&
    month >= 1 &&
    month <= 12 &&
    day >= 1 &&
    day <= (days[month - 1] ?? 0) &&
    Number(match[4]) < 24 &&
    Number(match[5]) < 60 &&
    Number(match[6]) < 60 &&
    (!match[7] || (Number(match[8]) < 24 && Number(match[9]) < 60)) &&
    Number.isFinite(Date.parse(value)) &&
    new Date(value).getUTCFullYear() >= 1 &&
    new Date(value).getUTCFullYear() <= 9999
  );
}
const draftFrom = (task: Task): TaskDraft => ({
  title: task.title,
  status: task.status,
  due_at: task.due_at ?? "",
  timezone: task.timezone ?? "",
  assignee_user_id: task.assignee_user_id ?? "",
  visibility: task.visibility,
  display_policy: task.display_policy,
});
const data = <T>(result: { data?: T; response?: Response }): NonNullable<T> => {
  if (!result.response?.ok || result.data == null)
    throw new RequestFailure(result.response?.status ?? 0);
  return result.data;
};
export function validateDraft(value: TaskDraft, original?: Task): string | undefined {
  if (typeof value.title !== "string" || !value.title.length || [...value.title].length > 200)
    return "題名を1〜200文字で入力してください。";
  if (!Object.hasOwn(statuses, value.status)) return "状態を確認してください。";
  if (typeof value.due_at !== "string" || (value.due_at && !validTimestamp(value.due_at)))
    return "期限は実在する日時とUTCオフセットを秒まで指定してください（例: 2026-10-28T18:00:00+09:00）。";
  if (typeof value.timezone !== "string" || (value.timezone && !validTimezone(value.timezone)))
    return "タイムゾーンはAsia/TokyoなどのIANA名で指定してください。";
  if (typeof value.assignee_user_id !== "string" || [...value.assignee_user_id].length > 128)
    return "担当者のユーザーIDは128文字以内で指定してください。";
  if (
    !["private", "shared"].includes(value.visibility) ||
    !Object.hasOwn(policies, value.display_policy)
  )
    return "公開範囲と表示先を確認してください。";
  if (value.visibility === "private" && value.display_policy === "shared_display")
    return "個人のタスクは共有画面に表示できません。表示先を変更してください。";
  if (
    original &&
    (value.visibility !== original.visibility ||
      value.display_policy !== original.display_policy ||
      value.timezone !== (original.timezone ?? "") ||
      value.assignee_user_id !== (original.assignee_user_id ?? ""))
  )
    return "編集できる項目は題名・状態・期限です。";
  if (!original && value.status !== "open") return "新しいタスクは未完了で作成します。";
  return undefined;
}
export function validateTask(value: Task, context: SessionContext): Task {
  if (
    !value ||
    !isId(value.id) ||
    value.tenant_id !== context.tenantId ||
    !Number.isSafeInteger(value.version) ||
    value.version < 1 ||
    validateDraft(draftFrom(value), value) ||
    !validTimestamp(value.created_at) ||
    !validTimestamp(value.updated_at) ||
    (Object.hasOwn(value, "due_at") && !validTimestamp(value.due_at)) ||
    (Object.hasOwn(value, "timezone") &&
      (typeof value.timezone !== "string" || !validTimezone(value.timezone))) ||
    (Object.hasOwn(value, "assignee_user_id") &&
      (typeof value.assignee_user_id !== "string" || !value.assignee_user_id)) ||
    (value.visibility === "private" && value.owner_user_id !== context.uid)
  )
    throw new Error("Invalid task");
  return value;
}
type Mutation = { key: string } & (
  | { kind: "create"; body: TaskCreate }
  | { kind: "update"; body: TaskPatch; original: Task; etag: string }
);
/** One tenant/identity generation; drafts and exact retries live in memory only. */
export class TasksController {
  private state = empty();
  private listeners = new Set<() => void>();
  private closed = false;
  private abort = new AbortController();
  private release: () => void;
  private mutation?: Mutation | undefined;
  readonly context: SessionContext;
  readonly surface: "mobile" | "display";
  constructor(context: SessionContext, surface: "mobile" | "display") {
    if (!context.tenantId) throw new Error("Tenant required");
    this.context = context;
    this.surface = surface;
    if (surface === "display") this.state.scope = "shared";
    this.release = context.own(() => this.dispose());
  }
  snapshot = () => this.state;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  private publish(state: TaskState) {
    this.state = state;
    for (const listener of this.listeners) listener();
  }
  dispose() {
    if (this.closed) return;
    this.closed = true;
    this.abort.abort();
    this.mutation = undefined;
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
  private visible(task: Task, filter = true) {
    return (
      (this.surface !== "display" ||
        (task.visibility === "shared" && task.display_policy === "shared_display")) &&
      (this.state.scope === "all" || task.visibility === this.state.scope) &&
      (!filter || this.state.filter === "all" || task.status === this.state.filter)
    );
  }
  private async readItems() {
    const items: Task[] = [],
      ids = new Set<string>(),
      cursors = new Set<string>();
    let cursor: string | undefined;
    for (let pageIndex = 0; pageIndex < 100; pageIndex++) {
      const page = await this.request(async (client, signal, tenant) =>
        data(
          await listTasks({
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
        const task = validateTask(value, this.context);
        if (this.state.filter !== "all" && task.status !== this.state.filter)
          throw new Error("Status mismatch");
        if (ids.has(task.id)) throw new Error("Duplicate task");
        ids.add(task.id);
        if (this.visible(task)) items.push(task);
      }
      if (page.next_cursor === null) return items;
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
  async scope(scope: TaskScope) {
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
  async filter(filter: TaskFilter) {
    if (
      this.closed ||
      this.state.busy ||
      this.mutation ||
      this.state.editor ||
      !["all", "open", "done", "cancelled"].includes(filter)
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
          status: "open",
          due_at: "",
          timezone: "",
          assignee_user_id: "",
          visibility: this.state.scope === "shared" ? "shared" : "private",
          display_policy: "never",
        },
      },
    });
  }
  draft(draft: TaskDraft) {
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
      getTask({ client, signal, path: { tenant_id: tenant, task_id: id } }),
    );
    const task = validateTask(data(result), this.context),
      etag = result.response?.headers.get("ETag");
    if (task.id !== id || etag !== `"${task.version}"` || !this.visible(task, false))
      throw new Error("Invalid selected task");
    return { task, etag };
  }
  async select(id: string) {
    if (!this.canSelect() || !this.state.items.some((task) => task.id === id)) return;
    this.publish({ ...this.state, busy: true, notice: undefined, conflict: undefined });
    try {
      const { task, etag } = await this.get(id);
      this.publish({
        ...this.state,
        busy: false,
        items: this.state.items
          .map((row) => (row.id === task.id ? task : row))
          .filter((row) => this.visible(row)),
        editor: { original: task, etag, draft: draftFrom(task) },
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
    const warning = validateDraft(editor.draft, editor.original);
    if (warning || (editor.original && editor.draft.visibility !== editor.original.visibility)) {
      this.publish({ ...this.state, notice: warning ?? "編集では公開範囲を変更できません。" });
      return;
    }
    this.mutation = editor.original
      ? {
          kind: "update",
          key: crypto.randomUUID(),
          original: editor.original,
          etag: editor.etag ?? "",
          body: {
            title: editor.draft.title,
            status: editor.draft.status,
            due_at: editor.draft.due_at || null,
          },
        }
      : {
          kind: "create",
          key: crypto.randomUUID(),
          body: {
            title: editor.draft.title,
            visibility: editor.draft.visibility,
            display_policy: editor.draft.display_policy,
            ...(editor.draft.due_at ? { due_at: editor.draft.due_at } : {}),
            ...(editor.draft.timezone ? { timezone: editor.draft.timezone } : {}),
            ...(editor.draft.assignee_user_id
              ? { assignee_user_id: editor.draft.assignee_user_id }
              : {}),
          },
        };
    await this.retry();
  }
  async refreshConflict() {
    const conflict = this.state.conflict;
    if (!conflict || this.closed || this.state.busy || this.mutation) return;
    this.publish({ ...this.state, busy: true });
    try {
      const { task, etag } = await this.get(conflict.before.id);
      this.publish({
        ...this.state,
        busy: false,
        conflict: { before: conflict.before, current: task, etag },
        items: this.state.items
          .map((row) => (row.id === task.id ? task : row))
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
      editor: { ...editor, original: conflict.current, etag: conflict.etag },
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
          ? createTask({
              client,
              signal,
              path: { tenant_id: tenant },
              body: mutation.body,
              headers: { "Idempotency-Key": mutation.key },
            })
          : updateTask({
              client,
              signal,
              path: { tenant_id: tenant, task_id: mutation.original.id },
              body: mutation.body,
              headers: { "Idempotency-Key": mutation.key, "If-Match": mutation.etag },
            }),
      );
      const task = validateTask(data(result), this.context);
      if (
        result.response?.status !== (mutation.kind === "create" ? 201 : 200) ||
        result.response?.headers.get("ETag") !== `"${task.version}"` ||
        (mutation.kind === "update" &&
          (task.id !== mutation.original.id ||
            task.version <= mutation.original.version ||
            task.visibility !== mutation.original.visibility)) ||
        (mutation.kind === "create" && task.visibility !== mutation.body.visibility)
      )
        throw new Error("Invalid saved task");
      this.mutation = undefined;
      // A replay returns the current canonical version, possibly edited by someone else.
      this.publish({
        ...this.state,
        busy: true,
        pending: false,
        editor: undefined,
        conflict: undefined,
        items: this.visible(task) ? [task] : [],
        notice: `保存の受付を確認しました。現在の状態: ${statuses[task.status]} (${task.status})。一覧の条件に合うタスクを表示しています。`,
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
