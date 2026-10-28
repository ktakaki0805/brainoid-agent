import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { access, mkdir, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { chromium } from "playwright-core";
const artifacts = process.env.UI_ARTIFACTS,
  marker = process.env.UI_REGISTRATION_MARKER;
const publicPairing = JSON.parse(process.env.UI_PAIRING_PUBLIC ?? "null");
assert.ok(artifacts && marker && process.env.UI_CHROMIUM);
assert.deepEqual(Object.keys(publicPairing).sort(), ["expires_at", "user_code"]);
assert.match(publicPairing.user_code, /^[A-Z0-9]{8}$/);
await mkdir(artifacts, { recursive: true });
const directory = await mkdtemp(join(tmpdir(), "ba-ui-09-browser-")),
  children = [];
const env = {
  ...process.env,
  VITE_API_URL: "http://127.0.0.1:8000",
  VITE_FIREBASE_API_KEY: "fixture-key",
  VITE_FIREBASE_AUTH_DOMAIN: "demo-brainoid.firebaseapp.com",
  VITE_FIREBASE_PROJECT_ID: "demo-brainoid",
  VITE_FIREBASE_APP_ID: "fixture-app",
  VITE_AUTH_EMULATOR: "http://127.0.0.1:9099",
};
let browser,
  page,
  stage = "build";
const stageAt = (value) => {
  stage = value;
  console.log(`Device UI stage: ${stage}`);
};
try {
  const outDir = join(directory, "build");
  const build = spawn(
    process.execPath,
    [
      "node_modules/vite/bin/vite.js",
      "build",
      "apps/mobile",
      "--mode",
      "emulator",
      "--outDir",
      outDir,
    ],
    { env, stdio: ["ignore", "pipe", "pipe"] },
  );
  build.stdout.resume();
  build.stderr.resume();
  assert.equal(await new Promise((resolve) => build.once("exit", resolve)), 0);
  const server = spawn(
    process.execPath,
    [
      "node_modules/vite/bin/vite.js",
      "preview",
      "apps/mobile",
      "--mode",
      "emulator",
      "--outDir",
      outDir,
      "--host",
      "127.0.0.1",
      "--strictPort",
      "--port",
      "5195",
    ],
    { env, stdio: ["ignore", "pipe", "pipe"] },
  );
  children.push(server);
  server.stdout.resume();
  server.stderr.resume();
  for (let i = 0; i < 100; i++) {
    if (server.exitCode !== null) throw Error("Owned Vite failed");
    try {
      if ((await fetch("http://127.0.0.1:5195")).ok) break;
    } catch {}
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  browser = await chromium.launchPersistentContext(join(directory, "profile"), {
    executablePath: process.env.UI_CHROMIUM,
    headless: true,
    viewport: { width: 390, height: 844 },
  });
  const destinations = new Set(),
    errors = [],
    management = [];
  await browser.route("**/*", (route) =>
    new URL(route.request().url()).hostname === "127.0.0.1" ? route.continue() : route.abort(),
  );
  page = await browser.newPage();
  let confirmationAttempts = 0;
  await page.route("**/v1/tenants/*/device-pairings:confirm", async (route) => {
    confirmationAttempts++;
    if (confirmationAttempts === 1) {
      const accepted = await route.fetch();
      assert.equal(accepted.status(), 201);
      await accepted.dispose();
      await route.abort("failed"); // The actual API committed; only its response is lost.
    } else if (confirmationAttempts === 2) {
      await route.fulfill({ status: 429, contentType: "application/json", body: "{}" });
    } else await route.continue();
  });
  page.on("pageerror", () => errors.push("browser error"));
  page.on("request", (request) => {
    const u = new URL(request.url());
    destinations.add(u.origin);
    if (/device-pairings:confirm|\/devices\//.test(u.pathname))
      management.push({
        method: request.method(),
        path: u.pathname,
        key: request.headers()["idempotency-key"],
        body: request.postData(),
      });
  });
  stageAt("login");
  await page.goto("http://127.0.0.1:5195");
  await page.getByLabel("メールアドレス").fill("ui-owner@example.test");
  await page.getByLabel("パスワード", { exact: true }).fill("synthetic-password-only");
  await page.getByRole("button", { name: "サインイン", exact: true }).click();
  await page.getByRole("button", { name: "Fixture Tenant A", exact: true }).click();
  const region = page.getByRole("region", { name: "Piの管理", exact: true });
  stageAt("pairing form");
  await region.getByRole("button", { name: "Piを登録", exact: true }).click();
  await region.getByLabel("登録コード", { exact: true }).fill(publicPairing.user_code);
  await region.getByLabel("端末名", { exact: true }).fill("Equivalent service from mobile");
  await region.getByRole("button", { name: "登録内容を確認", exact: true }).click();
  const confirm = region.getByRole("button", { name: "このテナントへ登録を承認", exact: true });
  assert.equal(await confirm.isDisabled(), true);
  assert.equal(management.filter((v) => v.method === "POST").length, 0);
  await region.screenshot({ path: join(artifacts, "actual-api-explicit-tenant-confirmation.png") });
  await region.getByRole("checkbox", { name: /登録先「Fixture Tenant A」/ }).check();
  await confirm.click();
  stageAt("committed response loss");
  const retry = region.getByRole("button", { name: "同じ操作の受付を再確認", exact: true });
  await retry.waitFor();
  await region.getByText(/現在の権限: オーナー/).waitFor();
  await region.screenshot({ path: join(artifacts, "actual-api-lost-confirmation.png") });
  await retry.click();
  stageAt("retry rate limited");
  await region
    .getByText(
      "試行回数の上限です。前回の結果は未確認です。時間をおいて同じ操作の受付を再確認してください。",
      { exact: true },
    )
    .waitFor();
  assert.equal(
    await region.getByRole("button", { name: "Piを登録", exact: true }).isDisabled(),
    true,
  );
  await region.screenshot({ path: join(artifacts, "actual-api-unknown-rate-limited.png") });
  await retry.click();
  stageAt("API approval");
  await region
    .getByText("登録を承認しました。Pi側で接続状態を確認してください。", { exact: true })
    .waitFor();
  stageAt("device exchange");
  let exchanged = false;
  for (let i = 0; i < 300; i++) {
    try {
      await access(marker);
      exchanged = true;
      break;
    } catch {}
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  assert.equal(exchanged, true);
  await region.screenshot({ path: join(artifacts, "actual-api-device-exchanged.png") });
  const selected = region.getByRole("region", { name: "端末の詳細" });
  const deviceText = await selected.textContent();
  assert.ok(deviceText.includes("Equivalent service from mobile"));
  stageAt("revoke");
  await region
    .getByRole("checkbox", { name: "この端末のクラウド利用を停止することを確認しました" })
    .check();
  await region.getByRole("button", { name: "端末を失効", exact: true }).click();
  await region
    .getByText("クラウドで端末を失効しました。端末内の秘密の消去は未確認です。", { exact: true })
    .waitFor();
  await region.screenshot({ path: join(artifacts, "actual-api-revoked-physical-unconfirmed.png") });
  const confirmations = management.filter((v) => v.method === "POST");
  assert.equal(confirmations.length, 3);
  assert.deepEqual(confirmations, [confirmations[0], confirmations[0], confirmations[0]]);
  assert.equal(management.filter((v) => v.method === "DELETE").length, 1);
  assert.deepEqual(errors, []);
  assert.equal(
    [...destinations].every((v) => /^http:\/\/127\.0\.0\.1:(5195|8000|9099)$/.test(v)),
    true,
  );
  assert.equal(
    await page.evaluate(() =>
      Object.keys(localStorage).some((k) => /device|pairing|credential/i.test(k)),
    ),
    false,
  );
  assert.doesNotMatch(await region.textContent(), /poll_secret|access_token|refresh_token/);
  console.log("Production devices browser and equivalent service passed");
} catch {
  console.log(`Device UI stage: FAILED ${stage}`);
  await page?.screenshot({ path: join(artifacts, "actual-api-failure.png"), fullPage: true });
  process.exitCode = 1;
} finally {
  await browser?.close();
  for (const child of children) {
    child.kill("SIGTERM");
    if (child.exitCode === null) await new Promise((resolve) => child.once("exit", resolve));
  }
  await rm(directory, { recursive: true, force: true });
}
