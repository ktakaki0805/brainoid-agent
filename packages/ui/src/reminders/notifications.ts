import {
  ackNotification,
  type Client,
  getNotification,
  getReminder,
  listNotifications,
  type Notification,
  type Reminder,
} from "@brainoid-internal/api-client";
import { isId } from "../chat/events.ts";
import { RequestFailure, type SessionContext, StaleContext } from "../session.ts";
import { validTimestamp } from "../tasks/controller.ts";
import { publishReminder } from "./canonical.ts";
import { policies, validateReminder } from "./controller.ts";
export interface NotificationHint {
  tenantId: string;
  notificationId: string;
}
/** Only IDs cross the OS boundary. Payload title/body/links are deliberately never read. */
export function notificationHint(value: unknown): NotificationHint | undefined {
  if (!value || typeof value !== "object") return;
  const data = value as Record<string, unknown>;
  if (!isId(data.tenant_id) || !isId(data.notification_id)) return;
  return { tenantId: data.tenant_id, notificationId: data.notification_id };
}
export const deliveryStates: Record<Notification["status"], string> = {
  pending: "配信待ち（受付未確認）",
  sent: "配信受付済み（端末表示は未確認）",
  displayed: "表示記録あり（本人確認は未完了）",
  acknowledged: "本人確認済み",
  expired: "期限切れ",
};
export function validateNotification(value: Notification, context: SessionContext): Notification {
  if (
    !value ||
    !isId(value.id) ||
    !isId(value.reminder_id) ||
    value.tenant_id !== context.tenantId ||
    !Number.isSafeInteger(value.version) ||
    value.version < 1 ||
    typeof value.title !== "string" ||
    !value.title ||
    [...value.title].length > 200 ||
    !Object.hasOwn(deliveryStates, value.status) ||
    !["private", "shared"].includes(value.visibility) ||
    !Object.hasOwn(policies, value.display_policy) ||
    (value.visibility === "private" && value.display_policy === "shared_display") ||
    !["user", "device"].includes(value.target_kind) ||
    typeof value.target_id !== "string" ||
    !value.target_id ||
    (value.target_kind === "user" && value.target_id !== context.uid) ||
    (value.target_kind === "device" && !isId(value.target_id)) ||
    !validTimestamp(value.created_at) ||
    !validTimestamp(value.updated_at) ||
    !validTimestamp(value.expires_at)
  )
    throw new Error("Invalid notification");
  return value;
}
type Selection = { notification: Notification; reminder: Reminder };
interface InboxState {
  items: readonly Notification[];
  busy: boolean;
  closed: boolean;
  pending: boolean;
  selected?: Selection | undefined;
  notice?: string | undefined;
}
const empty = (): InboxState => ({ items: [], busy: false, closed: false, pending: false });
const data = <T>(result: { data?: T; response?: Response }): NonNullable<T> => {
  if (!result.response?.ok || result.data == null)
    throw new RequestFailure(result.response?.status ?? 0);
  return result.data;
};
export class NotificationsController {
  private state = empty();
  private listeners = new Set<() => void>();
  private release: () => void;
  private abort = new AbortController();
  private epoch = 0;
  private mutation?: { id: string; reminderId: string; schedule: number; key: string } | undefined;
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
  private publish(state: InboxState) {
    this.state = state;
    for (const listener of this.listeners) listener();
  }
  dispose() {
    if (this.state.closed) return;
    this.abort.abort();
    this.mutation = undefined;
    this.publish({ ...empty(), closed: true });
    this.release?.();
  }
  private async request<T>(
    work: (client: Client, signal: AbortSignal, tenant: string) => Promise<T>,
  ): Promise<T> {
    if (this.state.closed) throw new StaleContext();
    const epoch = this.epoch;
    const signal = AbortSignal.any([
      this.context.signal,
      this.abort.signal,
      AbortSignal.timeout(15_000),
    ]);
    let detach = () => {};
    try {
      const aborted = new Promise<never>((_resolve, reject) => {
        const fail = () =>
          reject(
            epoch !== this.epoch || this.state.closed || this.context.signal.aborted
              ? new StaleContext()
              : new Error("Request timed out"),
          );
        signal.addEventListener("abort", fail, { once: true });
        detach = () => signal.removeEventListener("abort", fail);
        if (signal.aborted) fail();
      });
      const result = await Promise.race([
        this.context.run((client, _signal, tenant) => work(client, signal, tenant ?? "")),
        aborted,
      ]);
      if (epoch !== this.epoch || this.state.closed) throw new StaleContext();
      signal.throwIfAborted();
      return result;
    } finally {
      detach();
    }
  }

