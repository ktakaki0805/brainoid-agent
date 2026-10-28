import {
  type Client,
  type Device,
  type DevicePatch,
  type PairingConfirm,
  type Tenant,
  confirmDevicePairing,
  getDevice,
  getTenant,
  listDevices,
  revokeDevice,
  updateDevice,
} from "@brainoid-internal/api-client";
import { isId } from "../chat/events.ts";
import { RequestFailure, type SessionContext, StaleContext } from "../session.ts";
import { device, freeze, pairing, patch } from "./contracts.ts";

export interface DeviceState {
  tenant?: Tenant | undefined;
  devices: readonly Device[];
  selected?: Device | undefined;
  pairing?: PairingConfirm | undefined;
  editing?: DevicePatch | undefined;
  review?: { body: PairingConfirm; tenant: Tenant } | undefined;
  conflict?: { before: Device; current?: Device } | undefined;
  revokedId?: string | undefined;
  busy: boolean;
  pending: boolean;
  closed: boolean;
  notice?: string | undefined;
}
type Mutation = { key: string } & (
  | { kind: "pair"; body: PairingConfirm; tenant: Tenant }
  | { kind: "update"; body: DevicePatch; original: Device }
  | { kind: "revoke"; original: Device }
);
const empty = (): DeviceState => ({ devices: [], busy: false, pending: false, closed: false });
const data = <T>(r: { data?: T; response?: Response }): NonNullable<T> => {
  if (!r.response?.ok || r.data == null) throw new RequestFailure(r.response?.status ?? 0);
  return r.data;
};
class RoleLost extends Error {}
/** One serialized workspace owns canonical list/detail, confirmation and exact retry. */
export class DevicesController {
  private state = empty();
  private listeners = new Set<() => void>();
  private closed = false;
  private abort = new AbortController();
  private release: () => void;
  private mutation?: Mutation | undefined;
  readonly context: SessionContext;
  readonly surface: "mobile" | "display";
  constructor(context: SessionContext, surface: "mobile" | "display") {
    this.context = context;
    this.surface = surface;
    this.release = context.own(() => this.dispose());
  }
  snapshot = () => this.state;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  private publish(state: DeviceState) {
    this.state = freeze(state);
    for (const listener of this.listeners) listener();
  }
  private assert() {
    this.context.assertCurrent();
    if (this.closed) throw new StaleContext();
  }
  dispose() {
    if (this.closed) return;
    this.closed = true;
    this.abort.abort();
    this.mutation = undefined;
    this.publish({ ...empty(), closed: true });
    this.release?.();
  }
  private available() {
    try {
      this.assert();
      return this.surface === "mobile" && !this.state.busy && !this.mutation;
    } catch {
      return false;
    }
  }
  private async request<T>(
    work: (client: Client, signal: AbortSignal, tenant: string) => Promise<T>,
  ): Promise<T> {
    this.assert();
    const value = await this.context.run((client, signal, tenant) => {
      if (!tenant) throw new StaleContext();
      return work(client, AbortSignal.any([signal, this.abort.signal]), tenant);
    });
    this.assert();
    return value;
  }
  private async authorize() {
    const tenant = data(
      await this.request((client, signal, id) =>
        getTenant({ client, signal, path: { tenant_id: id } }),
      ),
    );
    if (
      tenant.id !== this.context.tenantId ||
      tenant.tenant_id !== tenant.id ||
      tenant.status !== "active" ||
      typeof tenant.name !== "string" ||
      !Number.isSafeInteger(tenant.version) ||
      tenant.version < 1 ||
      !["owner", "admin", "member"].includes(tenant.my_role ?? "")
    )
      throw new Error("Invalid tenant");
    if (tenant.my_role !== "owner" && tenant.my_role !== "admin") {
      this.mutation = undefined;
      this.publish({
        ...empty(),
        tenant,
        notice: "端末の登録・設定にはオーナーまたは管理者の権限が必要です。",
      });
      throw new RoleLost();
    }
    this.publish({ ...this.state, tenant });
    return tenant;
  }
  private canonical(v: Device) {
    const old =
      this.state.devices.find((row) => row.id === v.id) ??
      (this.state.selected?.id === v.id ? this.state.selected : undefined);
    if (old) {
      // Metadata version advances only after the independent authorization ledger finishes.
      // A pending intent can therefore suspend the current device at the same version,
      // and DB application can advance its authorization/generation before metadata does.
      const authorization = v.authorization_version ?? 0,
        previousAuthorization = old.authorization_version ?? 0;
      const sameField = (field: keyof Device) => {
        const left = v[field],
          right = old[field];
        return (
          JSON.stringify(Array.isArray(left) ? [...left].sort() : left) ===
          JSON.stringify(Array.isArray(right) ? [...right].sort() : right)
        );
      };
      if (
        v.version < old.version ||
        authorization < previousAuthorization ||
        v.connection_generation < old.connection_generation ||
        (v.version === old.version &&
          !(["name", "created_at", "updated_at", "capabilities"] as const).every(sameField)) ||
        (authorization === previousAuthorization &&
          !(["allowed_scopes", "allowed_entity_ids", "allowed_room_ids"] as const).every(
            sameField,
          )) ||
        (old.status === "revoked" && ["active", "offline"].includes(v.status)) ||
        (old.status === "suspended" &&
          ["active", "offline"].includes(v.status) &&
          v.version === old.version &&
          authorization === previousAuthorization &&
          v.connection_generation === old.connection_generation)
      )
        throw new Error("Device version regression");
    }
    return v;
  }
  private apply(v: Device) {
    this.canonical(v);
    this.publish({
      ...this.state,
      devices: [...this.state.devices.filter((row) => row.id !== v.id), v],
      selected: v,
    });
  }
  private async read(id: string) {
    const result = await this.request((client, signal, tenant) =>
      getDevice({ client, signal, path: { tenant_id: tenant, device_id: id } }),
    );
    const value = device(data(result), this.context.tenantId ?? "", id);
    if (result.response?.headers.get("ETag") !== `"${value.version}"`)
      throw new Error("Invalid device version");
    return this.canonical(value);
  }
  private fail(error: unknown) {
    if (this.closed || error instanceof StaleContext || this.context.signal.aborted) return;
    try {
      this.assert();
    } catch {
      this.dispose();
      return;
    }
    if (error instanceof RoleLost) return;
    if (error instanceof RequestFailure && [401, 403, 404].includes(error.status)) {
      this.mutation = undefined;
      this.publish({
        ...empty(),
        notice: "現在の権限では端末を管理できません。所属を確認し直してください。",
      });
    } else
      this.publish({
        ...this.state,
        busy: false,
        notice: "端末情報を確認できません。通信を確認して再取得してください。",
      });
  }
  async load() {
    if (!this.available()) return;
    this.publish({
      ...this.state,
      busy: true,
      selected: undefined,
      pairing: undefined,
      editing: undefined,
      review: undefined,
      conflict: undefined,
      notice: undefined,
    });
    try {
      await this.authorize();
      const rows: Device[] = [],
        cursors = new Set<string>();
      let cursor: string | undefined;
      do {
        const page = data(
          await this.request((client, signal, tenant) =>
            listDevices({
              client,
              signal,
              path: { tenant_id: tenant },
              query: { limit: 100, ...(cursor ? { cursor } : {}) },
            }),
          ),
        );
        if (
          !Array.isArray(page.items) ||
          page.items.length > 100 ||
          (page.next_cursor !== null &&
            (typeof page.next_cursor !== "string" ||
              !page.next_cursor ||
              page.next_cursor.length > 2048))
        )
          throw new Error("Invalid device page");
        for (const item of page.items) {
          const v = this.canonical(device(item, this.context.tenantId ?? ""));
          if (rows.some((row) => row.id === v.id)) throw new Error("Duplicate device");
          // A later page failure must not resurrect a device already observed as stopped.
          // Only retire an existing row here; publish the complete collection after all pages.
          if (v.status === "suspended" || v.status === "revoked") {
            this.publish({
              ...this.state,
              devices: this.state.devices.map((row) => (row.id === v.id ? v : row)),
            });
          }
          rows.push(v);
        }
        cursor = page.next_cursor ?? undefined;
        if (cursor) {
          if (cursors.has(cursor) || cursors.size >= 99) throw new Error("Invalid cursor");
          cursors.add(cursor);
        }
      } while (cursor);
      this.publish({ ...this.state, devices: rows, busy: false });
    } catch (error) {
      this.fail(error);
    }
  }
  async select(id: string) {
    if (!this.available() || !isId(id)) return;
    this.publish({
      ...this.state,
      busy: true,
      selected: undefined,
      editing: undefined,
      pairing: undefined,
      review: undefined,
      conflict: undefined,
      notice: undefined,
    });
    try {
      await this.authorize();
      this.apply(await this.read(id));
      this.publish({ ...this.state, busy: false });
    } catch (error) {
      this.fail(error);
    }
  }
  async beginPair(replaces?: string) {
    if (!this.available() || (replaces !== undefined && !isId(replaces))) return;
    this.publish({
      ...this.state,
      busy: true,
      pairing: undefined,
      review: undefined,
      editing: undefined,
      conflict: undefined,
      notice: undefined,
    });
    try {
      await this.authorize();
      if (replaces) {
        const old = await this.read(replaces);
        this.apply(old);
        if (old.status !== "revoked") throw new Error("Revoke first");
      }
      this.publish({
        ...this.state,
        busy: false,
        selected: undefined,
        pairing: {
          user_code: "",
          device_name: "",
          allowed_entity_ids: [],
          ...(replaces ? { replaces_device_id: replaces } : {}),
        },
      });
    } catch (error) {
      this.fail(error);
    }
  }
  draftPair(body: PairingConfirm) {
    if (
      !this.available() ||
      !this.state.pairing ||
      body.replaces_device_id !== this.state.pairing.replaces_device_id
    )
      return;
    this.publish({
      ...this.state,
      pairing: structuredClone(body),
      review: undefined,
      notice: undefined,
    });
  }
  async reviewPair() {
    if (!this.available() || !this.state.pairing || !pairing(this.state.pairing)) return;
    const body = structuredClone(this.state.pairing);
    this.publish({ ...this.state, busy: true, review: undefined, notice: undefined });
    try {
      const tenant = await this.authorize();
      this.publish({ ...this.state, busy: false, review: { body, tenant } });
    } catch (error) {
      this.fail(error);
    }
  }
  async confirmPair(tenantId: string, confirmed: boolean) {
    const review = this.state.review;
    if (
      !this.available() ||
      !review ||
      !confirmed ||
      tenantId !== review.tenant.id ||
      !pairing(review.body)
    )
      return;
    this.mutation = freeze({
      kind: "pair",
      key: crypto.randomUUID(),
      body: structuredClone(review.body),
      tenant: review.tenant,
    });
    await this.send();
  }
  edit() {
    const row = this.state.selected;
    if (!this.available() || !row || row.status === "revoked" || row.status === "suspended") return;
    this.publish({
      ...this.state,
      editing: {
        name: row.name,
        allowed_scopes: [...(row.allowed_scopes ?? [])],
        allowed_entity_ids: [...row.allowed_entity_ids],
        allowed_room_ids: [...(row.allowed_room_ids ?? [])],
      },
      conflict: undefined,
      notice: undefined,
    });
  }
  draftEdit(body: DevicePatch) {
    if (this.available() && this.state.editing)
      this.publish({ ...this.state, editing: structuredClone(body) });
  }
  async save() {
    const original = this.state.selected,
      body = this.state.editing;
    if (
      !this.available() ||
      !original ||
      !body ||
      !patch(body) ||
      this.state.conflict ||
      ["revoked", "suspended"].includes(original.status)
    )
      return;
    this.mutation = freeze({
      kind: "update",
      key: crypto.randomUUID(),
      original,
      body: structuredClone(body),
    });
    await this.send();
  }
  async revoke(confirmed: boolean) {
    const original = this.state.selected;
    if (
      !this.available() ||
      !original ||
      !confirmed ||
      this.state.conflict ||
      original.status === "revoked"
    )
      return;
    this.mutation = freeze({ kind: "revoke", key: crypto.randomUUID(), original });
    await this.send();
  }
  async retry() {
    if (!this.closed && !this.state.busy && this.mutation) await this.send();
  }
  private async send() {
    const m = this.mutation;
    if (!m) return;
    const uncertain = this.state.pending;
    this.publish({ ...this.state, busy: true, pending: true, notice: undefined });
    let transmitted = false;
    try {
      const tenant = await this.authorize();
      if (
        m.kind === "pair" &&
        !uncertain &&
        (tenant.id !== m.tenant.id ||
          tenant.name !== m.tenant.name ||
          tenant.my_role !== m.tenant.my_role ||
          tenant.version !== m.tenant.version)
      ) {
        this.mutation = undefined;
        this.publish({
          ...this.state,
          busy: false,
          pending: false,
          review: undefined,
          notice: "登録先の情報が変わりました。登録内容を確認し直してください。",
        });
        return;
      }
      const headers = { "Idempotency-Key": m.key };
      transmitted = true;
      if (m.kind === "revoke") {
        const result = await this.request((client, signal, id) =>
          revokeDevice({
            client,
            signal,
            path: { tenant_id: id, device_id: m.original.id },
            headers: { ...headers, "If-Match": `"${m.original.version}"` },
          }),
        );
        if (result.response?.status !== 204) throw new RequestFailure(result.response?.status ?? 0);
        this.mutation = undefined;
        this.publish({
          ...this.state,
          devices: this.state.devices.filter((v) => v.id !== m.original.id),
          selected: undefined,
          editing: undefined,
          conflict: undefined,
          revokedId: m.original.id,
          busy: false,
          pending: false,
          notice: "クラウドで端末を失効しました。端末内の秘密の消去は未確認です。",
        });
        return;
      }
      const result =
        m.kind === "pair"
          ? await this.request((client, signal, id) =>
              confirmDevicePairing({
                client,
                signal,
                path: { tenant_id: id },
                headers,
                body: m.body,
              }),
            )
          : await this.request((client, signal, id) =>
              updateDevice({
                client,
                signal,
                path: { tenant_id: id, device_id: m.original.id },
                headers: { ...headers, "If-Match": `"${m.original.version}"` },
                body: m.body,
              }),
            );
      const v = device(
        data(result),
        this.context.tenantId ?? "",
        m.kind === "update" ? m.original.id : undefined,
      );
      if (
        m.kind === "pair"
          ? result.response?.status !== 201 || v.status !== "active"
          : result.response?.headers.get("ETag") !== `"${v.version}"` ||
            v.version <= m.original.version ||
            ["revoked", "suspended"].includes(v.status)
      )
        throw new Error("Unconfirmed device mutation");
      const expected =
        m.kind === "pair"
          ? { name: m.body.device_name, allowed_entity_ids: m.body.allowed_entity_ids }
          : m.body;
      for (const [field, sent] of Object.entries(expected)) {
        const received = v[field as keyof Device];
        if (
          JSON.stringify(Array.isArray(sent) ? [...sent].sort() : sent) !==
          JSON.stringify(Array.isArray(received) ? [...received].sort() : received)
        )
          throw new Error("Device response differs from request");
      }
      this.apply(v);
      this.mutation = undefined;
      this.publish({
        ...this.state,
        busy: false,
        pending: false,
        pairing: undefined,
        review: undefined,
        editing: undefined,
        conflict: undefined,
        revokedId:
          m.kind === "pair" && m.body.replaces_device_id === this.state.revokedId
            ? undefined
            : this.state.revokedId,
        notice:
          m.kind === "pair"
            ? "登録を承認しました。Pi側で接続状態を確認してください。"
            : "設定を保存しました。許可範囲を変更した場合、再接続には失効後の再登録が必要です。",
      });
    } catch (error) {
      if (this.closed || this.context.signal.aborted) return;
      if (error instanceof RoleLost || error instanceof StaleContext) {
        this.fail(error);
        return;
      }
      if (error instanceof RequestFailure && [401, 403, 404].includes(error.status)) {
        this.fail(error);
        return;
      }
      if (error instanceof RequestFailure && error.status === 412 && m.kind !== "pair") {
        this.mutation = undefined;
        this.publish({
          ...this.state,
          pending: false,
          editing: undefined,
          selected: undefined,
          devices: this.state.devices.filter((v) => v.id !== m.original.id),
          conflict: { before: m.original },
          notice: "別の操作で端末が更新されました。最新の内容を確認してから操作してください。",
        });
        try {
          const current = await this.read(m.original.id);
          this.apply(current);
          this.publish({ ...this.state, conflict: { before: m.original, current }, busy: false });
        } catch (readError) {
          this.fail(readError);
        }
      } else if (
        uncertain &&
        error instanceof RequestFailure &&
        error.status >= 400 &&
        error.status < 500
      ) {
        // A refusal of this retry does not settle the earlier request whose response was lost.
        // In particular, rate limiting and pairing expiry are checked before the receipt.
        this.publish({
          ...this.state,
          busy: false,
          pending: true,
          notice:
            error.status === 429
              ? "試行回数の上限です。前回の結果は未確認です。時間をおいて同じ操作の受付を再確認してください。"
              : "前回の結果は未確認のままです。所属を選び直して端末一覧とPiの状態を照合してください。新しいコードで登録をやり直さないでください。",
        });
      } else if (error instanceof RequestFailure && error.status >= 400 && error.status < 500) {
        this.mutation = undefined;
        this.publish({
          ...this.state,
          busy: false,
          pending: false,
          review: undefined,
          notice:
            error.status === 429
              ? "試行回数の上限です。操作は受け付けられませんでした。時間をおいて内容を確認し直してください。"
              : m.kind === "pair"
                ? "登録できませんでした。コードの期限切れ・使用済み、登録先の違いを確認し、Piで新しいコードを表示してください。"
                : "設定は受け付けられませんでした。最新の端末情報と許可対象を確認してください。",
        });
      } else {
        if (!transmitted && !uncertain) {
          this.mutation = undefined;
          this.fail(error);
          this.publish({ ...this.state, busy: false, pending: false });
        } else
          this.publish({
            ...this.state,
            busy: false,
            pending: true,
            notice: "操作の結果を確認できません。同じ操作の受付を再確認してください。",
          });
      }
    }
  }
  acceptCurrent() {
    if (!this.available() || !this.state.conflict?.current) return;
    this.publish({
      ...this.state,
      conflict: undefined,
      notice: "最新の内容を採用しました。必要な操作を選び直してください。",
    });
  }
  cancel() {
    if (this.available())
      this.publish({ ...this.state, pairing: undefined, review: undefined, editing: undefined });
  }
}
