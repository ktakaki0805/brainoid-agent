import {
  type PrivateSession,
  type PrivateSessionCreate,
  createPrivateSession,
  getPrivateSession,
  endPrivateSession,
} from "@brainoid-internal/api-client";
import { isId } from "../chat/events.ts";
import { RequestFailure, type SessionContext } from "../session.ts";
import {
  type Clock,
  OutputLifetime,
  privateSession,
  sessionBinding,
  systemClock,
} from "./lifetime.ts";

interface State {
  session?: PrivateSession | undefined;
  destination: "phone" | "pi";
  busy: boolean;
  pending: boolean;
  notice: string;
}
type Pending =
  | { kind: "start"; device: string; body: PrivateSessionCreate; key: string }
  | { kind: "end"; session: PrivateSession; key: string };
export class OutputTargetController {
  private state: State = { destination: "phone", busy: false, pending: false, notice: "" };
  private listeners = new Set<() => void>();
  private pending: Pending | undefined;
  private closed = false;
  private revision = 0;
  private refreshing = false;
  private uncertain = false;
  get generation() {
    return this.revision;
  }
  private retired = new Set<string>();
  private release: () => void;
  readonly lifetime: OutputLifetime;
  readonly context: SessionContext;
  constructor(context: SessionContext, clock: Clock = systemClock) {
    this.context = context;
    this.lifetime = new OutputLifetime(clock, () => this.expire());
    this.release = context.own(() => this.dispose());
  }
  snapshot = () => this.state;
  pendingTarget() {
    const pending = this.pending;
    return pending
      ? pending.kind === "start"
        ? pending.device
        : pending.session.device_id
      : undefined;
  }
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  private publish(next: State) {
    this.state = next;
    for (const listener of this.listeners) listener();
  }
  private expire() {
    if (this.state.session) this.retired.add(this.state.session.id);
    this.revision++;
    this.publish({
      ...this.state,
      session: undefined,
      destination: "phone",
      busy: false,
      notice: "個人セッションの既知の期限に達しました。表示と待機中の出力を消去しました。",
    });
  }
  tick() {
    this.lifetime.tick();
  }
  target() {
    this.context.assertCurrent();
    this.tick();
    const session = this.state.session;
    return this.state.destination === "pi" && session && this.lifetime.session
      ? { device: session.device_id, session: session.id, binding: sessionBinding(session) }
      : undefined;
  }
  choose(destination: "phone" | "pi") {
    this.context.assertCurrent();
    this.tick();
    if (
      this.closed ||
      this.state.busy ||
      this.state.pending ||
      (destination === "pi" && this.state.session && this.retired.has(this.state.session.id)) ||
      (destination === "pi" && !this.state.session?.allowed_scopes.includes("conversation"))
    )
      return;
    if (this.state.destination !== destination) this.lifetime.invalidate();
    this.publish({ ...this.state, destination });
  }
  async start(
    device: string,
    scopes: PrivateSessionCreate["allowed_scopes"],
    acknowledged: boolean,
  ) {
    this.context.assertCurrent();
    if (
      this.closed ||
      this.state.busy ||
      this.pending ||
      this.retired.size >= 128 ||
      this.state.session ||
      !isId(device) ||
      !acknowledged ||
      !scopes.length ||
      scopes.length > 9 ||
      new Set(scopes).size !== scopes.length
    )
      return;
    this.pending = {
      kind: "start",
      device,
      body: { allowed_scopes: [...scopes], acknowledge_shared_space: true },
      key: crypto.randomUUID(),
    };
    this.uncertain = false;
    await this.retry();
  }
  private read(value: PrivateSession, response: Response, device: string, id?: string) {
    const result = privateSession(value);
    if (
      result.tenant_id !== this.context.tenantId ||
      result.user_id !== this.context.uid ||
      result.device_id !== device ||
      (id && result.id !== id) ||
      response.headers.get("ETag") !== `"${result.version}"`
    )
      throw new Error("Wrong session");
    return result;
  }
  private accept(value: PrivateSession) {
    const previous = this.state.session;
    if (
      previous &&
      (value.version < previous.version ||
        (value.status === "active" && sessionBinding(value) !== sessionBinding(previous)))
    )
      throw new Error("Changed consent");
    if (this.retired.has(value.id) || !this.lifetime.accept(value)) {
      this.lifetime.clear();
      this.retired.add(value.id);
      this.publish({
        ...this.state,
        session: undefined,
        destination: "phone",
        notice: "個人セッションは終了または失効しました。",
      });
      return;
    }
    this.publish({ ...this.state, session: value, notice: "" });
  }
  async retry() {
    const pending = this.pending;
    if (!pending || this.closed || this.state.busy) return;
    const revision = this.revision;
    this.publish({ ...this.state, busy: true, pending: true, notice: "" });
    try {
      await this.context.run(async (client, signal, tenant) => {
        const common = { client, signal: AbortSignal.any([signal, AbortSignal.timeout(12_000)]) };
        if (!tenant) throw new Error("Tenant required");
        if (pending.kind === "start") {
          const result = await createPrivateSession({
            ...common,
            path: { tenant_id: tenant, device_id: pending.device },
            body: pending.body,
            headers: { "Idempotency-Key": pending.key },
          });
          if (result.response?.status !== 201 || !result.data)
            throw new RequestFailure(result.response?.status ?? 0);
          const session = this.read(result.data, result.response, pending.device);
          if (
            JSON.stringify([...session.allowed_scopes].sort()) !==
            JSON.stringify([...pending.body.allowed_scopes].sort())
          )
            throw new Error("Wrong scopes");
          if (!this.closed && revision === this.revision) this.accept(session);
        } else {
          const result = await endPrivateSession({
            ...common,
            path: { tenant_id: tenant, private_session_id: pending.session.id },
            headers: { "Idempotency-Key": pending.key, "If-Match": `"${pending.session.version}"` },
          });
          if (result.response?.status !== 204)
            throw new RequestFailure(result.response?.status ?? 0);
          if (!this.closed && revision === this.revision)
            this.publish({ ...this.state, notice: "クラウドで個人セッションを終了しました。" });
        }
      });
      if (this.closed || revision !== this.revision) return;
      this.pending = undefined;
      this.publish({ ...this.state, busy: false, pending: false });
    } catch (error) {
      if (this.closed || revision !== this.revision || this.context.signal.aborted) return;
      if (pending.kind === "end" && error instanceof RequestFailure && error.status === 412) {
        try {
          const terminal = await this.context.run(async (client, signal, tenant) => {
            if (!tenant) throw new Error("Tenant required");
            const result = await getPrivateSession({
              client,
              signal: AbortSignal.any([signal, AbortSignal.timeout(12_000)]),
              path: { tenant_id: tenant, private_session_id: pending.session.id },
            });
            if (!result.response?.ok || !result.data) throw new Error("Unconfirmed termination");
            return (
              this.read(result.data, result.response, pending.session.device_id, pending.session.id)
                .status !== "active"
            );
          });
          if (terminal && !this.closed && revision === this.revision) {
            this.pending = undefined;
            this.publish({
              ...this.state,
              busy: false,
              pending: false,
              notice: "クラウドで個人セッションの終了または失効を確認しました。",
            });
            return;
          }
        } catch {
          /* Retain the original end intent if current termination is unconfirmed. */
        }
      }
      if (this.closed || revision !== this.revision || this.context.signal.aborted) return;
      // An uncertain mutation keeps its exact target/body/key. Do not turn a retry
      // into new consent, including after a terminal response or rate limiting.
      if (
        !this.uncertain &&
        error instanceof RequestFailure &&
        [400, 409, 412, 429].includes(error.status)
      ) {
        if (pending.kind === "start") this.pending = undefined;
      }
      if (this.pending) this.uncertain = true;
      this.publish({
        ...this.state,
        busy: false,
        pending: !!this.pending,
        notice:
          "結果を確認できません。同じ操作を再確認してください。終了が不明な場合も既知の期限で停止します。",
      });
    }
  }
  async end() {
    this.context.assertCurrent();
    const session = this.state.session;
    if (!session || this.closed || this.state.busy || this.pending) return;
    this.retired.add(session.id);
    this.lifetime.clear();
    this.revision++;
    this.pending = { kind: "end", session, key: crypto.randomUUID() };
    this.uncertain = false;
    this.publish({ ...this.state, session: undefined, destination: "phone", pending: true });
    await this.retry();
  }
  async refresh() {
    this.tick();
    const session = this.state.session,
      revision = this.revision;
    if (
      !session ||
      this.closed ||
      this.state.busy ||
      this.refreshing ||
      this.retired.has(session.id)
    )
      return;
    this.refreshing = true;
    try {
      const value = await this.context.run(async (client, signal, tenant) => {
        if (!tenant) throw new Error("Tenant required");
        const result = await getPrivateSession({
          client,
          signal: AbortSignal.any([signal, AbortSignal.timeout(12_000)]),
          path: { tenant_id: tenant, private_session_id: session.id },
        });
        if (!result.response?.ok || !result.data)
          throw new RequestFailure(result.response?.status ?? 0);
        return this.read(result.data, result.response, session.device_id, session.id);
      });
      this.tick();
      if (!this.closed && revision === this.revision) this.accept(value);
    } catch {
      /* No polling, retry or network failure extends the local deadline. */
    }
    this.refreshing = false;
  }
  suspend() {
    if (this.state.session) this.retired.add(this.state.session.id);
    this.revision++;
    this.lifetime.invalidate();
    this.publish({
      ...this.state,
      destination: "phone",
      busy: false,
      notice:
        "画面を離れたため個人表示を停止しました。Piの委任はスマホの終了操作または既知の期限で終了します。",
    });
  }
  dispose() {
    if (this.closed) return;
    this.closed = true;
    this.revision++;
    this.pending = undefined;
    this.retired.clear();
    this.lifetime.clear();
    this.publish({ destination: "phone", busy: false, pending: false, notice: "" });
    this.release();
  }
}
