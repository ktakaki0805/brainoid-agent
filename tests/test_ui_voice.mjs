import assert from "node:assert/strict";
import { test } from "node:test";
import { SessionController } from "../packages/ui/src/session.ts";
import { createSessionFixture, tenantA, tenantB } from "../packages/ui/src/session-fixture.ts";
import { ChatController } from "../packages/ui/src/chat/controller.ts";
import { fixtureShared, fixturePrivate } from "../packages/ui/src/chat/fixture.ts";
import { conversationPhase } from "../packages/ui/src/voice/controller.ts";
import { DeviceVoice } from "../apps/display/voice/controller.ts";
import { DeviceSessionController } from "../apps/display/session/controller.ts";
import { LocalSessionClient } from "../apps/display/session/client.ts";
const tick = () => new Promise((r) => setTimeout(r, 5));
const deferred = () => {
  let resolve;
  const promise = new Promise((r) => {
    resolve = r;
  });
  return { promise, resolve };
};
async function until(check) {
  for (let i = 0; i < 300; i++) {
    if (check()) return;
    await tick();
  }
  throw Error("Timed out");
}
function audioFixture() {
  const leases = [],
    controls = {};
  const factory = (_session, binding, changed) => {
    const calls = [],
      lease = {
        binding,
        calls,
        async start() {
          calls.push("start");
          changed({
            phase: controls.denied ? "closed" : "recording",
            ...(controls.denied ? { issue: "permission" } : {}),
          });
        },
        async finish() {
          calls.push("finish");
          changed({
            phase: controls.silent ? "closed" : "ready",
            ...(controls.silent ? { issue: "invalid-audio" } : {}),
          });
        },
        async upload() {
          calls.push("upload");
          changed({ phase: "uploading" });
          if (controls.upload) await controls.upload;
          if (controls.fail) {
            changed({ phase: "closed", issue: "network" });
            return;
          }
          changed({ phase: "idle" });
          return "60000000-0000-4000-8000-000000000001";
        },
        enqueue(sentence) {
          calls.push(sentence);
          changed({ phase: "playing" });
        },
        async close() {
          calls.push("close");
        },
      };
    leases.push(lease);
    return lease;
  };
  return { factory, controls, leases };
}
async function setup(change = () => {}) {
  const fixture = createSessionFixture(),
    audio = audioFixture();
  fixture.chat.controls.delay = 10;
  const requests = [],
    gates = {};
  const session = new SessionController({
    auth: fixture.auth,
    baseUrl: "https://session.invalid",
    transport: async (input, init) => {
      const req = input instanceof Request ? input : new Request(input, init);
      if (req.url.endsWith("/turns")) {
        requests.push(await req.clone().json());
        if (gates.turn) await gates.turn;
      }
      if (gates.command && req.url.includes("/commands/")) return gates.command(req);
      return fixture.transport(req);
    },
  });
  const stop = session.start();
  await tick();
  await session.signIn("synthetic@example.test", "fixture");
  await tick();
  await session.selectTenant(tenantA);
  const chat = new ChatController(session.snapshot().context, "mobile", audio.factory);
  await chat.load();
  await chat.select(fixtureShared);
  change({ fixture, audio, chat, gates });
  return {
    fixture,
    audio,
    chat,
    session,
    requests,
    gates,
    close() {
      chat.dispose();
      stop();
    },
  };
}
test("T006 state names derive Speaking from playback; independent flags do not replace phase", () => {
  for (const [phase, run, expected] of [
    ["idle", undefined, "Idle"],
    ["recording", undefined, "Listening"],
    ["uploading", undefined, "Transcribing"],
    ["idle", "running", "Thinking"],
    ["idle", "waiting_confirmation", "Confirming"],
    ["playing", "completed", "Speaking"],
  ])
    assert.equal(conversationPhase({ phase, muted: true, offline: true }, run), expected);
  assert.equal(conversationPhase({ phase: "closed", issue: "network" }), "Error");
});
test("record finish does not upload; send binds audio_input_id/speech and one live sentence", async () => {
  const f = await setup();
  try {
    await f.chat.record();
    assert.equal(f.chat.snapshot().voice.phase, "recording");
    await f.chat.voice.finish();
    assert.deepEqual(f.audio.leases[0].calls, ["start", "finish"]);
    await f.chat.sendRecording();
    assert.deepEqual(f.requests[0], {
      audio_input_id: "60000000-0000-4000-8000-000000000001",
      output_mode: "speech",
    });
    assert.equal(f.audio.leases[0].calls.filter((x) => typeof x === "object").length, 1);
    assert.equal(f.chat.snapshot().voice.phase, "playing");
    f.chat.voice.stop("読み上げ停止");
    assert.equal(f.chat.snapshot().run.status, "completed");
    assert.equal(f.chat.snapshot().voice.phase, "idle");
  } finally {
    f.close();
  }
});
for (const scenario of ["denied", "silent", "fail"])
  test(`T006 ${scenario} preserves text editing and a usable text send`, async () => {
    const f = await setup();
    try {
      f.audio.controls[scenario] = true;
      await f.chat.record();
      if (scenario !== "denied") await f.chat.voice.finish();
      if (scenario === "fail") await f.chat.sendRecording();
      assert.match(f.chat.snapshot().voice.notice, /文字/);
      f.chat.draft("文字から継続");
      await f.chat.send();
      assert.equal(f.requests.at(-1).output_mode, "text");
      assert.equal(f.chat.snapshot().run.status, "completed");
    } finally {
      f.close();
    }
  });
