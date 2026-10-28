import {
  createAccountOperation,
  createTenantOperation,
  downloadExport,
  getAccountOperation,
  getTenantOperation,
  getConversation,
  listConversations,
  type Operation,
  type Conversation,
  type OperationCreate,
  type CreateAccountOperationData,
} from "@brainoid-internal/api-client";
import {
  RequestFailure,
  StaleContext,
  type SessionContext,
  type SessionController,
} from "../session.ts";

export interface ExportDelivery {
  readonly available: boolean;
  save(blob: Blob, signal: AbortSignal): Promise<void>;
}
type Choice = "export" | "leave_tenant" | "delete_tenant" | "delete_account" | "conversation";
type Request = {
  key: string;
  tenantId?: string | undefined;
  body: OperationCreate | CreateAccountOperationData["body"];
  label: string;
};
export interface DataControlsState {
  review?: Request | undefined;
  operation?: Operation | undefined;
  label?: string | undefined;
  conversations: readonly Conversation[];
  cursor?: string | null | undefined;
  busy: boolean;
  pending: boolean;
  reauthentication: boolean;
  notice?: string | undefined;
  rejected?: boolean | undefined;
}
const empty = (): DataControlsState => ({
  conversations: [],
  busy: false,
  pending: false,
  reauthentication: false,
});
const idPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
function immutable<T>(value: T): T {
  if (value && typeof value === "object") {
    Object.freeze(value);
    for (const child of Object.values(value)) immutable(child);
  }
  return value;
}
function operation(value: unknown, kind: Operation["kind"], id?: string): Operation {
  const result = value as Operation;
  if (
    !result ||
    !idPattern.test(result.operation_id) ||
    (id && id !== result.operation_id) ||
    result.kind !== kind ||
    !["pending", "running", "succeeded", "failed", "outcome_unknown"].includes(result.status) ||
    !Number.isFinite(Date.parse(result.created_at)) ||
    !Number.isFinite(Date.parse(result.updated_at)) ||
    (result.download_expires_at !== undefined &&
      !Number.isFinite(Date.parse(result.download_expires_at)))
  )
    throw new Error("Invalid operation");
  return result;
}
/** Only an immutable receipt survives disposal of ordinary tenant caches. Never persisted. */
export class DataControlsController {
  private state = empty();
  private listeners = new Set<() => void>();
  private request: Request | undefined;
  private uncertain = false;
  private selected: string | undefined;
  private tenantContext: SessionContext | undefined;
  private abort = new AbortController();
  private epoch = 0;
  private closed = false;
  private stop: () => void;
  private release: () => void;
  readonly scope: SessionContext;
  readonly session: SessionController;
  readonly delivery: ExportDelivery;
  constructor(session: SessionController, delivery: ExportDelivery) {
    this.session = session;
    this.delivery = delivery;
    const scope = session.globalContext();
    if (!scope) throw new StaleContext();
    this.scope = scope;
    this.selected = session.selectedTenantId();
    this.tenantContext = session.snapshot().context;
    this.stop = session.subscribe(() => this.sync());
    this.release = scope.own(() => this.dispose());
  }
  snapshot = () => this.state;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  private publish(state: DataControlsState) {
    this.state = immutable(state);
    for (const listener of this.listeners) listener();
  }
  private sync() {
    if (this.closed) return;
    const selected = this.session.selectedTenantId(),
      context = this.session.snapshot().context;
    if (selected !== this.selected) {
      this.epoch++;
      this.abort.abort();
      this.abort = new AbortController();
      this.request = undefined;
      this.selected = selected;
      this.tenantContext = context;
      this.publish(empty());
    } else if (context !== this.tenantContext || this.tenantContext?.signal.aborted) {
      this.tenantContext = context;
      const keep = this.request && this.request.body.kind !== "export";
      if (!keep) {
        this.epoch++;
        this.abort.abort();
        this.abort = new AbortController();
        this.request = undefined;
      }
      this.publish({
        ...empty(),
        ...(keep
          ? {
              operation: this.state.operation,
              label: this.state.label,
              pending: this.state.pending,
              busy: this.state.busy,
              reauthentication: this.state.reauthentication,
              notice: this.state.notice,
            }
          : {}),
      });
    }
  }
  dispose() {
    if (this.closed) return;
    this.closed = true;
    this.epoch++;
    this.abort.abort();
    this.request = undefined;
    this.stop?.();
    this.release?.();
    this.publish(empty());
  }
  private live(epoch: number) {
    return !this.closed && epoch === this.epoch && !this.scope.signal.aborted;
  }
  private guard(epoch: number) {
    this.scope.assertCurrent();
    if (!this.live(epoch)) throw new StaleContext();
  }
  private context() {
    const { context, tenant } = this.session.snapshot();
    if (!context || !tenant || tenant.id !== this.selected || tenant.status !== "active")
      throw new StaleContext();
    context.assertCurrent();
    return { context, tenant };
  }
  async loadConversations(more = false) {
    if (this.closed || this.state.busy || this.request) return;
    const epoch = this.epoch;
    this.publish({ ...this.state, busy: true, notice: undefined });
    try {
      const { context, tenant } = this.context();
      const cursor = more ? this.state.cursor : undefined;
      const result = await context.run((client, signal) =>
        listConversations({
          client,
          signal,
          path: { tenant_id: tenant.id },
          query: { limit: 100, ...(cursor ? { cursor } : {}) },
        }),
      );
      this.guard(epoch);
      if (
        !result.data ||
        !result.response?.ok ||
        result.data.items.some((v) => v.tenant_id !== tenant.id || !idPattern.test(v.id))
      )
        throw new Error("Invalid conversations");
      this.publish({
        ...this.state,
        conversations: [...(more ? this.state.conversations : []), ...result.data.items],
        cursor: result.data.next_cursor,
      });
    } catch {
      if (this.live(epoch))
        this.publish({
          ...this.state,
          conversations: [],
          cursor: undefined,
          notice: "会話一覧を取得できません。所属を確認してください。",
        });
    } finally {
      if (this.live(epoch)) this.publish({ ...this.state, busy: false });
    }
  }
  async prepare(
    choice: Choice,
    options: {
      scope?: "my_data" | "visible_tenant_data";
      name?: string;
      conversation?: string;
      derived?: boolean;
    } = {},
  ) {
    if (this.closed || this.state.busy || this.request) return;
    const epoch = this.epoch;
    this.publish({
      ...this.state,
      busy: true,
      review: undefined,
      operation: undefined,
      notice: undefined,
    });
    try {
      this.guard(epoch);
      let body: OperationCreate | CreateAccountOperationData["body"],
        tenantId: string | undefined,
        label: string;
      if (choice === "delete_account") {
        body = { kind: "delete_account", confirm: true };
        label = "現在サインインしている本人のアカウント（すべての所属）";
      } else {
        const { context, tenant } = this.context();
        tenantId = tenant.id;
        label = tenant.name;
        if (choice === "export") {
          body = {
            kind: "export",
            scope: options.scope === "visible_tenant_data" ? "visible_tenant_data" : "my_data",
          };
          label += body.scope === "my_data" ? "：本人のデータ" : "：現在閲覧できる共有データ";
        } else if (choice === "leave_tenant") body = { kind: "leave_tenant" };
        else if (choice === "delete_tenant") {
          if (tenant.my_role !== "owner" || options.name !== tenant.name)
            throw new Error("Exact owner confirmation required");
          body = { kind: "delete_tenant", confirmation_name: tenant.name };
        } else {
          if (!this.state.conversations.some((v) => v.id === options.conversation))
            throw new Error("Select a current conversation");
          const result = await context.run((client, signal) =>
            getConversation({
              client,
              signal,
              path: { tenant_id: tenant.id, conversation_id: options.conversation as string },
            }),
          );
          this.guard(epoch);
          if (
            !result.response?.ok ||
            !result.data ||
            result.data.id !== options.conversation ||
            result.data.tenant_id !== tenant.id
          )
            throw new Error("Invalid conversation");
          body = {
            kind: "delete_resource",
            target_type: "conversation",
            target_id: result.data.id,
            expected_version: result.data.version,
            include_derived_memories: options.derived !== false,
          };
          label += `：会話 ${result.data.title ?? result.data.id}`;
        }
      }
      this.publish({ ...this.state, review: { key: crypto.randomUUID(), tenantId, body, label } });
    } catch {
      if (this.live(epoch))
        this.publish({
          ...this.state,
          review: undefined,
          notice: "対象と現在の権限を確認してください。要求は送信していません。",
        });
    } finally {
      if (this.live(epoch)) this.publish({ ...this.state, busy: false });
    }
  }
  cancel() {
    if (!this.state.busy && !this.request) this.publish({ ...this.state, review: undefined });
  }
  async resetRejected() {
    if (this.state.busy || !this.state.rejected || this.uncertain || this.state.operation) return;
    this.request = undefined;
    this.publish(empty());
    if (this.selected) await this.session.selectTenant(this.selected, false);
  }
  finish() {
    if (
      this.state.busy ||
      this.state.pending ||
      !this.state.operation ||
      this.request?.body.kind === "delete_account"
    )
      return;
    if (!["succeeded", "failed"].includes(this.state.operation.status)) return;
    this.request = undefined;
    this.epoch++;
    this.abort.abort();
    this.abort = new AbortController();
    this.publish(empty());
  }
  async confirm() {
    if (!this.state.review || this.state.busy || this.request) return;
    this.request = this.state.review;
    this.uncertain = false;
    await this.retry();
  }
  async retry() {
    if (!this.request || this.closed || this.state.busy) return;
    const original = this.request,
      epoch = this.epoch;
    // After confirmation keep only non-content identifiers with the exact retry body.
    const request = {
      ...original,
      label:
        original.body.kind === "delete_account"
          ? "本人アカウント"
          : `現在選択したテナント (${original.tenantId})`,
    };
    this.request = request;
    this.publish({
      ...this.state,
      busy: true,
      pending: true,
      review: undefined,
      label: request.label,
      notice: undefined,
    });
    if (request.body.kind !== "export") this.session.invalidateTenant();
    try {
      const result = await this.scope.run((client, signal) =>
        request.body.kind === "delete_account"
          ? createAccountOperation({
              client,
              signal: AbortSignal.any([signal, this.abort.signal]),
              headers: { "Idempotency-Key": request.key },
              body: request.body,
            })
          : createTenantOperation({
              client,
              signal: AbortSignal.any([signal, this.abort.signal]),
              path: { tenant_id: request.tenantId as string },
              headers: { "Idempotency-Key": request.key },
              body: request.body,
            }),
      );
      this.guard(epoch);
      if ((result.response?.status ?? 0) !== 202 || !result.data)
        throw new RequestFailure(result.response?.status ?? 0);
      const value = operation(result.data, request.body.kind);
      this.publish({
        ...this.state,
        operation: value,
        pending: false,
        reauthentication: false,
        notice: "要求を受け付けました。受付は物理消去や生成の完了を意味しません。",
      });
    } catch (error) {
      if (this.live(epoch)) {
        const status = error instanceof RequestFailure ? error.status : 0;
        if (!status || status >= 500) this.uncertain = true;
        this.publish({
          ...this.state,
          pending: true,
          rejected: !this.uncertain && [400, 409, 412, 422].includes(status),
          reauthentication:
            status === 403 &&
            ["delete_account", "leave_tenant", "delete_tenant"].includes(request.body.kind),
          notice:
            status === 409 && ["leave_tenant", "delete_account"].includes(request.body.kind)
              ? "最後の所有者（owner）は先に所有権を移譲するかテナントを削除してください。競合した要求は未完了です。会員設定で現在の所属を確認してください。"
              : status === 403
                ? ["delete_account", "leave_tenant", "delete_tenant"].includes(request.body.kind)
                  ? "受付を確認できません。現在の権限と5分以内の本人再認証が必要です。"
                  : "受付を確認できません。現在の権限と対象を確認してください。消去済みではありません。"
                : "受付を確認できません。消去済みではありません。同じ要求を再送して確認してください。",
        });
      }
    } finally {
      if (this.live(epoch)) this.publish({ ...this.state, busy: false });
    }
  }
  async reauthenticate(email: string, password: string) {
    if (this.closed || this.state.busy || !this.request) return;
    const epoch = this.epoch;
    this.publish({ ...this.state, busy: true });
    const ok = await this.session.reauthenticate(email, password);
    if (this.live(epoch))
      this.publish({
        ...this.state,
        busy: false,
        reauthentication: !ok,
        notice: ok
          ? "本人確認ができました。同じ要求を再送してください。"
          : "本人確認できませんでした。現在の本人として再試行してください。",
      });
  }
  async refresh() {
    const request = this.request,
      previous = this.state.operation;
    if (!request || !previous || this.closed || this.state.busy) return;
    const epoch = this.epoch;
    this.publish({ ...this.state, busy: true, notice: undefined });
    try {
      const result = await this.scope.run((client, signal) =>
        request.body.kind === "delete_account"
          ? getAccountOperation({ client, signal, path: { operation_id: previous.operation_id } })
          : getTenantOperation({
              client,
              signal,
              path: { tenant_id: request.tenantId as string, operation_id: previous.operation_id },
            }),
      );
      this.guard(epoch);
      if (!result.response?.ok || !result.data)
        throw new RequestFailure(result.response?.status ?? 0);
      this.publish({
        ...this.state,
        operation: operation(result.data, request.body.kind, previous.operation_id),
      });
    } catch {
      if (this.live(epoch)) {
        if (request.body.kind === "export") {
          this.request = undefined;
          this.session.invalidateTenant();
        } else
          this.publish({
            ...this.state,
            notice:
              "受付後の進捗を取得できません。認証終了や進捗の取得失敗だけから消去完了とは判断できません。",
          });
      }
    } finally {
      if (this.live(epoch)) this.publish({ ...this.state, busy: false });
    }
  }
  canDownload() {
    const op = this.state.operation;
    return (
      this.delivery.available &&
      op?.kind === "export" &&
      op.status === "succeeded" &&
      !!op.download_expires_at &&
      Date.parse(op.download_expires_at) > Date.now()
    );
  }
  async download() {
    if (!this.canDownload() || this.state.busy || !this.request) return;
    const epoch = this.epoch,
      request = this.request,
      op = this.state.operation as Operation;
    const signal = AbortSignal.any([
      this.scope.signal,
      this.abort.signal,
      AbortSignal.timeout(30_000),
    ]);
    let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
    const cancel = () => {
      void reader?.cancel().catch(() => {});
    };
    this.publish({ ...this.state, busy: true, notice: undefined });
    signal.addEventListener("abort", cancel);
    try {
      const result = await this.scope.run((client) =>
        downloadExport({
          client,
          signal,
          path: { tenant_id: request.tenantId as string, operation_id: op.operation_id },
          parseAs: "stream",
        }),
      );
      this.guard(epoch);
      signal.throwIfAborted();
      const response = result.response;
      if (
        !response?.ok ||
        response.headers.get("content-type")?.split(";")[0] !== "application/zip" ||
        !response.headers
          .get("cache-control")
          ?.split(",")
          .some((v) => v.trim().toLowerCase() === "no-store") ||
        !response.body
      ) {
        await response?.body?.cancel();
        throw new Error("Invalid export response");
      }
      const declared = response.headers.get("content-length"),
        length = declared === null ? undefined : Number(declared);
      if (
        length !== undefined &&
        (!Number.isSafeInteger(length) || length < 22 || length > 4 * 1024 * 1024)
      ) {
        await response.body.cancel();
        throw new Error("Export size");
      }
      reader = response.body.getReader();
      const chunks: Uint8Array<ArrayBuffer>[] = [];
      let size = 0;
      while (true) {
        const part = await reader.read();
        this.guard(epoch);
        signal.throwIfAborted();
        if (!this.canDownload()) throw new Error("Export expired");
        if (part.done) break;
        size += part.value.byteLength;
        if (size > 4 * 1024 * 1024) throw new Error("Export size");
        chunks.push(new Uint8Array(part.value));
      }
      if (size < 22 || (length !== undefined && size !== length))
        throw new Error("Incomplete export");
      const blob = new Blob(chunks, { type: "application/zip" });
      const signature = new Uint8Array(await blob.slice(0, 4).arrayBuffer());
      if (
        signature[0] !== 0x50 ||
        signature[1] !== 0x4b ||
        signature[2] !== 3 ||
        signature[3] !== 4
      )
        throw new Error("Invalid ZIP");
      const ending = new DataView(await blob.slice(-22).arrayBuffer());
      if (
        ending.getUint32(0, true) !== 0x06054b50 ||
        ending.getUint16(20, true) !== 0 ||
        ending.getUint16(8, true) < 1 ||
        ending.getUint16(8, true) !== ending.getUint16(10, true) ||
        ending.getUint32(12, true) + ending.getUint32(16, true) !== size - 22
      )
        throw new Error("Incomplete ZIP");
      // Source authority is checked by the API at every stream boundary, including EOF.
      // Status GET is only progress; it cannot reauthorize already exported bytes.
      this.guard(epoch);
      signal.throwIfAborted();
      if (!this.canDownload()) throw new Error("Export expired");
      await this.delivery.save(blob, signal);
      this.guard(epoch);
      signal.throwIfAborted();
      this.publish({
        ...this.state,
        notice: "保存・共有の操作を終了しました。保存先のファイルは端末で確認してください。",
      });
    } catch {
      if (this.live(epoch)) {
        this.request = undefined;
        this.session.invalidateTenant();
        this.publish({
          ...empty(),
          notice:
            "取得・保存・共有を完了できませんでした。期限・所属・忘却による失効を確認してください。共有先へ渡したコピーは回収できません。一時ファイルの清掃警告がある場合は対処してください。",
        });
      }
    } finally {
      signal.removeEventListener("abort", cancel);
      await reader?.cancel().catch(() => {});
      if (this.live(epoch)) this.publish({ ...this.state, busy: false });
    }
  }
}
