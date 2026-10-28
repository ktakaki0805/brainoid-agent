import assert from "node:assert/strict";
import { test } from "node:test";
import { RemindersController, validateReminder } from "../packages/ui/src/reminders/controller.ts";
import {
  sharedNotificationId as nid,
  privateNotificationId,
  privateReminderId,
  sharedReminderId as rid,
} from "../packages/ui/src/reminders/fixture.ts";
import {
  NotificationsController,
  notificationHint,
  validateNotification,
} from "../packages/ui/src/reminders/notifications.ts";
import { SessionController } from "../packages/ui/src/session.ts";
import { createSessionFixture, tenantA, tenantB } from "../packages/ui/src/session-fixture.ts";

const uid = "synthetic@example.test",
  tick = () => new Promise((resolve) => setTimeout(resolve, 5));
async function using(work, surface = "mobile", alter = (f) => f) {
  const fixture = createSessionFixture(),
    session = new SessionController({
      auth: fixture.auth,
      baseUrl: "https://session.invalid",
      transport: alter(fixture.transport),
    });
  const stop = session.start();
  await tick();
  await session.signIn(uid, "fake");
  await session.selectTenant(tenantA);
  const context = session.snapshot().context,
    reminders = new RemindersController(context, surface),
    inbox = new NotificationsController(context, surface);
  try {
    await reminders.load();
    await inbox.load();
    await work({ fixture, session, reminders, inbox, context });
  } finally {
    stop();
  }
}
const writes = (fixture) => fixture.reminders.controls.calls.filter((c) => c.method !== "GET");
function create(reminders, changes = {}) {
  reminders.create();
  reminders.draft({
    ...reminders.snapshot().editor.draft,
    title: "架空の予定",
    due_at: "2099-11-01T01:30:00-04:00",
    timezone: "America/New_York",
    ...changes,
  });
}
async function select(reminders, changes = {}, action = "update") {
  await reminders.select(rid, action);
  reminders.draft({ ...reminders.snapshot().editor.draft, ...changes });
}

test("reminder create explicit instant timezone target task; edit cancel snooze preserve API contract", () =>
  using(async ({ reminders, fixture }) => {
    create(reminders, { task_id: "90000000-0000-4000-8000-000000000001" });
    await reminders.save();
    assert.deepEqual(writes(fixture)[0].body, {
      title: "架空の予定",
      due_at: "2099-11-01T01:30:00-04:00",
      timezone: "America/New_York",
      delivery_target: { kind: "user", id: uid },
      visibility: "private",
      display_policy: "never",
      task_id: "90000000-0000-4000-8000-000000000001",
    });
    await select(reminders, { title: "題名のみ" });
    await reminders.save();
    assert.deepEqual(writes(fixture)[1].body, { title: "題名のみ" });
    assert.equal(writes(fixture)[1].match, '"1"');
    await select(reminders, { status: "cancelled" });
    await reminders.save();
    await select(
      reminders,
      { due_at: "2099-11-02T09:00:00+09:00", timezone: "Asia/Tokyo" },
      "snooze",
    );
    await reminders.save();
    assert.deepEqual(writes(fixture).at(-1).body, {
      due_at: "2099-11-02T09:00:00+09:00",
      timezone: "Asia/Tokyo",
      expected_version: 3,
    });
    assert.equal(writes(fixture).at(-1).match, null);
    const saved = reminders.snapshot().items.find((r) => r.id === rid);
    assert.equal(saved.schedule_version, 3);
    assert.equal(saved.status, "scheduled");
  }));
test("invalid future instant, timezone, private destination and display policy never submit", () =>
  using(async ({ reminders, fixture }) => {
    for (const changes of [
      { due_at: "2020-01-01T00:00:00Z" },
      { due_at: "2099-02-30T00:00:00Z" },
      { due_at: "2099-01-01T00:00:00-00:00" },
      { due_at: "2099-01-01T00:00:00" },
      { timezone: "invalid" },
      { delivery_target: { kind: "device", id: "bad" } },
      { delivery_target: { kind: "user", id: "someone-else" } },
      { display_policy: "shared_display" },
      { task_id: "bad" },
    ]) {
      create(reminders, changes);
      await reminders.save();
      assert.equal(writes(fixture).length, 0);
      assert.ok(reminders.snapshot().notice);
      reminders.closeEditor();
    }
  }));