for (const action of ["discard", "offline", "mute", "select", "tenant", "signout", "suspend"])
  test(`T021 delayed STT cannot create turn after ${action}`, async () => {
    const f = await setup(),
      gate = deferred();
    try {
      f.audio.controls.upload = gate.promise;
      await f.chat.record();
      await f.chat.voice.finish();
      const sending = f.chat.sendRecording();
      await tick();
      if (action === "discard") f.chat.voice.stop();
      if (action === "offline") f.chat.voice.network(true);
      if (action === "mute") f.chat.voice.mute();
      if (action === "select") await f.chat.select(fixturePrivate);
      if (action === "tenant") await f.session.selectTenant(tenantB);
      if (action === "signout") await f.session.signOut();
      if (action === "suspend") f.session.suspend();
      gate.resolve();
      await sending;
      assert.equal(f.requests.length, 0);
    } finally {
      f.close();
    }
  });
test("stop during create receipt keeps accepted text run without restoring speech", async () => {
  const f = await setup(),
    gate = deferred();
  try {
    f.gates.turn = gate.promise;
    await f.chat.record();
    await f.chat.voice.finish();
    const sending = f.chat.sendRecording();
    await until(() => f.requests.length);
    f.chat.voice.stop();
    gate.resolve();
    await sending;
    assert.equal(f.chat.snapshot().run.status, "completed");
    assert.equal(f.audio.leases[0].calls.filter((x) => typeof x === "object").length, 0);
  } finally {
    f.close();
  }
});
test("reconnect/history never reuses a mobile audio intention", async () => {
  const f = await setup(({ fixture }) => {
    fixture.chat.controls.scenario = "reconnect";
  });
  try {
    await f.chat.record();
    await f.chat.voice.finish();
    await f.chat.sendRecording();
    const count = f.audio.leases[0].calls.filter((x) => typeof x === "object").length;
    await f.chat.connect();
    await f.chat.select(fixtureShared);
    assert.equal(f.audio.leases[0].calls.filter((x) => typeof x === "object").length, count);
  } finally {
    f.close();
  }
});
test("Pi stop while prior stop is pending fences delayed record and waiting taps", async () => {
  const gate = deferred(),
    calls = [];
  let initial = true;
  const voice = new DeviceVoice(
    {
      audio: async (_signal, body) => {
        calls.push(body.operation);
        if (initial) {
          initial = false;
          await gate.promise;
        }
        return { stopped: true, run_cancelled: false };
      },
      waitForSpeech: async () => {
        calls.push("wait");
        return { waiting: true };
      },
    },
    () => {},
  );
  const stopping = voice.stop(),
    record = voice.operation("start");
  await tick();
  const newer = voice.stop();
  gate.resolve();
  await Promise.all([stopping, record, newer]);
  assert.equal(calls.includes("start"), false);
  voice.dispose();
});
test("Pi audio denial leaves actual DeviceSession text composer and typed turn usable", async () => {
  const tenant = tenantA,
    device = "20000000-0000-4000-8000-000000000002",
    run = "40000000-0000-4000-8000-000000000003",
    calls = [];
  const controller = new DeviceSessionController({
    audio: async (_signal, body) => {
      if (body?.operation === "stop") return { stopped: true, run_cancelled: false };
      throw Error("disabled");
    },
    snapshot: async () => ({
      ready: true,
      play_speech: false,
      snapshot: {
        device_id: device,
        tenant_id: tenant,
        connection_generation: 1,
        snapshot_id: "a".repeat(64),
        active_private_session: null,
        assigned_runs: [],
        pending_command_ids: [],
        pending_notification_ids: [],
        delivery_cursor: 0,
        next_cursor: null,
      },
    }),
    operation: async (body) => {
      calls.push(body);
      if (body.operation === "list_conversations")
        return {
          items: [
            {
              id: fixtureShared,
              title: "共有",
              tenant_id: tenant,
              visibility: "shared",
              display_policy: "shared_display",
            },
          ],
          next_cursor: null,
        };
      if (body.operation === "create_turn") return { run_id: run, status: "queued" };
      if (body.operation === "get_conversation")
        return {
          id: fixtureShared,
          tenant_id: tenant,
          visibility: "shared",
          display_policy: "shared_display",
        };
      return { run_id: run, conversation_id: fixtureShared, status: "cancelled" };
    },
  });
  try {
    await controller.refresh();
    await controller.voice.refresh();
    await controller.conversations();
    controller.draft("音声なしの文字入力");
    await controller.send();
    assert.equal(calls.find((c) => c.operation === "create_turn").turn.output_mode, "text");
    assert.equal(controller.snapshot().voice.available, false);
  } finally {
    controller.dispose();
  }
});

test("T007 command cancellation reads the actual claim-winning result and never cancels the run", async () => {
  const f = await setup(({ fixture }) => {
    fixture.chat.controls.scenario = "tools";
    fixture.chat.controls.commandId = "90000000-0000-4000-8000-000000000001";
  });
  try {
    const id = f.fixture.chat.controls.commandId;
    let status = "queued",
      cancelled = 0;
    f.gates.command = async (req) => {
      if (req.method === "POST") {
        cancelled++;
        status = "running";
        return Response.json({}, { status: 409 });
      }
      return Response.json({ id, tenant_id: tenantA, status });
    };
    f.chat.draft("操作の合成fixture");
    await f.chat.send();
    // The pending tool event records the command ID even when a later tool event omits it.
    assert.equal(f.chat.snapshot().events.tools[0].commandId, id);
    await f.chat.cancelCommand(id);
    assert.equal(cancelled, 1);
    assert.match(f.chat.snapshot().commandResults[id], /running/);
    assert.equal(f.chat.snapshot().run.status, "completed");
  } finally {
    f.close();
  }
});

