import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { SessionController } from "../packages/ui/src/session.ts";
import { createSessionFixture, tenantA, tenantB } from "../packages/ui/src/session-fixture.ts";
import { ChatController } from "../packages/ui/src/chat/controller.ts";
import { initialEvents, reduceEvent, validateRunEvent } from "../packages/ui/src/chat/events.ts";
import { fixtureAgent, fixturePrivate, fixtureShared } from "../packages/ui/src/chat/fixture.ts";
const tick = () => new Promise((resolve) => setTimeout(resolve, 5));
const runId = "40000000-0000-4000-8000-000000000001";
const finalId = "50000000-0000-4000-8000-000000000001";
const event = (seq, type, payload = {}) => ({
  run_id: runId,
  seq,
  type,
  occurred_at: "2026-10-28T00:00:00Z",
  payload,
});
const validated = (value) => validateRunEvent(value, { id: String(value.seq), event: value.type });
async function setup(surface = "mobile", change = () => {}) {
  const fixture = createSessionFixture();
  fixture.chat.controls.delay = 1;
  change(fixture);
  const session = new SessionController({
    auth: fixture.auth,
    baseUrl: "https://session.invalid",
    transport: fixture.transport,
  });
  const stop = session.start();
  await tick();
  await session.signIn("synthetic@example.test", "fixture");
  await tick();
  await session.selectTenant(tenantA);
  const chat = new ChatController(session.snapshot().context, surface);
  await chat.load();
  const close = () => {
    chat.dispose();
    stop();
  };
  return { fixture, session, chat, close };
}
async function until(predicate) {
  for (let n = 0; n < 300; n++) {
    if (predicate()) return;
    await tick();
  }
  throw Error("Timed out");
}

