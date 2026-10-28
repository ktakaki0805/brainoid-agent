import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { access, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { chromium } from "playwright-core";
const artifacts = process.env.UI_ARTIFACTS,
  device = process.env.UI_DEVICE_URL;
assert.ok(artifacts && device && process.env.UI_CHROMIUM);
await mkdir(artifacts, { recursive: true });
const directory = await mkdtemp(join(tmpdir(), "ba-ui-10-browser-"));
const children = [];
let browser, page;
const errors = [];
const localRequests = [];
async function child(args) {
  const value = spawn(process.execPath, args, { stdio: ["ignore", "pipe", "pipe"] });
  value.stdout.resume();
  value.stderr.resume();
  return value;
}
async function mobile() {
  const out = join(directory, "mobile");
  const build = await child([
    "node_modules/vite/bin/vite.js",
    "build",
    "apps/mobile",
    "--mode",
    "fake",
    "--outDir",
    out,
  ]);
  assert.equal(await new Promise((resolve) => build.once("exit", resolve)), 0);
  const server = await child([
    "node_modules/vite/bin/vite.js",
    "preview",
    "apps/mobile",
    "--mode",
    "fake",
    "--outDir",
    out,
    "--host",
    "127.0.0.1",
    "--strictPort",
    "--port",
    "5197",
  ]);
  children.push(server);
  for (let i = 0; i < 100; i++) {
    assert.equal(server.exitCode, null);
    try {
      if ((await fetch("http://127.0.0.1:5197")).ok) return "http://127.0.0.1:5197";
    } catch {}
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw Error("Owned preview readiness timed out");
}
async function selectChat(name) {
  const button = page.getByRole("button", { name, exact: true });
  if (!(await button.isVisible()))
    await page.getByText("会話を切り替える・新しく作成", { exact: true }).click();
  await button.click();
}
async function marker(name) {
  for (let attempt = 0; attempt < 300; attempt++) {
    try {
      return await readFile(join(process.env.UI_FIXTURE_CONTROL, name), "utf8");
    } catch {}
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw Error(`Fixture marker missing: ${name}`);
}
try {
  const url = await mobile();
  browser = await chromium.launchPersistentContext(join(directory, "profile"), {
    executablePath: process.env.UI_CHROMIUM,
    headless: true,
    viewport: { width: 390, height: 844 },
    ignoreDefaultArgs: ["--disable-back-forward-cache"],
  });
  page = await browser.newPage();
  page.on("response", (response) => {
    if (response.url().includes("/local/cloud/"))
      localRequests.push({ path: new URL(response.url()).pathname, status: response.status() });
  });
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(url);
  await page.getByLabel("メールアドレス").fill("synthetic@example.test");
  await page.getByLabel("パスワード", { exact: true }).fill("synthetic-password");
  await page.getByRole("button", { name: "サインイン", exact: true }).click();
  await page.getByRole("button", { name: "テナント A", exact: true }).click();
  const consent = page.getByRole("region", { name: "Pi個人セッション", exact: true });
  assert.equal(await consent.getByRole("button", { name: "確認したPiで開始" }).isDisabled(), true);
  await consent
    .getByLabel("対象PiのID", { exact: true })
    .fill("60000000-0000-4000-8000-000000000001");
  await consent.getByRole("checkbox", { name: /共有空間に個人情報/ }).check();
  await consent.screenshot({ path: join(artifacts, "mobile-explicit-consent.png") });
  await consent.getByRole("button", { name: "確認したPiで開始" }).click();
  await consent.getByText("個人セッション中", { exact: true }).waitFor();
  assert.equal(await consent.getByLabel("次の会話の文字出力先").inputValue(), "phone");
  await consent.getByLabel("次の会話の文字出力先").selectOption("pi");
  await page.getByText("認証・切替の確認", { exact: true }).click();
  await page.getByRole("combobox", { name: /会話イベント/ }).selectOption("policy_lost");
  await selectChat("個人 個人の会話");
  await page
    .getByText("この会話を個人セッション中のPiに表示できるようにする", { exact: true })
    .click();
  await page.getByRole("button", { name: "確認してPiへの表示を許可・同じ操作を再確認" }).click();
  await page.getByRole("region", { name: "表示設定の再確認", exact: true }).waitFor();
  await selectChat("個人 個人の会話");
  await page.getByText("共有画面への表示: 本人の個人セッションのみ", { exact: true }).waitFor();
  await selectChat("共有 みんなの会話");
  await page
    .getByRole("region", { name: "表示設定の再確認", exact: true })
    .screenshot({ path: join(artifacts, "mobile-policy-exact-recovery.png") });
  await page
    .getByRole("button", { name: "元の会話の表示設定を同じ操作で再確認", exact: true })
    .click();
  await page
    .getByRole("region", { name: "表示設定の再確認", exact: true })
    .waitFor({ state: "detached" });
  await consent.getByLabel("次の会話の文字出力先").selectOption("phone");
  await selectChat("共有 みんなの会話");
  await page.getByLabel("メッセージ", { exact: true }).fill("UI10 POLICY RECOVERY SEND");
  await page.getByRole("button", { name: "送信", exact: true }).click();
  await page.getByText("UI10 POLICY RECOVERY SEND", { exact: true }).waitFor();
  await consent.getByLabel("次の会話の文字出力先").selectOption("pi");
  await consent.screenshot({ path: join(artifacts, "mobile-active-explicit-output.png") });
  await consent.getByRole("button", { name: "個人セッションを終了", exact: true }).click();
  await consent.getByText("クラウドで個人セッションを終了しました。", { exact: true }).waitFor();
  await consent.screenshot({ path: join(artifacts, "mobile-ended.png") });
  await page.getByRole("button", { name: "テナント B", exact: true }).click();
  assert.equal(await consent.getByLabel("対象PiのID", { exact: true }).inputValue(), "");
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  await selectChat("個人 個人の会話");
  await page.getByLabel("メッセージ", { exact: true }).fill("UI10-MOBILE-PRIVATE-BFCACHE");
  await page.getByRole("button", { name: "送信", exact: true }).click();
  await page.getByText("UI10-MOBILE-PRIVATE-BFCACHE", { exact: true }).waitFor();
  await page.evaluate(() => {
    addEventListener("pagehide", () =>
      sessionStorage.setItem(
        "ui10-mobile-hidden",
        String(document.body.textContent.includes("UI10-MOBILE-PRIVATE-BFCACHE")),
      ),
    );
    addEventListener("pageshow", (event) =>
      sessionStorage.setItem("ui10-mobile-restored", String(event.persisted)),
    );
  });
  await page.goto(`${url}/?new-document=1`);
  await page.goBack({ waitUntil: "commit" });
  await page.getByRole("heading", { name: "サインイン", exact: true }).waitFor();
  assert.equal(await page.evaluate(() => sessionStorage.getItem("ui10-mobile-hidden")), "false");
  assert.equal(await page.evaluate(() => sessionStorage.getItem("ui10-mobile-restored")), "true");
  assert.equal((await page.content()).includes("UI10-MOBILE-PRIVATE-BFCACHE"), false);
  console.log(
    "UI10 browser: mobile explicit consent/output/policy/end/tenant clear and real bfcache passed.",
  );
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto(device);
  const output = page.getByRole("region", { name: "Piの会話出力", exact: true });
  await output
    .getByLabel("確認済みの応答", { exact: true })
    .getByText("UI10 SYNTHETIC PRIVATE FINAL", { exact: true })
    .waitFor();
  await output.screenshot({ path: join(artifacts, "display-production-private.png") });
  assert.equal(await page.getByRole("heading", { name: "ネットにつながらないときも" }).count(), 1);
  const streamRequests = [];
  page.on("request", (request) => {
    if (request.url().includes("/local/cloud/runs/") && request.url().endsWith("/events"))
      streamRequests.push(request.url());
  });
  await writeFile(join(process.env.UI_FIXTURE_CONTROL, "live-run.request"), "synthetic");
  const liveRun = await marker("live-run.ready");
  await output.getByRole("button", { name: "会話の状態を確認", exact: true }).click();
  await output.getByRole("combobox", { name: /割り当てられた会話/ }).selectOption(liveRun);
  for (let attempt = 0; attempt < 200; attempt++) {
    const start = localRequests.findLastIndex((value) =>
      value.path.endsWith(`/runs/${liveRun}/events`),
    );
    if (
      start >= 0 &&
      localRequests.slice(start + 1).filter((value) => value.path === "/local/cloud/operations")
        .length >= 2
    )
      break;
    await page.waitForTimeout(50);
    if (attempt === 199) throw Error("Replacement history canonical reads did not finish");
  }
  await page.waitForTimeout(100);
  assert.ok(
    streamRequests.filter((url) => url.includes(liveRun)).length <= 2,
    "History must not reopen in a loop",
  );
  await writeFile(join(process.env.UI_FIXTURE_CONTROL, "live-run.resume"), "synthetic");
  await page.waitForTimeout(500);
  await output.screenshot({ path: join(artifacts, "display-live-receive-probe.png") });
  await output
    .getByLabel("生成途中の応答", { exact: true })
    .getByText("PRIVATE FINAL", { exact: true })
    .waitFor();
  await output.screenshot({ path: join(artifacts, "display-running-history-live.png") });
  await writeFile(join(process.env.UI_FIXTURE_CONTROL, "live-run.finish"), "synthetic");
  await output
    .getByLabel("確認済みの応答", { exact: true })
    .getByText("UI10 SYNTHETIC PRIVATE FINAL", { exact: true })
    .waitFor();
  await marker("live-run.done");
  const liveRequests = streamRequests.filter((url) => url.includes(liveRun));
  assert.ok(liveRequests.length >= 1 && liveRequests.length <= 2);
  await writeFile(
    join(artifacts, "display-running-history-requests.json"),
    JSON.stringify(
      {
        run: liveRun,
        requests: liveRequests.length,
        finalText: await output.getByLabel("確認済みの応答", { exact: true }).textContent(),
      },
      null,
      2,
    ),
  );
  await output.getByRole("button", { name: "このPiでの個人表示を終了", exact: true }).click();
  await output.getByText(/このPiでの個人表示を終了しました/).waitFor();
  assert.equal((await page.content()).includes("UI10 SYNTHETIC PRIVATE FINAL"), false);
  await output.screenshot({ path: join(artifacts, "display-production-stopped.png") });
  await page.reload();
  await output.getByRole("heading", { name: "共有状態", exact: true }).waitFor();
  await output.getByText(/このPiのID: [0-9a-f]/).waitFor();
  assert.equal((await page.content()).includes("UI10 SYNTHETIC PRIVATE FINAL"), false);
  const second = await browser.newPage();
  await second.goto(device);
  await second
    .getByRole("region", { name: "Piの会話出力", exact: true })
    .getByText(/このPiのID: [0-9a-f]/)
    .waitFor();
  assert.equal((await second.content()).includes("UI10 SYNTHETIC PRIVATE FINAL"), false);
  await second.close();
  assert.ok(process.env.UI_FIXTURE_CONTROL);
  await writeFile(join(process.env.UI_FIXTURE_CONTROL, "new-session.request"), "synthetic");
  for (let attempt = 0; attempt < 200; attempt++) {
    try {
      await access(join(process.env.UI_FIXTURE_CONTROL, "new-session.ready"));
      break;
    } catch {}
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  await page.reload();
  await output.getByText("UI10 SYNTHETIC PRIVATE FINAL", { exact: true }).waitFor();
  await page.evaluate(() => {
    addEventListener("pagehide", () =>
      sessionStorage.setItem(
        "ui10-hidden",
        String(document.body.textContent.includes("UI10 SYNTHETIC PRIVATE FINAL")),
      ),
    );
    addEventListener("pageshow", (event) =>
      sessionStorage.setItem("ui10-restored", String(event.persisted)),
    );
  });
  await page.goto(url);
  await page.goBack({ waitUntil: "commit" });
  await output.getByText(/このPiのID: [0-9a-f]/).waitFor();
  assert.equal(await page.evaluate(() => sessionStorage.getItem("ui10-hidden")), "false");
  assert.equal((await page.content()).includes("UI10 SYNTHETIC PRIVATE FINAL"), false);
  await page.waitForFunction(
    () =>
      sessionStorage.getItem("ui10-restored") !== null ||
      performance.getEntriesByType("navigation")[0]?.type === "back_forward",
  );
  const restored = await page.evaluate(() => ({
    persisted: sessionStorage.getItem("ui10-restored"),
    navigation: performance.getEntriesByType("navigation")[0]?.type,
    reason: performance.getEntriesByType("navigation")[0]?.notRestoredReasons,
  }));
  assert.ok(restored.persisted === "true" || restored.navigation === "back_forward");
  await writeFile(
    join(artifacts, "display-history-restoration.json"),
    JSON.stringify(restored, null, 2),
  );
  await output.screenshot({ path: join(artifacts, "display-restored-shared.png") });
  console.log(
    `UI10 browser: production display local HTTP, stop/reload/second-tab/history restore passed; bfcache persisted=${await page.evaluate(() => sessionStorage.getItem("ui10-restored"))}.`,
  );
  assert.deepEqual(errors, []);
} catch (error) {
  if (page && new URL(page.url()).origin === device) {
    const diagnostic = await page
      .evaluate(async () => {
        const response = await fetch("/local/cloud/snapshot");
        return { status: response.status, value: await response.json(), now: Date.now() };
      })
      .catch(() => ({ unavailable: true }));
    await writeFile(
      join(artifacts, "synthetic-snapshot.json"),
      JSON.stringify(diagnostic, null, 2),
    );
  }
  await page?.screenshot({ path: join(artifacts, "failure.png"), fullPage: true });
  throw error;
} finally {
  await writeFile(
    join(artifacts, "synthetic-local-requests.json"),
    JSON.stringify(localRequests, null, 2),
  );
  await browser?.close();
  for (const server of children) {
    server.kill("SIGTERM");
    if (server.exitCode === null) await new Promise((resolve) => server.once("exit", resolve));
  }
  await rm(directory, { recursive: true, force: true });
}
