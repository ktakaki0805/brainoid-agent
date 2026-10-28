import assert from "node:assert/strict";
import { test } from "node:test";
import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import { DevicesController } from "../packages/ui/src/devices/controller.ts";
import { device, pairing, patch } from "../packages/ui/src/devices/contracts.ts";
import { deviceId, fixtureDevice } from "../packages/ui/src/devices/fixture.ts";
import { createSessionFixture, tenantA, tenantB } from "../packages/ui/src/session-fixture.ts";
import { SessionContext, SessionController } from "../packages/ui/src/session.ts";
const tick = () => new Promise((resolve) => setTimeout(resolve, 5));
const writes = (fixture) => fixture.devices.controls.calls.filter((v) => v.method !== "GET");
const body = { user_code: "ABCD1234", device_name: "確認するPi", allowed_entity_ids: [] };
async function using(
  work,
  { alter = (f) => f.transport, surface = "mobile", role = "owner" } = {},
) {
  const fixture = createSessionFixture();
  fixture.devices.controls.role = role;
  const session = new SessionController({
    auth: fixture.auth,
    baseUrl: "https://session.invalid",
    transport: alter(fixture),
  });
  const stop = session.start();
  await tick();
  await session.signIn("synthetic@example.test", "synthetic");
  await session.selectTenant(tenantA);
  const context = session.snapshot().context,
    devices = new DevicesController(context, surface);
  try {
    await devices.load();
    await work({ fixture, session, context, devices });
  } finally {
    devices.dispose();
    stop();
  }
}
async function review(devices, value = body) {
  await devices.beginPair();
  devices.draftPair(value);
  await devices.reviewPair();
  assert.ok(devices.snapshot().review);
}
async function select(devices) {
  await devices.select(deviceId);
  assert.ok(devices.snapshot().selected);
}
test("T003 pairing requires immutable explicit tenant confirmation; request contains public fields only", () =>
  using(async ({ devices, fixture }) => {
    await devices.confirmPair(tenantA, true);
    assert.equal(writes(fixture).length, 0);
    await review(devices);
    assert.throws(() => {
      devices.snapshot().review.body.user_code = "DIFF1234";
    });
    await devices.confirmPair(tenantA, false);
    await devices.confirmPair(tenantB, true);
    assert.equal(writes(fixture).length, 0);
    await devices.confirmPair(tenantA, true);
    assert.equal(writes(fixture).length, 1);
    assert.deepEqual(writes(fixture)[0].body, body);
    assert.equal(writes(fixture)[0].match, null);
    assert.equal(devices.snapshot().selected.name, body.device_name);
    assert.match(devices.snapshot().notice, /承認しました/);
  }));
for (const scenario of ["expired", "reused", "cross_tenant"])
  test(`T003 ${scenario} refusal never reports paired`, () =>
    using(async ({ devices, fixture }) => {
      await review(devices);
      fixture.devices.controls.scenario = scenario;
      await devices.confirmPair(tenantA, true);
      assert.equal(devices.snapshot().selected, undefined);
      assert.equal(devices.snapshot().pending, false);
      assert.equal(devices.snapshot().review, undefined);
      assert.match(devices.snapshot().notice, /登録できません/);
    }));
test("T003 successful code cannot be used by a different new operation", () =>
  using(async ({ devices, fixture }) => {
    await review(devices);
    await devices.confirmPair(tenantA, true);
    await review(devices);
    await devices.confirmPair(tenantA, true);
    assert.match(devices.snapshot().notice, /登録できません/);
    assert.notEqual(writes(fixture)[0].key, writes(fixture)[1].key);
  }));
test("pair response loss locks draft and retries exact key/body once under double tap", () =>
  using(async ({ devices, fixture }) => {
    await review(devices);
    fixture.devices.controls.scenario = "lost";
    await Promise.all([devices.confirmPair(tenantA, true), devices.confirmPair(tenantA, true)]);
    assert.equal(devices.snapshot().pending, true);
    const first = writes(fixture)[0];
    devices.cancel();
    devices.draftPair({ ...body, device_name: "changed" });
    await devices.beginPair();
    await devices.load();
    await devices.retry();
    assert.deepEqual(writes(fixture), [first, first]);
    assert.equal(devices.snapshot().selected.name, body.device_name);
  }));
