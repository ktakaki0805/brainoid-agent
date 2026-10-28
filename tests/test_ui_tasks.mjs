import assert from "node:assert/strict";
import { test } from "node:test";
import { ChatController } from "../packages/ui/src/chat/controller.ts";
import { fixtureShared } from "../packages/ui/src/chat/fixture.ts";
import { SessionController } from "../packages/ui/src/session.ts";
import { createSessionFixture, tenantA, tenantB } from "../packages/ui/src/session-fixture.ts";
import {
  TasksController,
  validateTask,
  validTimestamp,
  validTimezone,
} from "../packages/ui/src/tasks/controller.ts";
import { privateTaskId, sharedTaskId } from "../packages/ui/src/tasks/fixture.ts";

const tick = () => new Promise((resolve) => setTimeout(resolve, 5));
const uid = "synthetic@example.test";
async function using(work, surface = "mobile", alter = (fetch) => fetch) {
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
  const tasks = new TasksController(session.snapshot().context, surface);
  try {
    await tasks.load();
    await work({ tasks, fixture, session });
  } finally {
    tasks.dispose();
    stop();
  }
}
const calls = (fixture) => fixture.tasks.controls.calls;
const writes = (fixture) => calls(fixture).filter((call) => call.method !== "GET");
const row = (tasks) => tasks.snapshot().items.find((task) => task.id === sharedTaskId);
function create(tasks, changes = {}) {
  tasks.create();
  tasks.draft({ ...tasks.snapshot().editor.draft, title: "架空のタスク", ...changes });
}
async function edit(tasks, changes = {}) {
  await tasks.select(sharedTaskId);
  tasks.draft({ ...tasks.snapshot().editor.draft, ...changes });
}

test("create omits optional fields; update/complete/cancel/reopen and due clearing use canonical results", () =>
  using(async ({ tasks, fixture }) => {
    create(tasks);
    await tasks.save();
    assert.deepEqual(writes(fixture)[0].body, {
      title: "架空のタスク",
      visibility: "private",
      display_policy: "never",
    });
    assert.equal(
      tasks.snapshot().items.find((task) => task.title === "架空のタスク").status,
      "open",
    );
    for (const status of ["done", "cancelled", "open"]) {
      await edit(tasks, { status, due_at: "" });
      assert.notEqual(row(tasks).status, status);
      await tasks.save();
      assert.equal(row(tasks).status, status);
      assert.equal(row(tasks).due_at, undefined);
      assert.deepEqual(Object.keys(writes(fixture).at(-1).body).sort(), [
        "due_at",
        "status",
        "title",
      ]);
      assert.equal(writes(fixture).at(-1).body.due_at, null);
    }
  }));
test("explicit offset fixes instant independently of stored timezone; opaque assignee preserved", () =>
  using(async ({ tasks, fixture }) => {
    create(tasks, {
      due_at: "2026-11-01T01:30:00-04:00",
      timezone: "America/New_York",
      assignee_user_id: " opaque/user#1 ",
    });
    await tasks.save();
    const task = tasks.snapshot().items.find((task) => task.title === "架空のタスク");
    assert.equal(task.due_at, "2026-11-01T05:30:00.000Z");
    assert.equal(task.timezone, "America/New_York");
    assert.equal(task.assignee_user_id, " opaque/user#1 ");
    assert.equal(writes(fixture)[0].body.due_at, "2026-11-01T01:30:00-04:00");
  }));
for (const due of [
  "2026-11-01T01:30:00",
  "2026-02-30T09:00:00Z",
  "2025-02-29T09:00:00Z",
  "2026-13-01T09:00:00Z",
  "2026-01-01T24:00:00Z",
  "2026-01-01T09:60:00Z",
  "2026-01-01T09:00:00+24:00",
  "0000-01-01T00:00:00Z",
  "2026-01-01T00:00:00-00:00",
  "0001-01-01T00:00:00+09:00",
  "9999-12-31T23:59:59-09:00",
])
  test(`invalid/ambiguous due never submits: ${due}`, () =>
    using(async ({ tasks, fixture }) => {
      create(tasks, { due_at: due });
      await tasks.save();
      assert.equal(writes(fixture).length, 0);
      assert.equal(validTimestamp(due), false);
    }));
