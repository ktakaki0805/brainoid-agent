import assert from "node:assert/strict";
import { test } from "node:test";
import { NotesController, validateNote } from "../packages/ui/src/notes/controller.ts";
import { privateNoteId, sharedNoteId } from "../packages/ui/src/notes/fixture.ts";
import { SessionController } from "../packages/ui/src/session.ts";
import { createSessionFixture, tenantA, tenantB } from "../packages/ui/src/session-fixture.ts";

const tick = () => new Promise((resolve) => setTimeout(resolve, 5));
const uid = "synthetic@example.test";
async function setup(surface = "mobile", alter = (fetch) => fetch) {
  const fixture = createSessionFixture();
  const session = new SessionController({
    auth: fixture.auth,
    transport: alter(fixture.transport),
    baseUrl: "https://session.invalid",
  });
  const stop = session.start();
  await tick();
  await session.signIn(uid, "fixture");
  await tick();
  await session.selectTenant(tenantA);
  const notes = new NotesController(session.snapshot().context, surface);
  await notes.load();
  return {
    fixture,
    session,
    notes,
    close() {
      notes.dispose();
      stop();
    },
  };
}
async function using(work, ...args) {
  const env = await setup(...args);
  try {
    await work(env);
  } finally {
    env.close();
  }
}
function draft(notes, changes = {}) {
  notes.create();
  notes.draft({
    ...notes.snapshot().editor.draft,
    title: "架空の新しいノート",
    body: "入力中の本文",
    ...changes,
  });
}
const calls = (fixture) => fixture.notes.controls.calls;

test("create, search, edit display policy and empty body use canonical notes API", () =>
  using(async ({ notes, fixture }) => {
    draft(notes);
    await notes.save();
    const saved = notes.snapshot().items.find((note) => note.title === "架空の新しいノート");
    assert.equal(saved.visibility, "private");
    assert.equal(saved.owner_user_id, uid);
    await notes.load("入力中");
    assert.deepEqual(
      notes.snapshot().items.map((note) => note.id),
      [saved.id],
    );
    assert.equal(new URLSearchParams(calls(fixture).at(-1).query).get("query"), "入力中");
    await notes.select(saved.id, "edit");
    notes.draft({
      ...notes.snapshot().editor.draft,
      body: "",
      display_policy: "authenticated_session",
    });
    await notes.save();
    const updated = notes.snapshot().items.find((note) => note.id === saved.id);
    assert.equal(updated.version, 2);
    assert.equal(updated.body, "");
    assert.equal(updated.display_policy, "authenticated_session");
    const patch = calls(fixture).find((call) => call.method === "PATCH");
    assert.deepEqual(Object.keys(patch.body).sort(), ["body", "display_policy", "title"]);
    assert.equal(patch.match, '"1"');
  }));
test("scope change clears old list synchronously, only current visibility returns", () =>
  using(async ({ notes, fixture }) => {
    fixture.notes.controls.delay = 20;
    const changing = notes.scope("private");
    assert.deepEqual(notes.snapshot().items, []);
    await changing;
    assert.deepEqual(
      notes.snapshot().items.map((note) => note.id),
      [privateNoteId],
    );
    await notes.scope("shared");
    assert.deepEqual(
      notes.snapshot().items.map((note) => note.id),
      [sharedNoteId],
    );
  }));
test("invalid private display policy, size and immutable visibility never send writes", () =>
  using(async ({ notes, fixture }) => {
    draft(notes, { display_policy: "shared_display" });
    const count = calls(fixture).length;
    await notes.save();
    assert.equal(calls(fixture).length, count);
    assert.match(notes.snapshot().notice, /共有画面/);
    notes.draft({
      ...notes.snapshot().editor.draft,
      display_policy: "never",
      body: "a".repeat(8001),
    });
    await notes.save();
    assert.equal(calls(fixture).length, count);
    notes.closeEditor();
    await notes.select(privateNoteId, "edit");
    notes.draft({ ...notes.snapshot().editor.draft, visibility: "shared" });
    const before = calls(fixture).length;
    await notes.save();
    assert.equal(calls(fixture).length, before);
    assert.match(notes.snapshot().notice, /公開範囲/);
  }));
test("422 retains editable draft and reports refusal without success", () =>
  using(async ({ notes, fixture }) => {
    draft(notes);
    fixture.notes.controls.scenario = "reject";
    await notes.save();
    assert.equal(notes.snapshot().pending, false);
    assert.equal(notes.snapshot().editor.draft.body, "入力中の本文");
    assert.match(notes.snapshot().notice, /サーバーで拒否/);
    fixture.notes.controls.scenario = "normal";
    notes.draft({ ...notes.snapshot().editor.draft, body: "修正" });
    await notes.save();
    assert.equal(notes.snapshot().editor, undefined);
  }));
