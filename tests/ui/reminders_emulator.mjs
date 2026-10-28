import assert from "node:assert/strict";
import { PushController } from "../../apps/mobile/notifications/controller.ts";
import { nativePush } from "../../apps/mobile/platform/push.ts";
import { getReminder, listReminders, updateReminder } from "../../packages/api-client/src/index.ts";
import { RemindersController } from "../../packages/ui/src/reminders/controller.ts";
import { NotificationsController } from "../../packages/ui/src/reminders/notifications.ts";
import { SessionContext, SessionController } from "../../packages/ui/src/session.ts";

const baseUrl = process.env.UI_API_URL,
  tenant = process.env.UI_TENANT;
assert.ok(baseUrl && new URL(baseUrl).hostname === "127.0.0.1" && tenant);
const writes = [];
let lose = true;
const context = new SessionContext({
  uid: "demo-a-owner",
  tenantId: tenant,
  generation: 1,
  baseUrl,
  current: () => true,
  token: async () => "synthetic-reminder-reader",
  denied: () => {},
  transport: async (input, init) => {
    const request = new Request(input, init);
    if (request.method !== "GET")
      writes.push({
        path: new URL(request.url).pathname,
        body: await request.clone().text(),
        key: request.headers.get("Idempotency-Key"),
        match: request.headers.get("If-Match"),
      });
    const response = await fetch(request);
    if (lose && response.ok && request.method === "POST" && request.url.endsWith("/reminders")) {
      lose = false;
      await response.body?.cancel();
      throw Error("Synthetic response loss after real commit");
    }
    return response;
  },
});
const reminders = new RemindersController(context, "mobile"),
  inbox = new NotificationsController(context, "mobile"),
  display = new RemindersController(context, "display");
try {
  await inbox.load();
  assert.equal(inbox.snapshot().items.length, 1);
  const notification = inbox.snapshot().items[0];
  assert.ok(
    Date.parse(notification.expires_at) < Date.now(),
    "Audio deadline passed; inbox remains available",
  );
  await reminders.load();
  await inbox.open({ tenantId: tenant, notificationId: notification.id });
  assert.equal(inbox.snapshot().selected.notification.status, "pending");
  assert.equal(writes.length, 0);
  await inbox.acknowledge();
  assert.equal(inbox.snapshot().selected.notification.status, "acknowledged");
  assert.equal(
    reminders.snapshot().items.find((row) => row.id === notification.reminder_id).status,
    "acknowledged",
  );
  assert.equal(writes[0].match, null);
  await reminders.load();
  await reminders.select(notification.reminder_id, "snooze");
  reminders.draft({
    ...reminders.snapshot().editor.draft,
    due_at: "2099-11-01T01:30:00-04:00",
    timezone: "America/New_York",
  });
  await reminders.save();
  assert.equal(reminders.snapshot().items[0].schedule_version, 2);
  await inbox.open({ tenantId: tenant, notificationId: notification.id });
  assert.equal(inbox.snapshot().selected, undefined);
  await inbox.acknowledge();
  await context.run(async (client, signal) => {
    const result = await getReminder({
      client,
      signal,
      path: { tenant_id: tenant, reminder_id: notification.reminder_id },
    });
    assert.equal(result.data.status, "scheduled");
    assert.equal(result.data.schedule_version, 2);
    assert.equal(result.data.due_at, "2099-11-01T05:30:00Z");
  });
  reminders.create();
  reminders.draft({
    ...reminders.snapshot().editor.draft,
    title: "UI future shared",
    due_at: "2099-11-01T01:30:00-04:00",
    timezone: "America/New_York",
    visibility: "shared",
    display_policy: "shared_display",
  });
  await reminders.save();
  assert.equal(reminders.snapshot().pending, true);
  const created = await context.run(async (client, signal) =>
    (await listReminders({ client, signal, path: { tenant_id: tenant } })).data.items.find(
      (r) => r.title === "UI future shared",
    ),
  );
  async function external(version, title) {
    await context.run(async (client, signal) => {
      const result = await updateReminder({
        client,
        signal,
        path: { tenant_id: tenant, reminder_id: created.id },
        body: { title },
        headers: { "If-Match": `"${version}"`, "Idempotency-Key": crypto.randomUUID() },
      });
      assert.equal(result.response.status, 200);
    });
  }
  await external(1, "UI later canonical");
  await reminders.retry();
  assert.equal(
    reminders.snapshot().items.find((r) => r.id === created.id).title,
    "UI later canonical",
  );
  const createWrites = writes.filter((w) => w.path.endsWith("/reminders"));
  assert.deepEqual(createWrites[0], createWrites[1]);
  await reminders.select(created.id);
  reminders.draft({ ...reminders.snapshot().editor.draft, title: "UI retained draft" });
  await external(2, "UI concurrent");
  await reminders.save();
  assert.equal(reminders.snapshot().conflict.current.version, 3);
  assert.equal(reminders.snapshot().editor.draft.title, "UI retained draft");
  reminders.continueEditing();
  await reminders.save();
  await reminders.select(created.id);
  reminders.draft({ ...reminders.snapshot().editor.draft, status: "cancelled" });
  await reminders.save();
  await display.load();
  assert.equal(display.snapshot().items.length, 1);
  assert.equal(display.snapshot().items[0].status, "cancelled");
  const before = writes.length;
  display.create();
  await display.select(created.id, "snooze");
  await display.save();
  assert.equal(writes.length, before);
  // Synthetic native bridge, real HTTP + Firestore registration and closing-UID removal.
  const listeners = new Map();
  let current = { uid: "demo-a-owner" };
  const authListeners = new Set();
  const auth = {
    storage: "fake",
    current: async () => current,
    token: async () => "synthetic-reminder-reader",
    subscribe: (fn) => {
      authListeners.add(fn);
      queueMicrotask(() => fn(current));
      return () => authListeners.delete(fn);
    },
    signIn: async () => {},
    signOut: async () => {
      current = null;
      for (const fn of authListeners) fn(null);
    },
  };
  const session = new SessionController({ auth, baseUrl });
  const push = new PushController(
    session,
    nativePush({
      addListener: async (name, fn) => {
        listeners.set(name, fn);
        return { remove: async () => listeners.delete(name) };
      },
      checkPermissions: async () => ({ receive: "granted" }),
      requestPermissions: async () => ({ receive: "granted" }),
      getToken: async () => ({ token: "UI_SYNTHETIC_FCM" }),
      deleteToken: async () => {},
    }),
    "android",
  );
  const stop = session.start(),
    stopPush = push.start();
  try {
    for (let i = 0; i < 100 && !session.globalContext(); i++)
      await new Promise((r) => setTimeout(r, 5));
    await push.enable();
    assert.equal(push.snapshot().registered, true);
    listeners.get("tokenReceived")({ token: "UI_SYNTHETIC_ROTATED_FCM" });
    for (let i = 0; i < 100 && push.snapshot().busy; i++)
      await new Promise((r) => setTimeout(r, 5));
    await session.signOut();
    assert.equal(session.snapshot().phase, "signed-out");
  } finally {
    stopPush();
    stop();
  }
  console.log(
    "UI HTTP/Firestore past audio deadline retains inbox/ack, canonical acknowledgement sync, reminder canonical replay, 412, explicit timezone, readonly display, notification old generation and global installation passed",
  );
} finally {
  context.dispose();
}