test("Pi a stale status poll cannot replace a newer recording state", async () => {
  const gate = deferred();
  let polls = 0;
  const voice = new DeviceVoice(
    {
      audio: async (_signal, body) => {
        if (body?.operation === "stop") return { stopped: true, run_cancelled: false };
        if (body?.operation === "start") return {};
        if (++polls === 1) return gate.promise;
        return { enabled: true, capture: "recording", playback: "idle", muted: false };
      },
    },
    () => {},
  );
  const old = voice.refresh();
  await voice.operation("start");
  gate.resolve({ enabled: true, capture: "idle", playback: "idle", muted: false });
  await old;
  assert.equal(voice.state.phase, "recording");
  voice.dispose();
});

function piVoiceFixture() {
  const a = fixtureShared,
    b = "30000000-0000-4000-8000-000000000099";
  const run = "40000000-0000-4000-8000-000000000099",
    calls = [];
  const controls = {
    waiting: false,
    playback: "idle",
    capture: "idle",
    assigned: [],
    online: true,
  };
  const client = {
    audio: async (_signal, body) => {
      if (!body && controls.statusFailures > 0) {
        controls.statusFailures--;
        throw Error("Temporary audio status failure");
      }
      if (!body)
        return {
          enabled: true,
          capture: controls.capture,
          playback: controls.playback,
          muted: false,
          waiting: controls.waiting,
        };
      calls.push(body.operation);
      if (body.operation === "stop") {
        if (controls.stopFails) throw Error("Lost stop");
        controls.waiting = false;
        controls.capture = controls.playback = "idle";
        return { stopped: true, run_cancelled: false };
      }
      if (body.operation === "start") controls.capture = "recording";
      return {};
    },
    waitForSpeech: async () => {
      controls.waiting = true;
      return { waiting: true };
    },
    snapshot: async () => {
      if (!controls.online) throw Error("Offline");
      return {
        ready: true,
        play_speech: false,
        snapshot: {
          device_id: "20000000-0000-4000-8000-000000000099",
          tenant_id: tenantA,
          connection_generation: 1,
          snapshot_id: "a".repeat(64),
          active_private_session: null,
          assigned_runs: controls.assigned,
          pending_command_ids: [],
          pending_notification_ids: [],
          delivery_cursor: 0,
          next_cursor: null,
        },
      };
    },
    operation: async (body) => {
      calls.push(body.operation);
      if (body.operation === "list_conversations")
        return {
          items: [a, b].map((id) => ({
            id,
            title: id,
            tenant_id: tenantA,
            visibility: "shared",
            display_policy: "shared_display",
          })),
          next_cursor: null,
        };
      if (body.operation === "create_turn") return controls.create(body);
      if (body.operation === "get_run")
        return { run_id: run, conversation_id: b, status: "cancelled", last_event_seq: 0 };
      if (body.operation === "get_conversation")
        return {
          id: b,
          tenant_id: tenantA,
          visibility: "shared",
          display_policy: "shared_display",
        };
      throw Error("Unexpected operation");
    },
    events: async (_id, _session, _signal, _consume, mode) => {
      calls.push(`events:${mode}`);
    },
  };
  return { a, b, run, calls, controls, client, controller: new DeviceSessionController(client) };
}
for (const outcome of ["success", "failure"])
  for (const selection of ["conversation", "run", "offline"])
    test(`R1-01 ${selection} invalidates busy; late ${outcome} cannot clear a newer send`, async () => {
      const f = piVoiceFixture(),
        old = deferred(),
        fresh = deferred();
      f.controls.create = async (body) => {
        if (body.turn.text === "old") {
          await old.promise;
          if (outcome === "failure") throw Error("Old failure");
          return { run_id: "40000000-0000-4000-8000-000000000001", status: "queued" };
        }
        await fresh.promise;
        return { run_id: f.run, status: "queued" };
      };
      try {
        await f.controller.refresh();
        await f.controller.conversations();
        f.controller.draft("old");
        const sending = f.controller.send();
        await until(() => f.controller.snapshot().busy);
        if (selection === "conversation") f.controller.chooseConversation(f.b);
        if (selection === "run") {
          f.controls.assigned = [
            {
              run_id: f.run,
              conversation_id: f.b,
              output_mode: "text",
              assignment_generation: 1,
              last_event_seq: 0,
            },
          ];
          await f.controller.refresh();
          await f.controller.select(f.run);
        }
        if (selection === "offline") {
          f.controls.online = false;
          await f.controller.refresh();
          f.controls.online = true;
          await f.controller.refresh();
          f.controller.chooseConversation(f.b);
        }
        assert.equal(f.controller.snapshot().busy, false);
        await f.controller.record();
        assert.equal(f.controller.snapshot().voice.phase, "recording");
        f.controller.draft("new");
        const sendingNew = f.controller.send();
        await until(() => f.controller.snapshot().busy);
        old.resolve();
        await sending;
        assert.equal(f.controller.snapshot().busy, true);
        assert.equal(f.controller.snapshot().conversation, f.b);
        fresh.resolve();
        await sendingNew;
        await until(() => !f.controller.snapshot().busy);
        assert.equal(f.controller.snapshot().run, f.run);
      } finally {
        old.resolve();
        fresh.resolve();
        f.controller.dispose();
      }
    });