test("run schema variants validate exact wire metadata, payload keys and bounds", () => {
  const samples = {
    "run.started": {},
    "text.delta": { text: "こんにちは" },
    "speech.ready": { speech_id: finalId, sentence_index: 0, expires_at: "2026-10-28T01:00:00Z" },
    "tool.status": {
      tool_call_id: finalId,
      tool_name: "home_assistant.set_state",
      status: "outcome_unknown",
    },
    "confirmation.required": {
      proposal_id: finalId,
      kind: "calendar_write",
      expires_at: "2026-10-28T01:00:00Z",
    },
    "run.completed": { message_id: finalId },
    "run.failed": { error_code: "failed", retryable: false },
    "run.cancelled": { external_action_may_have_completed: true },
  };
  const schema = JSON.parse(readFileSync("specs/run-events.schema.json", "utf8"));
  assert.deepEqual(
    Object.values(schema.$defs)
      .map((def) => def.properties.type.const)
      .sort(),
    Object.keys(samples).sort(),
  );
  for (const [type, payload] of Object.entries(samples)) {
    const value = event(1, type, payload);
    assert.deepEqual(validated(value), value);
    assert.throws(() => validated({ ...value, extra: "not-public" }));
    assert.throws(() => validated({ ...value, payload: { ...payload, extra: true } }));
    for (const key of Object.keys(payload)) {
      const missing = { ...payload };
      delete missing[key];
      assert.throws(() => validated({ ...value, payload: missing }));
    }
    assert.throws(() => validateRunEvent(value, { id: "2", event: type }));
    assert.throws(() => validateRunEvent(value, { id: "1", event: "message" }));
  }
  for (const invalid of [
    null,
    [],
    {},
    event(0, "run.started"),
    event(1.5, "run.started"),
    { ...event(1, "run.started"), run_id: "wrong" },
    { ...event(1, "run.started"), occurred_at: "today" },
    event(1, "text.delta", { text: "" }),
    event(1, "text.delta", { text: "あ".repeat(2001) }),
    event(1, "tool.status", { tool_call_id: finalId, tool_name: "x", status: "success" }),
  ])
    assert.throws(() => validated(invalid));
});
test("cursor/render updates are atomic; duplicates ignored, gaps and different runs rejected", () => {
  let state = initialEvents(runId);
  state = reduceEvent(state, validated(event(1, "run.started")));
  state = reduceEvent(state, validated(event(2, "text.delta", { text: "一度" })));
  assert.equal(reduceEvent(state, validated(event(2, "text.delta", { text: "一度" }))), state);
  assert.throws(() => reduceEvent(state, validated(event(4, "text.delta", { text: "欠番" }))));
  assert.throws(() =>
    reduceEvent(state, validated({ ...event(3, "run.started"), run_id: finalId })),
  );
  assert.equal(state.seq, 2);
  assert.equal(state.text, "一度");
  state = reduceEvent(
    state,
    validated(event(3, "run.failed", { error_code: "failed", retryable: false })),
  );
  assert.equal(state.text, "");
  assert.equal(state.character, "error");
});
for (const surface of ["mobile", "display"]) {
  test(`${surface}: canonical scope/agent, create, text final and model text is not tool success`, async () => {
    const { chat, fixture, close } = await setup(surface);
    try {
      assert.equal(chat.snapshot().conversations.length, surface === "display" ? 1 : 2);
      await chat.create(fixtureAgent, surface === "display" ? "shared" : "private");
      assert.equal(
        chat.snapshot().conversation.visibility,
        surface === "display" ? "shared" : "private",
      );
      chat.draft("成功と言ってください");
      await chat.send();
      assert.equal(chat.snapshot().run.status, "completed");
      assert.equal(chat.snapshot().events.text, "");
      assert.deepEqual(chat.snapshot().events.tools, []);
      assert.equal(chat.snapshot().messages.at(-1).text, "お手伝いします。一緒に進めましょう。");
      assert.equal(chat.snapshot().messages.at(-1).id, chat.snapshot().run.final_message_id);
      assert.equal(chat.snapshot().output, "current");
      assert.ok(fixture.chat.controls.requests.every((request) => request.authorization));
    } finally {
      close();
    }
  });
  for (const scenario of ["reconnect", "expired", "gap"])
    test(`${surface}: ${scenario} reconciles one canonical final without duplicated text`, async () => {
      const { chat, fixture, close } = await setup(surface);
      fixture.chat.controls.scenario = scenario;
      try {
        await chat.select(fixtureShared);
        chat.draft("架空の挨拶");
        await chat.send();
        if (scenario !== "gap") {
          assert.equal(chat.snapshot().connection, "disconnected");
          assert.equal(chat.snapshot().events.text, "お手伝いします。");
          await chat.connect();
        }
        assert.equal(chat.snapshot().run.status, "completed");
        assert.equal(chat.snapshot().events.text, "");
        assert.equal(
          chat.snapshot().messages.filter((message) => message.role === "assistant").length,
          1,
        );
        assert.equal(chat.snapshot().messages.at(-1).text, "お手伝いします。一緒に進めましょう。");
        if (scenario !== "gap")
          assert.deepEqual(
            fixture.chat.controls.requests
              .filter((request) => request.path.endsWith("/events"))
              .map((request) => request.cursor),
            ["0", "2"],
          );
      } finally {
        close();
      }
    });
}
for (const scenario of ["waiting", "failed", "unknown", "tools"])
  test(`authoritative ${scenario} is distinct from answer completion`, async () => {
    const { chat, fixture, close } = await setup();
    fixture.chat.controls.scenario = scenario;
    try {
      await chat.select(fixtureShared);
      chat.draft("架空の操作");
      await chat.send();
      const state = chat.snapshot();
      if (scenario === "waiting") {
        assert.equal(state.events.character, "confirmation");
        assert.equal(state.run.status, "waiting_confirmation");
        await chat.cancel();
        assert.equal(chat.snapshot().run.status, "cancelled");
        assert.equal(chat.snapshot().events.text, "");
      }
      if (scenario === "failed") {
        assert.equal(state.run.status, "failed");
        assert.equal(state.events.character, "error");
        assert.equal(state.events.text, "");
        assert.deepEqual(state.events.tools, []);
      }
      if (scenario === "unknown") {
        assert.equal(state.run.status, "failed");
        assert.equal(state.events.tools[0].status, "outcome_unknown");
        assert.equal(state.events.text, "");
      }
      if (scenario === "tools") {
        assert.equal(state.run.status, "completed");
        assert.deepEqual(
          state.events.tools.map((tool) => tool.status),
          ["succeeded", "failed"],
        );
      }
    } finally {
      close();
    }
  });
