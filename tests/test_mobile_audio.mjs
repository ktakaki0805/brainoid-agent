import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { microphoneDeniedMessage, mobileAudio } from "../apps/mobile/platform/audio.ts";
import { sessionAudioTransport } from "../apps/mobile/platform/audio-transport.ts";
import {
  canonicalWav,
  encodeWav,
  fromBase64,
  MAX_FRAMES,
  MAX_WAV_BYTES,
  toBase64,
  validateWav,
} from "../apps/mobile/platform/pcm.ts";
import { SessionContext } from "../packages/ui/src/session.ts";

const tick = () => new Promise((resolve) => setImmediate(resolve));
function deferred() {
  let resolve;
  const promise = new Promise((r) => {
    resolve = r;
  });
  return { promise, resolve };
}
const fixtureWav = () => encodeWav(Int16Array.of(-32768, -1, 0, 1, 32767));
function fixture() {
  const calls = [],
    listeners = new Set(),
    owner = new AbortController(),
    states = [],
    controls = {};
  let active = 0,
    floor = 0,
    playing;
  const guard = (generation) => {
    if (generation !== active || generation <= floor) throw new Error("Stale native lease");
  };
  const plugin = {
    async addListener(_name, fn) {
      if (controls.listener) await controls.listener;
      listeners.add(fn);
      return {
        async remove() {
          listeners.delete(fn);
        },
      };
    },
    async activate({ generation }) {
      if (controls.activate) await controls.activate;
      if (generation <= Math.max(active, floor)) throw Error("Stale");
      active = generation;
      calls.push(["activate", generation]);
    },
    async permission({ generation }) {
      calls.push(["permission", generation]);
      if (controls.permission) await controls.permission;
      guard(generation);
      return { granted: controls.granted !== false };
    },
    async start({ generation }) {
      if (controls.start) await controls.start;
      guard(generation);
      calls.push(["start", generation]);
    },
    async finish({ generation }) {
      calls.push(["finish", generation]);
      if (controls.finish) await controls.finish;
      guard(generation);
      return { wav: controls.wav ?? toBase64(fixtureWav()) };
    },
    async play({ generation, wav, expiresAt }) {
      if (controls.playBridge) await controls.playBridge;
      guard(generation);
      calls.push(["play", generation, wav, expiresAt]);
      playing = deferred();
      if (controls.autoPlay !== false) playing.resolve();
      await playing.promise;
    },
    async stop({ generation }) {
      if (controls.stop) await controls.stop;
      floor = Math.max(floor, generation);
      calls.push(["stop", generation]);
      if (active <= floor) playing?.resolve();
    },
  };
  const transport = {
    async upload(wav, signal) {
      calls.push(["upload", wav, signal]);
      if (controls.upload) await controls.upload;
      return "audio_input_fixture";
    },
    async speech(id, signal) {
      calls.push(["speech", id, signal]);
      if (controls.speech) await controls.speech;
      return fixtureWav();
    },
  };
  const context = {
    key: "tenant:actor:session:run",
    signal: owner.signal,
    expiresAt: Date.now() + 60_000,
    isCurrent: () => controls.current !== false,
  };
  const adapter = mobileAudio(plugin, () => controls.now ?? Date.now());
  const open = () => adapter.open(context, transport, (state) => states.push(state));
  return {
    plugin,
    transport,
    context,
    owner,
    calls,
    states,
    controls,
    open,
    emit: (reason, generation = active) => {
      for (const fn of listeners) fn({ generation, reason });
    },
    sentence: (index, extra = {}) => ({
      contextKey: context.key,
      id: `speech_${index}`,
      index,
      expiresAt: Date.now() + 30_000,
      ...extra,
    }),
    listeners,
  };
}

test("PCM signed samples, 60 second exact boundary, malformed and silent input", () => {
  const bytes = fixtureWav();
  assert.equal(validateWav(bytes), 5);
  assert.deepEqual(fromBase64(toBase64(bytes)), bytes);
  assert.deepEqual([...new Int16Array(bytes.buffer.slice(44))], [-32768, -1, 0, 1, 32767]);
  const max = new Int16Array(MAX_FRAMES);
  max[0] = 1;
  assert.equal(validateWav(encodeWav(max)), MAX_FRAMES);
  assert.throws(() => encodeWav(new Int16Array(MAX_FRAMES + 1)));
  assert.throws(() => validateWav(encodeWav(new Int16Array(1))));
  for (const offset of [0, 4, 8, 16, 20, 22, 24, 28, 32, 34, 40]) {
    const invalid = bytes.slice();
    invalid[offset] ^= 255;
    assert.throws(() => validateWav(invalid));
  }
  assert.throws(() => fromBase64("A".repeat(MAX_WAV_BYTES * 2)));
});