test("R1-02 consumed and expired wait notices end without erasing an unconfirmed stop", async () => {
  const f = piVoiceFixture(),
    voice = f.controller.voice;
  try {
    await voice.wait();
    assert.match(voice.state.notice, /60秒間/);
    f.controls.waiting = false;
    f.controls.playback = "playing";
    await voice.refresh();
    assert.equal(voice.state.phase, "playing");
    assert.equal(voice.state.notice, "");
    f.controls.playback = "idle";
    await voice.refresh();
    assert.equal(voice.state.notice, "");
    await voice.wait();
    f.controls.waiting = false;
    await voice.refresh();
    assert.equal(voice.state.notice, "");
    f.controls.stopFails = true;
    await voice.stop();
    const notice = voice.state.notice;
    await voice.refresh();
    assert.match(notice, /停止は未確認/);
    assert.equal(voice.state.notice, notice);
  } finally {
    f.controller.dispose();
  }
});
for (const stopFails of [false, true])
  test(`R1-03 lost wait acknowledgement cannot revive from polling; stop failure=${stopFails}`, async () => {
    const f = piVoiceFixture();
    f.client.waitForSpeech = async () => {
      f.controls.waiting = true;
      f.controls.stopFails = stopFails;
      throw Error("Accepted, response lost");
    };
    try {
      await f.controller.refresh();
      await f.controller.voice.wait();
      assert.equal(f.calls.filter((c) => c === "stop").length, 3); // initial context, pre-wait and failed-wait invalidation
      f.controls.waiting = true; // Even a late daemon baseline completion cannot re-arm this screen.
      await f.controller.voice.refresh();
      assert.equal(f.controller.voice.state.waiting, false);
      f.controls.assigned = [
        {
          run_id: f.run,
          conversation_id: f.b,
          output_mode: "speech",
          assignment_generation: 1,
          last_event_seq: 0,
        },
      ];
      await f.controller.refresh();
      await tick();
      assert.equal(f.calls.includes("events:waiting"), false);
      f.controls.stopFails = false;
      f.client.waitForSpeech = async () => {
        f.controls.waiting = true;
        return { waiting: true };
      };
      await f.controller.voice.wait();
      await f.controller.voice.refresh();
      assert.equal(f.controller.voice.state.waiting, true);
    } finally {
      f.controller.dispose();
    }
  });
test("R1-03 late failed baseline cannot stop a newer acknowledged wait; old polls cannot erase it", async () => {
  const f = piVoiceFixture(),
    late = deferred(),
    poll = deferred();
  let first = true;
  f.client.waitForSpeech = async () => {
    if (first) {
      first = false;
      await late.promise;
      throw Error("Old timeout");
    }
    f.controls.waiting = true;
    return { waiting: true };
  };
  try {
    const old = f.controller.voice.wait();
    await until(() => !first);
    await f.controller.voice.wait();
    const generation = f.controller.voice.generation;
    const stops = f.calls.filter((c) => c === "stop").length;
    late.resolve();
    await old;
    assert.equal(f.controller.voice.generation, generation);
    assert.equal(f.calls.filter((c) => c === "stop").length, stops);
    assert.equal(f.controller.voice.state.waiting, true);
    const original = f.client.audio;
    f.client.audio = async (signal, body) => (body ? original(signal, body) : poll.promise);
    const polling = f.controller.voice.refresh();
    await f.controller.voice.wait();
    poll.resolve({
      enabled: true,
      capture: "idle",
      playback: "idle",
      muted: false,
      waiting: false,
    });
    await polling;
    assert.equal(f.controller.voice.state.waiting, true);
  } finally {
    late.resolve();
    poll.resolve({});
    f.controller.dispose();
  }
});

test("R1-03 a poll issued during baseline preparation cannot overwrite its later success", async () => {
  const f = piVoiceFixture(),
    baseline = deferred(),
    poll = deferred();
  let entered = false;
  f.client.waitForSpeech = async () => {
    entered = true;
    await baseline.promise;
    return { waiting: true };
  };
  const original = f.client.audio;
  f.client.audio = async (signal, body) => (body ? original(signal, body) : poll.promise);
  try {
    const waiting = f.controller.voice.wait();
    await until(() => entered);
    const polling = f.controller.voice.refresh();
    baseline.resolve();
    await waiting;
    poll.resolve({
      enabled: true,
      capture: "idle",
      playback: "idle",
      muted: false,
      waiting: false,
    });
    await polling;
    assert.equal(f.controller.voice.state.waiting, true);
    assert.match(f.controller.voice.state.notice, /60秒間/);
  } finally {
    baseline.resolve();
    poll.resolve({});
    f.controller.dispose();
  }
});

