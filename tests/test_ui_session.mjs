import assert from "node:assert/strict";
import { test } from "node:test";
import { authenticationConfiguration } from "../apps/mobile/src/auth-config.ts";
import { createNativeAuthentication } from "../apps/mobile/src/firebase-native.ts";
import { getTenant } from "../packages/api-client/src/index.ts";
import { SessionController } from "../packages/ui/src/session.ts";
import {
  createSessionFixture,
  fixtureTenant,
  tenantA,
  tenantB,
} from "../packages/ui/src/session-fixture.ts";

const tick = () => new Promise((resolve) => setImmediate(resolve));
const deferred = () => {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
};
async function setup(change = () => {}) {
  const fixture = createSessionFixture();
  change(fixture);
  const controller = new SessionController({
    auth: fixture.auth,
    baseUrl: "https://session.invalid",
    transport: fixture.transport,
  });
  const stop = controller.start();
  await tick();
  await controller.signIn("owner@example.test", "synthetic-password");
  await tick();
  return { fixture, controller, stop };
}

test("selecting B destroys A drafts, cache and stream ownership before any B response", async () => {
  const { controller, stop } = await setup();
  await controller.selectTenant(tenantA);
  const a = controller.snapshot().context;
  a.keep("private", "A only");
  let closed = 0;
  a.own(() => {
    closed++;
  });
  a.own(() => {
    throw Error("failed cleanup");
  });
  a.own(() => {
    closed++;
  });
  const pending = controller.selectTenant(tenantB);
  assert.equal(controller.snapshot().tenant, undefined);
  assert.equal(a.signal.aborted, true);
  assert.equal(closed, 2);
  assert.throws(() => a.read("private"), { name: "AbortError" });
  await pending;
  assert.equal(controller.snapshot().tenant.id, tenantB);
  assert.equal(controller.snapshot().context.read("private"), undefined);
  stop();
});

test("transport ignoring abort cannot apply a late A response after B", async () => {
  const late = deferred();
  const { controller, stop } = await setup((f) => {
    const original = f.transport;
    f.transport = (input, init) =>
      new URL(input.url).pathname.endsWith(tenantA) ? late.promise : original(input, init);
  });
  const pending = controller.selectTenant(tenantA);
  await tick();
  await controller.selectTenant(tenantB);
  late.resolve(Response.json(fixtureTenant(tenantA, "private A")));
  await pending;
  assert.equal(controller.snapshot().tenant.id, tenantB);
  stop();
});

test("body parsing delayed past switching is fenced as well as the fetch response", async () => {
  const body = deferred();
  const reading = deferred();
  const { controller, stop } = await setup((f) => {
    const original = f.transport;
    f.transport = async (input, init) => {
      if (!new URL(input.url).pathname.endsWith(tenantA)) return original(input, init);
      const response = Response.json(fixtureTenant(tenantA, "late body"));
      response.text = () => {
        reading.resolve();
        return body.promise;
      };
      return response;
    };
  });
  let settled = false;
  const pending = controller.selectTenant(tenantA).then(() => {
    settled = true;
  });
  await reading.promise;
  assert.equal(settled, false);
  assert.equal(controller.snapshot().tenant, undefined);
  await controller.selectTenant(tenantB);
  assert.equal(settled, false);
  body.resolve(JSON.stringify(fixtureTenant(tenantA, "late body")));
  await pending;
  assert.equal(controller.snapshot().tenant.id, tenantB);
  stop();
});

test("401 refreshes once and retries with same create body/key; profile GET is unnecessary", async () => {
  const keys = [];
  let posts = 0;
  const { controller, fixture, stop } = await setup((f) => {
    const original = f.transport;
    f.transport = async (input, init) => {
      if (input.method === "POST") {
        keys.push([input.headers.get("Idempotency-Key"), await input.clone().text()]);
        if (++posts === 1) return Response.json({}, { status: 401 });
      }
      return original(input, init);
    };
  });
  await controller.createTenant("My household", "Asia/Tokyo");
  assert.equal(fixture.controls.refreshes, 1);
  assert.equal(posts, 2);
  assert.deepEqual(keys[0], keys[1]);
  assert.equal(controller.snapshot().tenant.name, "My household");
  assert.equal(
    fixture.controls.calls.some((call) => call.includes("/users/me")),
    false,
  );
  stop();
});