test("valid leap day and both DST fold offsets remain distinct; invalid IANA and scope block submission", () =>
  using(async ({ tasks, fixture }) => {
    assert.ok(validTimestamp("2024-02-29T23:59:59.123456Z"));
    for (const offset of ["-04:00", "-05:00"])
      assert.ok(validTimestamp(`2026-11-01T01:30:00${offset}`));
    assert.notEqual(
      Date.parse("2026-11-01T01:30:00-04:00"),
      Date.parse("2026-11-01T01:30:00-05:00"),
    );
    for (const zone of ["Bad/Zone", "+09:00", "localtime"])
      assert.equal(validTimezone(zone), false);
    create(tasks, { timezone: "Bad/Zone" });
    await tasks.save();
    assert.equal(writes(fixture).length, 0);
    tasks.draft({
      ...tasks.snapshot().editor.draft,
      timezone: "",
      display_policy: "shared_display",
    });
    await tasks.save();
    assert.equal(writes(fixture).length, 0);
  }));
for (const changes of [
  { timezone: "UTC" },
  { assignee_user_id: "someone" },
  { display_policy: "never" },
  { visibility: "private" },
])
  test(`immutable task fields cannot be patched: ${JSON.stringify(changes)}`, () =>
    using(async ({ tasks, fixture }) => {
      await edit(tasks, changes);
      await tasks.save();
      assert.equal(writes(fixture).length, 0);
      assert.match(tasks.snapshot().notice, /編集|個人/);
    }));
for (const scenario of ["reject", "failed", "conflict", "lost_save"])
  test(`unsaved done never enters canonical rows during ${scenario}`, () =>
    using(async ({ tasks, fixture }) => {
      await edit(tasks, { status: "done" });
      fixture.tasks.controls.scenario = scenario;
      fixture.tasks.controls.delay = 10;
      const saving = tasks.save();
      assert.equal(row(tasks).status, "open");
      await saving;
      assert.equal(row(tasks).status, "open");
      assert.equal(tasks.snapshot().editor.draft.status, "done");
      assert.equal(tasks.snapshot().pending, ["failed", "lost_save"].includes(scenario));
      if (scenario === "conflict") assert.equal(tasks.snapshot().conflict.current.status, "open");
    }));
test("412 retains draft and current changed status even outside active status filter; explicit reconciliation uses new version/key", () =>
  using(async ({ tasks, fixture }) => {
    await tasks.filter("open");
    await edit(tasks, { status: "done" });
    const records = fixture.tasks.records(tenantA, uid),
      old = records.get(sharedTaskId);
    records.set(sharedTaskId, { ...old, status: "cancelled", version: 2 });
    await tasks.save();
    assert.equal(tasks.snapshot().conflict.current.status, "cancelled");
    assert.equal(tasks.snapshot().editor.draft.status, "done");
    assert.equal(row(tasks), undefined);
    await tasks.save();
    assert.equal(writes(fixture).length, 1);
    tasks.continueEditing();
    await tasks.save();
    assert.equal(writes(fixture)[1].match, '"2"');
    assert.notEqual(writes(fixture)[0].key, writes(fixture)[1].key);
    assert.match(tasks.snapshot().notice, /done/);
    assert.equal(row(tasks), undefined);
    await tasks.filter("done");
    assert.equal(row(tasks).status, "done");
  }));
