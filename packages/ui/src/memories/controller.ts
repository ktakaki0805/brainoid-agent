import {
  type Client,
  type Entity,
  type EntityCreate,
  createEntity,
  type Memory,
  type MemoryCreate,
  type Operation,
  type OperationCreate,
  createMemory,
  reviseMemory,
  getMemory,
  listMemories,
  searchMemories,
  listEntities,
  createTenantOperation,
  getTenantOperation,
} from "@brainoid-internal/api-client";
import { RequestFailure, type SessionContext, StaleContext } from "../session.ts";
import { isId } from "../chat/events.ts";

export const predicates = {
  located_in: "場所",
  prefers_setting: "好みの設定",
  prefers: "好み",
  related_to: "関係",
  has_attribute: "特徴",
};
export const policies = {
  never: "共有画面には表示しない",
  authenticated_session: "本人確認した画面だけ",
  shared_display: "共有画面に表示する",
};
export type ForgetPolicy = "suppress_reuse" | "delete_source";
export interface Editor {
  original?: Memory;
  etag?: string;
  draft: MemoryCreate;
}
export interface MemoryState {
  items: readonly Memory[];
  entities: readonly Entity[];
  busy: boolean;
  query: string;
  editor?: Editor | undefined;
  target?: Memory | undefined;
  conflict?: { before: Memory; current?: Memory } | undefined;
  operations: readonly { targetId: string; operation: Operation }[];
  notice?: string | undefined;
  pending: boolean;
  closed: boolean;
}
const empty = (): MemoryState => ({
  items: [],
  entities: [],
  busy: false,
  query: "",
  operations: [],
  pending: false,
  closed: false,
});
const data = <T>(result: { data?: T; response?: Response }): NonNullable<T> => {
  if (!result.response?.ok || result.data == null)
    throw new RequestFailure(result.response?.status ?? 0);
  return result.data;
};
const date = (value: unknown) =>
  typeof value === "string" &&
  /^\d{4}-\d\d-\d\dT/.test(value) &&
  Number.isFinite(Date.parse(value));
const version = (value: number) => Number.isSafeInteger(value) && value > 0;
export function validateMemory(value: Memory, context: SessionContext): Memory {
  if (
    !value ||
    !isId(value.id) ||
    value.tenant_id !== context.tenantId ||
    !version(value.version) ||
    !isId(value.subject_entity_id) ||
    !Object.hasOwn(predicates, value.predicate) ||
    !["private", "shared"].includes(value.visibility) ||
    !Object.hasOwn(policies, value.display_policy) ||
    (value.visibility === "private" &&
      (value.owner_user_id !== context.uid || value.display_policy === "shared_display")) ||
    !["candidate", "confirmed", "superseded", "retracted"].includes(value.status) ||
    !["user_explicit", "imported", "inferred"].includes(value.origin) ||
    (value.origin === "inferred" && value.status === "confirmed") ||
    value.deletion_epoch !== 0 ||
    !date(value.valid_from) ||
    (value.valid_to != null &&
      (!date(value.valid_to) || Date.parse(value.valid_to) <= Date.parse(value.valid_from))) ||
    !date(value.recorded_at) ||
    !date(value.created_at) ||
    !date(value.updated_at) ||
    (value.supersedes_fact_id !== undefined && !isId(value.supersedes_fact_id)) ||
    !Array.isArray(value.source_refs) ||
    value.source_refs.length > 20 ||
    value.source_refs.some(
      (ref) =>
        !ref ||
        !["message", "note", "import"].includes(ref.kind) ||
        !isId(ref.resource_id) ||
        !version(ref.version),
    ) ||
    (!value.source_refs.length &&
      (typeof value.created_by !== "string" ||
        !value.created_by ||
        value.origin !== "user_explicit" ||
        value.status === "candidate")) ||
    (value.object_entity_id === undefined) === (value.value === undefined) ||
    (value.object_entity_id !== undefined && !isId(value.object_entity_id)) ||
    (value.value !== undefined && !["string", "number", "boolean"].includes(typeof value.value)) ||
    (typeof value.value === "string" && [...value.value].length > 4000) ||
    (typeof value.value === "number" && !Number.isFinite(value.value))
  )
    throw new Error("Invalid memory");
  return value;
}
export function validateDraft(body: MemoryCreate): string | undefined {
  if (body.visibility === "private" && body.display_policy === "shared_display")
    return "個人の記憶は共有画面に表示できません。表示先を変更してください。";
  if (!isId(body.subject_entity_id)) return "記憶の対象を選んでください。";
  if (
    (body.value === undefined) === (body.object_entity_id === undefined) ||
    (body.object_entity_id !== undefined && !isId(body.object_entity_id))
  )
    return "内容または関係先を指定してください。";
  if (typeof body.value === "string" && (!body.value.trim() || [...body.value].length > 4000))
    return "内容を1〜4000文字で入力してください。";
  if (typeof body.value === "number" && !Number.isFinite(body.value))
    return "数値を確認してください。";
  if (body.valid_from !== undefined && !date(body.valid_from))
    return "開始日時を確認してください。";
  if (
    body.valid_to != null &&
    (!date(body.valid_to) ||
      Date.parse(body.valid_to) <= Date.parse(body.valid_from ?? new Date().toISOString()))
  )
    return "終了日時は開始日時より後にしてください。";
  return undefined;
}
type Mutation = { key: string } & (
  | { kind: "entity"; body: EntityCreate }
  | { kind: "save"; body: MemoryCreate; original?: Memory; etag?: string }
  | { kind: "forget"; body: Extract<OperationCreate, { kind: "forget_memory" }>; original: Memory }
);

