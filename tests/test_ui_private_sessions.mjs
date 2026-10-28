import assert from "node:assert/strict";
import { test } from "node:test";
import { OutputLifetime, privateSession } from "../packages/ui/src/output-target/lifetime.ts";
import { OutputTargetController } from "../packages/ui/src/output-target/controller.ts";
import { DeviceSessionController } from "../apps/display/session/controller.ts";
import { LocalSessionClient } from "../apps/display/session/client.ts";
import { SessionContext } from "../packages/ui/src/session.ts";
import { ChatController } from "../packages/ui/src/chat/controller.ts";
import {
  createChatFixture,
  fixturePrivate,
  fixtureShared,
} from "../packages/ui/src/chat/fixture.ts";
const tenant = "10000000-0000-4000-8000-000000000001",
  device = "60000000-0000-4000-8000-000000000001",
  sid = "70000000-0000-4000-8000-000000000001",
  runId = "40000000-0000-4000-8000-000000000001",
  finalId = "50000000-0000-4000-8000-000000000001";
const epoch = Date.parse("2026-10-28T00:00:00Z");
const iso = (value) => new Date(value).toISOString();
const session = (extra = {}) => ({
  id: sid,
  tenant_id: tenant,
  device_id: device,
  user_id: "synthetic-user",
  version: 1,
  status: "active",
  allowed_scopes: ["conversation"],
  created_at: iso(epoch),
  updated_at: iso(epoch),
  expires_at: iso(epoch + 300000),
  idle_expires_at: iso(epoch + 60000),
  ...extra,
});
function clock() {
  let wall = epoch,
    mono = 0;
  return {
    now: () => wall,
    monotonic: () => mono,
    advance: (ms) => {
      wall += ms;
      mono += ms;
    },
    rollback: () => {
      wall -= 1000;
    },
    mono: (value) => {
      mono = value;
    },
  };
}
const deferred = () => {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
};
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
async function until(predicate) {
  for (let i = 0; i < 200; i++) {
    if (predicate()) return;
    await tick();
  }
  throw Error("Timed out");
}
function phone(transport, time = clock()) {
  let context;
  context = new SessionContext({
    uid: "synthetic-user",
    tenantId: tenant,
    generation: 1,
    baseUrl: "https://fixture.invalid",
    current: () => true,
    token: async () => "synthetic-token",
    transport,
    denied: () => context.dispose(),
  });
  const controller = new OutputTargetController(context, time);
  return { context, controller, time };
}
const reply = (value, status = 200) =>
  Response.json(value, { status, headers: { ETag: `"${value.version}"` } });
test("T021 exact idle deadline cancels queued playback and late search/TTS; 5min cap and clock rollback", () => {
  for (const boundary of ["idle", "maximum", "rollback", "monotonic"]) {
    const time = clock(),
      lifetime = new OutputLifetime(time);
    let stopped = 0;
    lifetime.accept(
      session(boundary === "maximum" ? { idle_expires_at: iso(epoch + 300000) } : {}),
    );
    const current = lifetime.capture();
    lifetime.own(() => {
      stopped++;
    });
    if (boundary === "rollback") time.rollback();
    else if (boundary === "monotonic") time.mono(60000);
    else time.advance(boundary === "maximum" ? 300000 : 60000);
    assert.equal(current(), false);
    assert.equal(lifetime.session, undefined);
    assert.equal(stopped, 1);
  }
});
test("same consent version can extend idle, polling does not manufacture extra lifetime", () => {
  const time = clock(),
    lifetime = new OutputLifetime(time);
  lifetime.accept(session());
  const guard = lifetime.capture();
  time.advance(30000);
  lifetime.accept(session({ idle_expires_at: iso(epoch + 90000) }));
  assert.equal(guard(), true);
  time.advance(59999);
  assert.equal(guard(), true);
  time.advance(1);
  assert.equal(guard(), false);
});
test("R1-02 unchanged polls and wall lag cannot move idle or fixed monotonic deadlines", () => {
  for (const maximum of [false, true]) {
    const time = clock(),
      lifetime = new OutputLifetime(time);
    const dto = session(maximum ? { idle_expires_at: iso(epoch + 300000) } : {});
    lifetime.accept(dto);
    const guard = lifetime.capture();
    let stopped = 0;
    lifetime.own(() => stopped++);
    time.advance(30000);
    lifetime.accept(dto);
    time.mono(40000); // wall pauses while repeated HTTP observations arrive
    lifetime.accept(dto);
    lifetime.invalidate(); // an assignment change must not reset the fixed anchor
    lifetime.accept(dto);
    time.advance(20000);
    time.mono(maximum ? 300000 : 60000);
    assert.equal(lifetime.accept(dto), false);
    assert.equal(lifetime.session, undefined);
    assert.equal(guard(), false);
    assert.equal(stopped, 1);
    assert.equal(lifetime.accept(session({ idle_expires_at: iso(epoch + 300000) })), false);
  }
});
test("R1-02 same accept cannot expire then reinstall, and same-version idle advance stays capped", () => {
  const time = clock();
  let expired = 0;
  const life = new OutputLifetime(time, () => expired++);
  life.accept(session());
  time.advance(50000);
  time.mono(60000);
  assert.equal(life.accept(session()), false);
  assert.equal(expired, 1);
  assert.equal(life.session, undefined);
  const next = clock(),
    live = new OutputLifetime(next);
  live.accept(session());
  next.advance(30000);
  next.mono(40000);
  assert.equal(live.accept(session({ idle_expires_at: iso(epoch + 90000) })), true);
  next.mono(89999);
  assert.ok(live.session);
  next.mono(90000);
  assert.equal(live.session, undefined);
});
test("session validator rejects impossible bounds, identities, repeated/forbidden scopes", () => {
  for (const change of [
    { expires_at: iso(epoch + 300001) },
    { idle_expires_at: iso(epoch + 300001) },
    { device_id: "wrong" },
    { allowed_scopes: ["delete"] },
    { allowed_scopes: ["notes", "notes"] },
    { user_id: "" },
  ])
    assert.throws(() => privateSession(session(change)));
});
test("member starts only with explicit Pi/scope/exposure consent; destination stays phone", async () => {
  const calls = [];
  const { controller, context } = phone(async (request) => {
    calls.push(request);
    return reply(session(), 201);
  });
  await controller.start(device, ["conversation"], false);
  assert.equal(calls.length, 0);
  await controller.start(device, ["conversation"], true);
  assert.equal(calls.length, 1);
  assert.equal(controller.target(), undefined);
  assert.deepEqual(await calls[0].json(), {
    allowed_scopes: ["conversation"],
    acknowledge_shared_space: true,
  });
  assert.equal(calls[0].url.includes("/devices/"), true);
  controller.choose("pi");
  assert.equal(controller.target().device, device);
  context.dispose();
});
test("lost start and rate-limit retry use immutable key/body; terminal receipt never reactivates", async () => {
  const calls = [];
  let attempt = 0;
  const { controller, context } = phone(async (request) => {
    calls.push({ body: await request.text(), key: request.headers.get("Idempotency-Key") });
    attempt++;
    if (attempt === 1) throw Error("lost");
    if (attempt === 2) return new Response(null, { status: 429 });
    return reply(session({ status: "ended", version: 2 }), 201);
  });
  await controller.start(device, ["conversation"], true);
  await controller.start(device, ["notes"], true);
  await controller.retry();
  await controller.retry();
  assert.equal(calls.length, 3);
  assert.deepEqual(calls[0], calls[1]);
  assert.deepEqual(calls[1], calls[2]);
  assert.equal(controller.snapshot().session, undefined);
  assert.equal(controller.snapshot().pending, false);
  context.dispose();
});
for (const field of ["user_id", "tenant_id", "device_id"])
  test(`wrong ${field} cannot install private consent`, async () => {
    const { controller, context } = phone(async () =>
      reply(session({ [field]: field === "user_id" ? "another" : finalId }), 201),
    );
    await controller.start(device, ["conversation"], true);
    assert.equal(controller.snapshot().session, undefined);
    context.dispose();
  });