for (const role of ["owner", "admin"])
  test(`${role} can edit and revoke with key and quoted canonical version`, () =>
    using(
      async ({ devices, fixture }) => {
        await select(devices);
        devices.edit();
        devices.draftEdit({
          name: "変更名",
          allowed_scopes: ["shared"],
          allowed_entity_ids: [],
          allowed_room_ids: [],
        });
        await devices.save();
        assert.equal(writes(fixture)[0].match, '"1"');
        assert.equal(devices.snapshot().selected.version, 2);
        assert.equal(devices.snapshot().devices.find((v) => v.id === deviceId).name, "変更名");
        await devices.revoke(false);
        assert.equal(writes(fixture).length, 1);
        await devices.revoke(true);
        assert.equal(writes(fixture)[1].match, '"2"');
        assert.notEqual(writes(fixture)[0].key, writes(fixture)[1].key);
        assert.match(devices.snapshot().notice, /秘密の消去は未確認/);
        assert.equal(devices.snapshot().revokedId, deviceId);
        await devices.beginPair(deviceId);
        devices.draftPair({ ...body, replaces_device_id: deviceId });
        await devices.reviewPair();
        await devices.confirmPair(tenantA, true);
        assert.equal(writes(fixture)[2].body.replaces_device_id, deviceId);
        assert.notEqual(devices.snapshot().selected.id, deviceId);
        assert.equal(devices.snapshot().revokedId, undefined);
      },
      { role },
    ));
test("member cannot list/get/update/revoke/confirm even when controller methods are called", () =>
  using(
    async ({ devices, fixture }) => {
      await devices.select(deviceId);
      await devices.beginPair();
      devices.draftPair(body);
      await devices.reviewPair();
      await devices.confirmPair(tenantA, true);
      devices.edit();
      devices.draftEdit({ name: "x" });
      await devices.save();
      await devices.revoke(true);
      assert.equal(fixture.devices.controls.calls.length, 0);
      assert.equal(devices.snapshot().devices.length, 0);
    },
    { role: "member" },
  ));
test("role downgrade before confirmation drops private management data without sending a write", () =>
  using(async ({ devices, fixture }) => {
    await review(devices);
    fixture.devices.controls.role = "member";
    await devices.confirmPair(tenantA, true);
    assert.equal(writes(fixture).length, 0);
    assert.equal(devices.snapshot().review, undefined);
    assert.equal(devices.snapshot().devices.length, 0);
  }));
test("role downgrade before update and before an exact retry is reauthorized", () =>
  using(async ({ devices, fixture }) => {
    await select(devices);
    devices.edit();
    devices.draftEdit({ name: "name" });
    fixture.devices.controls.scenario = "lost";
    await devices.save();
    assert.equal(devices.snapshot().pending, true);
    fixture.devices.controls.role = "member";
    await devices.retry();
    assert.equal(writes(fixture).length, 1);
    assert.equal(devices.snapshot().pending, false);
    assert.equal(devices.snapshot().selected, undefined);
  }));
test("API current denial clears metadata and cannot claim settings saved", () =>
  using(async ({ devices, fixture, session }) => {
    await select(devices);
    devices.edit();
    devices.draftEdit({ name: "拒否対象" });
    fixture.devices.controls.scenario = "denied";
    await devices.save();
    assert.equal(devices.snapshot().closed, true);
    assert.equal(devices.snapshot().selected, undefined);
    assert.equal(session.snapshot().context, undefined);
  }));