test("provider WAV metadata is checked then removed before native playback", () => {
  const source = fixtureWav(),
    bytes = new Uint8Array(source.length + 10);
  bytes.set(source.subarray(0, 36));
  bytes.set(new TextEncoder().encode("JUNK"), 36);
  new DataView(bytes.buffer).setUint32(40, 1, true);
  bytes[44] = 42;
  bytes.set(source.subarray(36), 46);
  new DataView(bytes.buffer).setUint32(4, bytes.length - 8, true);
  assert.equal(validateWav(bytes), 5);
  assert.deepEqual(canonicalWav(bytes), source);
});

test("T-006 denied microphone preserves text input and provides repermission guidance", async () => {
  const f = fixture();
  f.controls.granted = false;
  const lease = f.open();
  await lease.start();
  assert.equal(f.states.at(-1).issue, "permission");
  assert.equal(f.states.at(-1).textInputAvailable, true);
  assert.match(microphoneDeniedMessage, /端末の設定/);
  assert.equal(f.calls.filter(([name]) => name === "start").length, 0);
  await lease.close();
});

for (const when of ["recording", "ready", "permission", "finish"])
  test(`discard during ${when} uploads zero`, async () => {
    const f = fixture(),
      delay = deferred();
    if (when === "permission") f.controls.permission = delay.promise;
    if (when === "finish") f.controls.finish = delay.promise;
    const lease = f.open(),
      starting = lease.start();
    await tick();
    let finishing;
    if (when !== "permission") await starting;
    if (["ready", "finish"].includes(when)) {
      finishing = lease.finish();
      await tick();
    }
    await lease.close();
    delay.resolve();
    await starting;
    await finishing;
    await lease.upload();
    assert.equal(f.calls.filter(([name]) => name === "upload").length, 0);
  });

test("max stop makes a bounded recording ready; upload explicit and exactly once", async () => {
  const f = fixture(),
    lease = f.open();
  await lease.start();
  f.emit("limit");
  await tick();
  assert.equal(f.states.at(-1).phase, "ready");
  assert.equal(f.calls.filter(([name]) => name === "upload").length, 0);
  assert.equal(await lease.upload(), "audio_input_fixture");
  await lease.upload();
  await lease.start();
  assert.equal(f.calls.filter(([name]) => name === "upload").length, 1);
  assert.equal(f.calls.filter(([name]) => name === "start").length, 1);
  assert.ok(f.calls.find(([name]) => name === "upload")[1].every((value) => value === 0));
  await lease.close();
});

test("T-007 out-of-order and duplicate sentences play once in order", async () => {
  const f = fixture(),
    lease = f.open();
  lease.enqueue(f.sentence(2));
  await tick();
  assert.equal(f.calls.filter(([name]) => name === "speech").length, 0);
  lease.enqueue(f.sentence(0));
  lease.enqueue(f.sentence(1));
  lease.enqueue(f.sentence(0));
  await tick();
  await tick();
  assert.deepEqual(
    f.calls.filter(([name]) => name === "speech").map((call) => call[1]),
    ["speech_0", "speech_1", "speech_2"],
  );
  assert.equal(f.calls.filter(([name]) => name === "play").length, 3);
  await lease.close();
});

for (const when of ["fetch", "play-bridge", "playing"])
  test(`stop during ${when} fences old playback`, async () => {
    const f = fixture(),
      delay = deferred();
    if (when === "fetch") f.controls.speech = delay.promise;
    if (when === "play-bridge") f.controls.playBridge = delay.promise;
    if (when === "playing") f.controls.autoPlay = false;
    const lease = f.open();
    lease.enqueue(f.sentence(0));
    lease.enqueue(f.sentence(1));
    await tick();
    await lease.close();
    delay.resolve();
    await tick();
    await tick();
    assert.equal(f.calls.filter(([name]) => name === "play").length, when === "playing" ? 1 : 0);
    assert.equal(f.calls.filter(([name]) => name === "speech").length, 1);
  });

