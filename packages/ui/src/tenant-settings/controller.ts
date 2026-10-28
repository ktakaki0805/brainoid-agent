import {
  acceptInvitation,
  createInvitation,
  getUsage,
  listMemberships,
  revokeMembershipByUid,
  transferOwner,
  updateMembershipByUid,
  type Invitation,
  type Membership,
  type Tenant,
  type Usage,
} from "@brainoid-internal/api-client";
import {
  RequestFailure,
  type SessionContext,
  type SessionController,
  StaleContext,
} from "../session.ts";

type Role = Membership["role"];
type Change = { key: string; tenantId: string; actor: string } & (
  | { kind: "invite"; role: "admin" | "member" }
  | { kind: "role"; target: Membership; role: Role }
  | { kind: "revoke"; target: Membership }
  | { kind: "transfer"; target: Membership; actorVersion: number }
);
export interface SettingsState {
  tenant?: Tenant | undefined;
  members: readonly Membership[];
  cursor?: string | null | undefined;
  usage?: Usage | undefined;
  invitation?: Invitation | undefined;
  review?: Change | undefined;
  conflict?: Membership | undefined;
  busy: boolean;
  pending: boolean;
  reauthentication: boolean;
  notice?: string | undefined;
}
const empty = (): SettingsState => ({
  members: [],
  busy: false,
  pending: false,
  reauthentication: false,
});
function immutable<T>(value: T): T {
  if (value && typeof value === "object") {
    Object.freeze(value);
    for (const child of Object.values(value)) immutable(child);
  }
  return value;
}
function data<T>(r: { data?: T; response?: Response }): NonNullable<T> {
  if (!r.response?.ok || r.data == null) throw new RequestFailure(r.response?.status ?? 0);
  return r.data;
}
const manager = (role?: Role) => role === "owner" || role === "admin";
const validMember = (v: Membership) =>
  typeof v.user_id === "string" &&
  v.user_id.length > 0 &&
  ["owner", "admin", "member"].includes(v.role) &&
  ["active", "suspended", "revoked"].includes(v.status) &&
  Number.isSafeInteger(v.version) &&
  v.version > 0;
/** Privileged data belongs to the current tenant generation. Only an exact uncertain request
 * survives a same-tenant refresh, scoped to the unchanged global authentication context. */