test("412 shows saved current alongside untouched draft and explicit continue uses fresh version", () =>
  using(async ({ reminders, fixture }) => {
    await select(reminders, { title: "未保存の題名" });
    fixture.reminders.controls.scenario = "conflict";
    await reminders.save();
    assert.equal(reminders.snapshot().editor.draft.title, "未保存の題名");
    assert.equal(reminders.snapshot().conflict.current.title, "別の端末で変更した予定");
    assert.equal(writes(fixture).length, 1);
    await reminders.save();
    assert.equal(writes(fixture).length, 1);
    reminders.continueEditing();
    await reminders.save();
    assert.equal(writes(fixture).at(-1).match, '"2"');
  }));
test("lost save retries exact request and renders later canonical changes without assuming original draft", () =>
  using(async ({ reminders, fixture }) => {
    await select(reminders, { title: "送った題名" });
    fixture.reminders.controls.scenario = "lost_save";
    await reminders.save();
    assert.equal(reminders.snapshot().pending, true);
    const records = fixture.reminders.records(tenantA, uid),
      before = records.get(rid);
    records.set(rid, { ...before, title: "その後の正本", version: before.version + 1 });
    await reminders.retry();
    const [first, retry] = writes(fixture);
    assert.deepEqual(first, retry);
    assert.equal(reminders.snapshot().pending, false);
    assert.equal(reminders.snapshot().items.find((r) => r.id === rid).title, "その後の正本");
  }));
test("T013 notification open is not ack; explicit ack uses only key and binds current identity", () =>
  using(async ({ inbox, fixture }) => {
    await inbox.open({ tenantId: tenantA, notificationId: nid });
    assert.equal(inbox.snapshot().selected.notification.status, "sent");
    assert.equal(writes(fixture).length, 0);
    await inbox.open({ tenantId: tenantA, notificationId: nid });
    assert.equal(writes(fixture).length, 0);
    await inbox.acknowledge();
    assert.equal(inbox.snapshot().selected.notification.status, "acknowledged");
    assert.equal(writes(fixture).length, 1);
    assert.equal(writes(fixture)[0].match, null);
    assert.deepEqual(writes(fixture)[0].body, { action: "acknowledged" });
    await inbox.acknowledge();
    assert.equal(writes(fixture).length, 1);
  }));
test("T038 snooze invalidates old notification and late ack never acknowledges new schedule", () =>
  using(async ({ reminders, inbox, fixture }) => {
    await inbox.open({ tenantId: tenantA, notificationId: nid });
    await select(reminders, { due_at: "2099-12-01T00:00:00Z" }, "snooze");
    await reminders.save();
    await inbox.acknowledge();
    assert.equal(inbox.snapshot().selected, undefined);
    assert.equal(fixture.reminders.records(tenantA, uid).get(rid).status, "scheduled");
    assert.equal(
      writes(fixture).some((w) => w.path.endsWith(":ack")),
      false,
    );
  }));
test("T030 payload allowlist ignores title body links and mismatched tenant never loads content", () =>
  using(async ({ session, inbox, fixture }) => {
    const payload = {
      tenant_id: tenantB,
      notification_id: nid,
      title: "PRIVATE",
      body: "PRIVATE",
      url: "https://evil.invalid",
    };
    assert.deepEqual(notificationHint(payload), { tenantId: tenantB, notificationId: nid });
    assert.equal(notificationHint({ ...payload, notification_id: "bad" }), undefined);
    const before = fixture.reminders.controls.calls.length;
    session.openNotification(payload);
    await inbox.open(notificationHint(payload));
    assert.equal(fixture.reminders.controls.calls.length, before);
    assert.equal(session.snapshot().notificationHint, undefined);
    await session.signOut();
    session.openNotification({ ...payload, tenant_id: tenantA });
    assert.equal(session.snapshot().notificationHint, undefined);
  }));
test("T014 offline inbox retry is independent of push registration and never speaks or acknowledges", () =>
  using(async ({ inbox, fixture }) => {
    fixture.reminders.controls.scenario = "expired";
    await inbox.load();
    assert.equal(inbox.snapshot().items.length, 0);
    assert.equal(writes(fixture).length, 0);
    fixture.reminders.controls.scenario = "normal";
    await inbox.load();
    assert.equal(inbox.snapshot().items.length, 2);
  }));
test("shared Pi controller filters private and never creates edits snoozes or acks", () =>
  using(async ({ reminders, inbox, fixture }) => {
    assert.deepEqual(
      reminders.snapshot().items.map((r) => r.id),
      [rid],
    );
    assert.deepEqual(
      inbox.snapshot().items.map((r) => r.id),
      [nid],
    );
    reminders.create();
    await reminders.select(rid, "snooze");
    await reminders.save();
    await inbox.open({ tenantId: tenantA, notificationId: nid });
    await inbox.acknowledge();
    assert.equal(reminders.snapshot().editor, undefined);
    assert.equal(writes(fixture).length, 0);
  }, "display"));
