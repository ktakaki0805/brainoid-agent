import assert from "node:assert/strict";
import { test } from "node:test";
import { SessionController } from "../packages/ui/src/session.ts";
import { createSessionFixture, tenantA, tenantB } from "../packages/ui/src/session-fixture.ts";
import { MemoryController, validateMemory } from "../packages/ui/src/memories/controller.ts";
import { subjectId, sharedMemoryId, privateMemoryId } from "../packages/ui/src/memories/fixture.ts";
const tick = () => new Promise((resolve) => setTimeout(resolve, 5));
async function setup(surface = "mobile", alter = (fetch) => fetch) {
  const fixture = createSessionFixture();
  const session = new SessionController({
    auth: fixture.auth,
    transport: alter(fixture.transport),
    baseUrl: "https://session.invalid",
  });
  const stop = session.start();
  await tick();
  await session.signIn("synthetic@example.test", "fixture");
  await tick();
  await session.selectTenant(tenantA);
  const memory = new MemoryController(session.snapshot().context, surface);
  await memory.load();
  return {
    fixture,
    session,
    memory,
    close() {
      memory.dispose();
      stop();
    },
  };
}
function draft(memory, changes = {}) {
  memory.create();
  memory.draft({
    ...memory.snapshot().editor.draft,
    subject_entity_id: subjectId,
    value: "新しい好み",
    ...changes,
  });
}
async function using(work, ...args) {
  const env = await setup(...args);
  try {
    await work(env);
  } finally {
    env.close();
  }
}

test("T-008 explicit save preserves scope/provenance and search uses API", () =>
  using(async ({ memory, fixture }) => {
    draft(memory);
    await memory.save();
    const saved = memory.snapshot().items.find((row) => row.value === "新しい好み");
    assert.equal(saved.status, "confirmed");
    assert.equal(saved.visibility, "private");
    assert.equal(saved.created_by, "synthetic@example.test");
    assert.deepEqual(saved.source_refs, []);
    await memory.load("新しい好み");
    assert.deepEqual(
      memory.snapshot().items.map((row) => row.id),
      [saved.id],
    );
    assert.equal(fixture.memories.controls.calls.at(-1).path.endsWith("memories:search"), true);
  }));
test("private/shared_display rejected before request; server validation surfaced without success", () =>
  using(async ({ memory, fixture }) => {
    draft(memory, { display_policy: "shared_display" });
    const count = fixture.memories.controls.calls.length;
    await memory.save();
    assert.equal(fixture.memories.controls.calls.length, count);
    assert.match(memory.snapshot().notice, /共有画面/);
    memory.draft({ ...memory.snapshot().editor.draft, display_policy: "never" });
    fixture.memories.controls.scenario = "reject";
    await memory.save();
    assert.match(memory.snapshot().notice, /サーバーで拒否/);
    assert.equal(memory.snapshot().pending, false);
    assert.ok(memory.snapshot().editor);
    assert.equal(memory.snapshot().items.length, 2);
  }));
test("T-010 revision uses strong ETag and replaces old fact, preserving scope", () =>
  using(async ({ memory, fixture }) => {
    await memory.select(sharedMemoryId, "revise");
    memory.draft({ ...memory.snapshot().editor.draft, value: "新しい照明" });
    await memory.save();
    assert.equal(
      memory.snapshot().items.some((item) => item.id === sharedMemoryId),
      false,
    );
    const saved = memory.snapshot().items.find((item) => item.value === "新しい照明");
    assert.equal(saved.supersedes_fact_id, sharedMemoryId);
    assert.equal(saved.visibility, "shared");
    assert.equal(fixture.memories.controls.calls.at(-1).match, '"1"');
    assert.equal(
      fixture.memories.records(tenantA, "synthetic@example.test").get(sharedMemoryId).status,
      "superseded",
    );
  }));
test("T-012 412 retains draft, retrieves old status and current list, requires explicit reselection", () =>
  using(async ({ memory, fixture }) => {
    await memory.select(sharedMemoryId, "revise");
    memory.draft({ ...memory.snapshot().editor.draft, value: "保持する入力" });
    fixture.memories.controls.scenario = "conflict";
    await memory.save();
    const state = memory.snapshot();
    assert.equal(state.conflict.before.status, "confirmed");
    assert.equal(state.conflict.current.status, "superseded");
    assert.equal(state.editor.draft.value, "保持する入力");
    assert.equal(
      state.items.some((item) => item.id === sharedMemoryId),
      false,
    );
    assert.ok(state.items.some((item) => item.value === "別の端末で更新した好み"));
    const count = fixture.memories.controls.calls.length;
    await memory.save();
    assert.equal(fixture.memories.controls.calls.length, count);
  }));