  private visible(value: Notification) {
    return (
      value.status !== "expired" &&
      (this.surface !== "display" ||
        (value.visibility === "shared" && value.display_policy === "shared_display"))
    );
  }
  private fail() {
    if (!this.state.closed && !this.context.signal.aborted)
      this.publish({
        ...this.state,
        busy: false,
        selected: undefined,
        items: [],
        pending: !!this.mutation,
        notice: this.mutation
          ? "確認操作の受付結果が不明です。同じ操作を再確認してください。"
          : "現在表示できる通知を確認できません。所属・期限・接続を確認して再取得してください。",
      });
  }
  invalidate() {
    if (this.state.closed) return;
    this.epoch++;
    this.abort.abort();
    this.abort = new AbortController();
    this.mutation = undefined;
    this.publish(empty());
  }
  async load() {
    if (this.state.closed || this.state.busy || this.mutation) return;
    this.publish({ ...empty(), busy: true });
    try {
      const items: Notification[] = [],
        ids = new Set<string>(),
        cursors = new Set<string>();
      let cursor: string | undefined;
      for (let pageIndex = 0; pageIndex < 100; pageIndex++) {
        const page = await this.request(async (client, signal, tenant) =>
          data(
            await listNotifications({
              client,
              signal,
              path: { tenant_id: tenant },
              query: { limit: 100, ...(cursor ? { cursor } : {}) },
            }),
          ),
        );
        if (!Array.isArray(page.items) || page.items.length > 100) throw new Error("Invalid page");
        for (const item of page.items) {
          const notification = validateNotification(item, this.context);
          if (ids.has(item.id)) throw new Error("Duplicate notification");
          ids.add(item.id);
          if (this.visible(notification)) items.push(notification);
        }
        if (page.next_cursor === null) {
          this.publish({ ...this.state, items, busy: false });
          return;
        }
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
    } catch (error) {
      if (!(error instanceof StaleContext)) this.fail();
    }
  }
  private async notification(id: string) {
    const result = await this.request((client, signal, tenant) =>
      getNotification({ client, signal, path: { tenant_id: tenant, notification_id: id } }),
    );
    const value = validateNotification(data(result), this.context);
    if (
      value.id !== id ||
      result.response?.headers.get("ETag") !== `"${value.version}"` ||
      !this.visible(value)
    )
      throw new Error("Unavailable notification");
    return value;
  }
  private async current(id: string): Promise<Selection> {
    const epoch = this.epoch;
    const assert = () => {
      if (epoch !== this.epoch || this.state.closed) throw new StaleContext();
    };
    const first = await this.notification(id);
    assert();
    const result = await this.request((client, signal, tenant) =>
      getReminder({ client, signal, path: { tenant_id: tenant, reminder_id: first.reminder_id } }),
    );
    assert();
    const reminder = validateReminder(data(result), this.context);
    if (
      reminder.id !== first.reminder_id ||
      result.response?.headers.get("ETag") !== `"${reminder.version}"` ||
      ["cancelled", "expired"].includes(reminder.status)
    )
      throw new Error("Unavailable reminder");
    // Notification has no public schedule_version. The second authenticated GET rechecks
    // the server's source/current_schedule after reading the canonical reminder.
    const notification = await this.notification(id);
    assert();
    if (
      notification.reminder_id !== reminder.id ||
      notification.title !== reminder.title ||
      notification.visibility !== reminder.visibility ||
      notification.display_policy !== reminder.display_policy ||
      notification.target_kind !== reminder.delivery_target.kind ||
      notification.target_id !== reminder.delivery_target.id ||
      (notification.status === "acknowledged" && reminder.status !== "acknowledged")
    )
      throw new Error("Changed notification");
    return { reminder, notification };
  }
  async open(hint: NotificationHint) {
    if (
      this.state.closed ||
      this.mutation ||
      !isId(hint.notificationId) ||
      hint.tenantId !== this.context.tenantId
    )
      return;
    this.invalidate();
    this.publish({ ...this.state, selected: undefined, items: [], busy: true, notice: undefined });
    try {
      const selected = await this.current(hint.notificationId);
      publishReminder(this.context, selected.reminder);
      this.publish({ ...this.state, selected, busy: false });
    } catch (error) {
      if (!(error instanceof StaleContext)) this.fail();
    }
  }
  async acknowledge() {
    const selected = this.state.selected;
    if (
      this.surface !== "mobile" ||
      !selected ||
      this.state.busy ||
      this.state.closed ||
      this.mutation ||
      selected.notification.status === "acknowledged"
    )
      return;
    this.mutation = {
      id: selected.notification.id,
      reminderId: selected.reminder.id,
      schedule: selected.reminder.schedule_version,
      key: crypto.randomUUID(),
    };
    await this.retry();
  }
  async retry() {
    const mutation = this.mutation;
    if (!mutation || this.state.closed || this.state.busy || this.surface !== "mobile") return;
    this.publish({
      ...this.state,
      selected: undefined,
      items: [],
      busy: true,
      pending: true,
      notice: undefined,
    });
    try {
      const before = await this.current(mutation.id);
      if (
        before.reminder.id !== mutation.reminderId ||
        before.reminder.schedule_version !== mutation.schedule
      ) {
        this.mutation = undefined;
        throw new Error("Old schedule");
      }
      const result = await this.request((client, signal, tenant) =>
        ackNotification({
          client,
          signal,
          path: { tenant_id: tenant, notification_id: mutation.id },
          body: { action: "acknowledged" },
          headers: { "Idempotency-Key": mutation.key },
        }),
      );
      const value = validateNotification(data(result), this.context);
      if (
        result.response?.status !== 200 ||
        value.id !== mutation.id ||
        value.reminder_id !== mutation.reminderId ||
        value.status !== "acknowledged" ||
        result.response.headers.get("ETag") !== `"${value.version}"`
      )
        throw new Error("Invalid acknowledgement");
      this.mutation = undefined;
      const selected = await this.current(mutation.id);
      if (selected.reminder.schedule_version !== mutation.schedule) throw new Error("Old schedule");
      publishReminder(this.context, selected.reminder);
      this.publish({
        ...this.state,
        selected,
        busy: false,
        pending: false,
        notice: "この通知の本人確認を保存しました。",
      });
    } catch (error) {
      if (error instanceof StaleContext) return;
      if (
        error instanceof RequestFailure &&
        [400, 403, 404, 409, 410, 412, 422].includes(error.status)
      )
        this.mutation = undefined;
      this.fail();
    }
  }
}