test("context switch and pagehide dispose private selections drafts and late reads", () =>
  using(async ({ reminders, inbox, session, fixture }) => {
    await reminders.select(privateReminderId);
    await inbox.open({ tenantId: tenantA, notificationId: privateNotificationId });
    fixture.reminders.controls.delay = 30;
    const pending = inbox.load();
    await session.selectTenant(tenantB);
    await pending;
    assert.equal(reminders.snapshot().closed, true);
    assert.equal(reminders.snapshot().editor, undefined);
    assert.equal(inbox.snapshot().selected, undefined);
    session.suspend();
    assert.equal(session.snapshot().context, undefined);
  }));
test("malformed canonical reminder and notification DTOs fail closed", () =>
  using(async ({ context, fixture }) => {
    const value = fixture.reminders.records(tenantA, uid).get(rid);
    for (const patch of [
      { tenant_id: tenantB },
      { schedule_version: 0 },
      { version: 0 },
      { due_at: "bad" },
      { status: "done" },
      { delivery_target: { kind: "user", id: "" } },
    ])
      assert.throws(() => validateReminder({ ...value, ...patch }, context));
    const response = await fixture.reminders.handle(
        new Request(`https://session.invalid/v1/tenants/${tenantA}/notifications/${nid}`),
        uid,
      ),
      notification = await response.json();
    for (const patch of [
      { tenant_id: tenantB },
      { version: 0 },
      { expires_at: "bad" },
      { target_id: "other" },
      { reminder_id: "bad" },
      { status: "dispatched" },
    ])
      assert.throws(() => validateNotification({ ...notification, ...patch }, context));
  }));
test("wrong ETag prevents saved claim and retains exact pending request", () =>
  using(
    async ({ reminders }) => {
      create(reminders);
      await reminders.save();
      assert.equal(reminders.snapshot().pending, true);
      assert.ok(reminders.snapshot().editor);
    },
    "mobile",
    (fetch) => async (input, init) => {
      const req = new Request(input, init),
        response = await fetch(req);
      if (req.method === "POST" && req.url.endsWith("/reminders"))
        response.headers.set("ETag", '"999"');
      return response;
    },
  ));
