import {
  type Client,
  createBrainoidClient,
  createTenant,
  getTenant,
  listTenants,
  type Tenant,
} from "@brainoid-internal/api-client";
import type { AuthenticationAdapter, AuthIdentity } from "../platform/auth.ts";
import { type NotificationHint, notificationHint } from "./reminders/notifications.ts";

export class StaleContext extends Error {
  constructor() {
    super("This context is no longer active");
    this.name = "AbortError";
  }
}
export class RequestFailure extends Error {
  readonly status: number;
  constructor(status: number) {
    super("Request failed");
    this.status = status;
  }
}

/** Every resource, including an SSE reader or a draft, belongs to this exact generation. */
export class SessionContext {
  readonly uid: string;
  readonly tenantId: string | undefined;
  readonly generation: number;
  readonly signal: AbortSignal;
  private readonly abort = new AbortController();
  private readonly resources = new Set<() => void>();
  private readonly values = new Map<string, unknown>();
  private readonly client: Client;
  private readonly current: () => boolean;

  constructor(options: {
    uid: string;
    tenantId?: string | undefined;
    generation: number;
    baseUrl: string;
    current: () => boolean;
    token: (refresh: boolean) => Promise<string>;
    transport: typeof fetch;
    denied: (status: number) => void;
  }) {
    this.uid = options.uid;
    this.tenantId = options.tenantId;
    this.generation = options.generation;
    this.signal = this.abort.signal;
    this.current = options.current;
    const transport = options.transport;
    this.client = createBrainoidClient({
      baseUrl: options.baseUrl,
      auth: async () => {
        this.assertCurrent();
        const token = await options.token(false);
        this.assertCurrent();
        return token;
      },
      fetch: async (input, init) => {
        this.assertCurrent();
        const original = new Request(input, init);
        const request = new Request(original, {
          signal: AbortSignal.any([original.signal, this.signal]),
          cache: "no-store",
          redirect: "error",
        });
        request.signal.throwIfAborted();
        // Keep the request body for the one authorized retry, including its idempotency key.
        const retry = request.clone();
        let response = await transport(request);
        await this.checkResponse(response, request.signal);
        if (response.status === 401) {
          await response.body?.cancel();
          let token: string;
          try {
            token = await options.token(true);
          } catch (error) {
            this.assertCurrent();
            request.signal.throwIfAborted();
            options.denied(401);
            throw error;
          }
          this.assertCurrent();
          request.signal.throwIfAborted();
          retry.headers.set("Authorization", `Bearer ${token}`);
          response = await transport(retry);
          await this.checkResponse(response, request.signal);
        }
        if ([401, 403, 404].includes(response.status)) options.denied(response.status);
        return response;
      },
    });
  }

  private async checkResponse(response: Response, signal: AbortSignal): Promise<void> {
    try {
      this.assertCurrent();
      signal.throwIfAborted();
    } catch (error) {
      await response.body?.cancel().catch(() => {});
      throw error;
    }
  }

  assertCurrent(): void {
    if (this.signal.aborted || !this.current()) throw new StaleContext();
  }
  async run<T>(
    operation: (client: Client, signal: AbortSignal, tenantId: string | undefined) => Promise<T>,
  ): Promise<T> {
    this.assertCurrent();
    const result = await operation(this.client, this.signal, this.tenantId);
    this.assertCurrent();
    return result;
  }
  keep(key: string, value: unknown): void {
    this.assertCurrent();
    this.values.set(key, value);
  }
  read(key: string): unknown {
    this.assertCurrent();
    return this.values.get(key);
  }
  own(dispose: () => void): () => void {
    this.assertCurrent();
    this.resources.add(dispose);
    return () => this.resources.delete(dispose);
  }
  dispose(): void {
    this.abort.abort();
    this.values.clear();
    for (const dispose of this.resources) {
      try {
        dispose();
      } catch {
        /* One failed resource must not retain the others. */
      }
    }
    this.resources.clear();
  }
}

export interface SessionState {
  readonly phase: "starting" | "signed-out" | "loading" | "selecting" | "ready";
  readonly uid?: string | undefined;
  readonly tenants: readonly Tenant[];
  readonly tenant?: Tenant | undefined;
  readonly context?: SessionContext | undefined;
  readonly nextCursor?: string | null | undefined;
  readonly message?: string | undefined;
  readonly busy?: boolean | undefined;
  readonly revision: number;
  readonly notificationHint?: NotificationHint | undefined;
}
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const messageFor = (status: number) =>
  status === 403
    ? "この操作は許可されていません。所属と権限を確認してください。"
    : status === 404
      ? "このテナントは利用できません。所属を確認してください。"
      : "通信を確認して、もう一度お試しください。";