test("412 preserves editable draft, requires explicit latest-version reconciliation and fresh key", () =>
  using(async ({ notes, fixture }) => {
    await notes.select(sharedNoteId, "edit");
    notes.draft({ ...notes.snapshot().editor.draft, body: "失いたくない入力" });
    fixture.notes.controls.scenario = "conflict";
    await notes.save();
    assert.equal(notes.snapshot().conflict.current.version, 2);
    assert.equal(notes.snapshot().editor.draft.body, "失いたくない入力");
    assert.equal(notes.snapshot().pending, false);
    const count = calls(fixture).length;
    await notes.save();
    assert.equal(calls(fixture).length, count);
    notes.draft({ ...notes.snapshot().editor.draft, body: "最新版を反映した入力" });
    notes.continueEditing();
    await notes.save();
    const edits = calls(fixture).filter((call) => call.method === "PATCH");
    assert.equal(edits[1].match, '"2"');
    assert.notEqual(edits[0].key, edits[1].key);
    assert.equal(
      notes.snapshot().items.find((note) => note.id === sharedNoteId).body,
      "最新版を反映した入力",
    );
  }));
test("search retains draft and selection/scope/create cannot silently discard it", () =>
  using(async ({ notes }) => {
    draft(notes);
    await notes.load("買い物");
    await notes.select(sharedNoteId, "delete");
    await notes.scope("shared");
    notes.create();
    assert.equal(notes.snapshot().editor.draft.body, "入力中の本文");
    assert.equal(notes.snapshot().target, undefined);
    assert.equal(notes.snapshot().scope, "all");
    notes.closeEditor();
    await notes.select(sharedNoteId, "delete");
    assert.equal(notes.snapshot().target.id, sharedNoteId);
  }));
for (const kind of ["create", "update"])
  test(`${kind} response loss replays exact request and shows later canonical edit`, () =>
    using(async ({ notes, fixture }) => {
      if (kind === "create") draft(notes);
      else {
        await notes.select(sharedNoteId, "edit");
        notes.draft({ ...notes.snapshot().editor.draft, body: "My update" });
      }
      fixture.notes.controls.scenario = "lost_save";
      await notes.save();
      assert.equal(notes.snapshot().pending, true);
      const rows = fixture.notes.records(tenantA, uid),
        saved = [...rows.values()].find((note) =>
          kind === "create" ? note.title === "架空の新しいノート" : note.id === sharedNoteId,
        );
      rows.set(saved.id, {
        ...saved,
        body: "Later canonical edit",
        version: saved.version + 1,
        display_policy: "authenticated_session",
      });
      const originalDraft = structuredClone(notes.snapshot().editor.draft);
      notes.draft({ ...originalDraft, body: "forbidden" });
      notes.closeEditor();
      await notes.load("other");
      assert.deepEqual(notes.snapshot().editor.draft, originalDraft);
      await notes.retry();
      const writes = calls(fixture).filter(
        (call) => call.method === (kind === "create" ? "POST" : "PATCH"),
      );
      assert.equal(writes.length, 2);
      assert.deepEqual(writes[0], writes[1]);
      assert.equal(
        notes.snapshot().items.find((note) => note.id === saved.id).body,
        "Later canonical edit",
      );
      assert.equal(notes.snapshot().editor, undefined);
    }));
for (const include of [true, false])
  test(`deletion acceptance/response loss exact replay, suppression persists after failure (${include})`, () =>
    using(async ({ notes, fixture }) => {
      await notes.select(sharedNoteId, "delete");
      fixture.notes.controls.scenario = "delete_failed";
      await notes.remove(include);
      assert.equal(notes.snapshot().pending, true);
      assert.equal(notes.snapshot().operations.length, 0);
      fixture.notes.controls.scenario = "lost_delete";
      await notes.retry();
      assert.equal(notes.snapshot().pending, true);
      await notes.retry();
      const operations = calls(fixture).filter((call) => call.path.endsWith("operations"));
      assert.equal(operations.length, 3);
      assert.deepEqual(operations[0], operations[1]);
      assert.deepEqual(operations[1], operations[2]);
      assert.equal(operations[0].body.include_derived_memories, include);
      assert.equal(operations[0].body.target_type, "note");
      assert.equal(notes.snapshot().target, undefined);
      assert.equal(
        notes.snapshot().items.some((note) => note.id === sharedNoteId),
        false,
      );
      const op = notes.snapshot().operations[0].operation;
      assert.equal(op.status, "pending");
      fixture.notes.controls.scenario = "purge_failed";
      await notes.refreshOperation(op.operation_id);
      assert.equal(notes.snapshot().operations[0].operation.status, "failed");
      fixture.notes.controls.scenario = "purge_unknown";
      await notes.refreshOperation(op.operation_id);
      assert.equal(notes.snapshot().operations[0].operation.status, "outcome_unknown");
      fixture.notes
        .records(tenantA, uid)
        .set(sharedNoteId, { ...notes.snapshot().items[0], id: sharedNoteId });
      await notes.load();
      assert.equal(
        notes.snapshot().items.some((note) => note.id === sharedNoteId),
        false,
      );
    }));