test("response loss retains exact body/key and blocks edit or alternate save", () =>
  using(async ({ memory, fixture }) => {
    draft(memory);
    fixture.memories.controls.scenario = "lost_save";
    await memory.save();
    assert.equal(memory.snapshot().pending, true);
    assert.ok(memory.snapshot().editor);
    const pending = structuredClone(memory.snapshot().editor.draft);
    memory.draft({ ...pending, value: "変更不可" });
    memory.create();
    await memory.forget("delete_source");
    assert.deepEqual(memory.snapshot().editor.draft, pending);
    await memory.retry();
    assert.equal(memory.snapshot().pending, false);
    const writes = fixture.memories.controls.calls.filter(
      (call) => call.method === "POST" && call.path.endsWith("memories"),
    );
    assert.equal(writes.length, 2);
    assert.equal(writes[0].key, writes[1].key);
    assert.deepEqual(writes[0].body, writes[1].body);
    assert.equal(memory.snapshot().items.filter((item) => item.value === "新しい好み").length, 1);
  }));
test("replayed create receipt already superseded never becomes current", () =>
  using(async ({ memory, fixture }) => {
    draft(memory);
    fixture.memories.controls.scenario = "lost_save";
    await memory.save();
    const rows = fixture.memories.records(tenantA, "synthetic@example.test"),
      created = [...rows.values()].find((row) => row.value === "新しい好み");
    rows.set(created.id, { ...created, status: "superseded", version: 2 });
    await memory.retry();
    assert.equal(
      memory.snapshot().items.some((row) => row.id === created.id),
      false,
    );
    assert.match(memory.snapshot().notice, /既に訂正/);
  }));
for (const policy of ["suppress_reuse", "delete_source"])
  test(`T-011 ${policy}: 202 removes current item; failed purge never restores or claims success`, () =>
    using(async ({ memory, fixture }) => {
      await memory.select(sharedMemoryId, "forget");
      await memory.forget(policy);
      assert.equal(
        memory.snapshot().items.some((row) => row.id === sharedMemoryId),
        false,
      );
      const operation = memory.snapshot().operations[0].operation;
      assert.equal(operation.status, "pending");
      assert.equal(fixture.memories.controls.calls.at(-1).body.source_policy, policy);
      fixture.memories.controls.scenario = "purge_failed";
      await memory.refreshOperation(operation.operation_id);
      assert.equal(memory.snapshot().operations[0].operation.status, "failed");
      await memory.load();
      assert.equal(
        memory.snapshot().items.some((row) => row.id === sharedMemoryId),
        false,
      );
      fixture.memories.controls.scenario = "normal";
      await memory.refreshOperation(operation.operation_id);
      assert.equal(memory.snapshot().operations[0].operation.status, "succeeded");
    }));
test("forget acceptance failure leaves item visible; response loss retries same operation", () =>
  using(async ({ memory, fixture }) => {
    await memory.select(sharedMemoryId, "forget");
    fixture.memories.controls.scenario = "forget_failed";
    await memory.forget("suppress_reuse");
    assert.equal(memory.snapshot().operations.length, 0);
    assert.ok(memory.snapshot().items.some((row) => row.id === sharedMemoryId));
    fixture.memories.controls.scenario = "lost_forget";
    await memory.retry();
    assert.equal(memory.snapshot().operations.length, 0);
    await memory.retry();
    assert.equal(memory.snapshot().operations.length, 1);
    const writes = fixture.memories.controls.calls.filter((call) =>
      call.path.endsWith("operations"),
    );
    assert.equal(new Set(writes.map((call) => call.key)).size, 1);
    assert.equal(fixture.memories.operations.size, 1);
  }));
