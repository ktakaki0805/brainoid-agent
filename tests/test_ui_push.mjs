import assert from "node:assert/strict";
import { test } from "node:test";
import { PushController } from "../apps/mobile/notifications/controller.ts";
import { nativePush } from "../apps/mobile/platform/push.ts";
import { RemindersController } from "../packages/ui/src/reminders/controller.ts";
import { NotificationsController } from "../packages/ui/src/reminders/notifications.ts";
import { SessionController } from "../packages/ui/src/session.ts";
import { createSessionFixture, tenantA, tenantB } from "../packages/ui/src/session-fixture.ts";

const tick = (ms = 5) => new Promise((resolve) => setTimeout(resolve, ms));
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((r, j) => {
    resolve = r;
    reject = j;
  });
  return { promise, resolve, reject };
};
function plugin() {
  const listeners = new Map(),
    calls = [],
    controls = {
      permission: "granted",
      token: "synthetic-token",
      delayDelete: undefined,
      delayGet: undefined,
      delayListener: undefined,
      error: false,
    };
  const api = {
    async addListener(name, listener) {
      calls.push(["listen", name]);
      const list = listeners.get(name) ?? [];
      list.push(listener);
      listeners.set(name, list);
      if (controls.delayListener) await controls.delayListener;
      return {
        remove: async () => {
          calls.push(["remove", name]);
          const index = list.indexOf(listener);
          if (index >= 0) list.splice(index, 1);
        },
      };
    },
    async checkPermissions() {
      calls.push(["check"]);
      if (controls.error) throw Error("fixture private provider detail");
      return { receive: controls.permission };
    },
    async requestPermissions() {
      calls.push(["request"]);
      return { receive: controls.permission };
    },
    async getToken() {
      calls.push(["get"]);
      if (controls.delayGet) await controls.delayGet;
      return { token: controls.token };
    },
    async deleteToken() {
      calls.push(["delete-start"]);
      if (controls.delayDelete) await controls.delayDelete;
      calls.push(["delete-end"]);
    },
  };
  return {
    api,
    calls,
    controls,
    emit: (event, data) => {
      for (const listener of listeners.get(event) ?? []) listener(data);
    },
    listeners,
  };
}
async function using(work, { permission = "granted", alter = (fetch) => fetch } = {}) {
  const fixture = createSessionFixture(),
    native = plugin(),
    writes = [],
    receipts = new Map();
  let record;
  native.controls.permission = permission;
  const transport = alter(async (input, init) => {
    const request = new Request(input, init),
      url = new URL(request.url);
    if (!url.pathname.includes("push-installations")) return fixture.transport(request);
    const body = request.method === "DELETE" ? undefined : await request.clone().json(),
      key = request.headers.get("Idempotency-Key"),
      uid = (await fixture.auth.current())?.uid;
    writes.push({
      uid,
      method: request.method,
      path: url.pathname,
      body,
      key,
      match: request.headers.get("If-Match"),
    });
    if (!uid) return Response.json({}, { status: 401 });
    if (receipts.has(key)) {
      const previous = receipts.get(key);
      return previous.status === 204
        ? new Response(null, { status: 204 })
        : Response.json(previous.body, {
            status: previous.status,
            headers: { ETag: `"${previous.body.version}"` },
          });
    }
    if (request.method === "DELETE") {
      assert.equal(request.headers.get("If-Match"), `"${record.version}"`);
      record = undefined;
      receipts.set(key, { status: 204 });
      return new Response(null, { status: 204 });
    }
    if (request.method === "PATCH")
      assert.equal(request.headers.get("If-Match"), `"${record.version}"`);
    record = {
      id: record?.id ?? crypto.randomUUID(),
      platform: "ios",
      version: (record?.version ?? 0) + 1,
      created_at: "2026-10-28T00:00:00Z",
    };
    const status = request.method === "POST" ? 201 : 200;
    receipts.set(key, { body: structuredClone(record), status });
    return Response.json(record, { status, headers: { ETag: `"${record.version}"` } });
  });
  const session = new SessionController({
      auth: fixture.auth,
      transport,
      baseUrl: "https://session.invalid",
    }),
    push = new PushController(session, nativePush(native.api), "ios");
  const stop = session.start(),
    stopPush = push.start();
  await tick();
  await session.signIn("user-a", "fake");
  await tick();
  await session.selectTenant(tenantA);
  try {
    await work({ fixture, session, push, native, writes });
  } finally {
    stopPush();
    stop();
    await tick();
  }
}