for (const delayName of ["permission", "start", "activate", "listener"])
  test(`late ${delayName} after replacement cannot start old recording`, async () => {
    const f = fixture(),
      delay = deferred();
    f.controls[delayName] = delay.promise;
    const old = f.open(),
      start = old.start();
    await tick();
    delete f.controls[delayName];
    const current = f.open();
    await current.start();
    delay.resolve();
    await start;
    await tick();
    const starts = f.calls.filter(([name]) => name === "start");
    assert.equal(starts.length, 1);
    assert.equal(f.states.at(-1).phase, "recording");
    await old.close();
    await current.close();
  });

test("late old stop cannot stop the newly activated lease", async () => {
  const f = fixture(),
    delay = deferred(),
    old = f.open();
  await old.start();
  f.controls.stop = delay.promise;
  const stopping = old.close();
  delete f.controls.stop;
  const current = f.open();
  await current.start();
  delay.resolve();
  await stopping;
  await current.finish();
  assert.equal(f.states.at(-1).phase, "ready");
  await current.close();
});

for (const reason of ["background", "interrupted", "error"])
  test(`${reason} drops recording and queue`, async () => {
    const f = fixture(),
      lease = f.open();
    await lease.start();
    f.emit(reason);
    await tick();
    assert.equal(f.states.at(-1).phase, "closed");
    await lease.upload();
    assert.equal(f.calls.filter(([name]) => name === "upload").length, 0);
    await lease.close();
  });

test("session abort immediately stops and ignores old native events", async () => {
  const f = fixture(),
    lease = f.open();
  await lease.start();
  f.emit("background", 1);
  assert.equal(f.states.at(-1).phase, "recording");
  f.owner.abort();
  assert.equal(f.states.at(-1).phase, "closed");
  await lease.close();
  await tick();
  assert.equal(f.listeners.size, 0);
});

test("expiry, other output binding, invalid sequence and changed identity never play", async () => {
  const f = fixture(),
    lease = f.open();
  lease.enqueue(f.sentence(0, { contextKey: "other" }));
  lease.enqueue(f.sentence(64));
  lease.enqueue(f.sentence(-1));
  await tick();
  assert.equal(f.calls.filter(([name]) => name === "speech").length, 0);
  const wait = deferred();
  f.controls.speech = wait.promise;
  lease.enqueue(f.sentence(0));
  await tick();
  f.controls.current = false;
  wait.resolve();
  await tick();
  assert.equal(f.calls.filter(([name]) => name === "play").length, 0);
  await lease.close();
});

test("sentence expiration after fetch rejects late audio; upload cancellation rejects late transcript", async () => {
  const f = fixture(),
    delay = deferred();
  f.controls.speech = delay.promise;
  const lease = f.open(),
    sentence = f.sentence(0);
  lease.enqueue(sentence);
  await tick();
  f.controls.now = sentence.expiresAt;
  delay.resolve();
  await tick();
  assert.equal(f.calls.filter(([name]) => name === "play").length, 0);
  await lease.close();
  const g = fixture(),
    pending = deferred();
  g.controls.upload = pending.promise;
  const recording = g.open();
  await recording.start();
  await recording.finish();
  const upload = recording.upload();
  await tick();
  await recording.close();
  pending.resolve();
  assert.equal(await upload, undefined);
});

test("invalid or silent WAV falls back to text without upload", async () => {
  for (const wav of [toBase64(encodeWav(new Int16Array(4))), "AAAA"]) {
    const f = fixture();
    f.controls.wav = wav;
    const lease = f.open();
    await lease.start();
    await lease.finish();
    assert.equal(f.states.at(-1).issue, "invalid-audio");
    assert.equal(f.states.at(-1).textInputAvailable, true);
    await lease.upload();
    assert.equal(f.calls.filter(([name]) => name === "upload").length, 0);
    await lease.close();
  }
});

function session(transport) {
  return new SessionContext({
    uid: "user_fixture",
    tenantId: "tenant_fixture",
    generation: 1,
    baseUrl: "https://audio.invalid/v1",
    current: () => true,
    token: async () => "synthetic-token",
    transport,
    denied: () => {},
  });
}
test("session HTTP adapter authenticates bounded no-store WAV and uploads contract once", async () => {
  const calls = [],
    context = session(async (request) => {
      calls.push(request);
      assert.equal(request.cache, "no-store");
      assert.equal(request.redirect, "error");
      assert.equal(request.headers.get("Authorization"), "Bearer synthetic-token");
      if (request.method === "POST") {
        assert.equal(request.headers.get("content-type"), "audio/wav");
        assert.equal(validateWav(new Uint8Array(await request.arrayBuffer())), 5);
        assert.ok(request.headers.get("Idempotency-Key"));
        return Response.json(
          {
            audio_input_id: "input_fixture",
            expires_at: new Date(Date.now() + 900_000).toISOString(),
          },
          { status: 201 },
        );
      }
      return new Response(fixtureWav(), {
        headers: { "Content-Type": "audio/wav", "Cache-Control": "no-store" },
      });
    });
  const transport = sessionAudioTransport(context),
    signal = new AbortController().signal;
  assert.equal(await transport.upload(fixtureWav(), signal), "input_fixture");
  assert.equal(validateWav(await transport.speech("speech_fixture", signal)), 5);
  assert.equal(calls.length, 2);
  context.dispose();
});