for (const status of [403, 404])
  test(`${status} immediately drops selected data without selecting another tenant`, async () => {
    const { controller, fixture, stop } = await setup();
    await controller.selectTenant(tenantA);
    const scope = controller.snapshot().context;
    scope.keep("private", "private draft");
    fixture.controls.status = status;
    await assert.rejects(
      scope.run((client, signal, tenantId) =>
        getTenant({ client, signal, path: { tenant_id: tenantId } }),
      ),
      { name: "AbortError" },
    );
    assert.equal(controller.snapshot().context, undefined);
    assert.equal(controller.snapshot().tenant, undefined);
    assert.equal(controller.snapshot().phase, "ready");
    assert.equal(
      fixture.controls.calls.some((call) => call.endsWith(tenantB)),
      false,
    );
    stop();
  });

test("unjoined and malformed URL IDs are never displayed as fetched", async () => {
  const { controller, fixture, stop } = await setup();
  await controller.selectTenant("not-a-tenant");
  assert.equal(controller.snapshot().tenant, undefined);
  assert.equal(fixture.controls.calls.length, 1);
  await controller.selectTenant("ffffffff-ffff-4fff-8fff-ffffffffffff");
  assert.equal(controller.snapshot().context, undefined);
  assert.match(controller.snapshot().message, /利用できません/);
  stop();
});

test("refresh finishing after signout cannot retry or resurrect the old user", async () => {
  const refresh = deferred();
  let requests = 0;
  const { controller, fixture, stop } = await setup((f) => {
    const token = f.auth.token;
    f.auth.token = (uid, force) => (force ? refresh.promise : token(uid, false));
    const original = f.transport;
    f.transport = (input, init) => {
      if (new URL(input.url).pathname.endsWith(tenantA)) {
        requests++;
        return Promise.resolve(Response.json({}, { status: 401 }));
      }
      return original(input, init);
    };
  });
  const pending = controller.selectTenant(tenantA);
  await tick();
  await controller.signOut();
  refresh.resolve("stale-secret");
  await pending;
  fixture.emit({ uid: "old-user" });
  assert.equal(requests, 1);
  assert.equal(controller.snapshot().phase, "signed-out");
  assert.equal(controller.snapshot().uid, undefined);
  stop();
});

test("old refresh failure after external UID change cannot sign out the new user", async () => {
  const refresh = deferred();
  const { controller, fixture, stop } = await setup((f) => {
    const token = f.auth.token;
    f.auth.token = (uid, force) => (force ? refresh.promise : token(uid, false));
    const original = f.transport;
    f.transport = (input, init) =>
      new URL(input.url).pathname.endsWith(tenantA)
        ? Promise.resolve(Response.json({}, { status: 401 }))
        : original(input, init);
  });
  const pending = controller.selectTenant(tenantA);
  await tick();
  fixture.emit({ uid: "new-user" });
  await tick();
  refresh.reject(Error("late refresh failure"));
  await pending;
  assert.equal(controller.snapshot().uid, "new-user");
  assert.equal(controller.snapshot().phase, "ready");
  stop();
});

test("second 401 removes all private content and requires a fresh sign-in", async () => {
  const { controller, fixture, stop } = await setup();
  fixture.controls.status = 401;
  await controller.selectTenant(tenantA);
  await tick();
  assert.equal(fixture.controls.refreshes, 1);
  assert.equal(controller.snapshot().phase, "signed-out");
  assert.equal(controller.snapshot().context, undefined);
  stop();
});

test("pending sign-in is serialized before signout and cannot restore credentials later", async () => {
  const pending = deferred();
  const actions = [];
  const fixture = createSessionFixture();
  const signIn = fixture.auth.signIn;
  const signOut = fixture.auth.signOut;
  fixture.auth.signIn = async (...args) => {
    actions.push("signin");
    await pending.promise;
    await signIn(...args);
  };
  fixture.auth.signOut = async () => {
    actions.push("signout");
    await signOut();
  };
  const controller = new SessionController({
    auth: fixture.auth,
    baseUrl: "https://session.invalid",
    transport: fixture.transport,
  });
  const stop = controller.start();
  await tick();
  const login = controller.signIn("user@example.test", "fixture");
  await tick();
  const logout = controller.signOut();
  pending.resolve();
  await Promise.all([login, logout]);
  assert.deepEqual(actions, ["signin", "signout"]);
  assert.equal(await fixture.auth.current(), null);
  assert.equal(controller.snapshot().phase, "signed-out");
  stop();
});