test("end clears immediately during hung GET; late same-version GET cannot restore it", async () => {
  const delayed = deferred(),
    started = deferred();
  const calls = [];
  const { controller, context } = phone(async (request) => {
    calls.push(request);
    if (request.method === "POST") return reply(session(), 201);
    if (request.method === "DELETE") return new Response(null, { status: 204 });
    started.resolve();
    return delayed.promise;
  });
  await controller.start(device, ["conversation"], true);
  controller.choose("pi");
  const read = controller.refresh();
  await started.promise;
  await controller.end();
  assert.equal(controller.snapshot().session, undefined);
  delayed.resolve(reply(session({ idle_expires_at: iso(epoch + 90000) })));
  await read;
  assert.equal(controller.target(), undefined);
  const end = calls.find((r) => r.method === "DELETE");
  assert.equal(end.headers.get("If-Match"), '"1"');
  context.dispose();
});
test("idle expires while network is unresolved and later idle response cannot revive consent", async () => {
  const delayed = deferred();
  const { controller, context, time } = phone(async (request) =>
    request.method === "POST" ? reply(session(), 201) : delayed.promise,
  );
  await controller.start(device, ["conversation"], true);
  const read = controller.refresh();
  time.advance(60000);
  controller.tick();
  delayed.resolve(reply(session({ idle_expires_at: iso(epoch + 90000) })));
  await read;
  assert.equal(controller.snapshot().session, undefined);
  context.dispose();
});
test("account/tenant context disposal discards delayed start without parsing/publishing it", async () => {
  const delayed = deferred(),
    started = deferred();
  const { controller, context } = phone(async () => {
    started.resolve();
    return delayed.promise;
  });
  const start = controller.start(device, ["conversation"], true);
  await started.promise;
  context.dispose();
  delayed.resolve(reply(session(), 201));
  await start;
  assert.equal(controller.snapshot().session, undefined);
});
function display(options = {}) {
  const time = clock(),
    calls = [],
    pending = deferred();
  let current = {
    ready: true,
    play_speech: false,
    snapshot: {
      device_id: device,
      tenant_id: tenant,
      connection_generation: 1,
      snapshot_id: "a".repeat(64),
      active_private_session: session(),
      assigned_runs: [
        {
          run_id: runId,
          conversation_id: fixturePrivate,
          output_mode: "text",
          assignment_generation: 1,
          last_event_seq: 0,
          private_session_id: sid,
        },
      ],
      pending_command_ids: [],
      pending_notification_ids: [],
      next_cursor: null,
      delivery_cursor: 0,
    },
  };
  const client = {
    snapshot: async () => structuredClone(current),
    stop: async (id) => {
      calls.push({ stop: id });
      return { status: "stopped", cloud_session_ended: false };
    },
    operation: async (body) => {
      calls.push(body);
      if (body.operation === "get_conversation")
        return {
          id: fixturePrivate,
          tenant_id: tenant,
          visibility: "private",
          display_policy: "authenticated_session",
          owner_user_id: "synthetic-user",
        };
      if (body.operation === "get_run")
        return {
          run_id: runId,
          conversation_id: fixturePrivate,
          status: "completed",
          final_message_id: finalId,
        };
      return {
        items: [{ id: finalId, role: "assistant", run_id: runId, text: "PRIVATE FINAL" }],
        next_cursor: null,
      };
    },
    events: async (_id, _session, signal) => {
      await new Promise((resolve) => signal.addEventListener("abort", resolve, { once: true }));
    },
    ...options,
  };
  const controller = new DeviceSessionController(client, time);
  return {
    controller,
    time,
    calls,
    client,
    pending,
    current: () => current,
    set: (value) => {
      current = value;
    },
  };
}
test("T020 shared snapshot never reads private history or unassigned run", async () => {
  const f = display();
  f.current().snapshot.active_private_session = null;
  f.current().snapshot.assigned_runs = [];
  await f.controller.refresh();
  assert.equal(f.calls.length, 0);
  assert.equal(f.controller.snapshot().session, undefined);
  f.controller.dispose();
});
test("T035 complete snapshot selects only matching private assignment and typed operation session", async () => {
  const f = display();
  await f.controller.refresh();
  await until(() => f.controller.snapshot().text);
  assert.equal(f.controller.snapshot().text, "PRIVATE FINAL");
  assert.ok(f.calls.every((call) => call.private_session_id === sid));
  f.controller.dispose();
});
for (const kind of [
  "next_cursor",
  "session_device",
  "foreign_session",
  "credential",
  "assignment",
  "tenant",
  "device",
])
  test(`T035 ${kind} mismatch invalidates old text and blocks publication`, async () => {
    const f = display();
    await f.controller.refresh();
    await until(() => f.controller.snapshot().text);
    const snap = f.current().snapshot;
    if (kind === "next_cursor") snap.next_cursor = "incomplete";
    if (kind === "session_device") snap.active_private_session.device_id = finalId;
    if (kind === "foreign_session") snap.assigned_runs[0].private_session_id = finalId;
    if (kind === "credential") snap.connection_generation = 0;
    if (kind === "assignment") {
      snap.assigned_runs[0].conversation_id = finalId;
    }
    if (kind === "tenant") {
      snap.tenant_id = finalId;
      snap.active_private_session.tenant_id = finalId;
    }
    if (kind === "device") {
      snap.device_id = finalId;
      snap.active_private_session.device_id = finalId;
    }
    await f.controller.refresh();
    assert.equal(f.controller.snapshot().text, "");
    f.controller.dispose();
  });