test("delete 412 removes stale confirmation and requires selecting current target", () =>
  using(async ({ notes, fixture }) => {
    await notes.select(sharedNoteId, "delete");
    const rows = fixture.notes.records(tenantA, uid);
    rows.set(sharedNoteId, { ...rows.get(sharedNoteId), version: 2 });
    await notes.remove(true);
    assert.equal(notes.snapshot().target, undefined);
    assert.equal(notes.snapshot().operations.length, 0);
    assert.equal(notes.snapshot().conflict.current.version, 2);
    await notes.select(sharedNoteId, "delete");
    assert.equal(notes.snapshot().target.version, 2);
  }));
for (const patch of [
  { kind: "export" },
  { status: "complete" },
  { operation_id: "bad" },
  { created_at: "bad" },
])
  test(`invalid operation acceptance retains exact retry: ${JSON.stringify(patch)}`, () =>
    using(
      async ({ notes }) => {
        await notes.select(sharedNoteId, "delete");
        await notes.remove(true);
        assert.equal(notes.snapshot().pending, true);
        assert.equal(notes.snapshot().operations.length, 0);
        assert.ok(notes.snapshot().target);
      },
      "mobile",
      (fetch) => async (input, init) => {
        const request = new Request(input, init),
          result = await fetch(request);
        if (request.method === "POST" && request.url.endsWith("operations") && result.ok)
          return Response.json({ ...(await result.json()), ...patch }, { status: 202 });
        return result;
      },
    ));
test("operation GET mismatched ID/kind cannot turn pending into success", () =>
  using(
    async ({ notes }) => {
      await notes.select(sharedNoteId, "delete");
      await notes.remove(true);
      const id = notes.snapshot().operations[0].operation.operation_id;
      await notes.refreshOperation(id);
      assert.equal(notes.snapshot().operations[0].operation.status, "pending");
      assert.match(notes.snapshot().notice, /応答/);
    },
    "mobile",
    (fetch) => async (input, init) => {
      const request = new Request(input, init),
        result = await fetch(request);
      if (request.method === "GET" && request.url.includes("operations/"))
        return Response.json({
          ...(await result.json()),
          operation_id: crypto.randomUUID(),
          status: "succeeded",
        });
      return result;
    },
  ));
test("display filters private and non-display shared rows, cannot mutate", () =>
  using(async ({ notes, fixture }) => {
    assert.deepEqual(
      notes.snapshot().items.map((note) => note.id),
      [sharedNoteId],
    );
    const count = calls(fixture).length;
    notes.create();
    await notes.select(sharedNoteId, "edit");
    await notes.save();
    await notes.remove(true);
    await notes.scope("private");
    assert.equal(calls(fixture).length, count);
    assert.equal(notes.snapshot().editor, undefined);
    const rows = fixture.notes.records(tenantA, uid);
    rows.set(sharedNoteId, { ...rows.get(sharedNoteId), display_policy: "authenticated_session" });
    await notes.load();
    assert.deepEqual(notes.snapshot().items, []);
  }, "display"));
for (const event of ["tenant", "signout", "identity", "pagehide"])
  test(`${event} synchronously clears note bodies, drafts, operations and retry`, () =>
    using(async ({ notes, session, fixture }) => {
      draft(notes);
      fixture.notes.controls.scenario = "lost_save";
      await notes.save();
      let change;
      if (event === "tenant") change = session.selectTenant(tenantB);
      else if (event === "signout") change = session.signOut();
      else if (event === "identity") fixture.emit({ uid: "another@example.test" });
      else session.suspend();
      assert.equal(notes.snapshot().closed, true);
      assert.deepEqual(notes.snapshot().items, []);
      assert.equal(notes.snapshot().editor, undefined);
      assert.equal(notes.snapshot().pending, false);
      await change;
      const count = calls(fixture).length;
      await notes.retry();
      assert.equal(calls(fixture).length, count);
    }));