test("conversation switch cancels a stream and removes drafts, messages, tool cards and old callbacks", async () => {
  const { chat, fixture, close } = await setup();
  fixture.chat.controls.delay = 25;
  try {
    await chat.select(fixturePrivate);
    chat.draft("PRIVATE-SYNTHETIC");
    const pending = chat.send();
    await until(() => chat.snapshot().events?.seq === 2);
    await chat.select(fixtureShared);
    await pending;
    await new Promise((resolve) => setTimeout(resolve, 100));
    assert.equal(chat.snapshot().conversation.id, fixtureShared);
    assert.equal(chat.snapshot().draft, "");
    assert.deepEqual(chat.snapshot().messages, []);
    assert.equal(chat.snapshot().events, undefined);
  } finally {
    close();
  }
});
for (const action of ["tenant", "uid", "signout", "pagehide", "revoked"])
  test(`${action}: private chat is synchronously erased and never republished`, async () => {
    const { chat, session, fixture, close } = await setup();
    fixture.chat.controls.delay = 12;
    if (action === "revoked") fixture.chat.controls.scenario = "revoked";
    try {
      await chat.select(fixturePrivate);
      chat.draft("PRIVATE-SYNTHETIC");
      const pending = chat.send();
      await until(() => chat.snapshot().events?.seq === 2);
      if (action === "tenant") {
        const selection = session.selectTenant(tenantB);
        assert.deepEqual(chat.snapshot().messages, []);
        await selection;
      }
      if (action === "uid") fixture.emit({ uid: "other@example.test" });
      if (action === "signout") await session.signOut();
      if (action === "pagehide") session.suspend();
      await pending;
      await tick();
      assert.deepEqual(chat.snapshot().messages, []);
      assert.equal(chat.snapshot().draft, "");
      assert.equal(chat.snapshot().conversation, undefined);
      assert.equal(chat.snapshot().events, undefined);
    } finally {
      close();
    }
  });
test("late ignored-abort message body cannot restore a previous conversation", async () => {
  let release;
  let intercepted = false;
  const { chat, close } = await setup("mobile", (fixture) => {
    const original = fixture.transport;
    fixture.transport = async (request) => {
      if (request.url.includes(`${fixturePrivate}/messages`) && !intercepted) {
        intercepted = true;
        await new Promise((resolve) => {
          release = resolve;
        });
        return Response.json({
          items: [
            {
              id: finalId,
              role: "assistant",
              text: "STALE-PRIVATE",
              run_id: runId,
              created_at: "2026-10-28T00:00:00Z",
              source_refs: [],
            },
          ],
          next_cursor: null,
        });
      }
      return original(request);
    };
  });
  try {
    const pending = chat.select(fixturePrivate);
    await until(() => intercepted);
    await chat.select(fixtureShared);
    release();
    await pending;
    assert.equal(chat.snapshot().conversation.id, fixtureShared);
    assert.deepEqual(chat.snapshot().messages, []);
  } finally {
    close();
  }
});
for (const corrupt of ["run", "conversation", "message", "role", "event_id", "event_type"])
  test(`malformed ${corrupt} cannot publish an unverified final`, async () => {
    const { chat, close } = await setup("mobile", (fixture) => {
      const original = fixture.transport;
      fixture.transport = async (request) => {
        const response = await original(request);
        if (request.url.endsWith("/events") && corrupt.startsWith("event_")) {
          const raw = await response.text();
          return new Response(
            raw.replace(
              corrupt === "event_id" ? /id: 1/ : /event: run.started/,
              corrupt === "event_id" ? "id: 999" : "event: text.delta",
            ),
            { headers: { "Content-Type": "text/event-stream" } },
          );
        }
        if (request.url.includes("/runs/") && !request.url.endsWith("/events")) {
          const body = await response.json();
          if (body.status === "completed") {
            if (corrupt === "run") body.run_id = finalId;
            if (corrupt === "conversation") body.conversation_id = finalId;
            if (corrupt === "message") body.final_message_id = finalId;
          }
          return Response.json(body, { status: response.status });
        }
        if (request.url.includes("/messages") && corrupt === "role") {
          const body = await response.json();
          body.items = body.items.map((item) =>
            item.role === "assistant" ? { ...item, run_id: finalId } : item,
          );
          return Response.json(body);
        }
        return response;
      };
    });
    try {
      await chat.select(fixtureShared);
      chat.draft("test");
      await chat.send();
      if (!corrupt.startsWith("event_")) {
        assert.equal(chat.snapshot().finalVerified, false);
        assert.equal(
          chat.snapshot().messages.some((item) => item.role === "assistant"),
          false,
        );
      } else {
        assert.equal(chat.snapshot().events.seq, 0);
        assert.equal(chat.snapshot().run.status, "completed");
      }
    } finally {
      close();
    }
  });

