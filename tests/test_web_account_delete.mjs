import assert from "node:assert/strict";
import { test } from "node:test";
import { SessionController } from "../packages/ui/src/session.ts";
import { createSessionFixture, tenantA } from "../packages/ui/src/session-fixture.ts";
import {
  AccountRecovery,
  locatorKey,
  ownerBinding,
} from "../apps/web-data-controls/src/recovery.ts";
import { authenticationConfiguration } from "../apps/mobile/src/auth-config.ts";
const tick = () => new Promise((resolve) => setImmediate(resolve));
async function settle(predicate, timeoutMs = 2000) {
  const deadline = performance.now() + timeoutMs;
  while (performance.now() < deadline) {
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  assert.fail("Account state did not settle");
}
test("state waiting allows asynchronous completion after a normal delay", async () => {
  let complete = false;
  const timer = setTimeout(() => {
    complete = true;
  }, 50);
  try {
    await settle(() => complete);
  } finally {
    clearTimeout(timer);
  }
});
test("state waiting rejects a never-completing state within a finite deadline", {
  timeout: 2000,
}, async () => {
  await assert.rejects(
    settle(() => false, 30),
    /Account state did not settle/,
  );
});
function memoryStorage() {
  const values = new Map();
  return {
    values,
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
}
async function using(
  work,
  { storage = memoryStorage(), wrap = (transport) => transport, auth } = {},
) {
  const fixture = createSessionFixture();
  const session = new SessionController({
    auth: auth ? auth(fixture.auth) : fixture.auth,
    baseUrl: "https://session.invalid",
    transport: wrap(fixture.transport),
    accountOnly: true,
    requestedTenant: () => tenantA,
  });
  const recovery = new AccountRecovery(session, () => storage);
  const stop = session.start();
  await tick();
  try {
    await work({ fixture, session, recovery, storage });
  } finally {
    recovery.dispose();
    stop();
  }
}
async function login({ session, recovery }, email = "synthetic@example.test") {
  await session.signIn(email, "synthetic-only");
  await settle(() => recovery.snapshot().controls && !recovery.snapshot().controls.snapshot().busy);
  return recovery.snapshot().controls;
}
async function accept(state) {
  const controls = await login(state);
  await controls.prepare("delete_account");
  await controls.reauthenticate("synthetic@example.test", "synthetic-only");
  await controls.confirm();
  assert.equal(controls.snapshot().operation.status, "pending");
  return controls;
}
const posts = (fixture) =>
  fixture.dataControls.controls.calls.filter((call) => call.method === "POST");
test("public entry uses global context only; requires actual reauthentication then target confirmation", () =>
  using(async (state) => {
    const { session, fixture } = state;
    const controls = await login(state);
    assert.deepEqual(fixture.controls.calls, []);
    for (const choice of ["delete_tenant", "leave_tenant", "export", "conversation"])
      await controls.prepare(choice, { name: "テナント A", conversation: tenantA });
    await controls.loadConversations();
    await session.selectTenant(tenantA);
    await session.createTenant("Injected", "UTC");
    await session.loadTenants();
    assert.deepEqual(fixture.controls.calls, []);
    assert.equal(controls.snapshot().review, undefined);
    await controls.prepare("delete_account");
    assert.match(controls.snapshot().review.label, /synthetic@example.test/);
    await controls.confirm();
    assert.equal(posts(fixture).length, 0);
    await controls.reauthenticate("other@example.test", "synthetic-only");
    await controls.confirm();
    assert.equal(posts(fixture).length, 0);
    await controls.reauthenticate("synthetic@example.test", "synthetic-only");
    assert.equal(posts(fixture).length, 0);
    await controls.confirm();
    assert.equal(fixture.controls.refreshes, 0);
    assert.deepEqual(posts(fixture)[0].body, { kind: "delete_account", confirm: true });
    assert.equal(posts(fixture)[0].path, "/v1/operations");
    assert.equal(controls.snapshot().operation.status, "pending");
  }));
test("reload clears authentication and private state; same-user login recovers only by authorized GET", () =>
  using(async (state) => {
    const { session, recovery, storage, fixture } = state;
    const old = await accept(state),
      id = old.snapshot().operation.operation_id;
    const saved = JSON.parse(storage.getItem(locatorKey));
    assert.deepEqual(Object.keys(saved).sort(), ["operation", "owner", "version"]);
    assert.equal(saved.owner, await ownerBinding("synthetic@example.test"));
    assert.equal(JSON.stringify(saved).includes("synthetic@"), false);
    recovery.suspend();
    await tick();
    assert.equal(old.snapshot().operation, undefined);
    assert.equal(session.globalContext(), undefined);
    assert.equal(await fixture.auth.current(), null);
    assert.equal(storage.getItem(locatorKey) !== null, true);
    await session.resume();
    const fresh = await login(state);
    assert.equal(fresh.snapshot().operation.operation_id, id);
    assert.equal(fresh.snapshot().operation.kind, "delete_account");
    assert.equal(posts(fixture).length, 1);
    await fresh.retry();
    assert.equal(posts(fixture).length, 1);
  }));
for (const transition of ["signout", "different-user", "null-auth", "aba"])
  test(`${transition} removes locator and private operation`, () =>
    using(async (state) => {
      const old = await accept(state);
      if (transition === "signout") await state.recovery.signOut();
      else if (transition === "null-auth") state.fixture.emit(null);
      else {
        state.fixture.emit({ uid: "other@example.test" });
        if (transition === "aba") state.fixture.emit({ uid: "synthetic@example.test" });
      }
      await tick();
      assert.equal(old.snapshot().operation, undefined);
      assert.equal(state.storage.getItem(locatorKey), null);
      if (state.recovery.snapshot().controls)
        assert.equal(state.recovery.snapshot().controls.snapshot().operation, undefined);
    }));
test("another identity after reload cannot read or display the stored operation", () =>
  using(async (state) => {
    await accept(state);
    state.recovery.suspend();
    await tick();
    await state.session.resume();
    const count = state.fixture.controls.calls.length;
    const controls = await login(state, "other@example.test");
    assert.equal(state.storage.getItem(locatorKey), null);
    assert.equal(controls.snapshot().operation, undefined);
    assert.equal(state.fixture.controls.calls.length, count);
  }));
test("forged matching owner hash still requires server authorization and cannot create an operation", () =>
  using(async (state) => {
    const controls = await accept(state),
      id = controls.snapshot().operation.operation_id;
    await state.recovery.signOut();
    state.storage.setItem(
      locatorKey,
      JSON.stringify({
        version: 1,
        owner: await ownerBinding("other@example.test"),
        operation: id,
      }),
    );
    const other = await login(state, "other@example.test");
    assert.equal(other.snapshot().operation, undefined);
    assert.match(other.snapshot().notice, /消去完了とは判断できません/);
    await other.prepare("delete_account");
    await other.confirm();
    await other.retry();
    assert.equal(posts(state.fixture).length, 1);
    assert.equal(other.snapshot().review, undefined);
  }));
for (const change of ["kind", "id", "version", "extra", "broken"])
  test(`untrusted ${change} locator is discarded without GET or POST`, async () => {
    const storage = memoryStorage();
    const value = {
      version: 1,
      owner: await ownerBinding("synthetic@example.test"),
      operation: "10000000-0000-4000-8000-000000000001",
    };
    if (change === "kind") value.kind = "delete_tenant";
    if (change === "id") value.operation = "../../tenants/target";
    if (change === "version") value.version = 2;
    if (change === "extra") value.apiUrl = "https://attacker.invalid";
    storage.setItem(locatorKey, change === "broken" ? "{" : JSON.stringify(value));
    await using(
      async (state) => {
        await login(state);
        assert.deepEqual(state.fixture.controls.calls, []);
        assert.equal(storage.getItem(locatorKey), null);
      },
      { storage },
    );
  });
for (const [type, operation] of [
  ["array", ["10000000-0000-4000-8000-000000000001"]],
  ["nested-array", [["10000000-0000-4000-8000-000000000001"]]],
  ["object", { operation: "10000000-0000-4000-8000-000000000001" }],
  ["null", null],
  ["number", 42],
  ["boolean", true],
])
  test(`non-string ${type} operation locator is cleared and fresh self confirmation remains usable`, async () => {
    const storage = memoryStorage();
    storage.setItem(
      locatorKey,
      JSON.stringify({
        version: 1,
        owner: await ownerBinding("synthetic@example.test"),
        operation,
      }),
    );
    await using(
      async (state) => {
        const controls = await login(state);
        assert.equal(storage.getItem(locatorKey), null);
        assert.deepEqual(state.fixture.controls.calls, []);
        assert.equal(controls.snapshot().recovery, undefined);
        await controls.prepare("delete_account");
        assert.deepEqual(controls.snapshot().review.body, {
          kind: "delete_account",
          confirm: true,
        });
        assert.equal(controls.snapshot().reauthentication, true);
        await controls.confirm();
        assert.deepEqual(state.fixture.controls.calls, []);
        await controls.reauthenticate("synthetic@example.test", "synthetic-only");
        assert.equal(controls.snapshot().reauthentication, false);
        assert.ok(controls.snapshot().review);
        assert.deepEqual(state.fixture.controls.calls, []);
      },
      { storage },
    );
  });
for (const response of ["kind", "id", "failed", "outcome_unknown", "403", "404", "401", "503"])
  test(`recovery response ${response} is never inferred to be completed`, async () => {
    let mode;
    await using(
      async (state) => {
        await accept(state);
        state.recovery.suspend();
        await tick();
        await state.session.resume();
        mode = response;
        await state.session.signIn("synthetic@example.test", "synthetic-only");
        await settle(
          () =>
            state.session.snapshot().phase === "signed-out" ||
            (state.recovery.snapshot().controls &&
              !state.recovery.snapshot().controls.snapshot().busy),
        );
        const current = state.recovery.snapshot().controls?.snapshot();
        assert.notEqual(current?.operation?.status, "succeeded");
        if (response === "401") {
          assert.equal(state.storage.getItem(locatorKey), null);
          assert.equal(current, undefined);
        } else if (["failed", "outcome_unknown"].includes(response))
          assert.equal(current.operation.status, response);
        else assert.equal(current.operation, undefined);
      },
      {
        wrap: (transport) => async (input, init) => {
          const request = new Request(input, init);
          const result = await transport(request);
          if (!mode || request.method !== "GET") return result;
          if (/^\d+$/.test(mode)) return Response.json({}, { status: Number(mode) });
          const op = await result.json();
          if (mode === "kind") op.kind = "delete_tenant";
          else if (mode === "id") op.operation_id = crypto.randomUUID();
          else op.status = mode;
          return Response.json(op);
        },
      },
    );
  });
test("initial token acquisition expiry clears operation and locator without relying on a later 401", async () => {
  let expired = false;
  await using(
    async (state) => {
      const controls = await accept(state);
      expired = true;
      await controls.refresh();
      await tick();
      assert.equal(state.storage.getItem(locatorKey), null);
      assert.equal(state.recovery.snapshot().controls, undefined);
    },
    {
      auth: (auth) => ({
        ...auth,
        token: async (...args) => {
          if (expired) throw new Error("Expired");
          return auth.token(...args);
        },
      }),
    },
  );
});
test("late operation GET cannot survive A-B-A identity changes or recreate storage", async () => {
  let release,
    entered = false;
  await using(
    async (state) => {
      const controls = await accept(state);
      const pending = controls.refresh();
      await settle(() => entered);
      state.fixture.emit({ uid: "other@example.test" });
      state.fixture.emit({ uid: "synthetic@example.test" });
      release();
      await pending;
      await tick();
      assert.equal(controls.snapshot().operation, undefined);
      assert.equal(state.storage.getItem(locatorKey), null);
      assert.equal(state.recovery.snapshot().controls?.snapshot().operation, undefined);
    },
    {
      wrap: (transport) => async (input, init) => {
        const request = new Request(input, init);
        const response = await transport(request);
        if (request.method === "GET") {
          entered = true;
          await new Promise((resolve) => {
            release = resolve;
          });
        }
        return response;
      },
    },
  );
});
test("storage failure keeps authenticated deletion usable and reports missing reload recovery", () =>
  using(
    async (state) => {
      const controls = await accept(state);
      assert.equal(controls.snapshot().operation.status, "pending");
      assert.match(state.recovery.snapshot().notice, /自動回復は利用できません/);
    },
    {
      storage: {
        getItem() {
          throw new Error("Disabled");
        },
        setItem() {
          throw new Error("Disabled");
        },
        removeItem() {
          throw new Error("Disabled");
        },
      },
    },
  ));
test("public build configuration has no default project/API and rejects emulator outside emulator mode", () => {
  assert.equal(authenticationConfiguration({}, "production"), undefined);
  const env = {
    VITE_API_URL: "http://127.0.0.1:8000",
    VITE_FIREBASE_API_KEY: "fixture",
    VITE_FIREBASE_AUTH_DOMAIN: "demo-brainoid.firebaseapp.com",
    VITE_FIREBASE_PROJECT_ID: "demo-brainoid",
    VITE_FIREBASE_APP_ID: "fixture",
    VITE_AUTH_EMULATOR: "http://127.0.0.1:9099",
  };
  assert.throws(() => authenticationConfiguration(env, "production"));
  assert.equal(authenticationConfiguration(env, "emulator").emulator, true);
  assert.throws(() =>
    authenticationConfiguration(
      { ...env, VITE_API_URL: "https://attacker.invalid/path" },
      "emulator",
    ),
  );
});
for (const replacement of ["same-user", "different-user", "tenant-selection"])
  test(`old initial token failure after ${replacement} cannot sign out the new context`, async () => {
    const fixture = createSessionFixture();
    const session = new SessionController({
      auth: fixture.auth,
      transport: fixture.transport,
      baseUrl: "https://session.invalid",
    });
    const stop = session.start();
    try {
      await tick();
      await session.signIn("synthetic@example.test", "synthetic");
      const token = fixture.auth.token;
      let reject;
      fixture.auth.token = () =>
        new Promise((_, fail) => {
          reject = fail;
        });
      const old = session.selectTenant(tenantA);
      await settle(() => reject);
      fixture.auth.token = token;
      if (replacement === "tenant-selection")
        await session.selectTenant("10000000-0000-4000-8000-000000000002");
      else
        await session.signIn(
          replacement === "same-user" ? "synthetic@example.test" : "other@example.test",
          "synthetic",
        );
      const current = session.globalContext();
      reject(new Error("Old credential expired"));
      await old;
      assert.equal(session.globalContext(), current);
      assert.equal(current.signal.aborted, false);
      assert.notEqual(session.snapshot().phase, "signed-out");
    } finally {
      stop();
    }
  });