test("native adapter uses FCM token on iOS, permission prompt only on action, refresh PATCH and manual remove", () =>
  using(async ({ push, native, writes, session }) => {
    assert.equal(writes.length, 0);
    assert.equal(
      native.calls.some(([call]) => call === "request"),
      false,
    );
    await push.enable();
    assert.equal(writes[0].body.fcm_token, "synthetic-token");
    assert.equal(writes[0].body.platform, "ios");
    native.emit("apnsTokenReceived", { token: "apns-must-not-register" });
    await tick();
    assert.equal(writes.length, 1);
    native.emit("tokenReceived", { token: "rotated-fcm" });
    await tick();
    assert.equal(writes[1].method, "PATCH");
    assert.equal(writes[1].match, '"1"');
    assert.equal(writes[1].body.fcm_token, "rotated-fcm");
    await session.selectTenant(tenantB);
    assert.equal(writes.length, 2);
    await push.disable();
    assert.equal(writes[2].method, "DELETE");
    assert.equal(push.snapshot().registered, false);
    await push.enable();
    assert.equal(writes.at(-1).method, "POST");
  }));
test("denied permission and unavailable provider leave reminder creation and inbox available", async () => {
  await using(
    async ({ push, native, writes, session }) => {
      await push.enable();
      assert.equal(push.snapshot().permission, "denied");
      assert.equal(writes.length, 0);
      assert.equal(
        native.calls.some(([call]) => call === "get"),
        false,
      );
      const context = session.snapshot().context,
        reminders = new RemindersController(context, "mobile"),
        inbox = new NotificationsController(context, "mobile");
      await reminders.load();
      await inbox.load();
      reminders.create();
      reminders.draft({
        ...reminders.snapshot().editor.draft,
        title: "OS拒否でも保存",
        due_at: "2099-01-01T00:00:00Z",
        timezone: "Asia/Tokyo",
      });
      await reminders.save();
      assert.equal(reminders.snapshot().pending, false);
      assert.equal(inbox.snapshot().items.length, 2);
    },
    { permission: "denied" },
  );
  await using(async ({ push, native, session }) => {
    native.controls.error = true;
    await session.selectTenant(tenantB);
    assert.ok(session.snapshot().context);
    assert.ok(push.snapshot());
  });
});
test("lost registration reuses exact key/body; no provider errors or tokens in public state", async () => {
  let lose = true;
  await using(
    async ({ push, writes }) => {
      await push.enable();
      assert.equal(push.snapshot().pending, true);
      assert.equal(JSON.stringify(push.snapshot()).includes("synthetic-token"), false);
      await push.retry();
      assert.deepEqual(writes[0], writes[1]);
      assert.equal(push.snapshot().registered, true);
    },
    {
      alter: (fetch) => async (input, init) => {
        const request = new Request(input, init),
          response = await fetch(request);
        if (lose && request.method === "POST" && request.url.includes("push-installations")) {
          lose = false;
          throw Error("synthetic response loss");
        }
        return response;
      },
    },
  );
});
test("ordinary signout removes global installation with closing UID and synchronously clears private UI", () =>
  using(async ({ push, session, writes, native }) => {
    await push.enable();
    const old = session.snapshot().context;
    const delayed = deferred();
    native.controls.delayDelete = delayed.promise;
    const signingOut = session.signOut();
    assert.equal(session.snapshot().context, undefined);
    assert.equal(old.signal.aborted, true);
    native.emit("tokenReceived", { token: "late" });
    await tick();
    assert.equal(writes.filter((w) => w.method === "DELETE").length, 1);
    delayed.resolve();
    await signingOut;
    assert.equal(session.snapshot().phase, "signed-out");
    assert.equal(
      writes.every((w) => w.uid === "user-a"),
      true,
    );
  }));