test("current 401 has one bounded retry; late 403 cannot revoke new context", async () => {
  let retry = 0;
  await using(
    async ({ reminders, fixture }) => {
      assert.equal(reminders.snapshot().items.length, 2);
      assert.equal(fixture.controls.refreshes, 1);
    },
    "mobile",
    (fetch) => async (input, init) => {
      const req = new Request(input, init);
      if (req.url.includes("/reminders") && retry++ === 0)
        return Response.json({}, { status: 401 });
      return fetch(req);
    },
  );
  let fail = false;
  await using(
    async ({ reminders, session }) => {
      fail = true;
      const old = reminders.load();
      await session.selectTenant(tenantB);
      await old;
      assert.equal(session.snapshot().context.tenantId, tenantB);
    },
    "mobile",
    (fetch) => async (input, init) => {
      const req = new Request(input, init);
      if (fail && req.url.includes("/reminders")) {
        await new Promise((resolve) => setTimeout(resolve, 30));
        return Response.json({}, { status: 403 });
      }
      return fetch(req);
    },
  );
});
test("competing push opens discard old multi-GET chain even when response ignores abort", async () => {
  let release,
    slow = false;
  const wait = new Promise((resolve) => {
    release = resolve;
  });
  await using(
    async ({ inbox }) => {
      slow = true;
      const old = inbox.open({ tenantId: tenantA, notificationId: nid });
      await tick();
      const next = inbox.open({ tenantId: tenantA, notificationId: privateNotificationId });
      await next;
      release();
      await old;
      assert.equal(inbox.snapshot().selected.notification.id, privateNotificationId);
    },
    "mobile",
    (fetch) => async (input, init) => {
      const req = new Request(input, init),
        result = await fetch(req);
      if (slow && req.url.endsWith(`/notifications/${nid}`)) await wait;
      return result;
    },
  );
});
test("past audio deadline retains authorized inbox open and explicit acknowledgement", async () => {
  await using(
    async ({ inbox, reminders, fixture }) => {
      assert.equal(inbox.snapshot().items.length, 2);
      await inbox.open({ tenantId: tenantA, notificationId: privateNotificationId });
      assert.ok(inbox.snapshot().selected);
      assert.equal(writes(fixture).length, 0);
      await inbox.acknowledge();
      assert.equal(inbox.snapshot().selected.notification.status, "acknowledged");
      assert.equal(
        reminders.snapshot().items.find((value) => value.id === privateReminderId).status,
        "acknowledged",
      );
    },
    "mobile",
    (fetch) => async (input, init) => {
      const request = new Request(input, init),
        response = await fetch(request);
      if (request.method === "GET" && request.url.includes("/notifications")) {
        const value = await response.json();
        if (value.items) for (const item of value.items) item.expires_at = "2020-01-01T00:00:00Z";
        else value.expires_at = "2020-01-01T00:00:00Z";
        return Response.json(value, { status: response.status, headers: response.headers });
      }
      return response;
    },
  );
});
test("inbox timeout releases busy when response body ignores abort", async () => {
  let stall = false;
  const keepAlive = setTimeout(() => {}, 17000);
  try {
    await using(
      async ({ inbox }) => {
        stall = true;
        await inbox.load();
        assert.equal(inbox.snapshot().busy, false);
        assert.equal(inbox.snapshot().items.length, 0);
        assert.ok(inbox.snapshot().notice);
      },
      "mobile",
      (fetch) => async (input, init) => {
        const request = new Request(input, init);
        if (stall && request.url.includes("/notifications"))
          return new Response(new ReadableStream({ start() {} }), {
            headers: { "Content-Type": "application/json" },
          });
        return fetch(request);
      },
    );
  } finally {
    clearTimeout(keepAlive);
  }
});
test("duplicate notification pages and invalid cursor never publish partial private list", async () => {
  await using(
    async ({ inbox }) => {
      assert.equal(inbox.snapshot().items.length, 0);
      assert.ok(inbox.snapshot().notice);
    },
    "mobile",
    (fetch) => async (input, init) => {
      const request = new Request(input, init),
        response = await fetch(request);
      if (request.url.includes("/notifications?")) {
        const page = await response.json();
        page.items.push(page.items[0]);
        return Response.json(page);
      }
      return response;
    },
  );
});
test("412 with cancelled canonical state keeps title draft usable without reopening schedule", () =>
  using(async ({ reminders, fixture }) => {
    await select(reminders, { title: "未保存の題名" });
    const rows = fixture.reminders.records(tenantA, uid),
      original = rows.get(rid);
    rows.set(rid, { ...original, status: "cancelled", version: 2, schedule_version: 2 });
    await reminders.save();
    reminders.continueEditing();
    assert.equal(reminders.snapshot().editor.draft.status, "cancelled");
    await reminders.save();
    assert.equal(reminders.snapshot().items.find((r) => r.id === rid).title, "未保存の題名");
  }));
test("inconsistent acknowledged projection never displays a scheduled reminder as confirmed", () =>
  using(
    async ({ inbox }) => {
      await inbox.open({ tenantId: tenantA, notificationId: nid });
      assert.equal(inbox.snapshot().selected, undefined);
      assert.ok(inbox.snapshot().notice);
    },
    "mobile",
    (fetch) => async (input, init) => {
      const request = new Request(input, init),
        response = await fetch(request);
      if (request.url.endsWith(`/notifications/${nid}`)) {
        const value = await response.json();
        return Response.json(
          { ...value, status: "acknowledged" },
          { headers: { ETag: `"${value.version}"` } },
        );
      }
      return response;
    },
  ));
test("canonical ack/open reconciles list and active filter while preserving dirty draft", () =>
  using(async ({ inbox, reminders, fixture }) => {
    await reminders.filter("scheduled");
    await select(reminders, { title: "PRIVATE-DIRTY-DRAFT" });
    const rows = fixture.reminders.records(tenantA, uid),
      original = rows.get(rid);
    rows.set(rid, { ...original, title: "現在の題名", version: original.version + 1 });
    await inbox.open({ tenantId: tenantA, notificationId: nid });
    assert.equal(reminders.snapshot().items.find((value) => value.id === rid).title, "現在の題名");
    assert.equal(reminders.snapshot().items.find((value) => value.id === rid).schedule_version, 1);
    assert.equal(reminders.snapshot().editor.draft.title, "PRIVATE-DIRTY-DRAFT");
    assert.equal(reminders.snapshot().conflict.current.title, "現在の題名");
    await inbox.acknowledge();
    assert.equal(
      reminders.snapshot().items.some((value) => value.id === rid),
      false,
    );
    assert.equal(reminders.snapshot().editor.draft.title, "PRIVATE-DIRTY-DRAFT");
    assert.equal(reminders.snapshot().conflict.current.status, "acknowledged");
    reminders.continueEditing();
    await reminders.save();
    assert.equal(rows.get(rid).title, "PRIVATE-DIRTY-DRAFT");
    assert.equal(rows.get(rid).status, "acknowledged");
  }));