/** Bound to one SessionContext; no memory data or receipt is persisted in browser storage. */
export class MemoryController {
  private state = empty();
  private listeners = new Set<() => void>();
  private closed = false;
  private readonly abort = new AbortController();
  private mutation?: Mutation | undefined;
  private release: () => void;
  readonly context: SessionContext;
  readonly surface: "mobile" | "display";
  constructor(context: SessionContext, surface: "mobile" | "display") {
    this.context = context;
    this.surface = surface;
    if (!context.tenantId) throw new Error("Tenant required");
    this.release = context.own(() => this.dispose());
  }
  snapshot = () => this.state;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  private publish(state: MemoryState) {
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
      const abort = new Promise<never>((_resolve, reject) => {
        const fail = () => reject(new StaleContext());
        signal.addEventListener("abort", fail, { once: true });
        detach = () => signal.removeEventListener("abort", fail);
        if (signal.aborted) fail();
      });
      const result = await Promise.race([
        this.context.run((client, _signal, tenant) => work(client, signal, tenant ?? "")),
        abort,
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
      notice: [400, 422].includes(status)
        ? "入力または公開範囲がサーバーで拒否されました。内容と表示先を確認してください。"
        : status === 409
          ? "状態が変わりました。再取得して確認してください。"
          : this.mutation
            ? "受付結果を確認できません。同じ内容で再確認してください。"
            : "通信または応答を確認できません。再取得してください。",
    });
  }
  private async pages<T>(
    read: (cursor?: string) => Promise<{ items: T[]; next_cursor: string | null }>,
  ): Promise<T[]> {
    const rows: T[] = [],
      seen = new Set<string>();
    let cursor: string | undefined;
    for (let i = 0; i < 100; i++) {
      const page = await read(cursor);
      if (!Array.isArray(page.items) || page.items.length > 100) throw new Error("Invalid page");
      rows.push(...page.items);
      if (page.next_cursor === null) return rows;
      if (
        typeof page.next_cursor !== "string" ||
        !page.next_cursor ||
        page.next_cursor.length > 2048 ||
        seen.has(page.next_cursor)
      )
        throw new Error("Invalid cursor");
      seen.add(page.next_cursor);
      cursor = page.next_cursor;
    }
    throw new Error("Too many pages");
  }
  private async readItems(query: string) {
    const rows = query
      ? (
          await this.request(async (client, signal, tenant) =>
            data(
              await searchMemories({
                client,
                signal,
                path: { tenant_id: tenant },
                body: { query, limit: 40 },
                headers: { "Idempotency-Key": crypto.randomUUID() },
              }),
            ),
          )
        ).items
      : await this.pages<Memory>((cursor) =>
          this.request(async (client, signal, tenant) =>
            data(
              await listMemories({
                client,
                signal,
                path: { tenant_id: tenant },
                query: { limit: 100, ...(cursor ? { cursor } : {}) },
              }),
            ),
          ),
        );
    const seen = new Set<string>();
    return rows
      .filter(
        (item) =>
          this.surface !== "display" ||
          (item.visibility === "shared" && item.display_policy === "shared_display"),
      )
      .map((item) => {
        const value = validateMemory(item, this.context);
        if (seen.has(item.id)) throw new Error("Duplicate memory");
        seen.add(item.id);
        return value;
      })
      .filter(
        (item) =>
          item.status === "confirmed" &&
          !this.state.operations.some((entry) => entry.targetId === item.id),
      );
  }
  async load(query = this.state.query) {
    if (this.closed || this.state.busy) return;
    this.publish({ ...this.state, busy: true, query, notice: undefined });
    try {
      const allEntities = await this.pages<Entity>((cursor) =>
        this.request(async (client, signal, tenant) =>
          data(
            await listEntities({
              client,
              signal,
              path: { tenant_id: tenant },
              query: { limit: 100, ...(cursor ? { cursor } : {}) },
            }),
          ),
        ),
      );
      const entities = allEntities.filter(
        (entity) =>
          this.surface !== "display" ||
          (entity.visibility === "shared" && entity.display_policy === "shared_display"),
      );
      if (
        entities.some(
          (entity) =>
            entity.tenant_id !== this.context.tenantId ||
            !isId(entity.id) ||
            typeof entity.label !== "string" ||
            (entity.visibility === "private" && entity.owner_user_id !== this.context.uid),
        )
      )
        throw new Error("Invalid entity");
      const items = await this.readItems(query);
      this.assert();
      this.publish({ ...this.state, entities, items, busy: false });
    } catch (error) {
      this.fail(error);
    }
  }
  create() {
    if (this.closed || this.surface !== "mobile" || this.state.busy || this.mutation) return;
    this.assert();
    this.publish({
      ...this.state,
      conflict: undefined,
      target: undefined,
      notice: undefined,
      editor: {
        draft: {
          subject_entity_id: "",
          predicate: "prefers",
          value: "",
          visibility: "private",
          display_policy: "never",
          source_refs: [],
        },
      },
    });
  }
  draft(draft: MemoryCreate) {
    if (this.closed || this.state.busy || this.mutation || !this.state.editor) return;
    this.assert();
    this.publish({ ...this.state, editor: { ...this.state.editor, draft }, notice: undefined });
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
      getMemory({ client, signal, path: { tenant_id: tenant, memory_id: id } }),
    );
    const memory = validateMemory(data(result), this.context),
      etag = result.response?.headers.get("ETag");
    if (memory.id !== id || etag !== `"${memory.version}"`) throw new Error("Invalid memory ETag");
    return { memory, etag };
  }
  async select(id: string, action: "revise" | "forget") {
    if (
      this.closed ||
      this.surface !== "mobile" ||
      this.state.busy ||
      this.mutation ||
      !this.state.items.some((item) => item.id === id)
    )
      return;
    this.publish({
      ...this.state,
      busy: true,
      editor: undefined,
      target: undefined,
      conflict: undefined,
      notice: undefined,
    });
    try {
      const { memory, etag } = await this.get(id);
      if (memory.status !== "confirmed") {
        await this.conflict(memory);
        return;
      }
      const draft: MemoryCreate = {
        subject_entity_id: memory.subject_entity_id,
        predicate: memory.predicate,
        ...(memory.object_entity_id
          ? { object_entity_id: memory.object_entity_id }
          : { value: memory.value ?? "" }),
        qualifiers: memory.qualifiers ?? {},
        visibility: memory.visibility,
        display_policy: memory.display_policy,
        source_refs: [],
        ...(memory.valid_to ? { valid_to: memory.valid_to } : {}),
      };
      this.publish({
        ...this.state,
        busy: false,
        ...(action === "revise"
          ? { editor: { original: memory, etag, draft } }
          : { target: memory }),
      });
    } catch (error) {
      this.fail(error);
    }
  }
  async createSubject(label: string, kind: EntityCreate["kind"]) {
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
    if (
      !label.trim() ||
      [...label.trim()].length > 200 ||
      !["person", "room", "appliance", "activity", "topic"].includes(kind)
    ) {
      this.publish({ ...this.state, notice: "対象の名前と種類を確認してください。" });
      return;
    }
    if (editor.draft.visibility === "private" && editor.draft.display_policy === "shared_display") {
      this.publish({ ...this.state, notice: "個人の対象は共有画面に表示できません。" });
      return;
    }
    this.mutation = {
      kind: "entity",
      key: crypto.randomUUID(),
      body: {
        label: label.trim(),
        kind,
        visibility: editor.draft.visibility,
        display_policy: editor.draft.display_policy,
      },
    };
    await this.retry();
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
    if (warning) {
      this.publish({ ...this.state, notice: warning });
      return;
    }
    if (
      editor.original &&
      (editor.draft.visibility !== editor.original.visibility ||
        editor.draft.display_policy !== editor.original.display_policy)
    ) {
      this.publish({ ...this.state, notice: "訂正では公開範囲と表示先を変更できません。" });
      return;
    }
    this.mutation = {
      kind: "save",
      key: crypto.randomUUID(),
      body: structuredClone(editor.draft),
      ...(editor.original ? { original: editor.original, etag: editor.etag ?? "" } : {}),
    };
    await this.retry();
  }
  async forget(policy: ForgetPolicy) {
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
      kind: "forget",
      key: crypto.randomUUID(),
      original: target,
      body: {
        kind: "forget_memory",
        target_id: target.id,
        expected_version: target.version,
        source_policy: policy,
      },
    };
    await this.retry();
  }
  private async conflict(before: Memory) {
    const { memory } = await this.get(before.id);
    const items = await this.readItems(this.state.query);
    this.assert();
    this.publish({
      ...this.state,
      busy: false,
      pending: false,
      target: undefined,
      items,
      conflict: { before, current: memory },
      notice:
        "別の更新がありました。入力は保存されていません。差分を確認し、一覧から現行の記憶を選び直してください。",
    });
  }
  async retry() {
    const mutation = this.mutation;
    if (!mutation || this.closed || this.state.busy || this.surface !== "mobile") return;
    this.publish({ ...this.state, busy: true, pending: true, notice: undefined });
    try {
      if (mutation.kind === "entity") {
        const entity = data(
          await this.request((client, signal, tenant) =>
            createEntity({
              client,
              signal,
              path: { tenant_id: tenant },
              body: mutation.body,
              headers: { "Idempotency-Key": mutation.key },
            }),
          ),
        );
        if (
          !isId(entity.id) ||
          entity.tenant_id !== this.context.tenantId ||
          entity.label !== mutation.body.label ||
          entity.kind !== mutation.body.kind ||
          entity.visibility !== mutation.body.visibility ||
          entity.display_policy !== mutation.body.display_policy ||
          (entity.visibility === "private" && entity.owner_user_id !== this.context.uid)
        )
          throw new Error("Invalid created entity");
        this.mutation = undefined;
        this.publish({
          ...this.state,
          busy: false,
          pending: false,
          entities: [...this.state.entities.filter((item) => item.id !== entity.id), entity],
          editor: this.state.editor
            ? {
                ...this.state.editor,
                draft: { ...this.state.editor.draft, subject_entity_id: entity.id },
              }
            : undefined,
          notice: "対象を登録しました。記憶の内容を確認して保存してください。",
        });
      } else if (mutation.kind === "save") {
        const result = await this.request(async (client, signal, tenant) =>
          mutation.original
            ? reviseMemory({
                client,
                signal,
                path: { tenant_id: tenant, memory_id: mutation.original.id },
                body: { replacement: mutation.body },
                headers: { "Idempotency-Key": mutation.key, "If-Match": mutation.etag ?? "" },
              })
            : createMemory({
                client,
                signal,
                path: { tenant_id: tenant },
                body: mutation.body,
                headers: { "Idempotency-Key": mutation.key },
              }),
        );
        const memory = validateMemory(data(result), this.context);
        if (
          mutation.original &&
          (memory.id === mutation.original.id || memory.supersedes_fact_id !== mutation.original.id)
        )
          throw new Error("Wrong revision");
        this.mutation = undefined;
        // A replay can return an already-superseded receipt. Never promote it to current.
        this.publish({
          ...this.state,
          busy: false,
          pending: false,
          editor: undefined,
          conflict: undefined,
          items: [
            ...this.state.items.filter(
              (item) => item.id !== memory.id && item.id !== mutation.original?.id,
            ),
            ...(memory.status === "confirmed" ? [memory] : []),
          ],
          notice:
            memory.status === "confirmed"
              ? "記憶を保存しました。"
              : "保存は受け付け済みですが、その記憶は既に訂正または撤回されています。一覧を再取得してください。",
        });
      } else {
        const result = await this.request((client, signal, tenant) =>
          createTenantOperation({
            client,
            signal,
            path: { tenant_id: tenant },
            body: mutation.body,
            headers: { "Idempotency-Key": mutation.key },
          }),
        );
        const operation = this.operation(data(result));
        if (result.response?.status !== 202) throw new Error("Not accepted");
        this.mutation = undefined;
        this.publish({
          ...this.state,
          busy: false,
          pending: false,
          target: undefined,
          items: this.state.items.filter((item) => item.id !== mutation.original.id),
          operations: [...this.state.operations, { targetId: mutation.original.id, operation }],
          notice: "忘却を受け付けました。再利用を停止し、消去処理を進めます。",
        });
      }
    } catch (error) {
      if (this.closed || this.context.signal.aborted) return;
      if (error instanceof RequestFailure && [400, 422, 412].includes(error.status)) {
        this.mutation = undefined;
        if (error.status === 412 && mutation.kind !== "entity" && mutation.original) {
          // Block resubmission even if the canonical read fails.
          this.publish({
            ...this.state,
            pending: false,
            target: undefined,
            conflict: { before: mutation.original },
            items: this.state.items.filter((item) => item.id !== mutation.original?.id),
          });
          try {
            await this.conflict(mutation.original);
          } catch (readError) {
            this.fail(readError);
          }
          return;
        }
      }
      this.fail(error);
    }
  }
  private operation(value: Operation) {
    if (
      !value ||
      !isId(value.operation_id) ||
      value.kind !== "forget_memory" ||
      !["pending", "running", "succeeded", "failed", "outcome_unknown"].includes(value.status) ||
      !date(value.created_at) ||
      !date(value.updated_at)
    )
      throw new Error("Invalid operation");
    return value;
  }
  async refreshOperation(id: string) {
    if (
      this.closed ||
      this.state.busy ||
      !this.state.operations.some((entry) => entry.operation.operation_id === id)
    )
      return;
    this.publish({ ...this.state, busy: true, notice: undefined });
    try {
      const operation = this.operation(
        await this.request(async (client, signal, tenant) =>
          data(
            await getTenantOperation({
              client,
              signal,
              path: { tenant_id: tenant, operation_id: id },
            }),
          ),
        ),
      );
      if (operation.operation_id !== id) throw new Error("Wrong operation");
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