test("external UID change fences old callbacks and serializes late native delete ahead of new token", () =>
  using(async ({ push, session, fixture, native, writes }) => {
    await push.enable();
    const delayed = deferred();
    native.controls.delayDelete = delayed.promise;
    const oldListener = native.listeners.get("tokenReceived")[0];
    fixture.emit({ uid: "user-b" });
    await tick();
    await session.selectTenant(tenantA);
    const enabling = push.enable();
    await tick();
    oldListener({ token: "late-a" });
    assert.equal(writes.length, 1);
    assert.equal(native.calls.filter(([call]) => call === "get").length, 1);
    delayed.resolve();
    await enabling;
    assert.equal(writes.at(-1).uid, "user-b");
    assert.equal(
      writes.some((w) => w.body?.fcm_token === "late-a"),
      false,
    );
    assert.equal(
      writes.some((w) => w.method === "DELETE"),
      false,
    );
  }));
test("late getToken cannot register after signout and next user waits for cleanup", () =>
  using(async ({ push, session, native, writes }) => {
    const late = deferred();
    native.controls.delayGet = late.promise;
    const enabling = push.enable();
    await tick();
    const signingOut = session.signOut();
    const signingIn = session.signIn("user-b", "fake");
    await tick();
    late.resolve();
    await enabling;
    await signingOut;
    await signingIn;
    await tick();
    assert.equal(writes.length, 0);
    native.controls.delayGet = undefined;
    await push.enable();
    assert.equal(writes[0].uid, "user-b");
  }));
test("late listener acquisition removes only owned handles and drops stale notifications", async () => {
  const native = plugin(),
    late = deferred();
  native.controls.delayListener = late.promise;
  const adapter = nativePush(native.api),
    opens = [],
    tokens = [],
    lease = adapter.open(
      (t) => tokens.push(t),
      (data) => opens.push(data),
      () => {},
    );
  const closing = lease.close();
  native.emit("tokenReceived", { token: "stale" });
  native.emit("notificationActionPerformed", { notification: { data: { tenant_id: tenantA } } });
  late.resolve();
  await closing;
  await tick();
  assert.deepEqual(opens, []);
  assert.deepEqual(tokens, []);
  assert.equal([...native.listeners.values()].flat().length, 0);
});
test("in-flight global read and auth callback cannot republish signed-in state during cleanup", async () => {
  let block = false;
  const delayed = deferred(),
    cleanup = deferred();
  await using(
    async ({ session, fixture }) => {
      const stop = session.beforeSignOut(async () => cleanup.promise);
      block = true;
      const listing = session.loadTenants();
      const logout = session.signOut();
      fixture.emit({ uid: "user-b" });
      delayed.resolve();
      await listing;
      assert.equal(session.snapshot().phase, "signed-out");
      assert.equal(session.snapshot().uid, undefined);
      assert.equal(session.globalContext(), undefined);
      cleanup.resolve();
      await logout;
      stop();
    },
    {
      alter: (fetch) => async (input, init) => {
        const request = new Request(input, init),
          result = await fetch(request);
        if (block && request.url.endsWith("/v1/tenants?limit=100")) await delayed.promise;
        return result;
      },
    },
  );
});
test("OS payload opens only current authenticated tenant and never auto-acks or uses arbitrary links", () =>
  using(async ({ native, session, writes }) => {
    native.emit("notificationActionPerformed", {
      notification: {
        title: "private",
        body: "private",
        data: {
          tenant_id: tenantA,
          notification_id: "b0000000-0000-4000-8000-000000000001",
          url: "https://evil.invalid",
        },
      },
    });
    assert.deepEqual(session.snapshot().notificationHint, {
      tenantId: tenantA,
      notificationId: "b0000000-0000-4000-8000-000000000001",
    });
    assert.equal(writes.length, 0);
    await session.signOut();
    native.emit("notificationActionPerformed", {
      notification: {
        data: { tenant_id: tenantA, notification_id: "b0000000-0000-4000-8000-000000000001" },
      },
    });
    assert.equal(session.snapshot().notificationHint, undefined);
  }));