export class SessionController {
  private endHooks = new Set<(closing: SessionContext) => Promise<void>>();
  private ending?: Promise<void> | undefined;
  globalContext = () => this.userContext;
  private selectedId: string | undefined;
  selectedTenantId = () => this.selectedId;
  canReauthenticate = () => !!this.options.auth.reauthenticate;
  /** Clear tenant caches while retaining only user-bound operation recovery. */
  invalidateTenant(): void {
    this.tenantContext?.dispose();
    this.tenantContext = undefined;
    this.publish({ phase: "selecting", uid: this.state.uid, tenants: this.state.tenants });
  }
  async reauthenticate(email: string, password: string): Promise<boolean> {
    const scope = this.userContext,
      uid = this.state.uid,
      tenant = this.selectedId,
      selection = this.selection;
    if (!scope || !uid || !this.options.auth.reauthenticate || this.ending) return false;
    this.invalidateTenant();
    const action = this.queue.then(async () => {
      scope.assertCurrent();
      if (this.selectedId !== tenant || this.selection !== selection) return false;
      await this.options.auth.reauthenticate?.(uid, email.trim(), password);
      scope.assertCurrent();
      if ((await this.options.auth.current())?.uid !== uid) throw new StaleContext();
      scope.assertCurrent();
      if (this.selectedId !== tenant || this.selection !== selection) return false;
      if (tenant) await this.selectTenant(tenant, false);
      return true;
    });
    this.queue = action.catch(() => {});
    try {
      return await action;
    } catch {
      if (
        !scope.signal.aborted &&
        this.selectedId === tenant &&
        this.selection === selection &&
        tenant
      )
        await this.selectTenant(tenant, false);
      return false;
    }
  }