test("display filters private and disallowed policy; every mutation entry is blocked", () =>
  using(async ({ memory, fixture }) => {
    assert.deepEqual(
      memory.snapshot().items.map((row) => row.id),
      [sharedMemoryId],
    );
    const count = fixture.memories.controls.calls.length;
    memory.create();
    await memory.select(sharedMemoryId, "revise");
    await memory.select(sharedMemoryId, "forget");
    await memory.save();
    await memory.forget("delete_source");
    await memory.retry();
    assert.equal(fixture.memories.controls.calls.length, count);
    assert.equal(memory.snapshot().editor, undefined);
  }, "display"));
test("scope/version/provenance/interval and schema-invalid memory data fail closed", () =>
  using(async ({ memory, session }) => {
    const valid = memory.snapshot().items[0],
      context = session.snapshot().context;
    for (const change of [
      { tenant_id: tenantB },
      { version: 0 },
      { subject_entity_id: "bad" },
      { visibility: "private", owner_user_id: "other" },
      { visibility: "private", owner_user_id: context.uid, display_policy: "shared_display" },
      { status: "yes" },
      { origin: "inferred", status: "confirmed" },
      { deletion_epoch: 1 },
      { valid_to: "2020-01-01T00:00:00Z" },
      { source_refs: [{ kind: "note", resource_id: "missing", version: 1 }] },
      { source_refs: [], created_by: undefined },
      { value: {}, object_entity_id: undefined },
    ])
      assert.throws(() => validateMemory({ ...valid, ...change }, context));
  }));
test("401 retries once with identical idempotency request and refreshed auth", () => {
  let fail = true;
  return using(
    async ({ memory, fixture }) => {
      draft(memory);
      await memory.save();
      assert.equal(fixture.controls.refreshes, 1);
      assert.match(memory.snapshot().notice, /保存しました/);
    },
    "mobile",
    (transport) => async (input, init) => {
      const request = new Request(input, init);
      if (fail && request.method === "POST" && new URL(request.url).pathname.endsWith("memories")) {
        fail = false;
        return Response.json({}, { status: 401 });
      }
      return transport(request);
    },
  );
});
test("current authorization refusal clears private memory state synchronously", () => {
  let denied = false;
  return using(
    async ({ memory, session }) => {
      assert.ok(memory.snapshot().items.some((row) => row.id === privateMemoryId));
      denied = true;
      await memory.load();
      assert.equal(session.snapshot().context, undefined);
      assert.equal(memory.snapshot().items.length, 0);
      assert.equal(memory.snapshot().closed, true);
    },
    "mobile",
    (transport) => async (input, init) =>
      denied ? Response.json({}, { status: 403 }) : transport(input, init),
  );
});
test("tenant switch fences late responses and clears draft/receipts/operations", () =>
  using(async ({ memory, fixture, session }) => {
    draft(memory, { value: "PRIVATE-DRAFT" });
    fixture.memories.controls.delay = 60;
    const saving = memory.save();
    await tick();
    await session.selectTenant(tenantB);
    await saving;
    assert.equal(memory.snapshot().closed, true);
    assert.equal(memory.snapshot().editor, undefined);
    assert.deepEqual(memory.snapshot().items, []);
    assert.deepEqual(memory.snapshot().operations, []);
    assert.equal(memory.snapshot().pending, false);
  }));
test("late list response after dispose cannot repopulate private data", () =>
  using(async ({ memory, fixture }) => {
    fixture.memories.controls.delay = 60;
    const loading = memory.load();
    await tick();
    memory.dispose();
    await loading;
    assert.equal(memory.snapshot().items.length, 0);
    await tick();
    assert.equal(memory.snapshot().closed, true);
  }));
test("pagination dedup/repeated cursor is an error, not a partial successful list", () =>
  using(
    async ({ memory }) => {
      assert.deepEqual(memory.snapshot().items, []);
      assert.match(memory.snapshot().notice, /確認できません/);
    },
    "mobile",
    (transport) => async (input, init) => {
      const request = new Request(input, init);
      const result = await transport(request);
      if (request.method === "GET" && new URL(request.url).pathname.endsWith("memories")) {
        const page = await result.json();
        return Response.json({ ...page, next_cursor: "repeat" });
      }
      return result;
    },
  ));