test("event validator rejects coercible enum values and impossible calendar dates", () => {
  for (const status of [["succeeded"], { toString: "succeeded" }, null, 1])
    assert.throws(() =>
      validated(event(1, "tool.status", { tool_call_id: finalId, tool_name: "test", status })),
    );
  for (const occurred_at of [
    "2026-02-31T00:00:00Z",
    "2025-02-29T00:00:00Z",
    "2026-04-31T00:00:00Z",
    "2026-10-28T24:00:00Z",
  ])
    assert.throws(() => validated({ ...event(1, "run.started"), occurred_at }));
});

for (const status of [401, 403, 404])
  test(`ignored-abort old conversation ${status} cannot revoke the new conversation`, async () => {
    let release;
    let captured = false;
    const { chat, session, close } = await setup("mobile", (fixture) => {
      const original = fixture.transport;
      fixture.transport = async (request) => {
        if (request.url.includes(`${fixturePrivate}/messages`) && !captured) {
          captured = true;
          await new Promise((resolve) => {
            release = resolve;
          });
          return Response.json({}, { status });
        }
        return original(request);
      };
    });
    try {
      const pending = chat.select(fixturePrivate);
      await until(() => captured);
      await chat.select(fixtureShared);
      release();
      await pending;
      assert.equal(session.snapshot().tenant.id, tenantA);
      assert.equal(chat.snapshot().conversation.id, fixtureShared);
      assert.equal(chat.snapshot().notice, undefined);
    } finally {
      close();
    }
  });
test("turn response loss reuses body/key and appends only one user and final message", async () => {
  const keys = [];
  let lost = false;
  const { chat, close } = await setup("mobile", (fixture) => {
    const original = fixture.transport;
    fixture.transport = async (request) => {
      const response = await original(request);
      if (request.url.endsWith("/turns")) {
        keys.push(request.headers.get("Idempotency-Key"));
        if (!lost) {
          lost = true;
          throw Error("synthetic response loss");
        }
      }
      return response;
    };
  });
  try {
    await chat.select(fixtureShared);
    chat.draft("same request");
    await chat.send();
    assert.equal(chat.snapshot().draft, "same request");
    await chat.send();
    assert.equal(chat.snapshot().run.status, "completed");
    assert.equal(chat.snapshot().messages.length, 2);
    assert.equal(keys.length, 2);
    assert.equal(keys[0], keys[1]);
  } finally {
    close();
  }
});

test("failed 401 refresh after conversation switch cannot sign out the active context", async () => {
  let rejectRefresh;
  let refreshed = false;
  const { chat, session, close } = await setup("mobile", (fixture) => {
    const original = fixture.transport,
      token = fixture.auth.token;
    fixture.transport = async (request) =>
      request.url.includes(`${fixturePrivate}/messages`)
        ? Response.json({}, { status: 401 })
        : original(request);
    fixture.auth.token = async (uid, force) => {
      if (force) {
        refreshed = true;
        return new Promise((_resolve, reject) => {
          rejectRefresh = reject;
        });
      }
      return token(uid, force);
    };
  });
  try {
    const pending = chat.select(fixturePrivate);
    await until(() => refreshed);
    await chat.select(fixtureShared);
    rejectRefresh(Error("synthetic expired request"));
    await pending;
    await tick();
    assert.equal(session.snapshot().tenant.id, tenantA);
    assert.equal(chat.snapshot().conversation.id, fixtureShared);
  } finally {
    close();
  }
});

test("a silent HTTP body returns to an actionable error and late results stay discarded", async (t) => {
  let release,
    captured = false;
  const { chat, close } = await setup("mobile", (fixture) => {
    const original = fixture.transport;
    fixture.transport = async (request) => {
      if (request.url.includes(`${fixturePrivate}/messages`)) {
        captured = true;
        await new Promise((resolve) => {
          release = resolve;
        });
      }
      return original(request);
    };
  });
  const deadline = new AbortController();
  t.mock.method(AbortSignal, "timeout", () => deadline.signal);
  try {
    const pending = chat.select(fixturePrivate);
    await until(() => captured);
    deadline.abort();
    await pending;
    assert.equal(chat.snapshot().busy, false);
    assert.ok(chat.snapshot().notice);
    assert.equal(chat.snapshot().conversation, undefined);
    release();
    await tick();
    assert.equal(chat.snapshot().conversation, undefined);
  } finally {
    close();
  }
});
test("silent SSE is bounded and does one canonical refresh without an automatic retry loop", async (t) => {
  let opened = 0,
    cancelled = false;
  const { chat, close } = await setup("mobile", (fixture) => {
    const original = fixture.transport;
    fixture.transport = async (request) => {
      if (request.url.endsWith("/events")) {
        opened++;
        return new Response(
          new ReadableStream({
            cancel() {
              cancelled = true;
            },
          }),
          { headers: { "Content-Type": "text/event-stream" } },
        );
      }
      return original(request);
    };
  });
  try {
    await chat.select(fixtureShared);
    chat.draft("bounded wait");
    t.mock.timers.enable({ apis: ["setTimeout"] });
    const pending = chat.send();
    for (let i = 0; i < 100 && opened === 0; i++)
      await new Promise((resolve) => setImmediate(resolve));
    assert.equal(opened, 1);
    t.mock.timers.tick(30_000);
    await pending;
    assert.equal(cancelled, true);
    assert.equal(chat.snapshot().busy, false);
    assert.equal(chat.snapshot().connection, "disconnected");
    assert.equal(opened, 1);
  } finally {
    close();
  }
});