  beforeSignOut(callback: (closing: SessionContext) => Promise<void>) {
    this.endHooks.add(callback);
    return () => {
      this.endHooks.delete(callback);
    };
  }
  openNotification(value: unknown) {
    const hint = notificationHint(value);
    if (!hint || this.ending || !this.state.context) return;
    if (hint.tenantId !== this.state.context.tenantId) {
      this.publish({
        ...this.state,
        message: "通知は対象テナントの通知一覧から確認してください。",
      });
      return;
    }
    this.publish({ ...this.state, notificationHint: hint });
  }
  private state: SessionState = { phase: "starting", tenants: [], revision: 0 };
  private listeners = new Set<() => void>();
  private userContext?: SessionContext | undefined;
  private tenantContext?: SessionContext | undefined;
  private generation = 0;
  private selection = 0;
  private userGeneration = 0;
  private blocked = false;
  private suspended = false;
  private stopAuth?: () => void;
  private queue: Promise<unknown> = Promise.resolve();
  private refresh: { epoch: number; promise: Promise<string> } | undefined;
  private creation: { name: string; timezone: string; key: string } | undefined;
  private listing = 0;
  private readonly options: {
    auth: AuthenticationAdapter;
    baseUrl: string;
    transport?: typeof fetch;
    navigate?: (tenantId?: string, replace?: boolean) => void;
    requestedTenant?: () => string | undefined;
  };
  constructor(options: SessionController["options"]) {
    this.options = options;
  }
  snapshot = (): SessionState => this.state;
  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };
  private publish(next: Omit<SessionState, "revision">): void {
    this.state = { ...next, revision: this.state.revision + 1 };
    for (const listener of this.listeners) listener();
  }
  start(): () => void {
    this.stopAuth = this.options.auth.subscribe((identity) => {
      if (!this.blocked && !this.suspended) this.identityChanged(identity);
    });
    return () => {
      this.stopAuth?.();
      this.clear();
    };
  }
  private clear(): void {
    this.tenantContext?.dispose();
    this.userContext?.dispose();
    this.tenantContext = undefined;
    this.userContext = undefined;
    this.userGeneration++;
    this.selection++;
    this.listing++;
    this.creation = undefined;
    this.refresh = undefined;
    this.selectedId = undefined;
  }
  private identityChanged(identity: AuthIdentity | null): void {
    if (identity?.uid === this.state.uid && this.userContext) return;
    this.clear();
    if (!identity) {
      this.publish({ phase: "signed-out", tenants: [] });
      return;
    }
    this.publish({ phase: "loading", uid: identity.uid, tenants: [] });
    this.userContext = this.makeContext(identity.uid);
    void this.loadTenants();
    const requested = this.options.requestedTenant?.();
    if (requested !== undefined) void this.selectTenant(requested, false);
  }
  private makeContext(uid: string, tenantId?: string): SessionContext {
    const epoch = this.userGeneration;
    const context = new SessionContext({
      uid,
      ...(tenantId === undefined ? {} : { tenantId }),
      generation: ++this.generation,
      baseUrl: this.options.baseUrl,
      transport: this.options.transport ?? globalThis.fetch,
      current: () =>
        !this.blocked &&
        !this.suspended &&
        epoch === this.userGeneration &&
        (tenantId === undefined ? this.userContext === context : this.tenantContext === context),
      token: async (force) => {
        if (!force) return this.options.auth.token(uid, false);
        if (this.refresh?.epoch === epoch) return this.refresh.promise;
        const promise = this.options.auth.token(uid, true);
        this.refresh = { epoch, promise };
        try {
          return await promise;
        } finally {
          if (this.refresh?.promise === promise) this.refresh = undefined;
        }
      },
      denied: (status) => {
        if (epoch !== this.userGeneration || this.blocked) return;
        if (status === 401) {
          void this.signOut("認証の有効期限が切れました。もう一度サインインしてください。");
          return;
        }
        // User-scoped failures belong to their operation's selection generation.
        if (tenantId !== undefined && this.tenantContext === context) this.denyTenant(status);
      },
    });
    return context;
  }
  private denyTenant(status: number): void {
    this.listing++;
    this.tenantContext?.dispose();
    this.tenantContext = undefined;
    this.publish({ phase: "ready", uid: this.state.uid, tenants: [], message: messageFor(status) });
  }
  async signIn(email: string, password: string): Promise<void> {
    if (this.ending) await this.ending;
    if (this.state.busy) return;
    this.blocked = true;
    this.clear();
    this.publish({ phase: "signed-out", tenants: [], busy: true });
    const epoch = this.userGeneration;
    const action = this.queue.then(async () => {
      if (epoch !== this.userGeneration) return;
      await this.options.auth.signIn(email, password);
      if (epoch !== this.userGeneration) return;
      const identity = await this.options.auth.current();
      if (epoch !== this.userGeneration) return;
      this.blocked = false;
      this.identityChanged(identity);
    });
    this.queue = action.catch(() => {});
    try {
      await action;
    } catch {
      if (epoch === this.userGeneration)
        this.publish({
          phase: "signed-out",
          tenants: [],
          message: "サインインできませんでした。入力と接続を確認してください。",
        });
    }
  }
  async signOut(message?: string): Promise<void> {
    if (this.ending) return this.ending;
    const uid = this.state.uid;
    this.blocked = true;
    let finish!: () => void;
    this.ending = new Promise<void>((resolve) => {
      finish = resolve;
    });
    // Ordinary contexts are fenced before any async cleanup or observer callback.
    let closing: SessionContext | undefined;
    if (uid)
      closing = new SessionContext({
        uid,
        generation: ++this.generation,
        baseUrl: this.options.baseUrl,
        transport: this.options.transport ?? globalThis.fetch,
        current: () => !!closing && !closing.signal.aborted,
        token: (force) => this.options.auth.token(uid, force),
        denied: () => {},
      });
    const hooks = closing
      ? [...this.endHooks].map((hook) => {
          try {
            return hook(closing as SessionContext);
          } catch {
            return Promise.resolve();
          }
        })
      : [];
    this.clear();
    this.options.navigate?.(undefined, true);
    this.publish({ phase: "signed-out", tenants: [], busy: true, ...(message ? { message } : {}) });
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      await Promise.race([
        Promise.allSettled(hooks),
        new Promise<void>((resolve) => {
          timer = setTimeout(resolve, 6000);
        }),
      ]);
    } finally {
      if (timer) clearTimeout(timer);
      closing?.dispose();
      await this.endSession(message, false);
      this.ending = undefined;
      finish();
    }
  }
  private async endSession(message: string | undefined, navigate: boolean): Promise<void> {
    this.blocked = true;
    this.clear();
    if (navigate) this.options.navigate?.(undefined, true);
    this.publish({ phase: "signed-out", tenants: [], busy: true, ...(message ? { message } : {}) });
    const epoch = this.userGeneration;
    const action = this.queue.then(() => this.options.auth.signOut());
    this.queue = action.catch(() => {});
    try {
      await action;
      if (epoch === this.userGeneration)
        this.publish({ phase: "signed-out", tenants: [], ...(message ? { message } : {}) });
    } catch {
      if (epoch === this.userGeneration)
        this.publish({
          phase: "signed-out",
          tenants: [],
          message: "端末のサインアウトを完了できませんでした。再試行してください。",
          busy: true,
        });
    }
  }
  async loadTenants(more = false, revalidate = true): Promise<void> {
    const scope = this.userContext;
    if (!scope || this.ending) return;
    const listing = ++this.listing;
    if (revalidate && !more && this.state.tenant)
      void this.selectTenant(this.state.tenant.id, false);
    const selection = this.selection;
    const cursor = more ? this.state.nextCursor : undefined;
    const previous = more ? this.state.tenants : [];
    this.publish({ ...this.state, tenants: previous, nextCursor: undefined, message: undefined });
    try {
      const result = await scope.run((client, signal) =>
        listTenants({ client, signal, query: { limit: 100, ...(cursor ? { cursor } : {}) } }),
      );
      if (listing !== this.listing) return;
      if (!result.data) throw new RequestFailure(result.response?.status ?? 0);
      this.publish({
        ...this.state,
        phase: this.state.phase === "loading" ? "ready" : this.state.phase,
        tenants: [...previous, ...result.data.items],
        nextCursor: result.data.next_cursor,
      });
    } catch (error) {
      if (listing !== this.listing || scope.signal.aborted) return;
      if (error instanceof RequestFailure && [403, 404].includes(error.status)) {
        if (selection === this.selection) this.denyTenant(error.status);
        return;
      }
      this.publish({
        ...this.state,
        phase: "ready",
        tenants: [],
        nextCursor: undefined,
        message:
          error instanceof RequestFailure && error.status === 400
            ? "所属が変更されました。一覧を更新してください。"
            : messageFor(error instanceof RequestFailure ? error.status : 0),
      });
    }
  }
  async selectTenant(id: string | undefined, navigate = true): Promise<void> {
    if (this.ending) return;
    this.selection++;
    this.selectedId = id;
    this.tenantContext?.dispose();
    this.tenantContext = undefined;
    if (!this.userContext || !this.state.uid) return;
    if (navigate) this.options.navigate?.(id);
    this.publish({
      phase: id === undefined ? "ready" : "selecting",
      uid: this.state.uid,
      tenants: this.state.tenants,
      nextCursor: this.state.nextCursor,
    });
    if (id === undefined) return;
    if (!uuid.test(id)) {
      this.denyTenant(404);
      return;
    }
    const scope = this.makeContext(this.state.uid, id);
    this.tenantContext = scope;
    try {
      const result = await scope.run((client, signal) =>
        getTenant({ client, signal, path: { tenant_id: id } }),
      );
      if (!result.data) throw new RequestFailure(result.response?.status ?? 0);
      if (result.data.id !== id || result.data.tenant_id !== id)
        throw new Error("Unexpected tenant");
      this.publish({ ...this.state, phase: "ready", tenant: result.data, context: scope });
    } catch (error) {
      if (scope.signal.aborted || this.tenantContext !== scope) return;
      this.denyTenant(error instanceof RequestFailure ? error.status : 0);
    }
  }
  async createTenant(name: string, timezone: string): Promise<void> {
    const scope = this.userContext;
    if (!scope || this.state.busy || this.ending) return;
    const body = { name: name.trim(), timezone: timezone.trim() };
    if (
      !this.creation ||
      this.creation.name !== body.name ||
      this.creation.timezone !== body.timezone
    ) {
      this.creation = { ...body, key: crypto.randomUUID() };
    }
    const key = this.creation.key;
    const selection = this.selection;
    this.publish({ ...this.state, busy: true, message: undefined });
    try {
      const result = await scope.run((client, signal) =>
        createTenant({ client, signal, body, headers: { "Idempotency-Key": key } }),
      );
      if (!result.data) throw new RequestFailure(result.response?.status ?? 0);
      if (this.creation?.key === key) this.creation = undefined;
      if (selection !== this.selection) return;
      this.publish({ ...this.state, busy: false });
      await this.loadTenants(false, false);
      scope.assertCurrent();
      if (selection === this.selection) await this.selectTenant(result.data.id);
    } catch (error) {
      if (scope.signal.aborted || selection !== this.selection) return;
      this.publish({
        ...this.state,
        busy: false,
        message: messageFor(error instanceof RequestFailure ? error.status : 0),
      });
    }
  }
  /** A cached browser document must never resume its independent in-memory login. */
  suspend(): void {
    this.suspended = true;
    if (this.options.auth.storage !== "native-firebase") {
      // The synchronous block survives freezing even if SDK cleanup cannot finish.
      void this.endSession(undefined, false);
      return;
    }
    this.clear();
    this.publish({ phase: "starting", tenants: [] });
  }
  async resume(): Promise<void> {
    this.suspended = false;
    if (this.blocked) {
      this.publish({ phase: "signed-out", tenants: [] });
      return;
    }
    const epoch = this.userGeneration;
    try {
      const identity = await this.options.auth.current();
      if (epoch === this.userGeneration) this.identityChanged(identity);
    } catch {
      if (epoch === this.userGeneration) void this.signOut();
    }
  }
}