test("display stop clears playback, blocks delayed search, and same-session snapshots do not restore", async () => {
  const delayed = deferred(),
    entered = deferred();
  const f = display();
  const original = f.client.operation;
  f.client.operation = async (body) => {
    if (body.operation === "list_messages") {
      entered.resolve();
      return delayed.promise;
    }
    return original(body);
  };
  await f.controller.refresh();
  await entered.promise;
  let cancelled = false;
  f.controller.lifetime.own(() => {
    cancelled = true;
  });
  await f.controller.stop();
  delayed.resolve({
    items: [{ id: finalId, run_id: runId, role: "assistant", text: "TOO LATE" }],
    next_cursor: null,
  });
  await tick();
  await f.controller.refresh();
  assert.equal(f.controller.snapshot().text, "");
  assert.equal(cancelled, true);
  assert.ok(f.calls.some((v) => v.stop === sid));
  f.controller.dispose();
});
test("offline exact deadline and reconnect never replay or resurrect old text", async () => {
  const f = display();
  await f.controller.refresh();
  await until(() => f.controller.snapshot().text);
  const original = f.client.snapshot;
  f.client.snapshot = async () => {
    throw Error("offline");
  };
  await f.controller.refresh();
  assert.equal(f.controller.snapshot().text, "");
  f.time.advance(60000);
  f.controller.tick();
  assert.equal(f.controller.snapshot().session, undefined);
  f.client.snapshot = original;
  f.current().snapshot.active_private_session.idle_expires_at = iso(epoch + 90000);
  await f.controller.refresh();
  assert.equal(f.controller.snapshot().session, undefined);
  assert.equal(f.controller.snapshot().text, "");
  f.controller.dispose();
});
test("display late first snapshot after pagehide does not install authority", async () => {
  const delayed = deferred();
  const f = display({ snapshot: async () => delayed.promise });
  const refresh = f.controller.refresh();
  f.controller.suspend();
  delayed.resolve(f.current());
  await refresh;
  assert.equal(f.controller.snapshot().session, undefined);
  f.controller.dispose();
});
test("failed final-message continuation does not publish partial private history", async () => {
  const f = display(),
    original = f.client.operation;
  f.client.operation = async (body) =>
    body.operation !== "list_messages"
      ? original(body)
      : body.cursor
        ? Promise.reject(Error("lost page"))
        : {
            items: [{ id: sid, role: "assistant", run_id: runId, text: "UNVERIFIED" }],
            next_cursor: "page2",
          };
  await f.controller.refresh();
  await until(() => f.controller.snapshot().notice);
  assert.equal(f.controller.snapshot().text, "");
  f.controller.dispose();
});
test("local client sends only cookie/CSRF and typed fields; SSE speech event has no playback", async () => {
  const calls = [],
    events = [];
  const client = new LocalSessionClient(async (path, options) => {
    calls.push({ path, options });
    if (path === "/local/session") return Response.json({ csrf_token: "synthetic-csrf" });
    if (path.endsWith("/events"))
      return new Response('event: run\ndata: {"type":"speech.ready"}\n\n', {
        headers: { "Content-Type": "text/event-stream" },
      });
    return Response.json({});
  });
  await client.operation(
    { operation: "get_run", run_id: runId, private_session_id: sid },
    new AbortController().signal,
  );
  await client.events(runId, sid, new AbortController().signal, (kind, value) =>
    events.push([kind, value]),
  );
  assert.equal(calls.length, 3);
  assert.ok(
    calls.every(
      ({ options }) => options.credentials === "same-origin" && !options.headers.Authorization,
    ),
  );
  assert.equal(calls[1].options.headers["X-CSRF-Token"], "synthetic-csrf");
  assert.equal(events[0][1].type, "speech.ready");
});
test("chat exact retry freezes output destination and session; switch invalidates late acceptance", async () => {
  const fixture = createChatFixture();
  fixture.controls.delay = 1;
  const requests = [],
    held = deferred(),
    started = deferred();
  let hold = false;
  const { context } = phone(async (request) => {
    if (request.url.endsWith("/private-sessions"))
      return reply(
        session({
          created_at: iso(Date.now()),
          updated_at: iso(Date.now()),
          expires_at: iso(Date.now() + 300000),
          idle_expires_at: iso(Date.now() + 60000),
        }),
        201,
      );
    if (request.url.endsWith("/turns")) {
      requests.push({
        body: await request.clone().json(),
        key: request.headers.get("Idempotency-Key"),
      });
      if (hold) {
        started.resolve();
        return held.promise;
      }
      throw Error("lost");
    }
    return fixture.handle(request, "synthetic-user");
  });
  const chat = new ChatController(context, "mobile");
  await chat.load();
  await chat.select(fixturePrivate);
  chat.draft("same text");
  await chat.send();
  await chat.send();
  assert.equal(requests[0].key, requests[1].key);
  assert.equal(requests[0].body.output_device_id, undefined);
  await chat.outputTarget.start(device, ["conversation"], true);
  assert.ok(chat.outputTarget.snapshot().session);
  chat.outputTarget.choose("pi");
  await chat.select(fixturePrivate);
  await chat.allowPrivateDisplay();
  chat.draft("same text");
  hold = true;
  const sending = chat.send();
  await started.promise;
  assert.equal(requests[2].body.output_device_id, device);
  assert.notEqual(requests[2].key, requests[0].key);
  chat.outputTarget.choose("phone");
  held.resolve(Response.json({ run_id: runId, status: "queued" }, { status: 202 }));
  await sending;
  assert.equal(chat.snapshot().events, undefined);
  assert.equal(chat.snapshot().draft, "");
  context.dispose();
});