test("412 shows canonical current and original; fresh confirmation required before new key", () =>
  using(async ({ devices, fixture }) => {
    await select(devices);
    devices.edit();
    devices.draftEdit({ name: "編集中" });
    fixture.devices.controls.scenario = "conflict";
    await devices.save();
    const state = devices.snapshot();
    assert.equal(state.conflict.before.version, 1);
    assert.equal(state.conflict.current.version, 2);
    assert.equal(state.editing, undefined);
    assert.equal(state.devices[0].version, 2);
    await devices.revoke(true);
    await devices.save();
    assert.equal(writes(fixture).length, 1);
    devices.acceptCurrent();
    devices.edit();
    devices.draftEdit({ name: "最新から編集" });
    await devices.save();
    assert.equal(writes(fixture)[1].match, '"2"');
    assert.notEqual(writes(fixture)[0].key, writes(fixture)[1].key);
  }));
for (const kind of ["update", "revoke"])
  test(`${kind} response loss exact retry preserves key/ETag and blocks unrelated management`, () =>
    using(async ({ devices, fixture }) => {
      await select(devices);
      fixture.devices.controls.scenario = "lost";
      if (kind === "update") {
        devices.edit();
        devices.draftEdit({ name: "応答不明" });
        await devices.save();
      } else await devices.revoke(true);
      assert.equal(devices.snapshot().pending, true);
      const first = writes(fixture)[0];
      await devices.beginPair();
      await devices.select(deviceId);
      await devices.retry();
      assert.deepEqual(writes(fixture), [first, first]);
      assert.equal(devices.snapshot().pending, false);
    }));
test("failed authorization read during unknown retry retains exact pending operation", () =>
  using(async ({ devices, fixture }) => {
    await review(devices);
    fixture.devices.controls.scenario = "lost";
    await devices.confirmPair(tenantA, true);
    fixture.controls.status = 503;
    await devices.retry();
    assert.equal(devices.snapshot().pending, true);
    fixture.controls.status = 200;
    await devices.retry();
    assert.deepEqual(writes(fixture)[0], writes(fixture)[1]);
  }));
test("display makes no management requests", () =>
  using(
    async ({ devices, fixture }) => {
      await devices.beginPair();
      await devices.select(deviceId);
      assert.equal(fixture.devices.controls.calls.length, 0);
    },
    { surface: "display" },
  ));
for (const status of [201, 403])
  test(`late pairing ${status} after disposal cannot parse response, deny new context or publish`, async () => {
    let release,
      entered = false,
      reads = 0;
    await using(
      async ({ devices, session }) => {
        await review(devices);
        const pending = devices.confirmPair(tenantA, true);
        while (!entered) await tick();
        devices.dispose();
        await session.selectTenant(tenantB);
        release();
        await pending;
        assert.equal(reads, 0);
        assert.equal(devices.snapshot().closed, true);
        assert.equal(session.snapshot().context.tenantId, tenantB);
      },
      {
        alter: (fixture) => async (request) => {
          if (!request.url.endsWith("device-pairings:confirm")) return fixture.transport(request);
          const response =
            status === 201 ? await fixture.transport(request) : Response.json({}, { status });
          const json = response.json.bind(response);
          response.json = async () => {
            reads++;
            return json();
          };
          entered = true;
          await new Promise((resolve) => {
            release = resolve;
          });
          return response;
        },
      },
    );
  });