for (const kind of ["create", "update"])
  test(`${kind} response loss replays exact body/key/If-Match; later canonical status wins`, () =>
    using(async ({ tasks, fixture }) => {
      if (kind === "create") create(tasks);
      else await edit(tasks, { status: "done" });
      fixture.tasks.controls.scenario = "lost_save";
      await tasks.save();
      const rows = fixture.tasks.records(tenantA, uid),
        task = [...rows.values()].find((value) =>
          kind === "create" ? value.title === "架空のタスク" : value.id === sharedTaskId,
        );
      rows.set(task.id, { ...task, status: "cancelled", version: task.version + 1 });
      const draft = structuredClone(tasks.snapshot().editor.draft);
      tasks.draft({ ...draft, status: "open" });
      tasks.closeEditor();
      await tasks.load();
      await tasks.filter("done");
      assert.deepEqual(tasks.snapshot().editor.draft, draft);
      await tasks.retry();
      assert.deepEqual(writes(fixture)[0], writes(fixture)[1]);
      assert.equal(
        tasks.snapshot().items.find((value) => value.id === task.id).status,
        "cancelled",
      );
      assert.equal(tasks.snapshot().pending, false);
      assert.equal(tasks.snapshot().editor, undefined);
    }));
test("display public filter and controller readonly prohibit all writes", () =>
  using(async ({ tasks, fixture }) => {
    assert.deepEqual(
      tasks.snapshot().items.map((task) => task.id),
      [sharedTaskId],
    );
    tasks.create();
    await tasks.select(sharedTaskId);
    await tasks.save();
    await tasks.retry();
    await tasks.scope("private");
    assert.equal(tasks.snapshot().editor, undefined);
    assert.equal(writes(fixture).length, 0);
    const rows = fixture.tasks.records(tenantA, uid);
    rows.set(sharedTaskId, { ...rows.get(sharedTaskId), display_policy: "authenticated_session" });
    await tasks.load();
    assert.deepEqual(tasks.snapshot().items, []);
  }, "display"));
for (const event of ["tenant", "identity", "signout", "pagehide"])
  test(`${event} clears canonical data/draft/exact retry synchronously`, () =>
    using(async ({ tasks, session, fixture }) => {
      await edit(tasks, { status: "done" });
      fixture.tasks.controls.scenario = "lost_save";
      await tasks.save();
      let change;
      if (event === "tenant") change = session.selectTenant(tenantB);
      else if (event === "identity") fixture.emit({ uid: "other" });
      else if (event === "signout") change = session.signOut();
      else session.suspend();
      assert.equal(tasks.snapshot().closed, true);
      assert.deepEqual(tasks.snapshot().items, []);
      assert.equal(tasks.snapshot().editor, undefined);
      assert.equal(tasks.snapshot().pending, false);
      await change;
      const count = writes(fixture).length;
      await tasks.retry();
      assert.equal(writes(fixture).length, count);
    }));
for (const code of [403, 404, 401])
  test(`current ${code} closes view with bounded authentication refresh`, () => {
    let denied = false;
    return using(
      async ({ tasks, session, fixture }) => {
        create(tasks);
        denied = true;
        await tasks.save();
        assert.equal(tasks.snapshot().closed, true);
        assert.equal(session.snapshot().context, undefined);
        assert.equal(fixture.controls.refreshes, code === 401 ? 1 : 0);
      },
      "mobile",
      (fetch) => async (input, init) =>
        denied && new Request(input, init).url.includes("/tasks")
          ? Response.json({}, { status: code })
          : fetch(input, init),
    );
  });
for (const code of [401, 403, 404])
  test(`late ${code} after task view disposal cannot affect current session`, () => {
    let delay = false,
      release,
      ready;
    const started = new Promise((resolve) => {
      ready = resolve;
    });
    return using(
      async ({ tasks, session, fixture }) => {
        delay = true;
        const loading = tasks.load();
        await started;
        const context = session.snapshot().context;
        tasks.dispose();
        release();
        await loading;
        await tick();
        assert.equal(session.snapshot().context, context);
        assert.equal(fixture.controls.refreshes, 0);
      },
      "mobile",
      (fetch) => async (input, init) => {
        if (delay && new Request(input, init).url.includes("/tasks")) {
          ready();
          await new Promise((resolve) => {
            release = resolve;
          });
          return Response.json({}, { status: code });
        }
        return fetch(input, init);
      },
    );
  });