test("disposed view aborts stale 403 before it can revoke a newer view in same context", () => {
  let release,
    armed = false;
  return using(
    async ({ memory, session }) => {
      const context = session.snapshot().context;
      armed = true;
      const pending = memory.load();
      await tick();
      memory.dispose();
      release();
      await pending;
      await tick();
      assert.equal(session.snapshot().context, context);
      assert.equal(context.signal.aborted, false);
      assert.equal(memory.snapshot().closed, true);
    },
    "mobile",
    (transport) => async (input, init) => {
      if (armed) {
        await new Promise((resolve) => {
          release = resolve;
        });
        return Response.json({}, { status: 403 });
      }
      return transport(input, init);
    },
  );
});
test("wrong operation ID response never marks requested purge completed", () => {
  let replace = false;
  return using(
    async ({ memory }) => {
      await memory.select(sharedMemoryId, "forget");
      await memory.forget("suppress_reuse");
      const id = memory.snapshot().operations[0].operation.operation_id;
      replace = true;
      await memory.refreshOperation(id);
      assert.equal(memory.snapshot().operations[0].operation.status, "pending");
      assert.match(memory.snapshot().notice, /確認できません/);
    },
    "mobile",
    (transport) => async (input, init) => {
      const response = await transport(input, init);
      if (replace)
        return Response.json({
          ...(await response.json()),
          operation_id: crypto.randomUUID(),
          status: "succeeded",
        });
      return response;
    },
  );
});
test("412 followed by failed canonical read retains input but does not invent fetched state", () => {
  let failed = false;
  return using(
    async ({ memory, fixture }) => {
      await memory.select(sharedMemoryId, "revise");
      memory.draft({ ...memory.snapshot().editor.draft, value: "残す入力" });
      fixture.memories.controls.scenario = "conflict";
      failed = true;
      await memory.save();
      assert.equal(memory.snapshot().editor.draft.value, "残す入力");
      assert.equal(memory.snapshot().conflict.current, undefined);
      assert.equal(
        memory.snapshot().items.some((item) => item.id === sharedMemoryId),
        false,
      );
      assert.equal(memory.snapshot().pending, false);
      const calls = fixture.memories.controls.calls.length;
      await memory.save();
      assert.equal(fixture.memories.controls.calls.length, calls);
    },
    "mobile",
    (transport) => async (input, init) => {
      const request = new Request(input, init);
      if (
        failed &&
        request.method === "GET" &&
        new URL(request.url).pathname.endsWith(sharedMemoryId)
      )
        return Response.json({}, { status: 503 });
      return transport(request);
    },
  );
});

test("empty tenant explicitly registers a distinct subject then saves memory; lost entity response reuses key", () => {
  let lose = true;
  return using(
    async ({ memory, fixture }) => {
      fixture.memories.controls.scenario = "empty";
      await memory.load();
      assert.equal(memory.snapshot().entities.length, 0);
      assert.equal(memory.snapshot().items.length, 0);
      memory.create();
      await memory.createSubject("架空の話題", "topic");
      assert.equal(memory.snapshot().pending, true);
      assert.equal(memory.snapshot().entities.length, 0);
      await memory.retry();
      const entity = memory.snapshot().entities[0];
      assert.equal(entity.label, "架空の話題");
      assert.equal(entity.visibility, "private");
      assert.equal(memory.snapshot().editor.draft.subject_entity_id, entity.id);
      assert.equal(memory.snapshot().items.length, 0);
      memory.draft({ ...memory.snapshot().editor.draft, value: "対象登録後の記憶" });
      await memory.save();
      assert.equal(memory.snapshot().items[0].subject_entity_id, entity.id);
      memory.create();
      await memory.createSubject("架空の話題", "topic");
      assert.equal(memory.snapshot().entities.length, 2);
      assert.notEqual(memory.snapshot().entities[0].id, memory.snapshot().entities[1].id);
      const calls = fixture.memories.controls.calls.filter(
        (call) => call.method === "POST" && call.path.endsWith("entities"),
      );
      assert.equal(calls[0].key, calls[1].key);
      assert.notEqual(calls[1].key, calls[2].key);
    },
    "mobile",
    (transport) => async (input, init) => {
      const request = new Request(input, init);
      const response = await transport(request);
      if (lose && request.method === "POST" && new URL(request.url).pathname.endsWith("entities")) {
        lose = false;
        throw Error("Synthetic entity response loss");
      }
      return response;
    },
  );
});