for (const change of ["tenant", "uid", "suspend"])
  test(`${change} destroys drafts and late list results`, async () => {
    let hold = false,
      release,
      entered = false;
    await using(
      async ({ devices, session, fixture }) => {
        await review(devices);
        hold = true;
        const pending = devices.load();
        while (!entered) await tick();
        if (change === "tenant") await session.selectTenant(tenantB);
        else if (change === "uid") fixture.emit({ uid: "another@example.test" });
        else session.suspend();
        release();
        await pending;
        assert.equal(devices.snapshot().closed, true);
        assert.equal(devices.snapshot().devices.length, 0);
        assert.equal(devices.snapshot().pairing, undefined);
      },
      {
        alter: (fixture) => async (request) => {
          const response = await fixture.transport(request);
          if (hold && request.method === "GET" && request.url.includes("/devices")) {
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
test("list reload removes vanished selected device and draft; invalid stale version cannot replace newer canonical", () =>
  using(async ({ devices, fixture }) => {
    await select(devices);
    devices.edit();
    devices.draftEdit({ name: "v2" });
    await devices.save();
    fixture.devices.rows.set(`${tenantA}:${deviceId}`, fixtureDevice(tenantA));
    await devices.select(deviceId);
    assert.equal(devices.snapshot().selected, undefined);
    assert.equal(devices.snapshot().devices[0].version, 2);
    fixture.devices.controls.scenario = "empty";
    await devices.load();
    assert.equal(devices.snapshot().devices.length, 0);
    assert.equal(devices.snapshot().editing, undefined);
  }));
test("re-pair refuses active device and cannot change approved replacement id", () =>
  using(async ({ devices, fixture }) => {
    await devices.beginPair(deviceId);
    assert.equal(devices.snapshot().pairing, undefined);
    assert.equal(writes(fixture).length, 0);
    await select(devices);
    await devices.revoke(true);
    await devices.beginPair(deviceId);
    devices.draftPair(body);
    assert.equal(devices.snapshot().pairing.replaces_device_id, deviceId);
  }));
test("T003 tenant metadata change invalidates review without submitting confirmation", () =>
  using(async ({ devices, fixture }) => {
    await review(devices);
    fixture.devices.controls.role = "admin";
    await devices.confirmPair(tenantA, true);
    assert.equal(writes(fixture).length, 0);
    assert.equal(devices.snapshot().review, undefined);
  }));
test("strict Device DTO validates actual required fields, target IDs, tenant, status and disallows secrets", () => {
  const valid = fixtureDevice(tenantA);
  assert.equal(device(valid, tenantA).id, deviceId);
  for (const difference of [
    { tenant_id: tenantB },
    { authorization_version: undefined },
    { allowed_scopes: undefined },
    { allowed_room_ids: undefined },
    { status: "paired" },
    { allowed_scopes: ["admin"] },
    { allowed_entity_ids: ["not-id"] },
    { capabilities: [1] },
    { version: 0 },
    { poll_secret: "synthetic-secret" },
    { access_token: "synthetic-token" },
    { refresh_token: "synthetic-token" },
  ])
    assert.throws(() => device({ ...valid, ...difference }, tenantA));
  assert.equal(pairing(body), true);
  for (const difference of [
    { user_code: "abc" },
    { device_name: " " },
    { allowed_entity_ids: ["bad"] },
    { replaces_device_id: "bad" },
    { poll_secret: "secret" },
  ])
    assert.equal(pairing({ ...body, ...difference }), false);
  for (const value of [
    {},
    { name: null },
    { status: "active" },
    { allowed_scopes: ["admin"] },
    { allowed_room_ids: ["bad"] },
  ])
    assert.equal(patch(value), false);
});
test("malformed or suspended success is unknown, never configured", () =>
  using(
    async ({ devices, fixture }) => {
      await review(devices);
      await devices.confirmPair(tenantA, true);
      assert.equal(devices.snapshot().pending, true);
      assert.equal(devices.snapshot().selected, undefined);
      assert.doesNotMatch(devices.snapshot().notice, /承認しました/);
      assert.equal(writes(fixture).length, 1);
    },
    {
      alter: (fixture) => async (request) => {
        const response = await fixture.transport(request);
        return request.url.endsWith("device-pairings:confirm")
          ? Response.json({ ...(await response.json()), status: "suspended" }, { status: 201 })
          : response;
      },
    },
  ));

// These are actual repository DTOs, with only transaction/object stores and clock synthetic.
// The fixture's unused HTTP TestClient is not started: this test owns no HTTP listener.
function ledgerTransitions() {
  return JSON.parse(
    execFileSync(
      "uv",
      [
        "run",
        "--all-packages",
        "--locked",
        "--offline",
        "python",
        "-B",
        "-c",
        `
import json
from types import SimpleNamespace
from fastapi import FastAPI
from test_devices import DeviceManagementTests
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.repositories.devices import DevicePatch
from brainoid_policy.control_ledger import LedgerError
results = []
for phase in ("intents", "commits"):
    fixture = DeviceManagementTests()
    fixture.enterContext = lambda context: SimpleNamespace(app=FastAPI())
    fixture.setUp()
    try:
        before = fixture.management.get(fixture.identity(), fixture.tenant, fixture.device.id)
        def fail(name):
            if "/" + phase + "/" in name:
                raise LedgerError()
        fixture.ledger._objects.after = fail
        changes = DevicePatch(allowed_scopes=["shared", "notes"])
        try:
            fixture.update(changes)
            raise AssertionError("Expected ledger failure")
        except APIError as error:
            assert error.code == ErrorCode.TEMPORARILY_UNAVAILABLE
        pending = fixture.management.get(fixture.identity(), fixture.tenant, fixture.device.id)
        assert before.version == pending.version == 1
        assert before.status == "active" and pending.status == "suspended"
        assert pending.authorization_version == (1 if phase == "intents" else 2)
        fixture.ledger._objects.after = lambda name: None
        recovered = fixture.update(changes)
        assert recovered.version == 2 and recovered.status == "active"
        results.append({"phase": phase, "before": before.model_dump(mode="json"), "pending": pending.model_dump(mode="json"), "recovered": recovered.model_dump(mode="json")})
    finally:
        fixture.tearDown()
        fixture.doCleanups()
print(json.dumps(results))
`,
      ],
      { encoding: "utf8", timeout: 30000, env: { ...process.env, PYTHONPATH: resolve("tests") } },
    ),
  );
}
test("R1-01 real ledger intent/commit failure suspends same metadata version; list/detail/draft retire and recovery advances", async () => {
  for (const { phase, before, pending, recovered } of ledgerTransitions()) {
    let current = before;
    const context = new SessionContext({
      uid: "demo-a-owner",
      tenantId: before.tenant_id,
      generation: 1,
      baseUrl: "https://session.invalid",
      current: () => true,
      token: async () => "synthetic",
      denied: () => {},
      transport: async (request) => {
        const path = new URL(request.url).pathname;
        if (path.endsWith("/devices"))
          return Response.json({ items: [current], next_cursor: null });
        if (path.includes("/devices/"))
          return Response.json(current, { headers: { ETag: `"${current.version}"` } });
        return Response.json({
          id: before.tenant_id,
          tenant_id: before.tenant_id,
          status: "active",
          name: "Repository fixture",
          version: 1,
          my_role: "owner",
        });
      },
    });
    const devices = new DevicesController(context, "mobile");
    try {
      await devices.load();
      await devices.select(before.id);
      devices.edit();
      assert.ok(devices.snapshot().editing, phase);
      current = pending;
      await devices.load();
      assert.deepEqual(devices.snapshot().devices, [pending], phase);
      assert.equal(devices.snapshot().selected, undefined);
      assert.equal(devices.snapshot().editing, undefined);
      assert.equal(devices.snapshot().notice, undefined);
      await devices.select(before.id);
      assert.deepEqual(devices.snapshot().selected, pending);
      devices.edit();
      assert.equal(devices.snapshot().editing, undefined);
      current = before;
      await devices.select(before.id);
      assert.equal(devices.snapshot().selected, undefined);
      assert.deepEqual(
        devices.snapshot().devices,
        [pending],
        "Old active DTO must not resurrect after suspension",
      );
      current = recovered;
      await devices.select(before.id);
      assert.deepEqual(devices.snapshot().selected, recovered);
      assert.deepEqual(devices.snapshot().devices, [recovered]);
      devices.edit();
      assert.ok(devices.snapshot().editing);
    } finally {
      devices.dispose();
      context.dispose();
    }
  }
});
for (const difference of [
  { name: "same-version metadata mismatch" },
  { allowed_scopes: [] },
  { authorization_version: 0 },
  { connection_generation: 0 },
  { tenant_id: tenantB },
  { access_token: "forbidden" },
])
  test(`R1-01 legitimate status handling retains DTO/metadata/authority fences: ${JSON.stringify(difference)}`, () =>
    using(async ({ devices, fixture }) => {
      fixture.devices.rows.set(`${tenantA}:${deviceId}`, {
        ...fixtureDevice(tenantA),
        ...difference,
      });
      await devices.select(deviceId);
      assert.equal(devices.snapshot().selected, undefined);
      assert.deepEqual(devices.snapshot().devices, [fixtureDevice(tenantA)]);
    }));
for (const kind of ["pair", "update", "revoke"])
  test(`R1-02 uncertain ${kind} retry429 retains exact operation and blocks new writes until reconciled`, async () => {
    let rate = false;
    const attempts = [];
    await using(
      async ({ devices, fixture }) => {
        if (kind === "pair") await review(devices);
        else await select(devices);
        fixture.devices.controls.scenario = "lost";
        if (kind === "pair") await devices.confirmPair(tenantA, true);
        else if (kind === "update") {
          devices.edit();
          devices.draftEdit({ name: "Unknown update" });
          await devices.save();
        } else await devices.revoke(true);
        assert.equal(devices.snapshot().pending, true);
        rate = true;
        await devices.retry();
        assert.equal(devices.snapshot().pending, true);
        assert.match(devices.snapshot().notice, /前回の結果は未確認/);
        assert.doesNotMatch(devices.snapshot().notice, /新しいコード/);
        await devices.beginPair();
        devices.cancel();
        await devices.reviewPair();
        await devices.confirmPair(tenantA, true);
        devices.edit();
        await devices.save();
        await devices.revoke(true);
        assert.equal(attempts.length, 2);
        rate = false;
        await devices.retry();
        assert.equal(devices.snapshot().pending, false);
        assert.equal(attempts.length, 3);
        assert.deepEqual(attempts, [attempts[0], attempts[0], attempts[0]]);
      },
      {
        alter: (fixture) => async (request) => {
          if (
            ["POST", "PATCH", "DELETE"].includes(request.method) &&
            /device-pairings:confirm|\/devices\//.test(request.url)
          ) {
            attempts.push({
              method: request.method,
              url: request.url,
              key: request.headers.get("Idempotency-Key"),
              match: request.headers.get("If-Match"),
              body: await request.clone().text(),
            });
            if (rate) return Response.json({}, { status: 429 });
          }
          return fixture.transport(request);
        },
      },
    );
  });
test("R1-02 initial429 is a definite refusal and permits a newly reviewed operation", async () => {
  let rate = true;
  const keys = [];
  await using(
    async ({ devices, fixture }) => {
      await review(devices);
      await devices.confirmPair(tenantA, true);
      assert.equal(devices.snapshot().pending, false);
      assert.equal(devices.snapshot().review, undefined);
      assert.equal(writes(fixture).length, 0);
      rate = false;
      await devices.reviewPair();
      await devices.confirmPair(tenantA, true);
      assert.ok(devices.snapshot().selected);
      assert.notEqual(keys[0], keys[1]);
    },
    {
      alter: (fixture) => async (request) => {
        if (request.url.endsWith("device-pairings:confirm")) {
          keys.push(request.headers.get("Idempotency-Key"));
          if (rate) return Response.json({}, { status: 429 });
        }
        return fixture.transport(request);
      },
    },
  );
});
for (const refusal of [409, 410])
  test(`R1-02 uncertain pairing retry${refusal} remains unconfirmed and directs explicit canonical recovery`, async () => {
    let refuse = false;
    await using(
      async ({ devices, fixture, session }) => {
        await review(devices);
        fixture.devices.controls.scenario = "lost";
        await devices.confirmPair(tenantA, true);
        refuse = true;
        await devices.retry();
        assert.equal(devices.snapshot().pending, true);
        assert.match(devices.snapshot().notice, /未確認のまま/);
        await devices.beginPair();
        await devices.confirmPair(tenantA, true);
        assert.equal(writes(fixture).length, 1);
        await session.selectTenant(tenantA);
        assert.equal(devices.snapshot().closed, true);
        const current = new DevicesController(session.snapshot().context, "mobile");
        try {
          await current.load();
          assert.equal(
            current.snapshot().devices.filter((v) => v.name === body.device_name).length,
            1,
          );
        } finally {
          current.dispose();
        }
      },
      {
        alter: (fixture) => async (request) =>
          refuse && request.url.endsWith("device-pairings:confirm")
            ? Response.json({}, { status: refusal })
            : fixture.transport(request),
      },
    );
  });
for (const [role, change] of [
  ["owner", { my_role: "admin" }],
  ["admin", { my_role: "owner" }],
  ["owner", { name: "Renamed current tenant" }],
  ["owner", { version: 2 }],
])
  test(`R1-03 unknown exact replay survives current still-manager tenant change ${JSON.stringify(change)}`, async () => {
    let changed = false;
    await using(
      async ({ devices, fixture }) => {
        await review(devices);
        fixture.devices.controls.scenario = "lost";
        await devices.confirmPair(tenantA, true);
        const first = writes(fixture)[0];
        changed = true;
        await devices.retry();
        assert.equal(devices.snapshot().pending, false);
        assert.deepEqual(writes(fixture), [first, first]);
        for (const [key, value] of Object.entries(change))
          assert.equal(devices.snapshot().tenant[key], value);
        assert.ok(devices.snapshot().selected);
      },
      {
        role,
        alter: (fixture) => async (request) => {
          const response = await fixture.transport(request);
          return changed && new URL(request.url).pathname === `/v1/tenants/${tenantA}`
            ? Response.json({ ...(await response.json()), ...change })
            : response;
        },
      },
    );
  });
for (const change of [{ name: "Renamed before send" }, { version: 2 }])
  test(`R1-03 first unsent confirmation still requires review after metadata change ${JSON.stringify(change)}`, async () => {
    let changed = false;
    await using(
      async ({ devices, fixture }) => {
        await review(devices);
        changed = true;
        await devices.confirmPair(tenantA, true);
        assert.equal(writes(fixture).length, 0);
        assert.equal(devices.snapshot().review, undefined);
        assert.equal(devices.snapshot().pending, false);
      },
      {
        alter: (fixture) => async (request) => {
          const response = await fixture.transport(request);
          return changed && new URL(request.url).pathname === `/v1/tenants/${tenantA}`
            ? Response.json({ ...(await response.json()), ...change })
            : response;
        },
      },
    );
  });
test("R1-03 unknown pairing still erases on actual member downgrade", () =>
  using(async ({ devices, fixture }) => {
    await review(devices);
    fixture.devices.controls.scenario = "lost";
    await devices.confirmPair(tenantA, true);
    fixture.devices.controls.role = "member";
    await devices.retry();
    assert.equal(writes(fixture).length, 1);
    assert.equal(devices.snapshot().pending, false);
    assert.equal(devices.snapshot().review, undefined);
    assert.equal(devices.snapshot().devices.length, 0);
  }));

test("R1-01 a later list page failure cannot retain active after a validated suspension", async () => {
  let failSecondPage = false;
  const suspended = { ...fixtureDevice(tenantA), status: "suspended" };
  await using(
    async ({ devices }) => {
      await select(devices);
      devices.edit();
      failSecondPage = true;
      await devices.load();
      assert.deepEqual(devices.snapshot().devices, [suspended]);
      assert.equal(devices.snapshot().selected, undefined);
      assert.equal(devices.snapshot().editing, undefined);
      assert.match(devices.snapshot().notice, /確認できません/);
    },
    {
      alter: (fixture) => async (request) => {
        const url = new URL(request.url);
        if (failSecondPage && url.pathname.endsWith("/devices"))
          return url.searchParams.has("cursor")
            ? Response.json({}, { status: 503 })
            : Response.json({ items: [suspended], next_cursor: "synthetic-next-page" });
        return fixture.transport(request);
      },
    },
  );
});