for (const stage of ["get_conversation", "get_run"])
  for (const outcome of ["success", "failure"])
    for (const action of ["stop", "record", "wait"])
      test(`R2-01 ${stage} late ${outcome} cannot POST future after ${action}`, async () => {
        const f = piVoiceFixture(),
          gate = deferred();
        let hold = false,
          entered = false;
        const original = f.client.operation;
        f.controls.assigned = [
          {
            run_id: f.run,
            conversation_id: f.b,
            output_mode: "speech",
            assignment_generation: 1,
            last_event_seq: 0,
          },
        ];
        f.client.operation = async (body) => {
          if (hold && body.operation === stage) {
            entered = true;
            await gate.promise;
            if (outcome === "failure") throw Error("Old read failed");
          }
          if (body.operation === "get_run")
            return { run_id: f.run, conversation_id: f.b, status: "running", last_event_seq: 0 };
          return original(body);
        };
        f.client.events = async (_id, _session, _signal, consume, mode) => {
          f.calls.push(`events:${mode}`);
          for (const [type, payload, seq] of [
            ["run.started", {}, 1],
            ["text.delta", { text: "文字を復元" }, 2],
          ])
            await consume("run", {
              run_id: f.run,
              seq,
              type,
              occurred_at: new Date().toISOString(),
              payload,
            });
        };
        try {
          await f.controller.refresh();
          await until(() => f.calls.includes("events:undefined"));
          hold = true;
          const selecting = f.controller.select(f.run, "future");
          await until(() => entered);
          if (action === "stop") await f.controller.voice.stop();
          if (action === "record") await f.controller.voice.operation("start");
          if (action === "wait") await f.controller.voice.wait();
          const generation = f.controller.voice.generation,
            stops = f.calls.filter((c) => c === "stop").length;
          gate.resolve();
          await selecting;
          assert.equal(f.calls.includes("events:future"), false);
          assert.equal(f.controller.voice.generation, generation);
          assert.equal(f.calls.filter((c) => c === "stop").length, stops);
          if (action === "record") assert.equal(f.controller.voice.state.phase, "recording");
          if (action === "wait") assert.equal(f.controller.voice.state.waiting, true);
          hold = false;
          await f.controller.select(f.run);
          assert.equal(f.controller.snapshot().provisional, "文字を復元");
        } finally {
          gate.resolve();
          f.controller.dispose();
        }
      });

test("R2-01 stopping while an older stream closes fences the future tap before canonical reads", async () => {
  const f = piVoiceFixture(),
    previous = deferred();
  let reads = 0;
  f.controls.assigned = [
    {
      run_id: f.run,
      conversation_id: f.b,
      output_mode: "speech",
      assignment_generation: 1,
      last_event_seq: 0,
    },
  ];
  const operation = f.client.operation;
  f.client.operation = async (body) => {
    reads++;
    if (body.operation === "get_run")
      return { run_id: f.run, conversation_id: f.b, status: "running", last_event_seq: 0 };
    return operation(body);
  };
  f.client.events = async (_id, _session, _signal, _consume, mode) => {
    f.calls.push(`events:${mode}`);
    await previous.promise;
  };
  try {
    await f.controller.refresh();
    await until(() => f.calls.includes("events:undefined"));
    const selecting = f.controller.select(f.run, "future");
    await tick();
    await f.controller.voice.stop();
    const atStop = reads;
    previous.resolve();
    await selecting;
    assert.equal(reads, atStop);
    assert.equal(f.calls.includes("events:future"), false);
  } finally {
    previous.resolve();
    f.controller.dispose();
  }
});

test("R2-01 a stopped explicit stream cannot POST after delayed local session bootstrap", async () => {
  const gate = deferred(),
    calls = [];
  const client = new LocalSessionClient(async (path, init) => {
    calls.push({ path, method: init.method });
    if (path === "/local/session") {
      await gate.promise;
      return Response.json({ csrf_token: "synthetic" });
    }
    if (path === "/local/audio") return Response.json({ stopped: true, run_cancelled: false });
    return new Response("", { headers: { "Content-Type": "text/event-stream" } });
  });
  const voice = new DeviceVoice(client, () => {});
  const captured = voice.capture();
  const streaming = client.events(
    "40000000-0000-4000-8000-000000000001",
    undefined,
    captured.signal,
    () => {},
    "future",
  );
  const rejected = assert.rejects(streaming, { name: "AbortError" });
  await until(() => calls.length);
  const stopping = voice.stop();
  gate.resolve();
  await Promise.all([stopping, rejected]);
  assert.equal(
    calls.some((c) => c.path.endsWith("/events")),
    false,
  );
  // A subsequent text-only stream is independent of the retired audio signal.
  await client.events(
    "40000000-0000-4000-8000-000000000001",
    undefined,
    new AbortController().signal,
    () => {},
  );
  assert.equal(calls.at(-1).method, "GET");
  voice.dispose();
});

