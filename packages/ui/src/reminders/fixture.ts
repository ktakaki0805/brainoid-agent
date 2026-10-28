import type { Notification, Reminder, ReminderCreate } from "@brainoid-internal/api-client";
export const sharedReminderId = "a0000000-0000-4000-8000-000000000001";
export const privateReminderId = "a0000000-0000-4000-8000-000000000002";
export const sharedNotificationId = "b0000000-0000-4000-8000-000000000001";
export const privateNotificationId = "b0000000-0000-4000-8000-000000000002";
const at = "2026-10-28T00:00:00Z";
export type ReminderScenario =
  | "normal"
  | "empty"
  | "conflict"
  | "lost_save"
  | "failed"
  | "denied"
  | "expired"
  | "audio_expired";
export function createReminderFixture() {
  const tenants = new Map<string, Map<string, Reminder>>();
  const receipts = new Map<string, { hash: string; id: string }>();
  const controls = {
    scenario: "normal" as ReminderScenario,
    delay: 0,
    calls: [] as {
      method: string;
      path: string;
      key: string | null;
      match: string | null;
      body: unknown;
    }[],
  };
  const make = (id: string, tenant: string, uid: string, body: ReminderCreate): Reminder => ({
    ...body,
    id,
    tenant_id: tenant,
    status: "scheduled",
    schedule_version: 1,
    version: 1,
    created_at: at,
    updated_at: at,
    ...(body.visibility === "private" ? { owner_user_id: uid } : {}),
  });
  function records(tenant: string, uid: string) {
    let rows = tenants.get(`${tenant}:${uid}`);
    if (!rows) {
      rows = new Map([
        [
          sharedReminderId,
          make(sharedReminderId, tenant, uid, {
            title: "みんなの防災確認",
            due_at: "2099-11-01T05:30:00Z",
            timezone: "America/New_York",
            delivery_target: { kind: "user", id: uid },
            visibility: "shared",
            display_policy: "shared_display",
          }),
        ],
        [
          privateReminderId,
          make(privateReminderId, tenant, uid, {
            title: "個人の受診予定",
            due_at: "2099-11-01T09:00:00Z",
            timezone: "Asia/Tokyo",
            delivery_target: { kind: "user", id: uid },
            visibility: "private",
            display_policy: "never",
          }),
        ],
      ]);
      tenants.set(`${tenant}:${uid}`, rows);
    }
    return rows;
  }
  function notification(id: string, rows: Map<string, Reminder>): Notification | undefined {
    const reminder = rows.get(
      id === sharedNotificationId
        ? sharedReminderId
        : id === privateNotificationId
          ? privateReminderId
          : "",
    );
    if (
      reminder?.schedule_version !== 1 ||
      ["cancelled", "expired"].includes(reminder.status) ||
      controls.scenario === "expired"
    )
      return;
    return {
      id,
      tenant_id: reminder.tenant_id,
      version: reminder.version,
      created_at: at,
      updated_at: at,
      reminder_id: reminder.id,
      title: reminder.title,
      status: reminder.status === "acknowledged" ? "acknowledged" : "sent",
      target_kind: reminder.delivery_target.kind,
      target_id: reminder.delivery_target.id,
      expires_at:
        controls.scenario === "audio_expired" ? "2020-01-01T00:00:00Z" : "2099-12-01T00:00:00Z",
      visibility: reminder.visibility,
      display_policy: reminder.display_policy,
    };
  }
  async function handle(request: Request, uid: string): Promise<Response | undefined> {
    const url = new URL(request.url),
      route =
        /^\/v1\/tenants\/([^/]+)\/(reminders|notifications)(?:\/([^/:]+)(?::(snooze|ack))?)?$/.exec(
          url.pathname,
        );
    if (!route) return;
    const tenant = route[1] ?? "",
      kind = route[2],
      id = route[3],
      action = route[4],
      rows = records(tenant, uid);
    const body = request.method === "GET" ? undefined : await request.clone().json();
    const key = request.headers.get("Idempotency-Key"),
      match = request.headers.get("If-Match");
    controls.calls.push({ method: request.method, path: url.pathname, key, match, body });
    if (controls.delay) await new Promise((resolve) => setTimeout(resolve, controls.delay));
    if (controls.scenario === "denied") return Response.json({}, { status: 403 });
    const result = (value: Reminder | Notification, status = 200) =>
      Response.json(value, { status, headers: { ETag: `"${value.version}"` } });
    if (kind === "notifications") {
      if (!id)
        return Response.json({
          items:
            controls.scenario === "empty"
              ? []
              : [sharedNotificationId, privateNotificationId].flatMap((id) => {
                  const value = notification(id, rows);
                  return value ? [value] : [];
                }),
          next_cursor: null,
        });
      const value = notification(id, rows);
      if (!value) return Response.json({}, { status: 404 });
      if (action === "ack") {
        if (!key || body.action !== "acknowledged") return Response.json({}, { status: 400 });
        const reminder = rows.get(value.reminder_id);
        if (!reminder) return Response.json({}, { status: 404 });
        rows.set(reminder.id, {
          ...reminder,
          status: "acknowledged",
          version: reminder.status === "acknowledged" ? reminder.version : reminder.version + 1,
        });
        return result(notification(id, rows) as Notification);
      }
      return result(value);
    }
    if (request.method === "GET") {
      if (id) {
        const value = rows.get(id);
        return value ? result(value) : Response.json({}, { status: 404 });
      }
      return Response.json({
        items:
          controls.scenario === "empty"
            ? []
            : [...rows.values()].filter(
                (row) =>
                  !url.searchParams.get("status") || row.status === url.searchParams.get("status"),
              ),
        next_cursor: null,
      });
    }
    if (controls.scenario === "failed") throw new TypeError("Synthetic disconnect");
    if (!key) return Response.json({}, { status: 400 });
    const receiptKey = `${tenant}:${uid}:${key}`,
      hash = JSON.stringify([request.method, url.pathname, match, body]),
      receipt = receipts.get(receiptKey);
    if (receipt) {
      if (receipt.hash !== hash) return Response.json({}, { status: 409 });
      const value = rows.get(receipt.id);
      return value ? result(value, id ? 200 : 201) : Response.json({}, { status: 404 });
    }
    const original = id ? rows.get(id) : undefined;
    if (id && !original) return Response.json({}, { status: 404 });
    if (original && controls.scenario === "conflict") {
      rows.set(original.id, {
        ...original,
        title: "別の端末で変更した予定",
        version: original.version + 1,
      });
      controls.scenario = "normal";
      return Response.json({}, { status: 412 });
    }
    if (
      original &&
      (action === "snooze"
        ? body.expected_version !== original.version
        : match !== `"${original.version}"`)
    )
      return Response.json({}, { status: 412 });
    const changed =
      original &&
      (action === "snooze" ||
        (body.due_at && body.due_at !== original.due_at) ||
        (body.timezone && body.timezone !== original.timezone) ||
        (body.status === "cancelled" && original.status !== "cancelled"));
    const { expected_version: _expected, ...patch } = body;
    const value: Reminder = original
      ? {
          ...original,
          ...patch,
          status:
            body.status === "cancelled" ? "cancelled" : changed ? "scheduled" : original.status,
          version: original.version + 1,
          schedule_version: original.schedule_version + (changed ? 1 : 0),
        }
      : make(crypto.randomUUID(), tenant, uid, body);
    rows.set(value.id, value);
    receipts.set(receiptKey, { hash, id: value.id });
    if (controls.scenario === "lost_save") {
      controls.scenario = "normal";
      throw new TypeError("Synthetic response loss");
    }
    return result(value, original ? 200 : 201);
  }
  return { controls, handle, records };
}