export class TenantSettingsController {
  private state = empty();
  private listeners = new Set<() => void>();
  private operation: Change | undefined;
  private selected: string | undefined;
  private context: SessionContext | undefined;
  private closed = false;
  private loading = 0;
  private epoch = 0;
  private stop: () => void;
  private release: () => void;
  readonly scope: SessionContext;
  readonly session: SessionController;
  constructor(session: SessionController) {
    this.session = session;
    const scope = session.globalContext();
    if (!scope) throw new StaleContext();
    this.scope = scope;
    this.selected = session.selectedTenantId();
    this.context = session.snapshot().context;
    this.release = scope.own(() => this.dispose());
    this.stop = session.subscribe(() => this.sync());
  }
  snapshot = () => this.state;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  private publish(value: SettingsState) {
    this.state = immutable(value);
    for (const listener of this.listeners) listener();
  }
  private sync() {
    if (this.closed) return;
    const selected = this.session.selectedTenantId(),
      context = this.session.snapshot().context;
    if (selected !== this.selected) {
      this.loading++;
      this.epoch++;
      this.operation = undefined;
      this.selected = selected;
      this.context = context;
      this.publish(empty());
    } else if (context !== this.context || this.context?.signal.aborted) {
      this.context = context;
      // Never carry lists, usage, codes or confirmation into a new privilege generation.
      this.publish({
        ...empty(),
        busy: this.state.busy,
        pending: !!this.operation,
        reauthentication: this.state.reauthentication,
        notice: this.state.notice,
      });
    }
  }
  dispose() {
    if (this.closed) return;
    this.closed = true;
    this.epoch++;
    this.loading++;
    this.operation = undefined;
    this.stop?.();
    this.release?.();
    this.publish(empty());
  }
  private assert(id = this.selected) {
    this.scope.assertCurrent();
    if (this.closed || !id || id !== this.session.selectedTenantId()) throw new StaleContext();
    return id;
  }
  private live(epoch: number) {
    return !this.closed && epoch === this.epoch && !this.scope.signal.aborted;
  }
  private guard(epoch: number) {
    if (!this.live(epoch)) throw new StaleContext();
  }
  private async refresh(more = false) {
    const id = this.assert(),
      ticket = ++this.loading;
    const previous = more ? this.state.members : [],
      cursor = more ? this.state.cursor : undefined;
    if (!more) await this.session.selectTenant(id, false);
    this.assert(id);
    if (ticket !== this.loading) throw new StaleContext();
    const { tenant, context } = this.session.snapshot();
    if (
      !context ||
      !tenant ||
      tenant.id !== id ||
      tenant.tenant_id !== id ||
      tenant.status !== "active"
    )
      throw new RequestFailure(403);
    if (!manager(tenant.my_role) && this.operation?.kind === "invite") this.operation = undefined;
    this.publish({
      ...this.state,
      tenant,
      members: previous,
      usage: undefined,
      invitation: undefined,
      review: undefined,
      cursor: undefined,
      pending: !!this.operation,
    });
    const usage = data(
      await context.run((client, signal) => getUsage({ client, signal, path: { tenant_id: id } })),
    );
    this.assert(id);
    if (
      !/^\d{4}-\d{2}$/.test(usage.period) ||
      ![usage.turns, usage.voice_seconds, usage.remaining_turns].every(
        (v) => Number.isSafeInteger(v) && v >= 0,
      ) ||
      !["available", "limited", "exhausted"].includes(usage.quota_state)
    )
      throw new Error("Invalid usage");
    let members: readonly Membership[] = [],
      next: string | null = null;
    if (manager(tenant.my_role)) {
      const page = data(
        await context.run((client, signal) =>
          listMemberships({
            client,
            signal,
            path: { tenant_id: id },
            query: { limit: 100, ...(cursor ? { cursor } : {}) },
          }),
        ),
      );
      if (!page.items.every(validMember)) throw new Error("Invalid membership");
      members = [...previous, ...page.items];
      next = page.next_cursor;
      if (new Set(members.map((v) => v.user_id)).size !== members.length)
        throw new Error("Invalid page");
    }
    context.assertCurrent();
    this.assert(id);
    if (ticket !== this.loading) throw new StaleContext();
    this.publish({
      ...this.state,
      tenant,
      members,
      cursor: next,
      usage,
      pending: !!this.operation,
    });
  }
  async load(more = false) {
    const epoch = this.epoch;
    if (this.closed || this.state.busy || !this.selected) return;
    this.publish({ ...this.state, busy: true, review: undefined, conflict: undefined });
    try {
      await this.refresh(more);
    } catch {
      if (this.live(epoch))
        this.publish({
          ...this.state,
          members: [],
          usage: undefined,
          tenant: undefined,
          cursor: undefined,
          invitation: undefined,
          notice: "現在の所属と利用量を取得できません。一覧を更新してください。",
        });
    } finally {
      if (this.live(epoch)) this.publish({ ...this.state, busy: false });
    }
  }
  canChange(target: Membership, role?: Role) {
    const actor = this.state.tenant?.my_role;
    return (
      manager(actor) &&
      target.status === "active" &&
      !(
        target.role === "owner" &&
        (role === undefined || role !== "owner") &&
        !this.state.cursor &&
        this.state.members.filter((v) => v.role === "owner" && v.status === "active").length <= 1
      ) &&
      !(
        actor === "admin" &&
        (target.user_id === this.scope.uid || target.role === "owner" || role === "owner")
      ) &&
      (role === undefined || role !== target.role)
    );
  }
  async prepare(kind: Change["kind"], uid?: string, role?: Role) {
    const epoch = this.epoch;
    if (this.closed || this.state.busy || this.operation) return;
    this.publish({
      ...this.state,
      busy: true,
      review: undefined,
      conflict: undefined,
      notice: undefined,
    });
    try {
      await this.refresh();
      this.guard(epoch);
      const tenantId = this.assert(),
        actor = this.scope.uid;
      if (!manager(this.state.tenant?.my_role)) throw new RequestFailure(403);
      const key = crypto.randomUUID();
      let review: Change;
      if (kind === "invite") {
        if (role !== "member" && role !== "admin") return;
        review = { kind, key, tenantId, actor, role };
      } else {
        // A target can be on a later page. The user loads that page again after refresh.
        let target = this.state.members.find((v) => v.user_id === uid);
        while (!target && this.state.cursor) {
          await this.refresh(true);
          this.guard(epoch);
          target = this.state.members.find((v) => v.user_id === uid);
        }
        // Count all effective owners before an owner-removal action, even on paginated lists.
        while (target?.role === "owner" && this.state.cursor) {
          await this.refresh(true);
          this.guard(epoch);
          target = this.state.members.find((v) => v.user_id === uid);
        }
        if (!target || !this.canChange(target, kind === "role" ? role : undefined))
          throw new RequestFailure(403);
        if (kind === "role") {
          if (!role) return;
          review = { kind, key, tenantId, actor, target, role };
        } else if (kind === "revoke") {
          if (target.user_id === actor) throw new RequestFailure(403);
          review = { kind, key, tenantId, actor, target };
        } else {
          let own = this.state.members.find((v) => v.user_id === actor);
          while (!own && this.state.cursor) {
            await this.refresh(true);
            this.guard(epoch);
            own = this.state.members.find((v) => v.user_id === actor);
          }
          if (
            this.state.tenant?.my_role !== "owner" ||
            !own ||
            own.role !== "owner" ||
            own.status !== "active" ||
            target.role === "owner" ||
            target.user_id === actor
          )
            throw new RequestFailure(403);
          review = { kind, key, tenantId, actor, target, actorVersion: own.version };
        }
      }
      this.publish({ ...this.state, review });
    } catch {
      if (this.live(epoch))
        this.publish({
          ...this.state,
          review: undefined,
          notice: "現在の権限では操作できません。所属を更新してください。",
        });
    } finally {
      if (this.live(epoch)) this.publish({ ...this.state, busy: false });
    }
  }
  cancel() {
    if (!this.state.busy && !this.operation) this.publish({ ...this.state, review: undefined });
  }
  hideCode() {
    this.publish({ ...this.state, invitation: undefined });
  }
  async confirm() {
    const epoch = this.epoch;
    const review = this.state.review;
    if (!review || this.state.busy || this.operation) return;
    this.publish({ ...this.state, busy: true });
    try {
      await this.refresh();
      this.guard(epoch);
      this.assert(review.tenantId);
      if (!manager(this.state.tenant?.my_role)) throw new RequestFailure(403);
      if (review.kind !== "invite") {
        let target = this.state.members.find((v) => v.user_id === review.target.user_id);
        while (
          (!target ||
            (review.kind === "transfer" &&
              !this.state.members.some((v) => v.user_id === review.actor))) &&
          this.state.cursor
        ) {
          await this.refresh(true);
          this.guard(epoch);
          target = this.state.members.find((v) => v.user_id === review.target.user_id);
        }
        while (target?.role === "owner" && this.state.cursor) {
          await this.refresh(true);
          this.guard(epoch);
          target = this.state.members.find((v) => v.user_id === review.target.user_id);
        }
        const own = this.state.members.find((v) => v.user_id === review.actor);
        if (
          !target ||
          target.version !== review.target.version ||
          target.role !== review.target.role ||
          target.status !== "active" ||
          (review.kind === "transfer" && own?.version !== review.actorVersion)
        ) {
          this.publish({
            ...this.state,
            conflict: target,
            notice: "会員情報が変更されました。現在の版を確認し、操作を選び直してください。",
          });
          return;
        }
        if (
          !this.canChange(target, review.kind === "role" ? review.role : undefined) ||
          (review.kind === "transfer" && this.state.tenant?.my_role !== "owner")
        )
          throw new RequestFailure(403);
      }
      this.operation = review;
      await this.send();
    } catch {
      if (this.live(epoch))
        this.publish({
          ...this.state,
          review: undefined,
          notice: "現在の権限では操作できません。",
        });
    } finally {
      if (this.live(epoch)) this.publish({ ...this.state, busy: false, pending: !!this.operation });
    }
  }
  async retry() {
    const epoch = this.epoch;
    if (!this.operation || this.state.busy || this.closed) return;
    this.publish({ ...this.state, busy: true });
    try {
      await this.send();
    } finally {
      if (this.live(epoch)) this.publish({ ...this.state, busy: false, pending: !!this.operation });
    }
  }
  private async send() {
    const epoch = this.epoch;
    const op = this.operation;
    if (!op) return;
    const id = this.assert(op.tenantId);
    this.session.invalidateTenant();
    this.publish({ ...empty(), busy: true, pending: true });
    try {
      const result = await this.scope.run<{ data?: unknown; response?: Response }>(
        (client, signal) => {
          const path = { tenant_id: id },
            headers = { "Idempotency-Key": op.key };
          if (op.kind === "invite")
            return createInvitation({ client, signal, path, headers, body: { role: op.role } });
          const versioned = {
            ...headers,
            "If-Match": `"${op.kind === "transfer" ? op.actorVersion : op.target.version}"`,
          };
          if (op.kind === "transfer")
            return transferOwner({
              client,
              signal,
              path,
              headers: versioned,
              body: { target_user_id: op.target.user_id, target_version: op.target.version },
            });
          const query = { user_id: op.target.user_id };
          if (op.kind === "role")
            return updateMembershipByUid({
              client,
              signal,
              path,
              query,
              headers: versioned,
              body: { role: op.role },
            });
          return revokeMembershipByUid({ client, signal, path, query, headers: versioned });
        },
      );
      this.guard(epoch);
      this.assert(id);
      if (!result.response?.ok) throw new RequestFailure(result.response?.status ?? 0);
      const invitation = op.kind === "invite" ? (data(result) as Invitation) : undefined;
      if (
        invitation &&
        (!/^[a-f0-9]{64}$/.test(invitation.code) ||
          op.kind !== "invite" ||
          invitation.role !== op.role ||
          !Number.isFinite(Date.parse(invitation.expires_at)))
      )
        throw new Error("Invalid invitation");
      if (op.kind === "role" || op.kind === "transfer") {
        const member = data(result) as Membership;
        if (
          !validMember(member) ||
          member.user_id !== op.target.user_id ||
          member.status !== "active" ||
          member.role !== (op.kind === "transfer" ? "owner" : op.role)
        )
          throw new Error("Invalid membership result");
      }
      // A successful response proves completion even if the following read is unavailable.
      // Issuance retains its exact key until the code can be displayed under current authority.
      if (op.kind !== "invite") this.operation = undefined;
      try {
        await this.refresh();
      } catch {
        this.guard(epoch);
        this.publish({
          ...empty(),
          busy: true,
          pending: !!this.operation,
          notice:
            op.kind === "invite"
              ? "招待の発行応答を受け取りましたが、現在の権限を確認できません。同じ要求で確認してください。"
              : "会員の変更を確認しました。最新の表示を取得できないため、一覧を更新してください。",
        });
        return;
      }
      this.guard(epoch);
      this.operation = undefined;
      this.publish({
        ...this.state,
        invitation: manager(this.state.tenant?.my_role) ? invitation : undefined,
        pending: false,
        review: undefined,
        notice:
          op.kind === "invite"
            ? "招待コードを発行しました。相手へ安全な方法で共有してください。"
            : "会員の変更を確認しました。減権限・所属失効では既存の招待コードがすべて無効になります。",
      });
    } catch (error) {
      if (!this.live(epoch) || this.selected !== id) return;
      const status = error instanceof RequestFailure ? error.status : 0;
      // Even a later 429/403 is not proof that an earlier attempt did not commit.
      // Retain exact receipts for member/owner mutation recovery, including recent-auth refusal.
      if ([400, 404, 410, 412, 422].includes(status)) this.operation = undefined;
      if (op.kind === "invite" && status === 403) this.operation = undefined;
      const notice =
        status === 412
          ? "会員の版が競合しました。最新の一覧を確認し、操作を選び直してください。"
          : status === 409
            ? "変更が競合しています。最後のownerは移譲またはテナント削除が必要です。未確定の変更は同じ要求で確認してください。"
            : status === 403
              ? "操作が拒否されました。権限と再認証を確認してください。以前の要求の結果は未確認です。"
              : status === 429
                ? "試行回数の制限中です。時間をおいて同じ要求を確認してください。先の要求が失敗したとは限りません。"
                : [400, 404, 410, 422].includes(status)
                  ? "操作を完了できません。対象または招待の有効性を確認してください。"
                  : "結果を確認できません。同じ要求で結果を確認してください。";
      try {
        await this.refresh();
        this.guard(epoch);
      } catch {
        /* Keep only the exact request, never cached privileged data. */
      }
      if (this.live(epoch) && this.selected === id)
        this.publish({
          ...this.state,
          pending: !!this.operation,
          review: undefined,
          invitation: undefined,
          reauthentication: status === 403 && (op.kind === "transfer" || op.kind === "revoke"),
          notice,
        });
    }
  }
  async reauthenticate(email: string, password: string) {
    const epoch = this.epoch;
    if (this.state.busy || this.closed) return;
    this.publish({ ...this.state, busy: true });
    const ok = await this.session.reauthenticate(email, password);
    if (this.live(epoch))
      this.publish({
        ...this.state,
        busy: false,
        reauthentication: !ok,
        pending: !!this.operation,
        notice: ok
          ? "本人確認が完了しました。同じ要求で結果を確認してください。"
          : "本人確認を完了できませんでした。現在のアカウントの認証情報を確認してください。",
      });
  }
}