test("permission refusal disposes current note data and shows session guidance", () =>
  using(async ({ notes, session, fixture }) => {
    draft(notes);
    fixture.notes.controls.scenario = "denied";
    await notes.save();
    assert.equal(notes.snapshot().closed, true);
    assert.equal(session.snapshot().context, undefined);
    assert.match(session.snapshot().message, /許可されていません/);
  }));
test("401 refresh preserves exact body/key/version once", () => {
  let first = true;
  const writes = [];
  return using(
    async ({ notes, fixture }) => {
      await notes.select(sharedNoteId, "edit");
      await notes.save();
      assert.equal(fixture.controls.refreshes, 1);
      assert.deepEqual(writes[0], writes[1]);
    },
    "mobile",
    (fetch) => async (input, init) => {
      const request = new Request(input, init);
      if (request.method === "PATCH") {
        writes.push({
          body: await request.clone().text(),
          key: request.headers.get("Idempotency-Key"),
          match: request.headers.get("If-Match"),
        });
        if (first) {
          first = false;
          return Response.json({}, { status: 401 });
        }
      }
      return fetch(request);
    },
  );
});
test("late denied response after view disposal cannot deny live session", () => {
  let release, started;
  const ready = new Promise((resolve) => {
    started = resolve;
  });
  let delaying = false;
  return using(
    async ({ notes, session }) => {
      delaying = true;
      const loading = notes.load();
      await ready;
      const context = session.snapshot().context;
      notes.dispose();
      release();
      await loading;
      await tick();
      assert.equal(session.snapshot().context, context);
      assert.equal(context.signal.aborted, false);
    },
    "mobile",
    (fetch) => async (input, init) => {
      if (delaying && new Request(input, init).url.includes("/notes")) {
        started();
        await new Promise((resolve) => {
          release = resolve;
        });
        return Response.json({}, { status: 403 });
      }
      return fetch(input, init);
    },
  );
});
test("delayed JSON body after context switch cannot restore note state", () => {
  let release, started;
  const ready = new Promise((resolve) => {
    started = resolve;
  });
  let delaying = false;
  return using(
    async ({ notes, session }) => {
      delaying = true;
      const loading = notes.load();
      await ready;
      await tick();
      await session.selectTenant(tenantB);
      assert.equal(notes.snapshot().closed, true);
      release();
      await loading;
      assert.deepEqual(notes.snapshot().items, []);
    },
    "mobile",
    (fetch) => async (input, init) => {
      const result = await fetch(input, init);
      if (delaying && new Request(input, init).url.includes("/notes")) {
        const body = await result.text();
        return new Response(
          new ReadableStream({
            start(controller) {
              release = () => {
                controller.enqueue(new TextEncoder().encode(body));
                controller.close();
              };
              started();
            },
          }),
          { headers: { "Content-Type": "application/json" } },
        );
      }
      return result;
    },
  );
});
test("single in-flight read prevents writes and cannot restore a deleted note", () =>
  using(async ({ notes, fixture }) => {
    fixture.notes.controls.delay = 25;
    const loading = notes.load();
    notes.create();
    assert.equal(notes.snapshot().editor, undefined);
    await loading;
    await notes.select(sharedNoteId, "delete");
    const deleting = notes.remove(true);
    await notes.load();
    await deleting;
    assert.equal(
      notes.snapshot().items.some((note) => note.id === sharedNoteId),
      false,
    );
  }));
for (const change of [
  { tenant_id: tenantB },
  { owner_user_id: "another" },
  { version: 0 },
  { body: {} },
  { display_policy: "shared_display" },
])
  test(`invalid private canonical row fails closed: ${JSON.stringify(change)}`, () =>
    using(async ({ notes, session }) => {
      const note = notes.snapshot().items.find((row) => row.id === privateNoteId);
      assert.throws(() => validateNote({ ...note, ...change }, session.snapshot().context));
    }));
test("pagination propagates fixed query/limit, rejects repeated cursor and oversize query", () => {
  let mode = 0,
    calls = [];
  return using(
    async ({ notes }) => {
      mode = 1;
      await notes.load("x");
      assert.equal(calls.length, 2);
      assert.equal(calls[1].get("cursor"), "next");
      assert.ok(calls.every((query) => query.get("query") === "x" && query.get("limit") === "100"));
      assert.match(notes.snapshot().notice, /応答/);
      const count = calls.length;
      await notes.load("a".repeat(201));
      assert.equal(calls.length, count);
    },
    "mobile",
    (fetch) => async (input, init) => {
      const request = new Request(input, init),
        url = new URL(request.url);
      if (mode && url.pathname.endsWith("/notes")) {
        calls.push(url.searchParams);
        return Response.json({ items: [], next_cursor: "next" });
      }
      return fetch(request);
    },
  );
});