function candidateFixture() {
  const f = piVoiceFixture();
  const old = "40000000-0000-4000-8000-000000000071",
    fresh = "40000000-0000-4000-8000-000000000072",
    next = "40000000-0000-4000-8000-000000000073";
  const attempts = [],
    reads = [],
    accepted = [];
  const row = (id) => ({
    run_id: id,
    conversation_id: f.b,
    output_mode: "speech",
    assignment_generation: 1,
    last_event_seq: 0,
  });
  const original = f.client.operation,
    snapshot = f.client.snapshot;
  f.client.snapshot = async () => {
    const value = await snapshot();
    if (f.controls.session) value.snapshot.active_private_session = f.controls.session;
    return value;
  };
  f.client.operation = async (body) => {
    if (body.operation === "get_conversation")
      return {
        id: body.conversation_id,
        tenant_id: tenantA,
        visibility: "shared",
        display_policy: "shared_display",
      };
    if (body.operation === "get_run") {
      reads.push(body.run_id);
      if (f.controls.readFailures) {
        f.controls.readFailures--;
        throw Error("Temporary canonical failure");
      }
      return {
        run_id: body.run_id,
        conversation_id: f.b,
        status: accepted.includes(body.run_id) ? "completed" : "running",
        final_message_id: "50000000-0000-4000-8000-000000000071",
        last_event_seq: accepted.includes(body.run_id) ? 2 : 0,
      };
    }
    if (body.operation === "list_messages")
      return {
        items: accepted.map((run_id) => ({
          id: "50000000-0000-4000-8000-000000000071",
          role: "assistant",
          run_id,
          text: "受理した音声の最終回答",
        })),
        next_cursor: null,
      };
    return original(body);
  };
  f.client.events = async (id, _session, _signal, consume, mode) => {
    if (mode !== "waiting") return;
    attempts.push(id);
    if (id === old) {
      if (f.controls.firstGate) await f.controls.firstGate;
      await consume("clear", { reason: "stream_stopped" });
      return;
    }
    if (f.controls.postFails) throw Error("Unconfirmed POST");
    assert.equal(f.controls.waiting, true, "daemon single-use gate");
    f.controls.waiting = false;
    f.controls.playback = "playing";
    accepted.push(id);
    await consume("run", {
      run_id: id,
      seq: 1,
      type: "run.started",
      occurred_at: new Date().toISOString(),
      payload: {},
    });
    await consume("run", {
      run_id: id,
      seq: 2,
      type: "run.completed",
      occurred_at: new Date().toISOString(),
      payload: { message_id: "50000000-0000-4000-8000-000000000071" },
    });
  };
  return {
    ...f,
    old,
    fresh,
    next,
    row,
    attempts,
    reads,
    accepted,
    async poll() {
      await f.controller.refresh();
      await tick();
    },
  };
}
for (const order of ["old-first", "fresh-first"])
  test(`R3-01 mixed ${order} candidates progress once despite reordered repeated snapshots`, async () => {
    const f = candidateFixture();
    try {
      await f.poll();
      await f.controller.voice.wait();
      f.controls.assigned = (
        order === "old-first" ? [f.old, f.fresh, f.next] : [f.fresh, f.old, f.next]
      ).map(f.row);
      await f.poll();
      f.controls.assigned.reverse();
      for (let i = 0; i < 4; i++) await f.poll();
      assert.deepEqual(f.accepted, [f.fresh]);
      assert.deepEqual(f.attempts, order === "old-first" ? [f.old, f.fresh] : [f.fresh]);
      assert.equal(f.controller.voice.state.waiting, false);
      assert.equal(f.controller.voice.state.phase, "playing");
    } finally {
      f.controller.dispose();
    }
  });

test("R3-01 arrivals while the first candidate is pending remain queued; no parallel attempt", async () => {
  const f = candidateFixture(),
    gate = deferred();
  f.controls.firstGate = gate.promise;
  try {
    await f.poll();
    await f.controller.voice.wait();
    f.controls.assigned = [f.old].map(f.row);
    await f.poll();
    f.controls.assigned = [f.old, f.fresh, f.next].map(f.row);
    await f.poll();
    await f.poll();
    assert.deepEqual(f.attempts, [f.old]);
    gate.resolve();
    await tick();
    await f.poll();
    await f.poll();
    assert.deepEqual(f.attempts, [f.old, f.fresh]);
    assert.deepEqual(f.accepted, [f.fresh]);
  } finally {
    gate.resolve();
    f.controller.dispose();
  }
});
for (const action of ["stop", "new-wait", "select", "offline", "expiry", "session", "remove"])
  test(`R3-01 ${action} clears pending candidates and old completion cannot affect a new wait`, async () => {
    const f = candidateFixture(),
      gate = deferred();
    f.controls.firstGate = gate.promise;
    try {
      await f.poll();
      await f.controller.conversations();
      await f.controller.voice.wait();
      f.controls.assigned = [f.old, f.fresh].map(f.row);
      await f.poll();
      assert.deepEqual(f.attempts, [f.old]);
      if (action === "stop") await f.controller.voice.stop();
      if (action === "new-wait") await f.controller.voice.wait();
      if (action === "select") f.controller.chooseConversation(f.b);
      if (action === "offline") {
        f.controls.online = false;
        await f.poll();
        f.controls.online = true;
      }
      if (action === "expiry") {
        f.controls.waiting = false;
        await f.controller.voice.refresh();
      }
      if (action === "remove") {
        f.controls.assigned = [f.fresh].map(f.row);
        await f.poll();
      }
      if (action === "session") {
        const now = Date.now();
        f.controls.session = {
          id: "70000000-0000-4000-8000-000000000001",
          tenant_id: tenantA,
          device_id: "20000000-0000-4000-8000-000000000099",
          user_id: "synthetic-user",
          version: 1,
          created_at: new Date(now).toISOString(),
          updated_at: new Date(now).toISOString(),
          expires_at: new Date(now + 300_000).toISOString(),
          idle_expires_at: new Date(now + 60_000).toISOString(),
          status: "active",
          allowed_scopes: ["conversation"],
        };
        await f.poll();
      }
      await f.poll();
      const generation = f.controller.voice.generation;
      gate.resolve();
      await tick();
      await f.poll();
      if (action === "new-wait") assert.equal(f.controller.voice.generation, generation);
      assert.deepEqual(f.accepted, []);
      assert.deepEqual(f.attempts, [f.old]);
      if (action === "new-wait") {
        assert.equal(f.controller.voice.state.waiting, true);
        f.controls.assigned.push(f.row(f.next));
        await f.poll();
        await f.poll();
        assert.deepEqual(f.accepted, [f.next]);
      }
    } finally {
      gate.resolve();
      f.controller.dispose();
    }
  });
for (const failures of [1, 2])
  test(`R3-01 canonical failure retries are bounded (${failures}) and leave actionable guidance`, async () => {
    const f = candidateFixture();
    try {
      await f.poll();
      await f.controller.voice.wait();
      f.controls.readFailures = failures;
      f.controls.assigned = [f.fresh].map(f.row);
      for (let i = 0; i < 5; i++) await f.poll();
      assert.equal(f.reads.length, failures === 1 ? 3 : 2);
      assert.deepEqual(f.accepted, failures === 1 ? [f.fresh] : []);
      if (failures === 2) assert.match(f.controller.voice.state.notice, /文字.*待機を開始し直/);
    } finally {
      f.controller.dispose();
    }
  });
