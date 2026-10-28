import assert from "node:assert/strict";
import { getNote, updateNote } from "../../packages/api-client/src/index.ts";
import { NotesController } from "../../packages/ui/src/notes/controller.ts";
import { SessionContext } from "../../packages/ui/src/session.ts";

const baseUrl = process.env.UI_API_URL,
  tenant = process.env.UI_TENANT;
assert.ok(baseUrl && new URL(baseUrl).hostname === "127.0.0.1" && tenant);
let loseSave = true,
  loseDelete = true;
const writes = [];
const context = new SessionContext({
  uid: "demo-a-owner",
  tenantId: tenant,
  generation: 1,
  baseUrl,
  current: () => true,
  token: async () => "synthetic-note-reader",
  denied: () => {
    throw Error("Unexpected current authorization refusal");
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
    if (response.ok && request.method === "POST") {
      if (loseSave && new URL(request.url).pathname.endsWith("notes")) {
        loseSave = false;
        await response.body?.cancel();
        throw Error("Synthetic saved response loss");
      }
      if (loseDelete && new URL(request.url).pathname.endsWith("operations")) {
        loseDelete = false;
        await response.body?.cancel();
        throw Error("Synthetic accepted response loss");
      }
    }
    return response;
  },
});
const notes = new NotesController(context, "mobile");
try {
  await notes.load();
  assert.equal(notes.snapshot().notice, undefined);
  notes.create();
  notes.draft({
    title: "UI note",
    body: "UI saved source",
    visibility: "shared",
    display_policy: "shared_display",
  });
  await notes.save();
  assert.equal(notes.snapshot().pending, true);
  await notes.retry();
  assert.deepEqual(writes[0], writes[1]);
  const saved = notes.snapshot().items.find((note) => note.title === "UI note");
  assert.ok(saved);
  await notes.load("UI saved");
  assert.ok(notes.snapshot().items.some((note) => note.id === saved.id));
  await notes.select(saved.id, "edit");
  notes.draft({ ...notes.snapshot().editor.draft, body: "UI retained input" });
  await context.run(async (client, signal) => {
    const result = await updateNote({
      client,
      signal,
      path: { tenant_id: tenant, note_id: saved.id },
      body: { body: "UI competing edit" },
      headers: { "If-Match": '"1"', "Idempotency-Key": crypto.randomUUID() },
    });
    assert.equal(result.response.status, 200);
  });
  await notes.save();
  assert.equal(notes.snapshot().conflict.current.version, 2);
  assert.equal(notes.snapshot().editor.draft.body, "UI retained input");
  notes.continueEditing();
  await notes.save();
  assert.equal(notes.snapshot().items.find((note) => note.id === saved.id).version, 3);
  await notes.select(saved.id, "edit");
  notes.draft({ ...notes.snapshot().editor.draft, display_policy: "authenticated_session" });
  await notes.save();
  assert.equal(
    notes.snapshot().items.find((note) => note.id === saved.id).display_policy,
    "authenticated_session",
  );
  // Read the real canonical result via generated SDK before submitting the delete target/version.
  await context.run(async (client, signal) => {
    const result = await getNote({
      client,
      signal,
      path: { tenant_id: tenant, note_id: saved.id },
    });
    assert.equal(result.data.body, "UI retained input");
    assert.equal(result.response.headers.get("ETag"), '"4"');
  });
  for (const include of [true, false]) {
    let id = saved.id;
    if (!include) {
      notes.create();
      notes.draft({
        title: "UI retained-derived note",
        body: "UI source two",
        visibility: "private",
        display_policy: "never",
      });
      await notes.save();
      id = notes.snapshot().items.find((note) => note.title === "UI retained-derived note").id;
    }
    await notes.select(id, "delete");
    await notes.remove(include);
    if (notes.snapshot().pending) await notes.retry();
    assert.equal(notes.snapshot().pending, false);
    assert.equal(
      notes.snapshot().items.some((note) => note.id === id),
      false,
    );
    const operation = notes.snapshot().operations.at(-1).operation;
    assert.equal(operation.status, "pending");
    await notes.refreshOperation(operation.operation_id);
    assert.equal(notes.snapshot().operations.at(-1).operation.status, "pending");
    await notes.load();
    assert.equal(
      notes.snapshot().items.some((note) => note.id === id),
      false,
    );
  }
  const deletes = writes.filter((call) => call.path.endsWith("operations"));
  assert.deepEqual(deletes[0], deletes[1]);
  console.log(
    "UI notes HTTP/Firestore create/search/update policy, exact replay, 412 retained draft and delete both derived policies passed",
  );
} finally {
  notes.dispose();
  context.dispose();
}