test("finite deadline clears busy even if transport never resolves", () => {
  let hanging = false;
  return using(
    async ({ notes }) => {
      const timeout = AbortSignal.timeout,
        seen = [];
      const keepAlive = setTimeout(() => {}, 100);
      AbortSignal.timeout = (ms) => {
        seen.push(ms);
        return timeout(10);
      };
      try {
        hanging = true;
        await notes.load();
        assert.deepEqual(seen, [15000]);
        assert.equal(notes.snapshot().busy, false);
        assert.match(notes.snapshot().notice, /通信/);
      } finally {
        AbortSignal.timeout = timeout;
        clearTimeout(keepAlive);
      }
    },
    "mobile",
    (fetch) => async (input, init) => (hanging ? new Promise(() => {}) : fetch(input, init)),
  );
});
test("failed conflict read retains draft and blocks save until latest is confirmed", () => {
  let rejectRead = false;
  return using(
    async ({ notes, fixture }) => {
      await notes.select(sharedNoteId, "edit");
      notes.draft({ ...notes.snapshot().editor.draft, body: "保留する入力" });
      fixture.notes.controls.scenario = "conflict";
      rejectRead = true;
      await notes.save();
      assert.equal(notes.snapshot().conflict.current, undefined);
      assert.equal(notes.snapshot().editor.draft.body, "保留する入力");
      notes.continueEditing();
      assert.ok(notes.snapshot().conflict);
      rejectRead = false;
      await notes.refreshConflict();
      notes.continueEditing();
      await notes.save();
      assert.equal(
        notes.snapshot().items.find((note) => note.id === sharedNoteId).body,
        "保留する入力",
      );
    },
    "mobile",
    (fetch) => async (input, init) => {
      const request = new Request(input, init);
      if (rejectRead && request.method === "GET" && request.url.includes(`/notes/${sharedNoteId}`))
        return Response.json({}, { status: 503 });
      return fetch(request);
    },
  );
});
for (const change of ["id", "tenant", "etag", "version"])
  test(`unbound save response remains pending (${change})`, () =>
    using(
      async ({ notes }) => {
        await notes.select(sharedNoteId, "edit");
        await notes.save();
        assert.equal(notes.snapshot().pending, true);
        assert.ok(notes.snapshot().editor);
        assert.match(notes.snapshot().notice, /受付結果/);
      },
      "mobile",
      (fetch) => async (input, init) => {
        const request = new Request(input, init),
          response = await fetch(request);
        if (request.method !== "PATCH") return response;
        const body = await response.json();
        if (change === "id") body.id = crypto.randomUUID();
        if (change === "tenant") body.tenant_id = tenantB;
        if (change === "version") body.version = 1;
        return Response.json(body, {
          headers: { ETag: change === "etag" ? '"900"' : `"${body.version}"` },
        });
      },
    ));
test("successful save with list refresh failure keeps canonical result and reports partial list", () => {
  let rejectList = false;
  return using(
    async ({ notes }) => {
      draft(notes);
      rejectList = true;
      await notes.save();
      assert.equal(notes.snapshot().pending, false);
      assert.equal(notes.snapshot().editor, undefined);
      assert.equal(notes.snapshot().items.length, 1);
      assert.match(notes.snapshot().notice, /一覧全体は取得できていません/);
    },
    "mobile",
    (fetch) => async (input, init) => {
      const request = new Request(input, init);
      if (
        rejectList &&
        request.method === "GET" &&
        new URL(request.url).pathname.endsWith("/notes")
      )
        return Response.json({}, { status: 503 });
      return fetch(request);
    },
  );
});
test("unexpected 200 operation response is not deletion acceptance", () =>
  using(
    async ({ notes }) => {
      await notes.select(sharedNoteId, "delete");
      await notes.remove(true);
      assert.equal(notes.snapshot().pending, true);
      assert.equal(notes.snapshot().operations.length, 0);
      assert.ok(notes.snapshot().target);
    },
    "mobile",
    (fetch) => async (input, init) => {
      const request = new Request(input, init),
        result = await fetch(request);
      if (request.method === "POST" && request.url.endsWith("operations"))
        return Response.json(await result.json(), { status: 200 });
      return result;
    },
  ));