test("401 retry carries exact task payload/key/If-Match once", () => {
  const requests = [];
  let first = true;
  return using(
    async ({ tasks, fixture }) => {
      await edit(tasks, { status: "done" });
      await tasks.save();
      assert.equal(row(tasks).status, "done");
      assert.equal(fixture.controls.refreshes, 1);
      assert.deepEqual(requests[0], requests[1]);
    },
    "mobile",
    (fetch) => async (input, init) => {
      const r = new Request(input, init);
      if (r.method === "PATCH") {
        requests.push({
          body: await r.clone().text(),
          key: r.headers.get("Idempotency-Key"),
          match: r.headers.get("If-Match"),
        });
        if (first) {
          first = false;
          return Response.json({}, { status: 401 });
        }
      }
      return fetch(r);
    },
  );
});
for (const change of ["id", "tenant", "etag", "version", "status", "due"])
  test(`malformed/unbound canonical mutation response stays uncertain: ${change}`, () =>
    using(
      async ({ tasks }) => {
        await edit(tasks, { status: "done" });
        await tasks.save();
        assert.equal(tasks.snapshot().pending, true);
        assert.equal(row(tasks).status, "open");
      },
      "mobile",
      (fetch) => async (input, init) => {
        const r = new Request(input, init),
          response = await fetch(r);
        if (r.method !== "PATCH") return response;
        const body = await response.json();
        if (change === "id") body.id = crypto.randomUUID();
        if (change === "tenant") body.tenant_id = tenantB;
        if (change === "version") body.version = 1;
        if (change === "status") body.status = "success";
        if (change === "due") body.due_at = "2026-02-31T00:00:00Z";
        return Response.json(body, {
          headers: { ETag: change === "etag" ? '"900"' : `"${body.version}"` },
        });
      },
    ));
test("pagination pins status/limit, consumes no duplicates and resets cursor on status change", () => {
  let mode = false;
  const queries = [];
  return using(
    async ({ tasks }) => {
      mode = true;
      await tasks.filter("open");
      assert.equal(tasks.snapshot().items.length, 2);
      assert.equal(queries[1].get("cursor"), "next");
      assert.ok(queries.every((q) => q.get("status") === "open" && q.get("limit") === "100"));
      await tasks.filter("done");
      assert.equal(queries.at(-1).has("cursor"), false);
      assert.deepEqual(tasks.snapshot().items, []);
    },
    "mobile",
    (fetch) => async (input, init) => {
      const r = new Request(input, init),
        url = new URL(r.url);
      if (!mode || !url.pathname.endsWith("/tasks")) return fetch(r);
      queries.push(url.searchParams);
      const response = await fetch(r),
        body = await response.json();
      if (url.searchParams.get("status") === "done") return response;
      const cursor = url.searchParams.has("cursor");
      return Response.json({
        items: [body.items[cursor ? 1 : 0]],
        next_cursor: cursor ? null : "next",
      });
    },
  );
});
for (const bad of ["duplicate", "cursor", "wrong-status"])
  test(`invalid pagination never publishes partial results: ${bad}`, () => {
    let mode = false;
    return using(
      async ({ tasks }) => {
        mode = true;
        await tasks.filter("open");
        assert.deepEqual(tasks.snapshot().items, []);
        assert.match(tasks.snapshot().notice, /応答/);
      },
      "mobile",
      (fetch) => async (input, init) => {
        const r = new Request(input, init),
          response = await fetch(r);
        if (!mode || !new URL(r.url).pathname.endsWith("/tasks")) return response;
        const body = await response.json();
        return Response.json({
          items:
            bad === "cursor"
              ? []
              : [{ ...body.items[0], ...(bad === "wrong-status" ? { status: "done" } : {}) }],
          next_cursor: "next",
        });
      },
    );
  });