test("token rotation retained while older registration remains uncertain", async () => {
  let lost = 2;
  await using(
    async ({ push, native, writes }) => {
      await push.enable();
      assert.equal(push.snapshot().pending, true);
      native.emit("tokenReceived", { token: "newest-fcm" });
      await tick();
      assert.equal(push.snapshot().pending, true);
      await push.retry();
      assert.equal(push.snapshot().registered, true);
      assert.equal(writes.at(-1).body.fcm_token, "newest-fcm");
      assert.equal(writes.at(-1).method, "PATCH");
    },
    {
      alter: (fetch) => async (input, init) => {
        const req = new Request(input, init),
          response = await fetch(req);
        if (req.method === "POST" && req.url.includes("push-installations") && lost-- > 0)
          throw Error("synthetic response loss");
        return response;
      },
    },
  );
});
test("bounded signout timeout fences closing context; late native delete cannot erase next UID token", () =>
  using(async ({ push, native, session, writes }) => {
    await push.enable();
    const delayed = deferred();
    native.controls.delayDelete = delayed.promise;
    await session.signOut();
    await session.signIn("user-b", "fake");
    await tick();
    const enable = push.enable();
    await tick();
    assert.equal(native.calls.filter(([call]) => call === "get").length, 1);
    delayed.resolve();
    await enable;
    assert.equal(writes.at(-1).uid, "user-b");
    assert.equal(writes.at(-1).method, "POST");
    const calls = native.calls.map(([call]) => call);
    assert.ok(calls.lastIndexOf("get") > calls.indexOf("delete-end"));
  }));
test("native initialization rejection does not throw and owns delayed listener removal", async () => {
  const native = plugin();
  native.controls.error = true;
  const fixture = createSessionFixture(),
    session = new SessionController({
      auth: fixture.auth,
      transport: fixture.transport,
      baseUrl: "https://session.invalid",
    });
  const push = new PushController(session, nativePush(native.api), "android"),
    stop = session.start(),
    stopPush = push.start();
  try {
    await tick();
    await session.signIn("user-a", "fake");
    await tick();
    await session.selectTenant(tenantA);
    assert.equal(push.snapshot().permission, "unavailable");
    assert.ok(session.snapshot().context);
    const inbox = new NotificationsController(session.snapshot().context, "mobile");
    await inbox.load();
    assert.equal(inbox.snapshot().items.length, 2);
  } finally {
    stopPush();
    stop();
    await tick();
  }
});
test("installation 412 is a confirmed refusal and explicit register recovers without endless same-match retry", async () => {
  let reject = false;
  await using(
    async ({ push, native, writes }) => {
      await push.enable();
      reject = true;
      native.emit("tokenReceived", { token: "new-token" });
      await tick();
      assert.equal(push.snapshot().pending, false);
      assert.equal(push.snapshot().registered, false);
      assert.match(push.snapshot().message, /再登録/);
      reject = false;
      await push.enable();
      assert.equal(push.snapshot().registered, true);
      assert.equal(writes.at(-1).method, "POST");
    },
    {
      alter: (fetch) => async (input, init) => {
        const request = new Request(input, init);
        if (reject && request.method === "PATCH" && request.url.includes("push-installations"))
          return Response.json({}, { status: 412 });
        return fetch(request);
      },
    },
  );
});
for (const status of [403, 404])
  test(`definitive installation ${status} clears exact pending request and permits explicit recovery`, async () => {
    let reject = true,
      attempts = 0;
    await using(
      async ({ push, session, native }) => {
        await push.enable();
        assert.equal(push.snapshot().pending, false);
        assert.ok(session.snapshot().context);
        const before = attempts;
        await push.retry();
        assert.equal(attempts, before);
        reject = false;
        await push.enable();
        assert.equal(push.snapshot().registered, true);
        reject = true;
        native.emit("tokenReceived", { token: "rotated" });
        await tick();
        assert.equal(push.snapshot().pending, false);
        assert.ok(session.snapshot().context);
        reject = false;
        await push.enable();
        assert.equal(push.snapshot().registered, true);
      },
      {
        alter: (fetch) => async (input, init) => {
          const request = new Request(input, init);
          if (request.url.includes("push-installations")) {
            attempts++;
            if (reject) return Response.json({}, { status });
          }
          return fetch(request);
        },
      },
    );
  });