export interface AcceptanceState {
  busy: boolean;
  pending: boolean;
  notice?: string | undefined;
}
export class InvitationAcceptanceController {
  private state: AcceptanceState = { busy: false, pending: false };
  private request: { code: string; key: string } | undefined;
  private listeners = new Set<() => void>();
  private closed = false;
  private release: () => void;
  readonly scope: SessionContext;
  readonly session: SessionController;
  constructor(session: SessionController) {
    this.session = session;
    const scope = session.globalContext();
    if (!scope) throw new StaleContext();
    this.scope = scope;
    this.release = scope.own(() => this.dispose());
  }
  snapshot = () => this.state;
  subscribe = (v: () => void) => {
    this.listeners.add(v);
    return () => {
      this.listeners.delete(v);
    };
  };
  private publish(v: AcceptanceState) {
    this.state = immutable(v);
    for (const listener of this.listeners) listener();
  }
  dispose() {
    this.closed = true;
    this.request = undefined;
    this.release?.();
    this.publish({ busy: false, pending: false });
  }
  async accept(code?: string) {
    if (this.closed || this.state.busy) return;
    if (!this.request) {
      if (!code || !/^[a-f0-9]{64}$/.test(code.trim())) {
        this.publish({
          busy: false,
          pending: false,
          notice: "64文字の招待コードを確認してください。",
        });
        return;
      }
      this.request = { code: code.trim(), key: crypto.randomUUID() };
    }
    const request = this.request;
    this.publish({ busy: true, pending: true });
    try {
      const result = data(
        await this.scope.run((client, signal) =>
          acceptInvitation({
            client,
            signal,
            body: { code: request.code },
            headers: { "Idempotency-Key": request.key },
          }),
        ),
      );
      if (result.user_id !== this.scope.uid) throw new Error("Unexpected identity");
      this.request = undefined;
      this.publish({
        busy: false,
        pending: false,
        notice: "参加を確認しました。所属一覧からテナントを選択してください。",
      });
      await this.session.loadTenants(false, false);
    } catch (error) {
      if (this.closed || this.scope.signal.aborted) return;
      const status = error instanceof RequestFailure ? error.status : 0;
      if ([400, 403, 404, 410, 422].includes(status)) this.request = undefined;
      this.publish({
        busy: false,
        pending: !!this.request,
        notice:
          status === 429
            ? "15分間の試行回数制限です。時間をおいて同じ要求を確認してください。先の参加が失敗したとは限りません。"
            : status === 410
              ? "招待の有効期限が切れています。新しいコードを依頼してください。"
              : [403, 404].includes(status)
                ? "この招待では参加を確認できません。所属一覧を確認し、発行者にお問い合わせください。"
                : "参加の結果を確認できません。同じ要求で確認してください。",
      });
    }
  }
}