test("R3-01 an unconfirmed POST is not replayed and its recovery guidance survives polls", async () => {
  const f = candidateFixture();
  try {
    await f.poll();
    await f.controller.voice.wait();
    f.controls.postFails = true;
    f.controls.assigned = [f.fresh].map(f.row);
    for (let i = 0; i < 4; i++) await f.poll();
    assert.deepEqual(f.attempts, [f.fresh]);
    assert.deepEqual(f.accepted, []);
    assert.match(f.controller.voice.state.notice, /文字.*待機を開始し直/);
  } finally {
    f.controller.dispose();
  }
});

for (const stage of ["before-collection", "active", "rejected", "post-uncertain"])
  for (const stopFails of [false, true])
    test(`R4-01 ${stage} status failure retires wait and candidates (stop fails: ${stopFails})`, async () => {
      const f = candidateFixture(),
        gate = deferred();
      try {
        await f.poll();
        await f.controller.voice.wait();
        f.controls.stopFails = stopFails;
        f.controls.assigned = [f.old, f.fresh].map(f.row);
        if (stage === "active") f.controls.firstGate = gate.promise;
        if (stage === "post-uncertain") {
          f.controls.assigned = [f.fresh].map(f.row);
          f.controls.postFails = true;
        }
        if (stage === "rejected" || stage === "post-uncertain") f.controls.statusFailures = 1;
        if (stage !== "before-collection") await f.poll();
        if (stage === "before-collection" || stage === "active") {
          f.controls.statusFailures = 1;
          await f.controller.voice.refresh();
        }
        assert.equal(f.controller.voice.state.waiting, false);
        assert.equal(f.controls.waiting, stopFails);
        const notice = f.controller.voice.state.notice;
        assert.match(notice, stopFails ? /停止は未確認/ : /文字入力.*待機を開始し直/);
        // Repeated failures, then GET success, cannot resurrect the old daemon wait.
        f.controls.statusFailures = 2;
        await f.controller.voice.refresh();
        await f.controller.voice.refresh();
        await f.controller.voice.refresh();
        gate.resolve();
        for (let i = 0; i < 3; i++) await f.poll();
        assert.equal(f.controller.voice.state.waiting, false);
        assert.deepEqual(f.accepted, []);
        assert.deepEqual(
          f.attempts,
          stage === "before-collection" ? [] : [stage === "post-uncertain" ? f.fresh : f.old],
        );
        assert.equal(
          f.controller.voice.state.notice,
          notice,
          "text restoration retains recovery guidance",
        );
        f.controls.stopFails = f.controls.postFails = false;
        await f.controller.voice.wait();
        await f.poll();
        assert.deepEqual(f.accepted, [], "new wait does not replay baseline candidates");
        f.controls.assigned.push(f.row(f.next));
        await f.poll();
        assert.deepEqual(f.accepted, [f.next]);
        assert.doesNotMatch(f.controller.voice.state.notice, /未確認|開始し直/);
      } finally {
        gate.resolve();
        f.controller.dispose();
      }
    });

for (const confirmed of [false, true])
  test(`R4-01 status failure after daemon consumption (observed: ${confirmed}) never attaches another candidate`, async () => {
    const f = candidateFixture();
    try {
      await f.poll();
      await f.controller.voice.wait();
      f.controls.assigned = [f.fresh, f.next].map(f.row);
      if (!confirmed) f.controls.statusFailures = 1;
      await f.poll();
      const stops = f.calls.filter((call) => call === "stop").length;
      if (confirmed) {
        assert.equal(f.controller.voice.state.phase, "playing");
        f.controls.statusFailures = 1;
        await f.controller.voice.refresh();
        assert.equal(f.calls.filter((call) => call === "stop").length, stops);
      }
      await f.controller.voice.refresh();
      for (let i = 0; i < 3; i++) await f.poll();
      assert.equal(f.controller.voice.state.waiting, false);
      assert.deepEqual(f.accepted, [f.fresh]);
      assert.deepEqual(f.attempts, [f.fresh]);
      assert.equal(f.controls.playback, confirmed ? "playing" : "idle");
    } finally {
      f.controller.dispose();
    }
  });