test("lost POST then inactive 404 replay stops exact retry without an automatic fresh POST", async () => {
  let stage = "lost",
    attempts = 0;
  await using(
    async ({ push, session, writes }) => {
      await push.enable();
      assert.equal(push.snapshot().pending, true);
      stage = "inactive";
      const before = attempts;
      await push.retry();
      assert.equal(attempts, before + 1);
      assert.equal(push.snapshot().pending, false);
      assert.ok(session.snapshot().context);
      stage = "healthy";
      await push.enable();
      assert.equal(push.snapshot().registered, true);
      assert.notEqual(writes[0].key, writes.at(-1).key);
    },
    {
      alter: (fetch) => async (input, init) => {
        const request = new Request(input, init);
        if (request.url.includes("push-installations")) {
          attempts++;
          if (stage === "inactive") return Response.json({}, { status: 404 });
          const response = await fetch(request);
          if (stage === "lost") throw new Error("Lost committed POST");
          return response;
        }
        return fetch(request);
      },
    },
  );
});
for (const failure of ["reconcile", "remove"])
  test(`failed disable after lost rotation offers live fresh-lease recovery: ${failure}`, async () => {
    let stage = "healthy";
    await using(
      async ({ push, session, native, writes }) => {
        await push.enable();
        stage = "lost";
        native.controls.token = "new-current-fcm";
        native.emit("tokenReceived", { token: native.controls.token });
        await tick();
        assert.equal(push.snapshot().pending, true);
        stage = failure;
        await push.disable();
        assert.equal(push.snapshot().pending, false);
        assert.equal(push.snapshot().removalUnconfirmed, true);
        assert.equal(push.snapshot().registered, false);
        assert.match(push.snapshot().message, /完了を確認できません/);
        assert.ok(session.snapshot().context);
        stage = "healthy";
        const before = writes.length;
        await push.enable();
        assert.ok(writes.length > before);
        assert.equal(push.snapshot().registered, true);
        assert.equal(push.snapshot().removalUnconfirmed, false);
        await push.disable();
        assert.equal(push.snapshot().registered, false);
      },
      {
        alter: (fetch) => async (input, init) => {
          const request = new Request(input, init);
          if (request.url.includes("push-installations")) {
            if (stage === "reconcile" && request.method === "PATCH")
              throw new Error("Reconciliation unavailable");
            if (stage === "remove" && request.method === "DELETE")
              throw new Error("Removal unavailable");
            const response = await fetch(request);
            if (stage === "lost" && request.method === "PATCH")
              throw new Error("Lost committed rotation");
            return response;
          }
          return fetch(request);
        },
      },
    );
  });
test("native close failure remains uncertain without unhandled rejection", () =>
  using(async ({ push, native }) => {
    await push.enable();
    const events = [];
    const handler = (error) => events.push(error);
    process.on("unhandledRejection", handler);
    const original = native.api.deleteToken;
    native.api.deleteToken = async () => {
      throw new Error("Native stop unavailable");
    };
    try {
      await push.disable();
      await tick();
      assert.equal(push.snapshot().removalUnconfirmed, true);
      assert.equal(push.snapshot().pending, false);
      assert.deepEqual(events, []);
      native.api.deleteToken = original;
      await push.enable();
      assert.equal(push.snapshot().registered, true);
    } finally {
      native.api.deleteToken = original;
      process.removeListener("unhandledRejection", handler);
    }
  }));

test("failed manual disable cannot publish or register under a replacement UID", async () => {
  const delayed = deferred();
  let removing = false;
  await using(
    async ({ push, session, native, writes }) => {
      await push.enable();
      removing = true;
      const disable = push.disable();
      await tick();
      await session.signOut();
      await session.signIn("user-b", "fake");
      await tick();
      native.controls.token = "user-b-current-fcm";
      await push.enable();
      const before = writes.length;
      delayed.reject(new Error("Old removal unavailable"));
      await disable;
      assert.equal(writes.length, before);
      assert.equal(writes.at(-1).uid, "user-b");
      assert.equal(writes.at(-1).body.fcm_token, "user-b-current-fcm");
      assert.equal(push.snapshot().registered, true);
      assert.equal(push.snapshot().removalUnconfirmed, false);
    },
    {
      alter: (fetch) => async (input, init) => {
        const request = new Request(input, init);
        if (removing && request.method === "DELETE") {
          removing = false;
          await delayed.promise;
        }
        return fetch(request);
      },
    },
  );
});