test("create response after explicit B selection cannot overwrite B or its UI state", async () => {
  const pending = deferred();
  const { controller, stop } = await setup((f) => {
    const original = f.transport;
    f.transport = (input, init) =>
      input.method === "POST" ? pending.promise : original(input, init);
  });
  const creation = controller.createTenant("New tenant", "Asia/Tokyo");
  await tick();
  await controller.selectTenant(tenantB);
  pending.resolve(
    Response.json(fixtureTenant("10000000-0000-4000-8000-000000000003", "New tenant"), {
      status: 201,
    }),
  );
  await creation;
  assert.equal(controller.snapshot().tenant.id, tenantB);
  assert.equal(controller.snapshot().busy, undefined);
  stop();
});

for (const status of [403, 404]) {
  test(`late create ${status} preserves the selected B context, draft and UI state`, async () => {
    const pending = deferred();
    const { controller, stop } = await setup((f) => {
      const original = f.transport;
      f.transport = (input, init) =>
        input.method === "POST" ? pending.promise : original(input, init);
    });
    const creation = controller.createTenant("New tenant", "Asia/Tokyo");
    await tick();
    await controller.selectTenant(tenantB);
    const selected = controller.snapshot();
    selected.context.keep("draft", "B private draft");
    pending.resolve(Response.json({}, { status }));
    await creation;
    assert.equal(controller.snapshot(), selected);
    assert.equal(selected.context.signal.aborted, false);
    assert.equal(selected.context.read("draft"), "B private draft");
    stop();
  });
}

test("create responses after signout cannot restore success or denial state", async () => {
  for (const status of [201, 403, 404]) {
    const pending = deferred();
    const { controller, stop } = await setup((f) => {
      const original = f.transport;
      f.transport = (input, init) =>
        input.method === "POST" ? pending.promise : original(input, init);
    });
    const creation = controller.createTenant("New tenant", "Asia/Tokyo");
    await tick();
    await controller.signOut();
    const signedOut = controller.snapshot();
    pending.resolve(Response.json(fixtureTenant(tenantA, "New tenant"), { status }));
    await creation;
    assert.equal(controller.snapshot(), signedOut);
    stop();
  }
});

test("lost create responses reuse the same idempotency key on explicit retry", async () => {
  const keys = [];
  let failed = false;
  const { controller, stop } = await setup((f) => {
    const original = f.transport;
    f.transport = async (input, init) => {
      if (input.method === "POST") {
        keys.push(input.headers.get("Idempotency-Key"));
        const result = await original(input, init);
        if (!failed) {
          failed = true;
          throw Error("response lost");
        }
        return result;
      }
      return original(input, init);
    };
  });
  await controller.createTenant("New tenant", "Asia/Tokyo");
  await controller.createTenant("New tenant", "Asia/Tokyo");
  assert.equal(keys.length, 2);
  assert.equal(keys[0], keys[1]);
  assert.equal(controller.snapshot().tenants.length, 3);
  stop();
});

test("native page suspension discards context and resume revalidates revoked membership", async () => {
  const { controller, fixture, stop } = await setup((f) => {
    f.auth.storage = "native-firebase";
  });
  await controller.selectTenant(tenantA);
  const context = controller.snapshot().context;
  controller.suspend();
  assert.equal(controller.snapshot().tenant, undefined);
  assert.equal(context.signal.aborted, true);
  fixture.controls.status = 404;
  await controller.resume();
  await controller.selectTenant(tenantA);
  assert.equal(controller.snapshot().tenant, undefined);
  stop();
});

test("browser restore stays signed out while SDK signout is frozen and stale callbacks arrive", async () => {
  const cleanup = deferred();
  const { controller, fixture, stop } = await setup((f) => {
    f.auth.signOut = () => cleanup.promise;
  });
  await controller.selectTenant(tenantA);
  const old = controller.snapshot().context;
  controller.suspend();
  assert.equal(controller.snapshot().phase, "signed-out");
  assert.equal(old.signal.aborted, true);
  await tick();
  // Independent in-memory SDK still has its old user until cleanup can run.
  assert.notEqual(await fixture.auth.current(), null);
  await controller.resume();
  fixture.emit({ uid: "old-user" });
  await controller.selectTenant(tenantA);
  assert.equal(controller.snapshot().phase, "signed-out");
  assert.equal(controller.snapshot().context, undefined);
  cleanup.resolve();
  await tick();
  fixture.emit({ uid: "old-user" });
  assert.equal(controller.snapshot().phase, "signed-out");
  // Only an explicit sign-in opens a new session.
  await controller.signIn("new@example.test", "synthetic-password");
  await controller.selectTenant(tenantB);
  assert.equal(controller.snapshot().tenant.id, tenantB);
  stop();
});

