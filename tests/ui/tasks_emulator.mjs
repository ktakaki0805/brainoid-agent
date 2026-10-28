import assert from "node:assert/strict";
import { getTask, listTasks, updateTask } from "../../packages/api-client/src/index.ts";
import { SessionContext } from "../../packages/ui/src/session.ts";
import { TasksController } from "../../packages/ui/src/tasks/controller.ts";

const baseUrl = process.env.UI_API_URL,
  tenant = process.env.UI_TENANT;
assert.ok(baseUrl && new URL(baseUrl).hostname === "127.0.0.1" && tenant);
let lose = true;
const writes = [];
const context = new SessionContext({
  uid: "demo-a-owner",
  tenantId: tenant,
  generation: 1,
  baseUrl,
  current: () => true,
  token: async () => "synthetic-task-reader",
  denied: () => {
    throw Error("Unexpected permission refusal");
  },
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
    if (lose && response.ok && request.method === "POST") {
      lose = false;
      await response.body?.cancel();
      throw Error("Synthetic response loss after real commit");
    }
    return response;
  },
});
const tasks = new TasksController(context, "mobile"),
  selection = new TasksController(context, "mobile"),
  display = new TasksController(context, "display");
async function external(id, version, body) {
  return context.run(async (client, signal) => {
    const result = await updateTask({
      client,
      signal,
      path: { tenant_id: tenant, task_id: id },
      body,
      headers: { "If-Match": `"${version}"`, "Idempotency-Key": crypto.randomUUID() },
    });
    assert.equal(result.response.status, 200);
    return result.data;
  });
}
try {
  await tasks.load();
  assert.deepEqual(tasks.snapshot().items, []);
  tasks.create();
  tasks.draft({
    ...tasks.snapshot().editor.draft,
    title: "UI task",
    due_at: "2026-11-01T01:30:00-04:00",
    timezone: "America/New_York",
    assignee_user_id: "demo-a-owner",
    visibility: "shared",
    display_policy: "shared_display",
  });
  await tasks.save();
  assert.equal(tasks.snapshot().pending, true);
  assert.deepEqual(tasks.snapshot().items, []);
  const created = await context.run(
    async (client, signal) =>
      (await listTasks({ client, signal, path: { tenant_id: tenant } })).data.items[0],
  );
  assert.equal(created.status, "open");
  assert.equal(created.due_at, "2026-11-01T05:30:00Z");
  await selection.filter("open");
  assert.equal(selection.snapshot().items[0].status, "open");
  await external(created.id, 1, { status: "cancelled", title: "UI later canonical" });
  await selection.select(created.id);
  assert.equal(selection.snapshot().editor.original.status, "cancelled");
  assert.equal(selection.snapshot().editor.original.version, 2);
  assert.equal(selection.snapshot().editor.draft.title, "UI later canonical");
  assert.deepEqual(selection.snapshot().items, []);
  selection.closeEditor();
  assert.deepEqual(selection.snapshot().items, []);
  await tasks.retry();
  assert.deepEqual(writes[0], writes[2]);
  assert.equal(tasks.snapshot().items[0].status, "cancelled");
  assert.equal(tasks.snapshot().items[0].title, "UI later canonical");
  await tasks.select(created.id);
  tasks.draft({ ...tasks.snapshot().editor.draft, status: "done", title: "UI retained input" });
  await external(created.id, 2, { status: "open", title: "UI concurrent edit" });
  await tasks.save();
  assert.equal(tasks.snapshot().conflict.current.status, "open");
  assert.equal(tasks.snapshot().editor.draft.status, "done");
  assert.equal(tasks.snapshot().items[0].status, "open");
  tasks.continueEditing();
  await tasks.save();
  assert.equal(tasks.snapshot().items[0].status, "done");
  await tasks.select(created.id);
  tasks.draft({ ...tasks.snapshot().editor.draft, status: "cancelled", due_at: "" });
  await tasks.save();
  assert.equal(tasks.snapshot().items[0].status, "cancelled");
  assert.equal(tasks.snapshot().items[0].due_at, undefined);
  await tasks.filter("done");
  assert.deepEqual(tasks.snapshot().items, []);
  await tasks.filter("cancelled");
  assert.equal(tasks.snapshot().items.length, 1);
  await context.run(async (client, signal) => {
    const result = await getTask({
      client,
      signal,
      path: { tenant_id: tenant, task_id: created.id },
    });
    assert.equal(result.data.status, "cancelled");
    assert.equal(result.data.version, 5);
    assert.equal(result.data.timezone, "America/New_York");
    assert.equal(result.response.headers.get("ETag"), '"5"');
  });
  await display.load();
  assert.equal(display.snapshot().items[0].status, "cancelled");
  const count = writes.length;
  display.create();
  await display.select(created.id);
  await display.save();
  assert.equal(writes.length, count);
  console.log(
    "UI tasks HTTP/Firestore: canonical replay after later edit, explicit offset, assigned UID, retained 412 draft, done/cancelled/due clear, status filters and readonly display passed",
  );
} finally {
  tasks.dispose();
  selection.dispose();
  display.dispose();
  context.dispose();
}