test("in-flight reads fence writes, filter changes and stale canonical updates", () =>
  using(async ({ tasks, fixture }) => {
    fixture.tasks.controls.delay = 20;
    const loading = tasks.load();
    tasks.create();
    await tasks.filter("done");
    assert.equal(tasks.snapshot().filter, "all");
    assert.equal(tasks.snapshot().editor, undefined);
    await loading;
    await edit(tasks, { status: "done" });
    const saving = tasks.save();
    await tasks.load();
    await saving;
    assert.equal(row(tasks).status, "done");
  }));
for (const change of [
  { tenant_id: tenantB },
  { owner_user_id: "other" },
  { due_at: null },
  { timezone: "Bad/Zone" },
  { assignee_user_id: null },
])
  test(`invalid private row rejected: ${JSON.stringify(change)}`, () =>
    using(async ({ tasks, session }) => {
      const task = tasks.snapshot().items.find((task) => task.id === privateTaskId);
      assert.throws(() => validateTask({ ...task, ...change }, session.snapshot().context));
    }));
test("AI task success prose displayed by real ChatController never mutates task state", () =>
  using(
    async ({ tasks, session, fixture }) => {
      const chat = new ChatController(session.snapshot().context, "mobile");
      try {
        fixture.chat.controls.delay = 1;
        await chat.load();
        await chat.select(fixtureShared);
        chat.draft("今日のタスクについて話して");
        await chat.send();
        for (
          let i = 0;
          i < 200 &&
          !chat.snapshot().messages.some((message) => message.text === "タスクを完了しました。");
          i++
        )
          await tick();
        assert.ok(
          chat.snapshot().messages.some((message) => message.text === "タスクを完了しました。"),
        );
        assert.equal(row(tasks).status, "open");
        assert.equal(writes(fixture).length, 0);
      } finally {
        chat.dispose();
      }
    },
    "mobile",
    (fetch) => async (input, init) => {
      const r = new Request(input, init),
        response = await fetch(r);
      if (new URL(r.url).pathname.endsWith(`/conversations/${fixtureShared}/messages`)) {
        const page = await response.json();
        page.items = page.items.map((message) =>
          message.role === "assistant" ? { ...message, text: "タスクを完了しました。" } : message,
        );
        return Response.json(page);
      }
      return response;
    },
  ));