test("display ongoing SSE text/speech and late callbacks share expiry cleanup", async () => {
  const f = display(),
    original = f.client.operation,
    entered = deferred();
  let consume;
  f.client.operation = async (body) =>
    body.operation === "get_run"
      ? { run_id: runId, conversation_id: fixturePrivate, status: "running" }
      : original(body);
  f.client.events = async (_run, _session, signal, callback) => {
    consume = callback;
    entered.resolve();
    await new Promise((resolve) => signal.addEventListener("abort", resolve, { once: true }));
  };
  await f.controller.refresh();
  await entered.promise;
  const event = (seq, type, payload = {}) => ({
    run_id: runId,
    seq,
    type,
    payload,
    occurred_at: iso(epoch),
  });
  consume("run", event(1, "run.started"));
  consume("run", event(2, "text.delta", { text: "PRIVATE STREAM" }));
  consume(
    "run",
    event(3, "speech.ready", {
      speech_id: finalId,
      sentence_index: 0,
      expires_at: iso(epoch + 30000),
    }),
  );
  assert.equal(f.controller.snapshot().provisional, "PRIVATE STREAM");
  let queue = ["synthetic audio"],
    stopped = 0;
  f.controller.lifetime.own(() => {
    queue = [];
    stopped++;
  });
  f.time.advance(60000);
  f.controller.tick();
  assert.equal(f.controller.snapshot().provisional, "");
  assert.equal(stopped, 1);
  assert.deepEqual(queue, []);
  assert.throws(() => consume("run", event(4, "text.delta", { text: "LATE" })));
  f.controller.dispose();
});
test("display same-version idle refresh preserves current assignment and expired callback cannot return", async () => {
  const f = display();
  await f.controller.refresh();
  await until(() => f.controller.snapshot().text);
  const calls = f.calls.length;
  f.time.advance(30000);
  f.current().snapshot.active_private_session.idle_expires_at = iso(epoch + 90000);
  await f.controller.refresh();
  assert.equal(f.calls.length, calls);
  assert.equal(f.controller.snapshot().text, "PRIVATE FINAL");
  f.time.advance(60000);
  f.controller.tick();
  assert.equal(f.controller.snapshot().text, "");
  f.controller.dispose();
});
test("reversed session and assignment generations cannot revive the previous output", async () => {
  const f = display();
  await f.controller.refresh();
  await until(() => f.controller.snapshot().text);
  const old = structuredClone(f.current());
  f.current().snapshot.active_private_session.id = finalId;
  f.current().snapshot.assigned_runs = [];
  await f.controller.refresh();
  f.set(old);
  await f.controller.refresh();
  assert.equal(f.controller.snapshot().session, undefined);
  assert.equal(f.controller.snapshot().text, "");
  f.controller.dispose();
  const g = display();
  await g.controller.refresh();
  await until(() => g.controller.snapshot().text);
  g.current().snapshot.assigned_runs[0].assignment_generation = 2;
  await g.controller.refresh();
  await until(() => g.controller.snapshot().text);
  g.current().snapshot.assigned_runs[0].assignment_generation = 1;
  await g.controller.refresh();
  assert.equal(g.controller.snapshot().text, "");
  g.controller.dispose();
});
test("mobile suspension erases delegated content but retains an explicit owner end operation", async () => {
  const { controller, context } = phone(async (request) =>
    request.method === "DELETE" ? new Response(null, { status: 204 }) : reply(session(), 201),
  );
  await controller.start(device, ["conversation"], true);
  controller.choose("pi");
  let cleared = false;
  controller.lifetime.own(() => {
    cleared = true;
  });
  controller.suspend();
  assert.equal(cleared, true);
  assert.equal(controller.target(), undefined);
  assert.ok(controller.snapshot().session);
  await controller.end();
  assert.equal(controller.snapshot().session, undefined);
  context.dispose();
});
test("local bootstrap is shared by concurrent consumers; stale CSRF is re-bootstrapped after reset", async () => {
  const { bootstrapLocalSession } = await import("../apps/display/src/recovery/local-session.ts");
  const delayed = deferred();
  let calls = 0;
  const transport = async () => {
    calls++;
    return delayed.promise;
  };
  const first = bootstrapLocalSession(new AbortController().signal, transport),
    second = bootstrapLocalSession(new AbortController().signal, transport);
  assert.equal(calls, 1);
  delayed.resolve(Response.json({ csrf_token: "one-cookie" }));
  assert.deepEqual(await first, await second);
  let generation = 1;
  const seen = [];
  const client = new LocalSessionClient(async (path, options) => {
    if (path === "/local/session") return Response.json({ csrf_token: `csrf-${generation}` });
    seen.push(options.headers["X-CSRF-Token"]);
    return options.headers["X-CSRF-Token"] === `csrf-${generation}`
      ? Response.json({})
      : new Response(null, { status: 401 });
  });
  await client.operation({ operation: "get_run", run_id: runId }, new AbortController().signal);
  generation++;
  await assert.rejects(client.snapshot(new AbortController().signal));
  await client.operation({ operation: "get_run", run_id: runId }, new AbortController().signal);
  assert.deepEqual(seen, ["csrf-1", "csrf-1", "csrf-2"]);
});