test("HTTP adapter rejects provider failure, missing no-store, malformed and oversized response", async () => {
  for (const response of [
    new Response(null, { status: 503 }),
    new Response(fixtureWav(), { headers: { "Content-Type": "audio/wav" } }),
    new Response(new Uint8Array(MAX_WAV_BYTES + 1), {
      headers: { "Content-Type": "audio/wav", "Cache-Control": "no-store" },
    }),
    new Response("invalid", {
      headers: { "Content-Type": "audio/wav", "Cache-Control": "no-store" },
    }),
  ]) {
    let calls = 0;
    const context = session(async () => {
      calls++;
      return response;
    });
    await assert.rejects(
      sessionAudioTransport(context).speech("speech_fixture", new AbortController().signal),
    );
    assert.equal(calls, 1);
    context.dispose();
  }
});

test("Android pure Java PCM implementation compiles and runs contract fixture", async () => {
  const directory = await mkdtemp(join(tmpdir(), "brainoid-audio-java-"));
  try {
    const compiled = spawnSync(
      "javac",
      [
        "-d",
        directory,
        "apps/mobile/native-audio-config/android/PcmWav.java",
        "tests/mobile/audio-fixtures/PcmWavFixture.java",
        "tests/mobile/audio-fixtures/ParseJava.java",
      ],
      { encoding: "utf8" },
    );
    assert.equal(compiled.status, 0, compiled.stderr);
    const ran = spawnSync("java", ["-cp", directory, "PcmWavFixture"], { encoding: "utf8" });
    assert.equal(ran.status, 0, ran.stderr);
    assert.match(ran.stdout, /passed/);
    const parsed = spawnSync(
      "java",
      [
        "-cp",
        directory,
        "ParseJava",
        "apps/mobile/native-audio-config/android/BrainoidAudioPlugin.java",
      ],
      { encoding: "utf8" },
    );
    assert.equal(parsed.status, 0, parsed.stderr);
    assert.match(parsed.stdout, /syntax parsed/);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("Capacitor bridge logging is disabled so raw base64 cannot enter device logs", async () => {
  const config = JSON.parse(await readFile("apps/mobile/capacitor.config.json", "utf8"));
  assert.equal(config.loggingBehavior, "none");
  assert.ok(!config.android?.loggingBehavior || config.android.loggingBehavior === "none");
  assert.ok(!config.ios?.loggingBehavior || config.ios.loggingBehavior === "none");
});

for (const reason of ["interrupted", "error"])
  test(`iOS configuration/conversion ${reason} during pending finish cannot become ready or upload`, async () => {
    const f = fixture(),
      delay = deferred();
    f.controls.finish = delay.promise;
    const lease = f.open();
    await lease.start();
    const finishing = lease.finish();
    await tick();
    // Native configuration loss / invalid format arrives before the old PCM result.
    f.emit(reason);
    delay.resolve();
    await finishing;
    await lease.upload();
    assert.equal(f.states.at(-1).phase, "closed");
    assert.equal(
      f.states.some((state) => state.phase === "ready"),
      false,
    );
    assert.equal(f.calls.filter(([name]) => name === "upload").length, 0);
    assert.equal(f.states.at(-1).textInputAvailable, true);
    await lease.close();
  });

test("stale configuration interruption from a replaced recording does not invalidate current PCM", async () => {
  const f = fixture(),
    old = f.open();
  await old.start();
  const oldGeneration = f.calls.find(([name]) => name === "start")[1];
  const current = f.open();
  await current.start();
  f.emit("interrupted", oldGeneration);
  assert.equal(f.states.at(-1).phase, "recording");
  await current.finish();
  assert.equal(await current.upload(), "audio_input_fixture");
  await old.close();
  await current.close();
});
