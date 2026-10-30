import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { chromium } from "playwright-core";
const artifacts = process.env.UI_ARTIFACTS;
assert.ok(artifacts && process.env.UI_CHROMIUM);
await mkdir(artifacts, { recursive: true });
const directory = await mkdtemp(join(tmpdir(), "ba-voice-05-browser-"));
const owned = [],
  errors = [],
  requests = [];
let browser, page;
const birth = async (pid) =>
  (await readFile(`/proc/${pid}/stat`, "utf8")).split(") ")[1].split(" ")[19];
async function server(app, mode, port) {
  const out = join(directory, app);
  const build = spawn(
    process.execPath,
    ["node_modules/vite/bin/vite.js", "build", `apps/${app}`, "--mode", mode, "--outDir", out],
    { stdio: ["ignore", "pipe", "pipe"] },
  );
  let buildLog = "";
  build.stdout.on("data", (v) => {
    buildLog += v;
  });
  build.stderr.on("data", (v) => {
    buildLog += v;
  });
  assert.equal(await new Promise((resolve) => build.once("exit", resolve)), 0, buildLog);
  const child = spawn(
    process.execPath,
    [
      "node_modules/vite/bin/vite.js",
      "preview",
      `apps/${app}`,
      "--mode",
      mode,
      "--outDir",
      out,
      "--host",
      "127.0.0.1",
      "--strictPort",
      "--port",
      String(port),
    ],
    { stdio: ["ignore", "pipe", "pipe"] },
  );
  child.stdout.resume();
  child.stderr.resume();
  owned.push({ child, birth: await birth(child.pid) });
  const url = `http://127.0.0.1:${port}`;
  for (let n = 0; n < 100; n++) {
    try {
      if ((await fetch(url)).ok) return url;
    } catch {}
    await new Promise((r) => setTimeout(r, 100));
  }
  throw Error("Owned preview unavailable");
}
async function shot(name, locator) {
  await locator.evaluate((element) => element.scrollIntoView({ block: "start" }));
  await page.waitForTimeout(120);
  if (name !== "display-text-fallback.png") {
    const status = await locator.evaluate((element) => {
      const rows = [...element.querySelectorAll(":scope > p")];
      return rows.slice(0, 3).map((row) => ({
        text: row.textContent,
        top: row.getBoundingClientRect().top,
        bottom: row.getBoundingClientRect().bottom,
        visibility: getComputedStyle(row).visibility,
      }));
    });
    assert.match(status[1].text, /接続: .*マイク: .*個人セッション:/);
    assert.match(status[2].text, /発話先:/);
    assert.ok(status[1].top >= 0 && status[2].bottom < (await page.evaluate(() => innerHeight)));
    await writeFile(join(artifacts, `${name}.state.json`), JSON.stringify(status, null, 2));
  }
  await page.screenshot({ path: join(artifacts, name) });
}
try {
  const mobile = await server("mobile", "fake", 5191),
    display = await server("display", "production", 5192);
  browser = await chromium.launchPersistentContext(join(directory, "profile"), {
    executablePath: process.env.UI_CHROMIUM,
    headless: true,
    viewport: { width: 390, height: 844 },
    ignoreDefaultArgs: ["--disable-back-forward-cache"],
  });
  page = await browser.newPage();
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto(mobile);
  await page.getByLabel("メールアドレス").fill("synthetic@example.test");
  await page.getByLabel("パスワード", { exact: true }).fill("synthetic-password");
  await page.getByRole("button", { name: "サインイン", exact: true }).click();
  await page.getByRole("button", { name: "テナント A", exact: true }).click();
  await page.getByRole("button", { name: "共有 みんなの会話", exact: true }).click();
  const voice = page.getByRole("region", { name: "音声の状態と操作" });
  await page.getByText("音声の確認", { exact: true }).click();
  for (const [scenario, name] of [
    ["permission", "denied"],
    ["silent", "silent"],
    ["network", "provider"],
  ]) {
    await page.getByLabel("音声の動作").selectOption(scenario);
    await voice.getByRole("button", { name: "録音を開始", exact: true }).click();
    if (scenario !== "permission")
      await voice.getByRole("button", { name: "録音を終了（未送信）", exact: true }).click();
    if (scenario === "network")
      await voice.getByRole("button", { name: "録音を送信", exact: true }).click();
    await voice.getByText(/Error ·/).waitFor();
    assert.match(await voice.textContent(), /文字で入力/);
    await page.getByLabel("メッセージ", { exact: true }).fill(`合成の${name}文字入力`);
    assert.equal(await page.getByRole("button", { name: "送信", exact: true }).isEnabled(), true);
    await shot(`mobile-${name}.png`, voice);
    await voice.getByRole("button", { name: "読み上げを停止", exact: true }).click();
  }
  await page.getByLabel("音声の動作").selectOption("normal");
  await voice.getByRole("button", { name: "録音を開始", exact: true }).click();
  await voice.getByText(/Listening ·/).waitFor();
  await shot("mobile-listening.png", voice);
  await voice.getByRole("button", { name: "録音を終了（未送信）", exact: true }).click();
  await shot("mobile-recorded-unsent.png", voice);
  await voice.getByRole("button", { name: "録音を送信", exact: true }).click();
  await voice.getByText(/Transcribing ·/).waitFor();
  await shot("mobile-transcribing.png", voice);
  await voice.getByText(/Speaking ·/).waitFor();
  await shot("mobile-speaking.png", voice);
  await voice.getByRole("button", { name: "読み上げを停止", exact: true }).click();
  await voice.getByText(/生成と家電・予定の操作は継続/).waitFor();
  await page.getByText("保存済みの最終回答を表示しています。", { exact: false }).waitFor();
  await page.getByText("認証・切替の確認", { exact: true }).click();
  await page.getByLabel("会話イベント").selectOption("waiting");
  await page.getByLabel("メッセージ", { exact: true }).fill("確認待ちの合成入力");
  await page.getByRole("button", { name: "送信", exact: true }).click();
  await voice.getByText(/Thinking ·/).waitFor();
  await shot("mobile-thinking.png", voice);
  await voice.getByText(/Confirming ·/).waitFor();
  await shot("mobile-confirming.png", voice);
  await voice.getByRole("button", { name: "読み上げを停止", exact: true }).click();
  await voice.getByText(/Confirming ·/).waitFor();
  await page.getByRole("button", { name: "生成を停止", exact: true }).click();
  await voice.getByText(/Idle ·/).waitFor();
  await shot("mobile-idle-after-cancel.png", voice);
  await page.getByLabel("会話イベント").selectOption("normal");
  await voice.getByRole("button", { name: "マイクをミュート", exact: true }).click();
  assert.match(await voice.textContent(), /マイク: ミュート/);
  await page.evaluate(() => {
    Object.defineProperty(navigator, "onLine", { configurable: true, value: false });
    window.dispatchEvent(new Event("offline"));
  });
  assert.match(await voice.textContent(), /オフライン/);
  await shot("mobile-offline-muted.png", voice);
  await page.evaluate(() => {
    Object.defineProperty(navigator, "onLine", { configurable: true, value: true });
    window.dispatchEvent(new Event("online"));
  });
  await voice.getByRole("button", { name: "マイクのミュートを解除", exact: true }).click();
  await voice.getByRole("button", { name: "録音を開始", exact: true }).click();
  await page.evaluate(() => {
    addEventListener("pageshow", (event) => {
      sessionStorage.setItem("voice-bfcache-restored", String(event.persisted));
    });
    addEventListener("pagehide", () => {
      sessionStorage.setItem(
        "voice-hidden-dom",
        String(document.querySelectorAll(".selected-tenant").length),
      );
    });
  });
  await page.goto(`${mobile}/?voice-away=1`);
  await page.goBack({ waitUntil: "commit" });
  await page.waitForFunction(() => sessionStorage.getItem("voice-bfcache-restored") === "true");
  assert.equal(await page.evaluate(() => sessionStorage.getItem("voice-hidden-dom")), "0");
  assert.equal(await page.getByText(/Listening ·|Speaking ·/).count(), 0);
  await page.reload();
  assert.equal(await page.getByText(/Listening ·|Speaking ·/).count(), 0);
  await writeFile(
    join(artifacts, "mobile-lifecycle.json"),
    JSON.stringify({ pageshowPersisted: true, pagehidePrivateDOM: 0, reloadAutoplay: false }),
  );

  await page.close();
  page = await browser.newPage();
  await page.setViewportSize({ width: 1280, height: 800 });
  page.on("pageerror", (e) => errors.push(e.message));
  const tenant = "10000000-0000-4000-8000-000000000001",
    device = "60000000-0000-4000-8000-000000000001",
    conversation = "30000000-0000-4000-8000-000000000001",
    final = "50000000-0000-4000-8000-000000000001",
    sid = "70000000-0000-4000-8000-000000000001",
    staleRun = "40000000-0000-4000-8000-000000000099";
  let run = "40000000-0000-4000-8000-000000000001",
    capture = "idle",
    playback = "idle",
    runStatus = "completed",
    assigned = [],
    privateSession = null,
    offline = false,
    enabled = true,
    input = "",
    output = "",
    waiting = false,
    failStatus = 0,
    failStatusAfterReject = false,
    failWait = false,
    failStop = false;
  const now = () => new Date().toISOString();
  await page.route("**/local/**", async (route) => {
    const req = route.request(),
      path = new URL(req.url()).pathname,
      body = req.postDataJSON();
    requests.push({
      path,
      method: req.method(),
      ...(body ? { operation: body.operation, mode: body.mode } : {}),
    });
    const reply = (value, status = 200) =>
      route.fulfill({
        status,
        contentType: "application/json",
        headers: { "Cache-Control": "no-store" },
        body: JSON.stringify(value),
      });
    if (path === "/local/session") return reply({ csrf_token: "synthetic-csrf" });
    if (path === "/local/diagnostics")
      return reply({
        service: "running",
        storage: "ready",
        reset: "idle",
        network: "link_up",
        clock: "synchronized",
        free_bytes: 1024 ** 3,
        volume_pct: 50,
        microphone_muted: false,
      });
    if (path === "/local/cloud/snapshot")
      return reply(
        offline
          ? {}
          : {
              ready: true,
              play_speech: false,
              snapshot: {
                tenant_id: tenant,
                device_id: device,
                connection_generation: 1,
                snapshot_id: "a".repeat(64),
                active_private_session: privateSession,
                assigned_runs: assigned,
                pending_command_ids: [],
                pending_notification_ids: [],
                delivery_cursor: 0,
                next_cursor: null,
              },
            },
        offline ? 503 : 200,
      );
    if (path === "/local/audio/devices")
      return reply({
        devices: [
          { id: "synthetic-mic", direction: "input" },
          { id: "synthetic-speaker", direction: "output" },
        ],
      });
    if (path === "/local/audio") {
      if (body?.operation === "stop") {
        if (failStop) return reply({}, 503);
        capture = playback = "idle";
        waiting = false;
        return reply({ stopped: true, run_cancelled: false });
      }
      if (!body && failStatus > 0) {
        failStatus--;
        return reply({}, 503);
      }
      if (!enabled) return reply({}, 404);
      if (body?.operation === "select") {
        input = body.input_device;
        output = body.output_device;
      }
      if (body?.operation === "start") capture = "recording";
      if (body?.operation === "finish") capture = "ready";
      if (body?.operation === "send") {
        capture = "idle";
        return reply({
          audio_input_id: final,
          expires_at: new Date(Date.now() + 900_000).toISOString(),
        });
      }
      return reply({
        enabled: true,
        capture,
        playback,
        muted: false,
        waiting,
        input_device: input,
        output_device: output,
        notice: null,
        queued: 0,
      });
    }
    if (path === "/local/cloud/audio-wait") {
      waiting = true;
      if (failWait) {
        failStop = true;
        return reply({}, 503);
      }
      return reply({ waiting: true });
    }
    if (path.endsWith("/stop")) {
      privateSession = null;
      assigned = [];
      playback = "idle";
      return reply({ status: "stopped", cloud_session_ended: false });
    }
    if (path.endsWith("/events")) {
      if (body?.mode === "waiting" && path.includes(staleRun)) {
        if (failStatusAfterReject) failStatus = 1;
        return route.fulfill({
          contentType: "text/event-stream",
          body: 'event: clear\ndata: {"reason":"stream_stopped"}\n\n',
        });
      }
      if (body?.mode === "waiting") {
        assert.equal(req.method(), "POST");
        assert.equal(req.headers()["x-csrf-token"], "synthetic-csrf");
        waiting = false;
        playback = "playing";
      }
      runStatus = "completed";
      const events = [
        ["run.started", {}],
        ["text.delta", { text: "合成応答" }],
        [
          "speech.ready",
          {
            sentence_index: 0,
            speech_id: final,
            expires_at: new Date(Date.now() + 60_000).toISOString(),
          },
        ],
        ["run.completed", { message_id: final }],
      ]
        .map(
          ([type, payload], i) =>
            `event: run\ndata: ${JSON.stringify({ run_id: run, seq: i + 1, type, occurred_at: now(), payload })}\n\n`,
        )
        .join("");
      return route.fulfill({ contentType: "text/event-stream", body: events });
    }
    if (body?.operation === "list_conversations")
      return reply({
        items: [
          {
            id: conversation,
            tenant_id: tenant,
            title: "合成の共有会話",
            visibility: "shared",
            display_policy: "shared_display",
          },
        ],
        next_cursor: null,
      });
    if (body?.operation === "get_conversation")
      return reply({
        id: conversation,
        tenant_id: tenant,
        visibility: privateSession ? "private" : "shared",
        display_policy: privateSession ? "authenticated_session" : "shared_display",
        ...(privateSession ? { owner_user_id: "synthetic-user" } : {}),
      });
    if (body?.operation === "create_turn") {
      assert.equal(req.headers()["x-csrf-token"], "synthetic-csrf");
      runStatus = "running";
      assigned = [
        {
          run_id: run,
          conversation_id: conversation,
          output_mode: body.turn.output_mode,
          assignment_generation: 1,
          last_event_seq: 0,
        },
      ];
      if (body.turn.audio_input_id) playback = "playing";
      return reply({ run_id: run, status: "queued" });
    }
    if (body?.operation === "get_run")
      return reply({
        run_id: body.run_id,
        conversation_id: conversation,
        status: runStatus,
        final_message_id: final,
        last_event_seq: runStatus === "completed" ? 4 : 0,
      });
    if (body?.operation === "list_messages")
      return reply({
        items: [
          {
            id: final,
            role: "assistant",
            run_id: run,
            text: privateSession ? "PRIVATE SYNTHETIC OUTPUT" : "合成の最終応答",
          },
        ],
        next_cursor: null,
      });
    return reply({}, 404);
  });
  await page.goto(display);
  const pi = page.getByRole("region", { name: "Piの会話出力" }),
    pv = pi.getByRole("region", { name: "音声の状態と操作" });
  await pi.getByRole("button", { name: "入力できる会話を確認" }).click();
  await pi.getByLabel("入力先の会話").selectOption(conversation);
  await pi.getByText("音声機器と読み上げの待機", { exact: true }).click();
  await pi.getByRole("button", { name: "音声機器を確認", exact: true }).click();
  await pi.getByLabel("入力機器", { exact: true }).selectOption("synthetic-mic");
  await pi.getByLabel("出力機器", { exact: true }).selectOption("synthetic-speaker");
  await pi.getByRole("button", { name: "選択した音声機器を使用" }).click();
  await pv.getByRole("button", { name: "録音を開始", exact: true }).click();
  await pv.getByText(/Listening ·/).waitFor();
  await shot("display-listening.png", pv);
  await pv.getByRole("button", { name: "録音を終了（未送信）", exact: true }).click();
  await pv.getByRole("button", { name: "録音を送信", exact: true }).click();
  await pv.getByText(/Speaking ·/).waitFor();
  await shot("display-speaking.png", pv);
  await pv.getByRole("button", { name: "読み上げを停止", exact: true }).click();
  await pi.getByText("合成の最終応答", { exact: true }).waitFor();
  assigned = [];
  await pi.getByRole("button", { name: "会話の状態を確認", exact: true }).click();
  await pi.getByRole("button", { name: "次の新しい割当の読み上げを待つ", exact: true }).click();
  await pv.getByText(/60秒間、次の新しい割当を待機中/).waitFor();
  await shot("display-waiting.png", pv);
  run = "40000000-0000-4000-8000-000000000002";
  assigned = [
    {
      run_id: run,
      conversation_id: conversation,
      output_mode: "speech",
      assignment_generation: 1,
      last_event_seq: 0,
    },
  ];
  assigned.unshift({
    run_id: staleRun,
    conversation_id: conversation,
    output_mode: "speech",
    assignment_generation: 1,
    last_event_seq: 0,
  });
  runStatus = "running";
  await pi.getByRole("button", { name: "会話の状態を確認", exact: true }).click();
  await pv.getByText(/Speaking ·/).waitFor();
  assert.ok(
    requests.some(
      (r) => r.method === "POST" && r.mode === "waiting" && r.path.endsWith(`/runs/${run}/events`),
    ),
  );
  assert.doesNotMatch(
    await pv.textContent(),
    /60秒間、次の新しい割当を待機中|ここからスマホで録音を送信/,
  );
  assert.deepEqual(
    requests.filter((r) => r.mode === "waiting").map((r) => r.path.split("/").at(-2)),
    [staleRun, run],
  );
  await shot("display-waiting-live-speaking.png", pv);
  await pv.getByRole("button", { name: "読み上げを停止", exact: true }).click();
  await page.waitForTimeout(2300);
  assert.doesNotMatch(
    await pv.textContent(),
    /60秒間、次の新しい割当を待機中|ここからスマホで録音を送信/,
  );
  await shot("display-wait-consumed-idle.png", pv);

  // An unknown audio status explicitly retires the acknowledged wait and queue.
  assigned = [];
  await pi.getByRole("button", { name: "会話の状態を確認", exact: true }).click();
  await pi.getByRole("button", { name: "次の新しい割当の読み上げを待つ", exact: true }).click();
  await pv.getByText(/60秒間、次の新しい割当を待機中/).waitFor();
  const waitingBeforeFailure = requests.filter((r) => r.mode === "waiting").length;
  run = "40000000-0000-4000-8000-000000000003";
  assigned = [staleRun, run].map((run_id) => ({
    run_id,
    conversation_id: conversation,
    output_mode: "speech",
    assignment_generation: 1,
    last_event_seq: 0,
  }));
  failStatusAfterReject = true;
  runStatus = "running";
  await pi.getByRole("button", { name: "会話の状態を確認", exact: true }).click();
  await pv.getByText(/待機状態を確認できない.*待機を開始し直/).waitFor();
  failStatusAfterReject = false;
  await page.waitForTimeout(2300);
  assert.equal(waiting, false);
  assert.equal(await pi.getByText(/新規音声を待機中 ·/).count(), 0);
  assert.deepEqual(
    requests
      .filter((r) => r.mode === "waiting")
      .slice(waitingBeforeFailure)
      .map((r) => r.path.split("/").at(-2)),
    [staleRun],
  );
  await shot("display-status-failed-wait-retired.png", pv);
  await pi.getByLabel("メッセージ").fill("待機を解除しても文字で送信");
  await pi.getByRole("button", { name: "文字を送信", exact: true }).click();
  await pi.getByText("合成の最終応答", { exact: true }).waitFor();
  await pi.getByRole("button", { name: "次の新しい割当の読み上げを待つ", exact: true }).click();
  await pv.getByText(/60秒間、次の新しい割当を待機中/).waitFor();
  run = "40000000-0000-4000-8000-000000000004";
  assigned.push({
    run_id: run,
    conversation_id: conversation,
    output_mode: "speech",
    assignment_generation: 1,
    last_event_seq: 0,
  });
  runStatus = "running";
  await pi.getByRole("button", { name: "会話の状態を確認", exact: true }).click();
  await pv.getByText(/Speaking ·/).waitFor();
  assert.equal(
    requests
      .filter((r) => r.mode === "waiting")
      .at(-1)
      .path.split("/")
      .at(-2),
    run,
  );
  assert.doesNotMatch(await pv.textContent(), /開始し直|待機状態を確認できない/);
  await shot("display-status-recovered-new-wait-speaking.png", pv);
  await pv.getByRole("button", { name: "読み上げを停止", exact: true }).click();

  await pi.getByRole("button", { name: "次の新しい割当の読み上げを待つ", exact: true }).click();
  await pv.getByText(/60秒間、次の新しい割当を待機中/).waitFor();
  failStop = true;
  failStatus = 2;
  await pv.getByText(/停止は未確認/).waitFor();
  await page.waitForTimeout(4500);
  assert.equal(waiting, true);
  assert.equal(await pi.getByText(/新規音声を待機中 ·/).count(), 0);
  assert.match(await pv.textContent(), /停止は未確認/);
  await shot("display-status-failed-stop-unconfirmed.png", pv);
  failStop = false;
  await pv.getByRole("button", { name: "読み上げを停止", exact: true }).click();

  failWait = true;
  await pi.getByRole("button", { name: "次の新しい割当の読み上げを待つ", exact: true }).click();
  await pv.getByText(/停止は未確認/).waitFor();
  await page.waitForTimeout(2300);
  assert.equal(waiting, true); // Daemon orphan fixture: failed stop must not re-arm the UI.
  assert.equal(await pi.getByText(/新規音声を待機中 ·/).count(), 0);
  assert.match(await pv.textContent(), /録音・読み上げの待機を解除/);
  assert.doesNotMatch(await pv.textContent(), /音声意図/);
  await shot("display-wait-failed-stop-unconfirmed.png", pv);
  failWait = failStop = false;
  await pi.getByRole("button", { name: "次の新しい割当の読み上げを待つ", exact: true }).click();
  await pv.getByText(/60秒間、次の新しい割当を待機中/).waitFor();
  await pv.getByRole("button", { name: "読み上げを停止", exact: true }).click();
  enabled = false;
  await page.waitForTimeout(2300);
  await pi.getByLabel("メッセージ").fill("音声なしでも文字入力");
  assert.equal(await pi.getByRole("button", { name: "文字を送信", exact: true }).isEnabled(), true);
  await shot("display-text-fallback.png", pi.getByLabel("メッセージ"));
  enabled = true;
  offline = true;
  await page.waitForTimeout(2300);
  await pv
    .getByText(/オフライン/)
    .first()
    .waitFor();
  await shot("display-offline.png", pv);
  offline = false;
  const privateCreated = Date.now();
  privateSession = {
    id: sid,
    tenant_id: tenant,
    device_id: device,
    user_id: "synthetic-user",
    version: 1,
    created_at: new Date(privateCreated).toISOString(),
    updated_at: new Date(privateCreated).toISOString(),
    expires_at: new Date(privateCreated + 300_000).toISOString(),
    idle_expires_at: new Date(Date.now() + 50_000).toISOString(),
    status: "active",
    allowed_scopes: ["conversation"],
  };
  assigned = [
    {
      run_id: run,
      conversation_id: conversation,
      output_mode: "speech",
      assignment_generation: 2,
      last_event_seq: 4,
      private_session_id: sid,
    },
  ];
  await pi.getByRole("button", { name: "会話の状態を確認", exact: true }).click();
  await pi.getByText("PRIVATE SYNTHETIC OUTPUT", { exact: true }).waitFor();
  await shot("display-private.png", pv);
  await pi.getByRole("button", { name: "このPiでの個人表示を終了", exact: true }).click();
  assert.equal(await page.getByText("PRIVATE SYNTHETIC OUTPUT", { exact: true }).count(), 0);
  await shot("display-private-cleared.png", pv);
  await page.reload();
  assert.equal(await page.getByText("PRIVATE SYNTHETIC OUTPUT", { exact: true }).count(), 0);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  assert.deepEqual(errors, []);
  await writeFile(join(artifacts, "requests.json"), JSON.stringify(requests, null, 2));
  await writeFile(
    join(artifacts, "result.json"),
    JSON.stringify({
      mobile: "390x844",
      display: "1280x800 actual production entry with local HTTP fixture",
      errors,
      hardware: false,
    }),
  );
  console.log("VOICE-05 actual UI smoke passed");
} catch (error) {
  await writeFile(
    join(artifacts, "failure-dom.txt"),
    (await page.locator("body").textContent()) ?? "",
  );
  await page?.screenshot({ path: join(artifacts, "failure.png"), fullPage: true });
  throw error;
} finally {
  await browser?.close();
  for (const item of owned) {
    if (item.child.exitCode === null) {
      assert.equal(await birth(item.child.pid), item.birth);
      item.child.kill("SIGTERM");
      await new Promise((resolve) => item.child.once("exit", resolve));
    }
  }
  await rm(directory, { recursive: true, force: true });
}