test("owner end conflict verifies terminal state without restoring output or inventing a new DELETE", async () => {
  const calls = [];
  const { controller, context } = phone(async (request) => {
    calls.push(request.method);
    return request.method === "POST"
      ? reply(session(), 201)
      : request.method === "DELETE"
        ? new Response(null, { status: 412 })
        : reply(session({ status: "expired", version: 2 }));
  });
  await controller.start(device, ["conversation"], true);
  await controller.end();
  assert.deepEqual(calls, ["POST", "DELETE", "GET"]);
  assert.equal(controller.snapshot().pending, false);
  assert.equal(controller.snapshot().session, undefined);
  context.dispose();
});

test("R1-02 phone and Pi repeated observations retain deadline through hung/offline refresh", async () => {
  const held = deferred();
  let hang = false;
  const p = phone(async () => (hang ? held.promise : reply(session(), 201)));
  await p.controller.start(device, ["conversation"], true);
  p.controller.choose("pi");
  p.time.advance(30000);
  await p.controller.refresh();
  p.time.mono(40000);
  await p.controller.refresh();
  hang = true;
  const pending = p.controller.refresh();
  p.time.advance(20000);
  p.time.mono(60000);
  p.controller.tick();
  assert.equal(p.controller.snapshot().session, undefined);
  assert.equal(p.controller.target(), undefined);
  held.resolve(reply(session()));
  await pending;
  assert.equal(p.controller.snapshot().session, undefined);
  p.context.dispose();
  const f = display();
  await f.controller.refresh();
  await until(() => f.controller.snapshot().text);
  f.time.advance(30000);
  await f.controller.refresh();
  f.time.mono(40000);
  await f.controller.refresh();
  f.client.snapshot = async () => {
    throw Error("offline");
  };
  await f.controller.refresh();
  f.time.advance(20000);
  f.time.mono(60000);
  f.controller.tick();
  assert.equal(f.controller.snapshot().session, undefined);
  assert.equal(f.controller.snapshot().text, "");
  f.controller.dispose();
});
for (const interrupt of [false, true])
  test(`R1-01 exact uncertain policy recovery after reselection/cleanup: ${interrupt}`, async () => {
    const fixture = createChatFixture();
    fixture.controls.delay = 1;
    const captured = [],
      held = deferred(),
      entered = deferred();
    let attempts = 0,
      turns = 0;
    const { context } = phone(async (request) => {
      if (request.method === "PATCH") {
        captured.push({
          url: request.url,
          body: await request.clone().text(),
          key: request.headers.get("Idempotency-Key"),
          version: request.headers.get("If-Match"),
        });
        const result = await fixture.handle(request, "synthetic-user");
        attempts++;
        if (attempts === 1) {
          entered.resolve();
          if (interrupt) await held.promise;
          throw Error("committed response lost");
        }
        if (attempts === 2) return new Response(null, { status: 429 });
        return result;
      }
      if (request.url.endsWith("/turns")) {
        turns++;
        return Response.json({ run_id: runId, status: "queued" }, { status: 202 });
      }
      return fixture.handle(request, "synthetic-user");
    });
    const chat = new ChatController(context, "mobile");
    await chat.load();
    await chat.select(fixturePrivate);
    const pending = chat.allowPrivateDisplay();
    await entered.promise;
    if (interrupt) {
      await chat.select(fixtureShared);
      held.resolve();
    }
    await pending;
    chat.clearOutput();
    await chat.select(fixturePrivate);
    assert.equal(chat.snapshot().conversation.display_policy, "authenticated_session");
    assert.equal(chat.pendingDisplayPolicy, fixturePrivate);
    await chat.select(fixtureShared);
    await chat.allowPrivateDisplay();
    assert.equal(chat.pendingDisplayPolicy, fixturePrivate);
    await chat.allowPrivateDisplay();
    assert.equal(chat.pendingDisplayPolicy, undefined);
    assert.deepEqual(captured, [captured[0], captured[0], captured[0]]);
    assert.equal(chat.snapshot().conversation.id, fixtureShared);
    chat.draft("shared phone after reconciliation");
    await chat.send();
    assert.equal(turns, 1);
    context.dispose();
  });
