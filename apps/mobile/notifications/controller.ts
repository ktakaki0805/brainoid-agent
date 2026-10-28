import {
  type Client,
  type PushInstallation,
  refreshPushInstallation,
  registerPushInstallation,
  removePushInstallation,
} from "@brainoid-internal/api-client";
import {
  RequestFailure,
  type SessionContext,
  type SessionController,
  StaleContext,
} from "@brainoid-internal/ui/session";
import type { NativePush, PushLease, PushPermission } from "../platform/push.ts";

interface PushState {
  permission: PushPermission | "unavailable";
  busy: boolean;
  registered: boolean;
  pending: boolean;
  removalUnconfirmed: boolean;
  message?: string | undefined;
}
const initial = (): PushState => ({
  permission: "prompt",
  busy: false,
  registered: false,
  pending: false,
  removalUnconfirmed: false,
});
const id = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export class PushController {
  private state = initial();
  private listeners = new Set<() => void>();
  private scope?: SessionContext | undefined;
  private lease?: PushLease | undefined;
  private installed?: PushInstallation | undefined;
  private token?: string | undefined;
  private pending?: { key: string; token: string; original?: PushInstallation } | undefined;
  private latestToken: string | undefined;
  private queue: Promise<unknown> = Promise.resolve();
  private ending = false;
  private stopSession?: () => void;
  private stopEnd?: () => void;
  private release: (() => void) | undefined;
  private session: SessionController;
  private native: NativePush;
  private platform: "ios" | "android";
  constructor(session: SessionController, native: NativePush, platform: "ios" | "android") {
    this.session = session;
    this.native = native;
    this.platform = platform;
  }
  snapshot = () => this.state;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  private publish(next: PushState) {
    this.state = next;
    for (const listener of this.listeners) listener();
  }
  start() {
    this.stopSession = this.session.subscribe(() => this.sync());
    this.stopEnd = this.session.beforeSignOut((closing) => this.disable(closing));
    this.sync();
    return () => {
      this.stopSession?.();
      this.stopEnd?.();
      this.clear();
    };
  }
  private clear() {
    this.ending = true;
    this.release?.();
    this.release = undefined;
    const lease = this.lease;
    this.lease = undefined;
    this.scope = undefined;
    this.installed = undefined;
    this.token = undefined;
    this.latestToken = undefined;
    this.pending = undefined;
    void lease?.close().catch(() => {});
    this.publish(initial());
  }
  private sync() {
    const scope = this.session.globalContext();
    if (scope === this.scope) return;
    this.clear();
    if (!scope) return;
    this.scope = scope;
    this.ending = false;
    try {
      this.lease = this.native.open(
        (token) => {
          if (this.scope === scope && !this.ending) void this.rotate(token);
        },
        (data) => {
          if (this.scope === scope && !this.ending) this.session.openNotification(data);
        },
        () => {
          if (this.scope === scope && !this.ending)
            this.publish({
              ...this.state,
              message: "新しい通知があります。通知一覧を再取得してください。",
            });
        },
      );
      this.release = scope.own(() => this.clear());
      const lease = this.lease;
      void lease
        .permission(false)
        .then((permission) => {
          if (this.scope === scope && !this.ending) this.publish({ ...this.state, permission });
        })
        .catch(() => {
          if (this.scope === scope) this.unavailable();
        });
    } catch {
      this.unavailable();
    }
  }
  private unavailable() {
    this.publish({
      ...this.state,
      busy: false,
      permission: "unavailable",
      message: "OS通知を準備できません。リマインダー作成とアプリ内通知は利用できます。",
    });
  }
  async enable() {
    if (this.scope && !this.lease) {
      const removalUnconfirmed = this.state.removalUnconfirmed;
      this.scope = undefined;
      this.sync();
      this.publish({ ...this.state, removalUnconfirmed });
    }
    const scope = this.scope,
      lease = this.lease;
    if (!scope || !lease || this.ending || this.state.busy || this.pending) return;
    this.publish({ ...this.state, busy: true, message: undefined });
    try {
      const permission = await lease.permission(true);
      if (scope !== this.scope || this.ending) return;
      this.publish({ ...this.state, permission, busy: false });
      if (permission !== "granted") {
        this.publish({
          ...this.state,
          message: "OS通知は許可されていません。アプリ内の未確認一覧をご利用ください。",
        });
        return;
      }
      this.publish({ ...this.state, busy: true });
      const token = await lease.token();
      if (scope !== this.scope || this.ending) return;
      await this.rotate(token);
    } catch {
      if (scope === this.scope && !this.ending) this.unavailable();
    }
  }
  private rotate(token: string) {
    const scope = this.scope;
    if (!scope || this.ending || !token || token.length > 4096) return Promise.resolve();
    this.latestToken = token;
    const action = this.queue.then(async () => {
      if (!scope || scope !== this.scope || this.ending || !token || token.length > 4096) return;
      if (token === this.token && !this.pending) {
        this.publish({ ...this.state, busy: false });
        return;
      }
      if (this.pending) {
        const accepted = await this.send(scope);
        if (!accepted || this.pending || scope !== this.scope || this.ending) return;
      }
      this.pending = {
        key: crypto.randomUUID(),
        token,
        ...(this.installed ? { original: this.installed } : {}),
      };
      await this.send(scope);
    });
    this.queue = action.catch(() => {});
    return action;
  }
  async retry() {
    const scope = this.scope;
    if (!scope || this.ending || this.state.busy || !this.pending) return;
    const result = this.queue.then(() => this.send(scope));
    this.queue = result.catch(() => {});
    const accepted = await result;
    if (
      accepted &&
      scope === this.scope &&
      !this.pending &&
      this.latestToken &&
      this.latestToken !== this.token
    )
      await this.rotate(this.latestToken);
  }
  private async request<T>(
    scope: SessionContext,
    work: (client: Client, signal: AbortSignal) => Promise<T>,
    timeout = 5000,
  ): Promise<T> {
    const signal = AbortSignal.any([scope.signal, AbortSignal.timeout(timeout)]);
    let detach = () => {};
    try {
      const aborted = new Promise<never>((_resolve, reject) => {
        const fail = () =>
          reject(scope.signal.aborted ? new StaleContext() : new Error("Request timed out"));
        signal.addEventListener("abort", fail, { once: true });
        detach = () => signal.removeEventListener("abort", fail);
        if (signal.aborted) fail();
      });
      const result = await Promise.race([scope.run((client) => work(client, signal)), aborted]);
      signal.throwIfAborted();
      return result;
    } finally {
      detach();
    }
  }
  private async send(scope: SessionContext): Promise<boolean> {
    const pending = this.pending;
    if (!pending || scope !== this.scope || this.ending) return false;
    this.publish({ ...this.state, busy: true, pending: true });
    try {
      const result = await this.request(scope, (client, signal) =>
        pending.original
          ? refreshPushInstallation({
              client,
              signal: AbortSignal.any([signal, AbortSignal.timeout(5000)]),
              path: { installation_id: pending.original.id },
              body: { fcm_token: pending.token, app_version: "0.1.0" },
              headers: {
                "Idempotency-Key": pending.key,
                "If-Match": `"${pending.original.version}"`,
              },
            })
          : registerPushInstallation({
              client,
              signal: AbortSignal.any([signal, AbortSignal.timeout(5000)]),
              body: { fcm_token: pending.token, platform: this.platform, app_version: "0.1.0" },
              headers: { "Idempotency-Key": pending.key },
            }),
      );
      const value = result.data;
      if (!result.response?.ok || !value) throw new RequestFailure(result.response?.status ?? 0);
      if (
        result.response.status !== (pending.original ? 200 : 201) ||
        !id.test(value.id) ||
        value.platform !== this.platform ||
        !Number.isSafeInteger(value.version) ||
        value.version < 1 ||
        typeof value.created_at !== "string" ||
        !Number.isFinite(Date.parse(value.created_at)) ||
        result.response.headers.get("ETag") !== `"${value.version}"` ||
        (pending.original &&
          (value.id !== pending.original.id || value.version <= pending.original.version))
      )
        throw new Error("Invalid installation");
      if (scope !== this.scope || this.ending) return false;
      this.installed = value;
      this.token = pending.token;
      this.pending = undefined;
      this.publish({
        ...this.state,
        busy: false,
        registered: true,
        pending: false,
        removalUnconfirmed: false,
        message: "OS通知の配信先を登録しました。配信や表示の保証ではありません。",
      });
      return true;
    } catch (error) {
      if (scope !== this.scope || this.ending) return false;
      if (
        error instanceof RequestFailure &&
        [400, 403, 404, 409, 412, 422].includes(error.status)
      ) {
        this.pending = undefined;
        this.installed = undefined;
        this.token = undefined;
        this.publish({
          ...this.state,
          busy: false,
          pending: false,
          registered: false,
          message:
            "通知登録が拒否・解除または更新されました。所属・登録数を確認し、許可・登録から現在の配信先を再登録してください。アプリ内通知は利用できます。",
        });
      } else
        this.publish({
          ...this.state,
          busy: false,
          pending: true,
          message:
            "通知登録の受付結果が不明です。同じ登録を再確認できます。アプリ内通知は利用できます。",
        });
      return false;
    }
  }

  async disable(closing?: SessionContext) {
    const scope = this.scope,
      lease = this.lease,
      installed = this.installed,
      pending = this.pending;
    if (!scope || !lease || this.ending) return;
    const requestScope = closing ?? scope;
    this.ending = true;
    const closed = lease.close().then(
      () => true,
      () => false,
    );
    this.publish({ ...this.state, busy: true });
    let removed = false;
    try {
      await this.queue;
      let target = installed;
      // A lost registration is reconciled under the same original UID, using only
      // the captured exact request; no state or new UID is consulted after await.
      if (pending) {
        const result = await this.request(requestScope, (client, signal) =>
          pending.original
            ? refreshPushInstallation({
                client,
                signal: AbortSignal.any([signal, AbortSignal.timeout(2000)]),
                path: { installation_id: pending.original.id },
                body: { fcm_token: pending.token, app_version: "0.1.0" },
                headers: {
                  "Idempotency-Key": pending.key,
                  "If-Match": `"${pending.original.version}"`,
                },
              })
            : registerPushInstallation({
                client,
                signal: AbortSignal.any([signal, AbortSignal.timeout(2000)]),
                body: { fcm_token: pending.token, platform: this.platform, app_version: "0.1.0" },
                headers: { "Idempotency-Key": pending.key },
              }),
        );
        const value = result.data;
        if (
          !result.response?.ok ||
          !value ||
          !id.test(value.id) ||
          value.platform !== this.platform ||
          !Number.isSafeInteger(value.version) ||
          value.version < 1 ||
          typeof value.created_at !== "string" ||
          !Number.isFinite(Date.parse(value.created_at)) ||
          result.response.headers.get("ETag") !== `"${value.version}"` ||
          (pending.original && value.id !== pending.original.id)
        )
          throw new Error("Registration unconfirmed");
        target = value;
      }
      if (target) {
        const key = crypto.randomUUID(),
          record = target;
        const remove = () =>
          this.request(requestScope, (client, signal) =>
            removePushInstallation({
              client,
              signal: AbortSignal.any([signal, AbortSignal.timeout(2000)]),
              path: { installation_id: record.id },
              headers: { "Idempotency-Key": key, "If-Match": `"${record.version}"` },
            }),
          );
        let result: Awaited<ReturnType<typeof remove>>;
        try {
          result = await remove();
        } catch {
          result = await remove();
        }
        if (result.response?.status !== 204) throw new Error("Removal unconfirmed");
      }
      removed = await closed;
    } catch {
      // The original request/lease is no longer reusable after close. Expose a
      // fresh-lease action, while explicitly retaining uncertainty about removal.
    } finally {
      if (this.scope === scope) {
        this.installed = undefined;
        this.token = undefined;
        this.latestToken = undefined;
        this.pending = undefined;
        this.lease = undefined;
        this.publish({
          ...this.state,
          busy: false,
          pending: false,
          registered: false,
          removalUnconfirmed: !removed,
          message: removed
            ? "OS通知を解除しました。アプリ内通知は利用できます。"
            : "通知解除の完了を確認できません。OS設定でも停止できます。配信先を再確認・登録してから解除を再試行できます。",
        });
      }
    }
  }
}