for (const scenario of ["read_failure", "message_failure"])
  test(`R1-001 ${scenario}: failed canonical read retains prefix with cursor through resume`, async () => {
    const { chat, fixture, close } = await setup();
    fixture.chat.controls.scenario = scenario;
    try {
      await chat.select(fixtureShared);
      chat.draft("preserve prefix");
      await chat.send();
      assert.equal(chat.snapshot().connection, "disconnected");
      assert.ok(chat.snapshot().notice);
      assert.equal(chat.snapshot().events.seq, 2);
      assert.equal(chat.snapshot().events.text, "お手伝いします。");
      let resumedText;
      const unsubscribe = chat.subscribe(() => {
        if (chat.snapshot().events?.seq === 3 && !chat.snapshot().events.terminal)
          resumedText = chat.snapshot().events.text;
      });
      await chat.connect();
      unsubscribe();
      assert.equal(resumedText, "お手伝いします。一緒に進めましょう。");
      assert.deepEqual(
        fixture.chat.controls.requests
          .filter((request) => request.path.endsWith("/events"))
          .map((request) => request.cursor),
        ["0", "2"],
      );
      assert.equal(chat.snapshot().finalVerified, true);
      assert.equal(
        chat.snapshot().messages.filter((message) => message.role === "assistant").length,
        1,
      );
    } finally {
      close();
    }
  });
for (const surface of ["mobile", "display"])
  for (const scenario of ["fast_tools", "fast_unknown", "disconnect_tools"])
    test(`R1-002 ${surface} ${scenario}: terminal worker history recovered without reviving text or character`, async () => {
      const { chat, fixture, close } = await setup(surface);
      fixture.chat.controls.scenario = scenario;
      const terminalSnapshots = [];
      const unsubscribe = chat.subscribe(() => {
        const state = chat.snapshot();
        if (state.run && ["completed", "failed"].includes(state.run.status))
          terminalSnapshots.push({
            status: state.run.status,
            text: state.events?.text,
            character: state.events?.character,
            seq: state.events?.seq,
            tools: state.events?.tools.map((tool) => tool.status),
          });
      });
      try {
        await chat.select(fixtureShared);
        chat.draft("worker is independent");
        await chat.send();
        const state = chat.snapshot();
        assert.equal(state.run.status, scenario === "fast_unknown" ? "failed" : "completed");
        assert.equal(state.events.seq, state.run.last_event_seq);
        assert.deepEqual(
          state.events.tools.map((tool) => tool.status),
          scenario === "fast_unknown" ? ["outcome_unknown"] : ["succeeded", "failed"],
        );
        assert.equal(
          fixture.chat.controls.requests.filter((request) => request.path.endsWith("/events"))
            .length,
          scenario === "disconnect_tools" ? 2 : 1,
        );
        assert.ok(terminalSnapshots.some((snapshot) => snapshot.seq < state.run.last_event_seq));
        assert.ok(
          terminalSnapshots.every(
            (snapshot) =>
              snapshot.text === "" &&
              snapshot.character === (snapshot.status === "failed" ? "error" : "idle"),
          ),
        );
        assert.equal(
          state.messages.filter((message) => message.role === "assistant").length,
          scenario === "fast_unknown" ? 0 : 1,
        );
        await chat.select(fixtureShared);
        assert.deepEqual(
          chat.snapshot().events.tools.map((tool) => tool.status),
          scenario === "fast_unknown" ? ["outcome_unknown"] : ["succeeded", "failed"],
        );
        assert.equal(
          fixture.chat.controls.requests.filter((request) => request.path.endsWith("/turns"))
            .length,
          1,
        );
      } finally {
        unsubscribe();
        close();
      }
    });