const history = (seq, status = "running") => ({
  run: { run_id: runId, conversation_id: fixturePrivate, status, last_event_seq: seq },
  messages: { items: [], next_cursor: null },
  replace: true,
  play_speech: false,
});
const event = (seq, type, payload = {}) => ({
  run_id: runId,
  seq,
  type,
  payload,
  occurred_at: iso(epoch),
});
test("R1-03 running history remains on one stream, seeds sequence, ignores speech and reaches canonical final", async () => {
  const f = display(),
    original = f.client.operation;
  let status = "running",
    seq = 2,
    streams = 0,
    consume;
  f.client.operation = async (body) =>
    body.operation === "get_run"
      ? {
          run_id: runId,
          conversation_id: fixturePrivate,
          status,
          last_event_seq: seq,
          ...(status === "completed" ? { final_message_id: finalId } : {}),
        }
      : original(body);
  f.client.events = async (_id, _session, signal, callback) => {
    streams++;
    consume = callback;
    await callback("history", history(2));
    assert.equal(signal.aborted, false);
    await new Promise((resolve) => signal.addEventListener("abort", resolve, { once: true }));
  };
  await f.controller.refresh();
  await until(() => consume);
  await consume("history", history(2));
  await consume(
    "run",
    event(3, "speech.ready", {
      speech_id: finalId,
      sentence_index: 0,
      expires_at: iso(epoch + 30000),
    }),
  );
  assert.equal(f.controller.snapshot().provisional, "");
  await consume("run", event(4, "text.delta", { text: "LIVE AFTER HISTORY" }));
  assert.equal(f.controller.snapshot().provisional, "LIVE AFTER HISTORY");
  seq = 5;
  status = "completed";
  await consume("run", event(5, "run.completed", { message_id: finalId }));
  assert.equal(f.controller.snapshot().text, "PRIVATE FINAL");
  assert.equal(streams, 1);
  f.time.advance(60000);
  f.controller.tick();
  assert.equal(f.controller.snapshot().text, "");
  f.controller.dispose();
});
test("R1-03 history then closed transport reconnects only on refresh", async () => {
  const f = display(),
    original = f.client.operation;
  let streams = 0;
  f.client.operation = async (body) =>
    body.operation === "get_run"
      ? { run_id: runId, conversation_id: fixturePrivate, status: "running", last_event_seq: 2 }
      : original(body);
  f.client.events = async (_id, _session, _signal, consume) => {
    streams++;
    await consume("history", history(2));
  };
  await f.controller.refresh();
  await until(() => streams === 1);
  await tick();
  assert.equal(streams, 1);
  await f.controller.refresh();
  await until(() => streams === 2);
  await tick();
  assert.equal(streams, 2);
  f.controller.dispose();
});
test("R1-03 terminal replacement history is reread on current stream without reopening", async () => {
  const f = display(),
    original = f.client.operation;
  let reads = 0,
    streams = 0;
  f.client.operation = async (body) =>
    body.operation === "get_run"
      ? {
          run_id: runId,
          conversation_id: fixturePrivate,
          status: ++reads === 1 ? "running" : "completed",
          last_event_seq: 3,
          final_message_id: finalId,
        }
      : original(body);
  f.client.events = async (_id, _session, signal, consume) => {
    streams++;
    await consume("history", history(3, "completed"));
    assert.equal(signal.aborted, true);
  };
  await f.controller.refresh();
  await until(() => f.controller.snapshot().text);
  assert.equal(streams, 1);
  assert.equal(f.controller.snapshot().text, "PRIVATE FINAL");
  f.controller.dispose();
});