test("late list response cannot undo canonical acknowledgement", async () => {
  let slow = false,
    release;
  const delayed = new Promise((resolve) => {
    release = resolve;
  });
  await using(
    async ({ inbox, reminders }) => {
      slow = true;
      const loading = reminders.load();
      await tick();
      await inbox.open({ tenantId: tenantA, notificationId: nid });
      await inbox.acknowledge();
      release();
      await loading;
      assert.equal(
        reminders.snapshot().items.find((value) => value.id === rid).status,
        "acknowledged",
      );
    },
    "mobile",
    (fetch) => async (input, init) => {
      const request = new Request(input, init),
        result = await fetch(request);
      if (slow && request.url.includes("/reminders?")) await delayed;
      return result;
    },
  );
});
test("ack retry reconciles current canonical status after response loss", async () => {
  let lost = true;
  await using(
    async ({ inbox, reminders, fixture }) => {
      await inbox.open({ tenantId: tenantA, notificationId: nid });
      await inbox.acknowledge();
      assert.equal(inbox.snapshot().pending, true);
      await inbox.retry();
      assert.equal(
        reminders.snapshot().items.find((value) => value.id === rid).status,
        "acknowledged",
      );
      const acks = writes(fixture).filter((call) => call.path.endsWith(":ack"));
      assert.deepEqual(acks[0], acks[1]);
    },
    "mobile",
    (fetch) => async (input, init) => {
      const request = new Request(input, init),
        result = await fetch(request);
      if (lost && request.url.endsWith(":ack")) {
        lost = false;
        throw new Error("Synthetic lost acknowledgement");
      }
      return result;
    },
  );
});
test("late ack work cannot overwrite a newer snooze", async () => {
  let slow = false,
    release;
  const delayed = new Promise((resolve) => {
    release = resolve;
  });
  await using(
    async ({ inbox, reminders }) => {
      await inbox.open({ tenantId: tenantA, notificationId: nid });
      slow = true;
      const ack = inbox.acknowledge();
      await tick();
      await select(reminders, { due_at: "2099-12-01T00:00:00Z" }, "snooze");
      inbox.invalidate();
      await reminders.save();
      release();
      await ack;
      const value = reminders.snapshot().items.find((row) => row.id === rid);
      assert.equal(value.status, "scheduled");
      assert.equal(value.schedule_version, 2);
      assert.equal(inbox.snapshot().selected, undefined);
    },
    "mobile",
    (fetch) => async (input, init) => {
      const request = new Request(input, init),
        result = await fetch(request);
      if (slow && request.url.endsWith(":ack")) await delayed;
      return result;
    },
  );
});

test("late detail response cannot replace a newer acknowledged canonical editor", async () => {
  let release,
    held = false;
  const delayed = new Promise((resolve) => {
    release = resolve;
  });
  await using(
    async ({ inbox, reminders }) => {
      held = true;
      const selecting = reminders.select(rid);
      await tick();
      await inbox.open({ tenantId: tenantA, notificationId: nid });
      await inbox.acknowledge();
      release();
      await selecting;
      assert.equal(reminders.snapshot().editor.original.status, "acknowledged");
      assert.equal(reminders.snapshot().editor.etag, '"2"');
      assert.equal(reminders.snapshot().items.find((row) => row.id === rid).status, "acknowledged");
    },
    "mobile",
    (fetch) => async (input, init) => {
      const request = new Request(input, init),
        response = await fetch(request);
      if (held && request.url.endsWith(`/reminders/${rid}`)) {
        held = false;
        await delayed;
      }
      return response;
    },
  );
});
test("late canonical work is discarded after tenant context disposal", async () => {
  let release,
    held = false;
  const delayed = new Promise((resolve) => {
    release = resolve;
  });
  await using(
    async ({ inbox, reminders, session }) => {
      await inbox.open({ tenantId: tenantA, notificationId: nid });
      held = true;
      const ack = inbox.acknowledge();
      await tick();
      await session.selectTenant(tenantB);
      release();
      await ack;
      assert.equal(reminders.snapshot().closed, true);
      assert.deepEqual(reminders.snapshot().items, []);
      assert.equal(inbox.snapshot().selected, undefined);
    },
    "mobile",
    (fetch) => async (input, init) => {
      const request = new Request(input, init),
        response = await fetch(request);
      if (held && request.url.endsWith(":ack")) await delayed;
      return response;
    },
  );
});
