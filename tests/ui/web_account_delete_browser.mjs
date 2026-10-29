import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdir, mkdtemp, readFile, writeFile, rm } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { chromium } from "playwright-core";
const artifacts = process.env.UI_ARTIFACTS;
assert.ok(artifacts && process.env.UI_CHROMIUM);
await mkdir(artifacts, { recursive: true });
const directory = await mkdtemp(join(tmpdir(), "ba-ui-13-writer-browser-"));
const children = [],
  ownership = [],
  requests = [],
  errors = [];
const env = {
  ...process.env,
  VITE_API_URL: "http://127.0.0.1:8000",
  VITE_FIREBASE_API_KEY: "fixture-key",
  VITE_FIREBASE_AUTH_DOMAIN: "demo-brainoid.firebaseapp.com",
  VITE_FIREBASE_PROJECT_ID: "demo-brainoid",
  VITE_FIREBASE_APP_ID: "fixture-app",
  VITE_AUTH_EMULATOR: "http://127.0.0.1:9099",
};
const origin = "http://127.0.0.1:5200",
  locatorKey = "brainoid-account-operation-v1";
let browser,
  page,
  stage = "build",
  injectedStatus;
async function launch(args) {
  const child = spawn(process.execPath, args, { env, stdio: ["ignore", "pipe", "pipe"] });
  const record = {
    argv: args,
    pid: child.pid,
    birth: await readFile(`/proc/${child.pid}/stat`, "utf8"),
    start: new Date().toISOString(),
  };
  ownership.push(record);
  children.push(child);
  child.stdout.on("data", () => {});
  child.stderr.on("data", () => {});
  child.once("exit", (code, signal) => {
    record.exit = code;
    record.signal = signal;
    record.end = new Date().toISOString();
  });
  return child;
}
function at(value) {
  stage = value;
  console.log(`Web account deletion: ${stage}`);
}
try {
  const out = join(directory, "build");
  const build = await launch([
    "node_modules/vite/bin/vite.js",
    "build",
    "apps/web-data-controls",
    "--mode",
    "emulator",
    "--outDir",
    out,
  ]);
  assert.equal(await new Promise((resolve) => build.once("exit", resolve)), 0);
  const server = await launch([
    "node_modules/vite/bin/vite.js",
    "preview",
    "apps/web-data-controls",
    "--mode",
    "emulator",
    "--outDir",
    out,
    "--host",
    "127.0.0.1",
    "--strictPort",
    "--port",
    "5200",
  ]);
  for (let i = 0; i < 100; i++) {
    try {
      if ((await fetch(origin)).ok) break;
    } catch {}
    assert.equal(server.exitCode, null);
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  browser = await chromium.launchPersistentContext(join(directory, "profile"), {
    executablePath: process.env.UI_CHROMIUM,
    headless: true,
    viewport: { width: 390, height: 844 },
    ignoreDefaultArgs: ["--disable-back-forward-cache"],
  });
  await browser.route("**/*", async (route) => {
    const request = route.request(),
      url = new URL(request.url());
    if (url.hostname !== "127.0.0.1") return route.abort();
    if (url.port === "8000") {
      requests.push({
        method: request.method(),
        path: url.pathname,
        body: request.method() === "POST" ? request.postDataJSON() : undefined,
      });
      assert.equal(url.pathname.startsWith("/v1/operations"), true);
      if (injectedStatus && request.method() === "GET") {
        if (typeof injectedStatus === "number")
          return route.fulfill({
            status: injectedStatus,
            contentType: "application/json",
            body: "{}",
            headers: { "Access-Control-Allow-Origin": origin },
          });
        const response = await route.fetch(),
          data = await response.json();
        return route.fulfill({ response, json: { ...data, status: injectedStatus } });
      }
    }
    return route.continue();
  });
  page = await browser.newPage();
  page.on("pageerror", () => errors.push("pageerror"));
  const controls = page.getByRole("region", { name: "データの管理", exact: true });
  const login = async (email = "ui-delete@example.test") => {
    await page.getByLabel("メールアドレス", { exact: true }).fill(email);
    await page.getByLabel("パスワード", { exact: true }).fill("synthetic-password-only");
    await page.getByRole("button", { name: "サインイン", exact: true }).click();
    await controls.waitFor();
  };
  const reauth = async (email = "ui-delete@example.test") => {
    await page.getByLabel("削除確認のメールアドレス", { exact: true }).fill(email);
    await page.getByLabel("削除確認のパスワード", { exact: true }).fill("synthetic-password-only");
    await page.getByRole("button", { name: "削除の本人確認をする", exact: true }).click();
  };
  const screenshot = async (name) => {
    assert.equal(
      await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      true,
    );
    await page.screenshot({ path: join(artifacts, `${name}.png`), fullPage: true });
  };
  const postCount = () => requests.filter((r) => r.method === "POST").length;
  at("signed-out-mobile-no-app-or-private-state");
  await page.goto(
    `${origin}/?tenant=other&uid=other&kind=delete_tenant&apiUrl=https://attacker.invalid`,
  );
  await page.getByRole("heading", { name: "サインイン", exact: true }).waitFor();
  assert.equal(await controls.count(), 0);
  assert.equal(requests.length, 0);
  await screenshot("signed-out-mobile");
  at("malformed-array-locator-cleared-before-authenticated-request");
  await page.evaluate(async (key) => {
    const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode("demo-a-member"));
    const owner = Array.from(new Uint8Array(bytes), (v) => v.toString(16).padStart(2, "0")).join(
      "",
    );
    sessionStorage.setItem(
      key,
      JSON.stringify({ version: 1, owner, operation: ["10000000-0000-4000-8000-000000000001"] }),
    );
  }, locatorKey);
  await login();
  assert.equal(await page.evaluate((key) => sessionStorage.getItem(key), locatorKey), null);
  assert.equal(requests.length, 0);
  at("actual-reauthentication-before-explicit-target-confirmation");
  await controls.getByRole("button", { name: "本人アカウントの削除を確認", exact: true }).click();
  assert.equal(
    await page.getByRole("button", { name: "対象と範囲を確認して要求", exact: true }).count(),
    0,
  );
  assert.equal(postCount(), 0);
  await reauth("ui-other@example.test");
  await controls.getByText(/本人確認できませんでした/).waitFor();
  assert.equal(postCount(), 0);
  await reauth();
  await controls.getByRole("region", { name: "データ操作の確認", exact: true }).waitFor();
  assert.match(await controls.textContent(), /demo-a-member/);
  assert.equal(postCount(), 0);
  await screenshot("target-confirmation-mobile");
  await page.getByRole("button", { name: "対象と範囲を確認して要求", exact: true }).click();
  await controls.getByText(/受付済み・処理待ち/).waitFor();
  assert.equal(postCount(), 1);
  assert.deepEqual(
    requests.find((r) => r.method === "POST"),
    { method: "POST", path: "/v1/operations", body: { kind: "delete_account", confirm: true } },
  );
  const locator = await page.evaluate((key) => sessionStorage.getItem(key), locatorKey);
  assert.ok(locator);
  assert.equal(await page.evaluate(() => localStorage.length), 0);
  assert.deepEqual(await page.evaluate(() => Object.keys(sessionStorage)), [locatorKey]);
  await screenshot("accepted-mobile");
  at("real-sdk-memory-auth-cleared-on-reload-same-person-relogin-recovers");
  await page.reload();
  await page.getByRole("heading", { name: "サインイン", exact: true }).waitFor();
  assert.equal(await controls.count(), 0);
  await login();
  await controls.getByText(`受付番号: ${JSON.parse(locator).operation}`).waitFor();
  assert.equal(postCount(), 1);
  await screenshot("recovered-mobile");
  at("desktop-progress-statuses-and-unavailable-are-not-completion");
  await page.setViewportSize({ width: 1280, height: 900 });
  for (const [status, text] of [
    ["running", "処理中（完了ではありません）"],
    ["failed", "処理に失敗しました。消去完了ではありません。"],
    ["outcome_unknown", "処理結果を確認できません。消去完了ではありません。"],
    ["succeeded", "サーバーで削除処理の完了を確認しました"],
  ]) {
    injectedStatus = status;
    await controls.getByRole("button", { name: "進捗を確認", exact: true }).click();
    await controls.getByText(text, { exact: true }).waitFor();
    await screenshot(`injected-${status}-desktop`);
  }
  // Return to a real authenticated pending response before testing an unavailable result.
  injectedStatus = undefined;
  await controls.getByRole("button", { name: "進捗を確認", exact: true }).click();
  await controls.getByText(/受付済み・処理待ち/).waitFor();
  injectedStatus = 404;
  await controls.getByRole("button", { name: "進捗を確認", exact: true }).click();
  await controls.getByText(/消去完了とは判断できません/).waitFor();
  assert.equal(
    await controls.getByText("サーバーで削除処理の完了を確認しました", { exact: true }).count(),
    0,
  );
  injectedStatus = undefined;
  at("different-user-after-reload-and-forged-locator-reauthorization");
  await page.reload();
  await login("ui-other@example.test");
  assert.equal(await page.evaluate((key) => sessionStorage.getItem(key), locatorKey), null);
  assert.equal(await controls.getByRole("region", { name: "要求の進捗" }).count(), 0);
  await page.getByRole("button", { name: "サインアウト", exact: true }).click();
  await page.getByRole("heading", { name: "サインイン", exact: true }).waitFor();
  await page.evaluate(
    async ({ locator, locatorKey }) => {
      const value = JSON.parse(locator),
        bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode("demo-b-member"));
      value.owner = Array.from(new Uint8Array(bytes), (v) => v.toString(16).padStart(2, "0")).join(
        "",
      );
      sessionStorage.setItem(locatorKey, JSON.stringify(value));
    },
    { locator, locatorKey },
  );
  await login("ui-other@example.test");
  await controls.getByText(/消去完了とは判断できません/).waitFor();
  assert.equal(await controls.getByRole("region", { name: "要求の進捗" }).count(), 0);
  assert.equal(postCount(), 1);
  await page.getByRole("button", { name: "サインアウト", exact: true }).click();
  at("last-owner-real-api-rejection-is-not-acceptance");
  await login("ui-owner@example.test");
  await controls.getByRole("button", { name: "本人アカウントの削除を確認", exact: true }).click();
  await reauth("ui-owner@example.test");
  await page.getByRole("button", { name: "対象と範囲を確認して要求", exact: true }).click();
  await controls.getByText(/競合した要求は未完了/).waitFor();
  assert.equal(await controls.getByRole("region", { name: "要求の進捗" }).count(), 0);
  await screenshot("last-owner-rejected-desktop");
  await page.getByRole("button", { name: "サインアウト", exact: true }).click();
  at("real-auth-emulator-disables-user-api-rejects-and-clears-private-state");
  await page.evaluate(({ locator, locatorKey }) => sessionStorage.setItem(locatorKey, locator), {
    locator,
    locatorKey,
  });
  await login();
  await controls.getByText(`受付番号: ${JSON.parse(locator).operation}`).waitFor();
  const disabled = await fetch(
    "http://127.0.0.1:9099/identitytoolkit.googleapis.com/v1/projects/demo-brainoid/accounts:update",
    {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: "Bearer owner" },
      body: JSON.stringify({ localId: "demo-a-member", disableUser: true }),
    },
  );
  assert.equal(disabled.ok, true);
  await controls.getByRole("button", { name: "進捗を確認", exact: true }).click();
  await page.getByRole("heading", { name: "サインイン", exact: true }).waitFor();
  assert.equal(await controls.count(), 0);
  assert.equal(await page.evaluate((key) => sessionStorage.getItem(key), locatorKey), null);
  assert.equal(await page.getByRole("region", { name: "サインイン中の本人" }).count(), 0);
  await screenshot("expired-desktop");
  at("cross-document-back-clears-private-form-before-cache");
  await login("ui-other@example.test");
  await page.evaluate(() => {
    addEventListener("pagehide", () =>
      sessionStorage.setItem(
        "test-hidden-private",
        String(document.querySelectorAll('[aria-label="データの管理"]').length),
      ),
    );
    addEventListener("pageshow", (event) =>
      sessionStorage.setItem("test-bfcache", String(event.persisted)),
    );
  });
  await page.goto(`${origin}/?next=document`);
  await page.getByRole("heading", { name: "サインイン", exact: true }).waitFor();
  await page.goBack({ waitUntil: "commit" });
  await page.getByRole("heading", { name: "サインイン", exact: true }).waitFor();
  assert.equal(await page.evaluate(() => sessionStorage.getItem("test-hidden-private")), "0");
  assert.equal(await controls.count(), 0);
  await writeFile(
    join(artifacts, "observations.json"),
    JSON.stringify(
      {
        requests,
        errors,
        bfcacheRestored: await page.evaluate(() => sessionStorage.getItem("test-bfcache")),
        realProvider: "local Firebase Auth/Firestore emulators",
        injectedProgressStates: ["running", "failed", "outcome_unknown", "succeeded", 404],
      },
      null,
      2,
    ),
  );
  assert.deepEqual(errors, []);
  console.log("Web account deletion real SDK/API browser flows passed");
} catch (error) {
  await page?.screenshot({ path: join(artifacts, "failure.png"), fullPage: true });
  console.log(
    `Web account deletion failed at ${stage}: ${error instanceof Error ? error.name : "failure"}`,
  );
  process.exitCode = 1;
} finally {
  await browser?.close();
  for (const child of children)
    if (child.exitCode === null && child.signalCode === null) {
      child.kill("SIGTERM");
      await new Promise((resolve) => child.once("exit", resolve));
    }
  for (const record of ownership) {
    try {
      await readFile(`/proc/${record.pid}/stat`);
      record.closed = false;
    } catch {
      record.closed = true;
    }
  }
  await writeFile(join(artifacts, "ownership.json"), JSON.stringify(ownership, null, 2));
  await rm(directory, { recursive: true, force: true });
}