test("delayed JSON body after context switch cannot restore task state", () => {
  let release, started;
  const ready = new Promise((resolve) => {
    started = resolve;
  });
  let delaying = false;
  return using(
    async ({ tasks, session }) => {
      delaying = true;
      const loading = tasks.load();
      await ready;
      await tick();
      await session.selectTenant(tenantB);
      assert.equal(tasks.snapshot().closed, true);
      release();
      await loading;
      assert.deepEqual(tasks.snapshot().items, []);
    },
    "mobile",
    (fetch) => async (input, init) => {
      const result = await fetch(input, init);
      if (delaying && new Request(input, init).url.includes("/tasks")) {
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
test("finite deadline clears busy even if transport never resolves", () => {
  let hanging = false;
  return using(
    async ({ tasks }) => {
      const timeout = AbortSignal.timeout,
        seen = [];
      const keepAlive = setTimeout(() => {}, 100);
      AbortSignal.timeout = (ms) => {
        seen.push(ms);
        return timeout(10);
      };
      try {
        hanging = true;
        await tasks.load();
        assert.deepEqual(seen, [15000]);
        assert.equal(tasks.snapshot().busy, false);
        assert.match(tasks.snapshot().notice, /通信/);
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
    async ({ tasks, fixture }) => {
      await tasks.select(sharedTaskId);
      tasks.draft({ ...tasks.snapshot().editor.draft, title: "保留する入力" });
      fixture.tasks.controls.scenario = "conflict";
      rejectRead = true;
      await tasks.save();
      assert.equal(tasks.snapshot().conflict.current, undefined);
      assert.equal(tasks.snapshot().editor.draft.title, "保留する入力");
      tasks.continueEditing();
      assert.ok(tasks.snapshot().conflict);
      rejectRead = false;
      await tasks.refreshConflict();
      tasks.continueEditing();
      await tasks.save();
      assert.equal(
        tasks.snapshot().items.find((task) => task.id === sharedTaskId).title,
        "保留する入力",
      );
    },
    "mobile",
    (fetch) => async (input, init) => {
      const request = new Request(input, init);
      if (rejectRead && request.method === "GET" && request.url.includes(`/tasks/${sharedTaskId}`))
        return Response.json({}, { status: 503 });
      return fetch(request);
    },
  );
});
test("successful save with list refresh failure keeps canonical result and reports partial list", () => {
  let rejectList = false;
  return using(
    async ({ tasks }) => {
      create(tasks);
      rejectList = true;
      await tasks.save();
      assert.equal(tasks.snapshot().pending, false);
      assert.equal(tasks.snapshot().editor, undefined);
      assert.equal(tasks.snapshot().items.length, 1);
      assert.match(tasks.snapshot().notice, /一覧全体は取得できていません/);
    },
    "mobile",
    (fetch) => async (input, init) => {
      const request = new Request(input, init);
      if (
        rejectList &&
        request.method === "GET" &&
        new URL(request.url).pathname.endsWith("/tasks")
      )
        return Response.json({}, { status: 503 });
      return fetch(request);
    },
  );
});
for (const method of ["POST", "PATCH"])
  test(`202 is not a confirmed ${method} task mutation`, () =>
    using(
      async ({ tasks }) => {
        if (method === "POST") create(tasks);
        else await edit(tasks, { status: "done" });
        await tasks.save();
        assert.equal(tasks.snapshot().pending, true);
        assert.ok(tasks.snapshot().editor);
        assert.equal(row(tasks).status, "open");
      },
      "mobile",
      (fetch) => async (input, init) => {
        const r = new Request(input, init),
          response = await fetch(r);
        return r.method === method
          ? Response.json(await response.json(), { status: 202, headers: response.headers })
          : response;
      },
    ));
test("selected target ETag mismatch is not editable", () =>
  using(
    async ({ tasks }) => {
      await tasks.select(sharedTaskId);
      assert.equal(tasks.snapshot().editor, undefined);
      assert.match(tasks.snapshot().notice, /応答/);
    },
    "mobile",
    (fetch) => async (input, init) => {
      const r = new Request(input, init),
        response = await fetch(r);
      if (new URL(r.url).pathname.endsWith(`/tasks/${sharedTaskId}`))
        return Response.json(await response.json(), { headers: { ETag: '"999"' } });
      return response;
    },
  ));
for (const [filter, before, current] of [
  ["all", "done", "open"],
  ["open", "open", "cancelled"],
])
  test(`selection reconciles newest canonical title/due/version/status and close retains it: ${before} -> ${current} (${filter})`, () =>
    using(async ({ tasks, fixture }) => {
      const records = fixture.tasks.records(tenantA, uid),
        old = records.get(sharedTaskId);
      records.set(sharedTaskId, { ...old, status: before });
      await tasks.load();
      await tasks.filter(filter);
      const updated = {
        ...old,
        status: current,
        title: "他の端末の最新題名",
        due_at: "2026-11-02T00:00:00Z",
        version: 2,
      };
      records.set(sharedTaskId, updated);
      await tasks.select(sharedTaskId);
      assert.deepEqual(tasks.snapshot().editor.original, updated);
      assert.equal(tasks.snapshot().editor.etag, '"2"');
      assert.equal(tasks.snapshot().editor.draft.status, current);
      assert.equal(tasks.snapshot().editor.draft.title, updated.title);
      assert.equal(tasks.snapshot().editor.draft.due_at, updated.due_at);
      const expected = filter === "all" ? updated : undefined;
      assert.deepEqual(row(tasks), expected);
      tasks.draft({
        ...tasks.snapshot().editor.draft,
        title: "未保存の題名",
        status: "done",
        due_at: "2026-11-03T00:00:00Z",
      });
      assert.deepEqual(row(tasks), expected);
      tasks.closeEditor();
      assert.equal(tasks.snapshot().editor, undefined);
      assert.deepEqual(row(tasks), expected);
      assert.equal(writes(fixture).length, 0);
    }));