test("R1-02 changed same-id binding cannot bypass expiry; clock failure survives another consent", () => {
  const time = clock(),
    life = new OutputLifetime(time);
  life.accept(session());
  time.advance(60000);
  life.tick();
  assert.equal(life.accept(session({ version: 2, idle_expires_at: iso(epoch + 120000) })), false);
  const later = clock(),
    failed = new OutputLifetime(later);
  failed.accept(session());
  later.rollback();
  failed.tick();
  assert.equal(failed.accept(session({ id: finalId })), false);
  assert.equal(failed.session, undefined);
});
for (const boundary of ["expiry", "stop", "replacement"])
  test(`R1-03 delayed history typed read fences buffered live event: ${boundary}`, async () => {
    const f = display(),
      original = f.client.operation,
      entered = deferred(),
      delayed = deferred();
    let conversationReads = 0,
      cancelled = 0;
    f.client.operation = async (body) => {
      if (body.operation === "get_run")
        return {
          run_id: runId,
          conversation_id: fixturePrivate,
          status: "running",
          last_event_seq: 2,
        };
      if (body.operation === "get_conversation" && ++conversationReads === 2) {
        entered.resolve();
        return delayed.promise;
      }
      return original(body);
    };
    const wire = new LocalSessionClient(async (path) =>
      path === "/local/session"
        ? Response.json({ csrf_token: "synthetic" })
        : new Response(
            new ReadableStream({
              start(controller) {
                controller.enqueue(
                  new TextEncoder().encode(
                    `event: history\ndata: ${JSON.stringify(history(2))}\n\nevent: run\ndata: ${JSON.stringify(event(3, "text.delta", { text: "MUST NOT PUBLISH" }))}\n\n`,
                  ),
                );
              },
              cancel() {
                cancelled++;
              },
            }),
            { headers: { "Content-Type": "text/event-stream" } },
          ),
    );
    f.client.events = wire.events.bind(wire);
    await f.controller.refresh();
    await entered.promise;
    if (boundary === "expiry") {
      f.time.advance(60000);
      f.controller.tick();
    } else if (boundary === "stop") await f.controller.stop();
    else {
      f.current().snapshot.active_private_session.id = finalId;
      f.current().snapshot.assigned_runs = [];
      await f.controller.refresh();
    }
    delayed.resolve(await original({ operation: "get_conversation" }));
    await until(() => cancelled === 1);
    assert.equal(f.controller.snapshot().text, "");
    assert.equal(f.controller.snapshot().provisional, "");
    f.controller.dispose();
  });

for (const ending of [undefined, null])
  for (const terminal of [false, true])
    test(`R2-01 real bridge absent/null end cursor: ${String(ending)}, terminal=${terminal}`, async () => {
      const f = display(),
        original = f.client.operation;
      let status = "running",
        seq = 2,
        opens = 0;
      f.client.operation = async (body) =>
        body.operation === "get_run"
          ? {
              run_id: runId,
              conversation_id: fixturePrivate,
              status,
              last_event_seq: seq,
              ...(status === "completed" ? { final_message_id: finalId } : {}),
            }
          : original(body);
      f.client.events = async (_id, _session, signal, consume) => {
        opens++;
        if (terminal) {
          status = "completed";
          seq = 4;
        }
        const value = history(seq, status);
        value.messages = {
          items: [{ id: finalId, run_id: runId, role: "user", text: "FIRST USER MESSAGE" }],
          ...(ending === undefined ? {} : { next_cursor: ending }),
        };
        await consume("history", value);
        if (!terminal) {
          await consume("run", event(3, "text.delta", { text: "AFTER FIRST HISTORY" }));
          assert.equal(f.controller.snapshot().provisional, "AFTER FIRST HISTORY");
          status = "completed";
          seq = 4;
          await consume("run", event(4, "run.completed", { message_id: finalId }));
        }
        assert.equal(signal.aborted, true);
      };
      await f.controller.refresh();
      await until(() => f.controller.snapshot().text);
      assert.equal(opens, 1);
      assert.equal(f.controller.snapshot().text, "PRIVATE FINAL");
      f.controller.dispose();
    });
for (const cursor of [false, 7, {}, "", "x".repeat(2049)])
  test(`R2-01 rejects malformed history cursor ${typeof cursor}:${String(cursor).slice(0, 10)}`, async () => {
    const f = display(),
      original = f.client.operation;
    let continued = false;
    f.client.operation = async (body) =>
      body.operation === "get_run"
        ? { run_id: runId, conversation_id: fixturePrivate, status: "running", last_event_seq: 2 }
        : original(body);
    f.client.events = async (_id, _session, _signal, consume) => {
      const value = history(2);
      value.messages.next_cursor = cursor;
      await consume("history", value);
      continued = true;
    };
    await f.controller.refresh();
    await until(() => f.controller.snapshot().notice || continued);
    assert.equal(continued, false);
    assert.equal(f.controller.snapshot().text, "");
    assert.equal(f.controller.snapshot().provisional, "");
    f.controller.dispose();
  });
