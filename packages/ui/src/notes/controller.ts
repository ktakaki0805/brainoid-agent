import {
  type Client,
  createNote,
  createTenantOperation,
  getNote,
  getTenantOperation,
  listNotes,
  type Note,
  type NoteCreate,
  type NotePatch,
  type Operation,
  type OperationCreate,
  updateNote,
} from "@brainoid-internal/api-client";
import { isId } from "../chat/events.ts";
import { policies } from "../memories/controller.ts";
import { RequestFailure, type SessionContext, StaleContext } from "../session.ts";

export { policies };
export type NoteScope = "all" | "private" | "shared";
export interface NoteEditor {
  original?: Note;
  etag?: string;
  draft: NoteCreate;
}
export interface NoteState {
  items: readonly Note[];
  scope: NoteScope;
  query: string;
  busy: boolean;
  pending: boolean;
  closed: boolean;
  editor?: NoteEditor | undefined;
  target?: Note | undefined;
  conflict?: { before: Note; current?: Note; etag?: string } | undefined;
  operations: readonly { targetId: string; operation: Operation }[];
  notice?: string | undefined;
}
const empty = (): NoteState => ({
  items: [],
  scope: "all",
  query: "",
  busy: false,
  pending: false,
  closed: false,
  operations: [],
});
const date = (value: unknown) =>
  typeof value === "string" &&
  /^\d{4}-\d\d-\d\dT/.test(value) &&
  Number.isFinite(Date.parse(value));
