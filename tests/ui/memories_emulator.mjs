import assert from "node:assert/strict";
import { createMemory, getMemory, reviseMemory } from "../../packages/api-client/src/index.ts";
import { SessionContext } from "../../packages/ui/src/session.ts";
import { MemoryController } from "../../packages/ui/src/memories/controller.ts";
const baseUrl = process.env.UI_API_URL,
  tenant = process.env.UI_TENANT;
assert.ok(baseUrl && new URL(baseUrl).hostname === "127.0.0.1" && tenant);
let loseSave = true,
  loseForget = true;
const context = new SessionContext({
  uid: "demo-a-owner",
  tenantId: tenant,
  generation: 1,
  baseUrl,
  current: () => true,
  token: async () => "synthetic-memory-reader",
  denied: () => {
    throw Error("Unexpected current authorization refusal");
  },
  transport: async (input, init) => {
    const request = new Request(input, init),
      response = await fetch(request);
    if (response.ok && request.method === "POST") {
      if (loseSave && new URL(request.url).pathname.endsWith("memories")) {
        loseSave = false;
        await response.body?.cancel();
        throw Error("Synthetic saved response loss");
      }
      if (loseForget && new URL(request.url).pathname.endsWith("operations")) {
        loseForget = false;
        await response.body?.cancel();
        throw Error("Synthetic accepted response loss");
      }
    }
    return response;
  },
});
const controller = new MemoryController(context, "mobile");
try {
  await controller.load();
  assert.equal(controller.snapshot().notice, undefined);
  controller.create();
  controller.draft({
    ...controller.snapshot().editor.draft,
    visibility: "shared",
    display_policy: "shared_display",
  });
  await controller.createSubject("UI synthetic topic", "topic");
  assert.equal(controller.snapshot().pending, false);
  const body = { ...controller.snapshot().editor.draft, value: "UI synthetic saved preference" };
  controller.draft(body);
  await controller.save();
  assert.equal(controller.snapshot().pending, true);
  await controller.retry();
  assert.equal(controller.snapshot().pending, false);
  const saved = controller.snapshot().items.find((item) => item.value === body.value);
  assert.ok(saved);
  assert.equal(saved.created_by, context.uid);
  await controller.load("UI synthetic saved preference");
  assert.ok(controller.snapshot().items.some((item) => item.id === saved.id));
  assert.equal(controller.snapshot().notice, undefined);
  await context.run(async (client, signal) => {
    const invalid = await createMemory({
      client,
      signal,
      path: { tenant_id: tenant },
      body: { ...body, visibility: "private", display_policy: "shared_display" },
      headers: { "Idempotency-Key": crypto.randomUUID() },
    });
    assert.equal(invalid.response.status, 422);
  });
  await controller.select(saved.id, "revise");
  controller.draft({
    ...controller.snapshot().editor.draft,
    value: "UI retained conflicting input",
  });
  await context.run(async (client, signal) => {
    const result = await reviseMemory({
      client,
      signal,
      path: { tenant_id: tenant, memory_id: saved.id },
      body: { replacement: { ...body, value: "UI concurrent revision" } },
      headers: { "If-Match": '"1"', "Idempotency-Key": crypto.randomUUID() },
    });
    assert.equal(result.response.status, 200);
  });
  await controller.save();
  assert.equal(controller.snapshot().conflict.current.status, "superseded");
  assert.equal(controller.snapshot().editor.draft.value, "UI retained conflicting input");
  await controller.load("");
  const current = controller.snapshot().items.find((item) => item.supersedes_fact_id === saved.id);
  assert.ok(current);
  assert.equal(
    controller.snapshot().items.some((item) => item.id === saved.id),
    false,
  );
  await controller.select(current.id, "revise");
  controller.draft({
    ...controller.snapshot().editor.draft,
    value: "UI corrected current preference",
  });
  await controller.save();
  assert.equal(
    controller.snapshot().items.some((item) => item.id === current.id),
    false,
  );
  for (const [id, policy] of [
    [process.env.UI_SUPPRESS, "suppress_reuse"],
    [process.env.UI_DELETE_SOURCE, "delete_source"],
  ]) {
    await controller.select(id, "forget");
    await controller.forget(policy);
    if (controller.snapshot().pending) await controller.retry();
    assert.equal(controller.snapshot().pending, false);
    assert.equal(
      controller.snapshot().items.some((item) => item.id === id),
      false,
    );
    const operation = controller.snapshot().operations.at(-1).operation;
    assert.equal(operation.status, "pending");
    await controller.refreshOperation(operation.operation_id);
    assert.equal(controller.snapshot().operations.at(-1).operation.status, "pending");
  }
  await context.run(async (client, signal) => {
    const old = await getMemory({
      client,
      signal,
      path: { tenant_id: tenant, memory_id: saved.id },
    });
    assert.equal(old.data.status, "superseded");
  });
  console.log(
    "UI memory controller + HTTP + official Firestore: entity/create/search/422, response loss replay, 412/draft/current revision, 202/status and both source policies passed",
  );
} finally {
  controller.dispose();
  context.dispose();
}