function pagedDisplay(total) {
  const f = display(),
    original = f.client.operation;
  const pages = [];
  let status = "completed";
  const operation = async (body) => {
    if (body.operation === "get_run")
      return {
        run_id: runId,
        conversation_id: fixturePrivate,
        status,
        last_event_seq: 2,
        ...(status === "completed" ? { final_message_id: finalId } : {}),
      };
    if (body.operation !== "list_messages") return original(body);
    const page = Number(body.cursor ?? 0) + 1;
    pages.push(page);
    return {
      items: [
        {
          id:
            page === total
              ? finalId
              : `50000000-0000-4000-8000-${String(page + 1).padStart(12, "0")}`,
          run_id: runId,
          role: "assistant",
          text: page === total ? "PRIVATE CANONICAL FINAL" : "MUST NOT DISPLAY OLDER PRIVATE TEXT",
        },
      ],
      ...(page < total ? { next_cursor: String(page) } : {}),
    };
  };
  f.client.operation = operation;
  return { ...f, pages, operation, status: (value) => (status = value) };
}
for (const total of [100, 101, 201])
  for (const fromLive of [false, true])
    test(`R2-02 bounded continuation reaches page ${total}, live=${fromLive}`, async () => {
      const f = pagedDisplay(total);
      let consume;
      if (fromLive) {
        f.status("running");
        f.client.events = async (_id, _session, signal, callback) => {
          consume = callback;
          await new Promise((resolve) => signal.addEventListener("abort", resolve, { once: true }));
        };
      }
      await f.controller.refresh();
      if (fromLive) {
        await until(() => consume);
        await consume("run", event(1, "run.started"));
        f.status("completed");
        await consume("run", event(2, "run.completed", { message_id: finalId }));
      }
      for (let batch = 1; batch <= Math.ceil(total / 100); batch++) {
        await until(() => f.pages.length === Math.min(batch * 100, total));
        await tick();
        if (f.pages.length < total) {
          assert.equal(f.controller.snapshot().text, "");
          assert.equal(f.controller.snapshot().provisional, "");
          await f.controller.refresh();
        }
      }
      assert.deepEqual(
        f.pages,
        Array.from({ length: total }, (_, i) => i + 1),
      );
      assert.equal(f.controller.snapshot().text, "PRIVATE CANONICAL FINAL");
      f.controller.dispose();
    });
for (const boundary of ["stop", "expiry", "run", "assignment", "session", "credential"])
  test(`R2-02 delayed continuation cannot publish after ${boundary}`, async () => {
    const f = pagedDisplay(201),
      entered = deferred(),
      held = deferred();
    await f.controller.refresh();
    await until(() => f.pages.length === 100);
    await tick();
    f.client.operation = async (body) => {
      if (body.operation === "list_messages" && body.cursor === "100") {
        entered.resolve();
        return held.promise;
      }
      return f.operation(body);
    };
    await f.controller.refresh();
    await entered.promise;
    if (boundary === "stop") await f.controller.stop();
    else if (boundary === "expiry") {
      f.time.advance(60000);
      f.controller.tick();
    } else if (boundary === "run") {
      f.current().snapshot.assigned_runs.push({
        ...f.current().snapshot.assigned_runs[0],
        run_id: finalId,
      });
      await f.controller.refresh();
      await f.controller.select(finalId);
    } else {
      if (boundary === "assignment") f.current().snapshot.assigned_runs[0].assignment_generation++;
      if (boundary === "session") {
        f.current().snapshot.active_private_session.id = finalId;
        f.current().snapshot.assigned_runs[0].private_session_id = finalId;
      }
      if (boundary === "credential") f.current().snapshot.connection_generation++;
      await f.controller.refresh();
    }
    held.resolve({
      items: [{ id: finalId, run_id: runId, role: "assistant", text: "STALE PRIVATE FINAL" }],
    });
    await tick();
    await tick();
    assert.equal(f.controller.snapshot().text, "");
    assert.equal(f.controller.snapshot().provisional, "");
    f.controller.dispose();
  });
test("R2-02 cross-batch cursor cycle is bounded and never publishes partial history", async () => {
  const f = pagedDisplay(9999);
  f.client.operation = async (body) => {
    const value = await f.operation(body);
    if (body.operation === "list_messages" && body.cursor === "130") value.next_cursor = "50";
    return value;
  };
  for (let batch = 0; batch < 6; batch++) {
    const before = f.pages.length;
    await f.controller.refresh();
    await until(() => f.pages.length > before);
    await until(() => f.controller.snapshot().notice);
    await tick();
    assert.ok(f.pages.length - before <= 100);
    if (f.controller.snapshot().notice.includes("現在状態")) break;
  }
  assert.match(f.controller.snapshot().notice, /現在状態/);
  assert.ok(f.pages.length < 600);
  assert.equal(f.controller.snapshot().text, "");
  f.controller.dispose();
});
test("R2-02 malformed and rejected expired cursor fail closed; rejection permits a fresh bounded lookup", async () => {
  const { LocalOperationFailure } = await import("../apps/display/session/client.ts");
  const rejecting = new LocalSessionClient(async (path) =>
    path === "/local/session"
      ? Response.json({ csrf_token: "synthetic" })
      : new Response(null, { status: 410 }),
  );
  await assert.rejects(
    rejecting.operation({ operation: "list_messages" }, new AbortController().signal),
    (error) => error instanceof LocalOperationFailure && error.status === 410,
  );
  for (const mode of ["malformed", "expired", "network"]) {
    const f = pagedDisplay(101);
    await f.controller.refresh();
    await until(() => f.pages.length === 100);
    await tick();
    let failed = false;
    f.client.operation = async (body) => {
      if (body.operation === "list_messages" && body.cursor === "100" && !failed) {
        failed = true;
        if (mode === "malformed") return { items: [], next_cursor: 7 };
        if (mode === "expired") throw new LocalOperationFailure(410);
        throw Error("offline page");
      }
      return f.operation(body);
    };
    await f.controller.refresh();
    await until(() => failed);
    await tick();
    assert.equal(f.controller.snapshot().text, "");
    await f.controller.refresh();
    await until(() => f.pages.length > 100);
    await tick();
    if (mode === "expired") {
      assert.equal(f.pages[100], 1);
      await f.controller.refresh();
    }
    await until(() => f.controller.snapshot().text);
    assert.equal(f.controller.snapshot().text, "PRIVATE CANONICAL FINAL");
    f.controller.dispose();
  }
});