const data = <T>(result: { data?: T; response?: Response }): NonNullable<T> => {
  if (!result.response?.ok || result.data == null)
    throw new RequestFailure(result.response?.status ?? 0);
  return result.data;
};
export function validateDraft(value: NoteCreate): string | undefined {
  if (typeof value.title !== "string" || !value.title.length || [...value.title].length > 200)
    return "題名を1〜200文字で入力してください。";
  if (typeof value.body !== "string" || [...value.body].length > 8000)
    return "本文は8000文字以内で入力してください。";
  if (
    !["private", "shared"].includes(value.visibility) ||
    !Object.hasOwn(policies, value.display_policy)
  )
    return "公開範囲と表示先を確認してください。";
  if (value.visibility === "private" && value.display_policy === "shared_display")
    return "個人のノートは共有画面に表示できません。表示先を変更してください。";
  return undefined;
}
export function validateNote(value: Note, context: SessionContext): Note {
  if (
    !value ||
    !isId(value.id) ||
    value.tenant_id !== context.tenantId ||
    !Number.isSafeInteger(value.version) ||
    value.version < 1 ||
    validateDraft(value) ||
    !date(value.created_at) ||
    !date(value.updated_at) ||
    (value.visibility === "private" && value.owner_user_id !== context.uid)
  )
    throw new Error("Invalid note");
  return value;
}
type Mutation = { key: string } & (
  | { kind: "create"; body: NoteCreate }
  | { kind: "update"; body: NotePatch; original: Note; etag: string }
  | { kind: "delete"; body: Extract<OperationCreate, { kind: "delete_resource" }>; original: Note }
);
/** One tenant/identity generation; drafts and exact retries live in memory only. */
export class NotesController {
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
  private publish(state: NoteState) {
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
  private visible(note: Note) {
    return (
      (this.surface !== "display" ||
        (note.visibility === "shared" && note.display_policy === "shared_display")) &&
      (this.state.scope === "all" || note.visibility === this.state.scope) &&
      !this.state.operations.some((entry) => entry.targetId === note.id)
    );
  }
  private async readItems(query: string) {
    const items: Note[] = [],
      ids = new Set<string>(),
      cursors = new Set<string>();
    let cursor: string | undefined;
    for (let pageIndex = 0; pageIndex < 100; pageIndex++) {
      const page = await this.request(async (client, signal, tenant) =>
        data(
          await listNotes({
            client,
            signal,
            path: { tenant_id: tenant },
            query: { query, limit: 100, ...(cursor ? { cursor } : {}) },
          }),
        ),
      );
      if (!Array.isArray(page.items) || page.items.length > 100) throw new Error("Invalid page");
      for (const value of page.items) {
        const note = validateNote(value, this.context);
        if (ids.has(note.id)) throw new Error("Duplicate note");
        ids.add(note.id);
        if (this.visible(note)) items.push(note);
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
  async load(query = this.state.query) {
    if (this.closed || this.state.busy || this.mutation) return;
    if ([...query].length > 200) {
      this.publish({ ...this.state, notice: "検索は200文字以内で入力してください。" });
      return;
    }
    this.publish({ ...this.state, busy: true, items: [], query, notice: undefined });
    try {
      const items = await this.readItems(query);
      this.assert();
      this.publish({ ...this.state, items, busy: false });
    } catch (error) {
      this.fail(error);
    }
  }
  async scope(scope: NoteScope) {
    if (
      this.closed ||
      this.surface !== "mobile" ||
      this.state.busy ||
      this.mutation ||
      this.state.editor ||
      this.state.target ||
      !["all", "shared", "private"].includes(scope)
    )
      return;
    this.assert();
    this.publish({ ...this.state, scope, items: [], conflict: undefined, notice: undefined });
    await this.load();
  }
  private canSelect() {
    return (
      !this.closed &&
      this.surface === "mobile" &&
      !this.state.busy &&
      !this.mutation &&
      !this.state.editor &&
      !this.state.target
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
          body: "",
          visibility: this.state.scope === "shared" ? "shared" : "private",
          display_policy: "never",
        },
      },
    });
  }
  draft(draft: NoteCreate) {
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
      target: undefined,
      conflict: undefined,
      notice: undefined,
    });
  }
  private async get(id: string) {
    const result = await this.request((client, signal, tenant) =>
      getNote({ client, signal, path: { tenant_id: tenant, note_id: id } }),
    );
    const note = validateNote(data(result), this.context),
      etag = result.response?.headers.get("ETag");
    if (note.id !== id || etag !== `"${note.version}"` || !this.visible(note))
      throw new Error("Invalid selected note");
    return { note, etag };
  }
  async select(id: string, action: "edit" | "delete") {
    if (!this.canSelect() || !this.state.items.some((note) => note.id === id)) return;
    this.publish({ ...this.state, busy: true, notice: undefined, conflict: undefined });
    try {
      const { note, etag } = await this.get(id);
      this.publish({
        ...this.state,
        busy: false,
        ...(action === "edit"
          ? {
              editor: {
                original: note,
                etag,
                draft: {
                  title: note.title,
                  body: note.body,
                  visibility: note.visibility,
                  display_policy: note.display_policy,
                },
              },
            }
          : { target: note }),
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
    const warning = validateDraft(editor.draft);
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
            body: editor.draft.body,
            display_policy: editor.draft.display_policy,
          },
        }
      : { kind: "create", key: crypto.randomUUID(), body: structuredClone(editor.draft) };
    await this.retry();
  }
  async remove(includeDerived: boolean) {
    const target = this.state.target;
    if (
      this.closed ||
      this.surface !== "mobile" ||
      this.state.busy ||
      this.mutation ||
      !target ||
      this.state.conflict
    )
      return;
    this.mutation = {
      kind: "delete",
      key: crypto.randomUUID(),
      original: target,
      body: {
        kind: "delete_resource",
        target_type: "note",
        target_id: target.id,
        expected_version: target.version,
        include_derived_memories: includeDerived,
      },
    };
    await this.retry();
  }
  async refreshConflict() {
    const conflict = this.state.conflict;
    if (!conflict || this.closed || this.state.busy || this.mutation) return;
    this.publish({ ...this.state, busy: true });
    try {
      const { note, etag } = await this.get(conflict.before.id);
      this.publish({
        ...this.state,
        busy: false,
        conflict: { before: conflict.before, current: note, etag },
        items: this.state.items.map((row) => (row.id === note.id ? note : row)),
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
  private operation(value: Operation, expectedId?: string) {
    if (
      !value ||
      !isId(value.operation_id) ||
      (expectedId && value.operation_id !== expectedId) ||
      value.kind !== "delete_resource" ||
      !["pending", "running", "succeeded", "failed", "outcome_unknown"].includes(value.status) ||
      !date(value.created_at) ||
      !date(value.updated_at)
    )
      throw new Error("Invalid note deletion operation");
    return value;
  }
  async retry() {
    const mutation = this.mutation;
    if (!mutation || this.closed || this.state.busy || this.surface !== "mobile") return;
    this.publish({ ...this.state, busy: true, pending: true, notice: undefined });
    try {
      if (mutation.kind === "delete") {
        const result = await this.request((client, signal, tenant) =>
          createTenantOperation({
            client,
            signal,
            path: { tenant_id: tenant },
            body: mutation.body,
            headers: { "Idempotency-Key": mutation.key },
          }),
        );
        const accepted = data(result);
        if (result.response?.status !== 202) throw new Error("Unexpected deletion acceptance");
        const operation = this.operation(accepted);
        if (
          this.state.operations.some(
            (entry) => entry.operation.operation_id === operation.operation_id,
          )
        )
          throw new Error("Reused operation ID");
        this.mutation = undefined;
        this.publish({
          ...this.state,
          busy: false,
          pending: false,
          target: undefined,
          editor: undefined,
          conflict: undefined,
          items: this.state.items.filter((note) => note.id !== mutation.original.id),
          operations: [...this.state.operations, { targetId: mutation.original.id, operation }],
          notice: "削除を受け付けました。通常の一覧から外し、消去状況を確認できます。",
        });
      } else {
        const result = await this.request((client, signal, tenant) =>
          mutation.kind === "create"
            ? createNote({
                client,
                signal,
                path: { tenant_id: tenant },
                body: mutation.body,
                headers: { "Idempotency-Key": mutation.key },
              })
            : updateNote({
                client,
                signal,
                path: { tenant_id: tenant, note_id: mutation.original.id },
                body: mutation.body,
                headers: { "Idempotency-Key": mutation.key, "If-Match": mutation.etag },
              }),
        );
        const note = validateNote(data(result), this.context);
        if (
          result.response?.headers.get("ETag") !== `"${note.version}"` ||
          (mutation.kind === "update" &&
            (note.id !== mutation.original.id ||
              note.version <= mutation.original.version ||
              note.visibility !== mutation.original.visibility)) ||
          (mutation.kind === "create" && note.visibility !== mutation.body.visibility)
        )
          throw new Error("Invalid saved note");
        this.mutation = undefined;
        // A replay returns the current canonical version, possibly edited by someone else.
        this.publish({
          ...this.state,
          busy: true,
          pending: false,
          editor: undefined,
          conflict: undefined,
          query: "",
          items: this.visible(note) ? [note] : [],
          notice: "保存の受付を確認しました。現在のノートを表示しています。",
        });
        try {
          const items = await this.readItems("");
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
          target: undefined,
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
  async refreshOperation(id: string) {
    if (
      this.closed ||
      this.state.busy ||
      this.mutation ||
      !this.state.operations.some((entry) => entry.operation.operation_id === id)
    )
      return;
    this.publish({ ...this.state, busy: true, notice: undefined });
    try {
      const operation = this.operation(
        data(
          await this.request((client, signal, tenant) =>
            getTenantOperation({ client, signal, path: { tenant_id: tenant, operation_id: id } }),
          ),
        ),
        id,
      );
      this.publish({
        ...this.state,
        busy: false,
        operations: this.state.operations.map((entry) =>
          entry.operation.operation_id === id ? { ...entry, operation } : entry,
        ),
      });
    } catch (error) {
      this.fail(error);
    }
  }
}