test("failed SDK cleanup after pagehide cannot reopen the cached browser session", async () => {
  const { controller, fixture, stop } = await setup((f) => {
    f.auth.signOut = async () => {
      throw Error("cleanup failed");
    };
  });
  await controller.selectTenant(tenantA);
  controller.suspend();
  await tick();
  await controller.resume();
  fixture.emit({ uid: "old-user" });
  assert.equal(controller.snapshot().phase, "signed-out");
  assert.equal(controller.snapshot().context, undefined);
  stop();
});

test("native adapter calls native SDK, rejects UID changes during token retrieval and removes its listener", async () => {
  const token = deferred();
  let uid = "A";
  let listener;
  let removed = false;
  const calls = [];
  const plugin = {
    getCurrentUser: async () => ({ user: { uid } }),
    getTenantId: async () => ({ tenantId: null }),
    getIdToken: async (options) => {
      calls.push(options);
      return token.promise;
    },
    addListener: async (event, callback) => {
      assert.equal(event, "authStateChange");
      listener = callback;
      return {
        remove: async () => {
          removed = true;
        },
      };
    },
    signInWithEmailAndPassword: async (options) => calls.push(options),
    signOut: async () => {
      uid = null;
    },
  };
  const auth = createNativeAuthentication(plugin);
  const stop = auth.subscribe(() => {});
  await tick();
  await auth.signIn("synthetic@example.test", "fixture");
  const pending = auth.token("A", true);
  await tick();
  uid = "B";
  listener({ user: { uid } });
  token.resolve({ token: "stale" });
  await assert.rejects(pending);
  stop();
  await tick();
  assert.equal(removed, true);
  assert.deepEqual(calls[1], { forceRefresh: true });
  assert.equal(auth.storage, "native-firebase");
});

test("production fails closed on emulator configuration and missing config never guesses a project", () => {
  assert.equal(authenticationConfiguration({}, "production"), undefined);
  const env = {
    VITE_API_URL: "http://127.0.0.1:8000",
    VITE_FIREBASE_API_KEY: "fixture-key",
    VITE_FIREBASE_AUTH_DOMAIN: "demo-brainoid.firebaseapp.com",
    VITE_FIREBASE_PROJECT_ID: "demo-brainoid",
    VITE_FIREBASE_APP_ID: "fixture-app",
    VITE_AUTH_EMULATOR: "http://127.0.0.1:9099",
  };
  assert.throws(() => authenticationConfiguration(env, "production"));
  assert.equal(authenticationConfiguration(env, "emulator").emulator, true);
  assert.throws(() =>
    authenticationConfiguration({ ...env, VITE_FIREBASE_PROJECT_ID: "real-project" }, "emulator"),
  );
});

test("an active generated SSE reader is cancelled at tenant switch without emitting old frames", async () => {
  const { streamRunEvents } = await import("../packages/api-client/src/index.ts");
  let cancelled = false;
  const { controller, stop } = await setup((fixture) => {
    const original = fixture.transport;
    fixture.transport = (input, init) => {
      if (new URL(input.url).pathname.endsWith("/events")) {
        return Promise.resolve(
          new Response(
            new ReadableStream({
              cancel() {
                cancelled = true;
              },
            }),
            { headers: { "Content-Type": "text/event-stream" } },
          ),
        );
      }
      return original(input, init);
    };
  });
  await controller.selectTenant(tenantA);
  const context = controller.snapshot().context;
  const result = await context.run((client, signal, tenantId) =>
    streamRunEvents({
      client,
      signal,
      path: { tenant_id: tenantId, run_id: "20000000-0000-4000-8000-000000000001" },
    }),
  );
  const next = result.stream.next();
  await tick();
  await controller.selectTenant(tenantB);
  assert.deepEqual(await next, { value: undefined, done: true });
  assert.equal(cancelled, true);
  stop();
});

test("token acquisition delayed past signout never sends its credential to the transport", async () => {
  const { controller, fixture, stop } = await setup();
  const token = deferred();
  fixture.auth.token = () => token.promise;
  const pending = controller.selectTenant(tenantA);
  await tick();
  await controller.signOut();
  token.resolve("old-token");
  await pending;
  assert.equal(
    fixture.controls.calls.some((call) => call.endsWith(tenantA)),
    false,
  );
  assert.equal(controller.snapshot().phase, "signed-out");
  stop();
});