for (const outcome of ["success", "failure"])
  for (const action of ["stop", "wait", "record", "offline", "session", "expiry"])
    test(`R4-01 old status ${outcome} after ${action} cannot restore or stop another wait`, async () => {
      const f = candidateFixture(),
        gate = deferred();
      try {
        await f.poll();
        await f.controller.voice.wait();
        const audio = f.client.audio;
        let held = false;
        f.client.audio = async (signal, body) => {
          if (!body && !held) {
            held = true;
            await gate.promise;
            if (outcome === "failure") throw Error("Old failed poll");
            return {
              enabled: true,
              capture: "idle",
              playback: "idle",
              muted: false,
              waiting: true,
            };
          }
          return audio(signal, body);
        };
        const old = f.controller.voice.refresh();
        if (action === "stop") await f.controller.voice.stop();
        if (action === "wait") await f.controller.voice.wait();
        if (action === "record") await f.controller.voice.operation("start");
        if (action === "offline") {
          f.controls.online = false;
          await f.poll();
        }
        if (action === "session") {
          const now = Date.now();
          f.controls.session = {
            id: "70000000-0000-4000-8000-000000000001",
            tenant_id: tenantA,
            device_id: "20000000-0000-4000-8000-000000000099",
            user_id: "synthetic-user",
            version: 1,
            created_at: new Date(now).toISOString(),
            updated_at: new Date(now).toISOString(),
            expires_at: new Date(now + 300_000).toISOString(),
            idle_expires_at: new Date(now + 60_000).toISOString(),
            status: "active",
            allowed_scopes: ["conversation"],
          };
          await f.poll();
        }
        if (action === "expiry") {
          f.controls.waiting = false;
          await f.controller.voice.refresh();
        }
        const generation = f.controller.voice.generation;
        const stops = f.calls.filter((call) => call === "stop").length;
        gate.resolve();
        await old;
        assert.equal(f.controller.voice.generation, generation);
        assert.equal(f.calls.filter((call) => call === "stop").length, stops);
        assert.equal(f.controller.voice.state.waiting, action === "wait");
        if (action === "record") assert.equal(f.controller.voice.state.phase, "recording");
      } finally {
        gate.resolve();
        f.controller.dispose();
      }
    });

test("R4-01 failed status stop and diagnostics cannot overwrite a newer wait", async () => {
  const f = candidateFixture(),
    stopping = deferred(),
    diagnostics = deferred();
  try {
    await f.poll();
    await f.controller.voice.wait();
    const audio = f.client.audio;
    let holdStop = true,
      diagnosisStarted = false;
    f.client.audio = async (signal, body) => {
      if (body?.operation === "stop" && holdStop) {
        holdStop = false;
        await stopping.promise;
        throw Error("Stop response lost");
      }
      return audio(signal, body);
    };
    f.client.diagnostics = async () => {
      diagnosisStarted = true;
      await diagnostics.promise;
      return { microphone_muted: true };
    };
    f.controls.statusFailures = 1;
    const failed = f.controller.voice.refresh();
    await until(() => !holdStop);
    const newer = f.controller.voice.wait();
    await tick();
    assert.equal(f.controller.voice.state.waiting, false, "new wait awaits old stop");
    stopping.resolve();
    await newer;
    diagnostics.resolve();
    await failed;
    assert.equal(diagnosisStarted, false, "old error path no longer owns diagnostics");
    assert.equal(f.controller.voice.state.waiting, true);
    assert.doesNotMatch(f.controller.voice.state.notice, /未確認|開始し直/);
  } finally {
    stopping.resolve();
    diagnostics.resolve();
    f.controller.dispose();
  }
});

test("R4-01 audio status recovery keeps actual Pi text send usable", async () => {
  const f = piVoiceFixture();
  try {
    await f.controller.refresh();
    await f.controller.conversations();
    f.controller.chooseConversation(f.b);
    await f.controller.voice.wait();
    f.controls.statusFailures = 1;
    await f.controller.voice.refresh();
    assert.match(f.controller.voice.state.notice, /文字入力.*開始し直/);
    f.controls.create = async (body) => {
      assert.equal(body.turn.text, "音声が不明でも文字で送信");
      assert.equal(body.turn.output_mode, "text");
      return { run_id: f.run, status: "queued" };
    };
    f.controller.draft("音声が不明でも文字で送信");
    await f.controller.send();
    assert.equal(f.controller.snapshot().busy, false);
    assert.equal(f.calls.includes("create_turn"), true);
    assert.equal(f.controller.voice.state.waiting, false);
  } finally {
    f.controller.dispose();
  }
});

for (const expires of [false, true])
  test(`R4-01 old status-stop bootstrap cannot POST after new wait (expires: ${expires})`, async () => {
    const gate = deferred(),
      calls = [];
    let bootstraps = 0,
      waiting = false,
      failStatus = false;
    const client = new LocalSessionClient(async (path, init) => {
      if (path === "/local/session") {
        bootstraps++;
        if (bootstraps === 2) await gate.promise; // Deliberately ignore cancellation.
        return Response.json({ csrf_token: "synthetic" });
      }
      if (path === "/local/audio" && init.method === "GET") {
        if (failStatus) {
          failStatus = false;
          return Response.json({}, { status: 403 });
        }
        return Response.json({
          enabled: true,
          capture: "idle",
          playback: "idle",
          muted: false,
          waiting,
        });
      }
      if (path === "/local/audio") {
        calls.push("stop");
        waiting = false;
        return Response.json({ stopped: true, run_cancelled: false });
      }
      if (path === "/local/cloud/audio-wait") {
        calls.push("wait");
        waiting = true;
        return Response.json({ waiting: true });
      }
      return Response.json({ microphone_muted: false });
    });
    const voice = new DeviceVoice(client, () => {});
    try {
      await voice.wait();
      failStatus = true;
      const failed = voice.refresh();
      await until(() => bootstraps === 2);
      if (expires) await new Promise((resolve) => setTimeout(resolve, 5_100));
      const newer = voice.wait();
      await tick();
      assert.deepEqual(calls, ["stop", "wait"], "new wait cannot pass the old bootstrap");
      gate.resolve();
      await Promise.all([failed, newer]);
      assert.deepEqual(
        calls,
        expires ? ["stop", "wait", "stop", "wait"] : ["stop", "wait", "stop", "stop", "wait"],
      );
      assert.equal(voice.state.waiting, true);
      assert.equal(waiting, true);
      assert.doesNotMatch(voice.state.notice, /未確認|開始し直/);
    } finally {
      gate.resolve();
      voice.dispose();
    }
  });
