import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdir, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { chromium } from "playwright-core";

const directory = await mkdtemp(join(tmpdir(), "ui-emulator-browser-"));
const artifacts = process.env.UI_ARTIFACTS;
if (!artifacts || process.env.UI_API_URL !== "http://127.0.0.1:8000")
  throw Error("Explicit local UI environment required");
await mkdir(artifacts, { recursive: true });
const env = {
  ...process.env,
  VITE_API_URL: "http://127.0.0.1:8000",
  VITE_FIREBASE_API_KEY: "fixture-key",
  VITE_FIREBASE_AUTH_DOMAIN: "demo-brainoid.firebaseapp.com",
  VITE_FIREBASE_PROJECT_ID: "demo-brainoid",
  VITE_FIREBASE_APP_ID: "fixture-app",
  VITE_AUTH_EMULATOR: "http://127.0.0.1:9099",
};
let child;
let browser;
let page;
let stage = "startup";
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
  build.stdout.on("data", () => {});
  build.stderr.on("data", () => {});
  assert.equal(await new Promise((resolve) => build.once("exit", resolve)), 0);
  child = spawn(
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
      "--port",
      "5175",
    ],
    { env, stdio: ["ignore", "pipe", "pipe"] },
  );
  child.stdout.on("data", () => {});
  child.stderr.on("data", () => {});
  for (let i = 0; i < 100; i++) {
    if (child.exitCode !== null) throw Error("Vite preview exited early");
    try {
      if ((await fetch("http://127.0.0.1:5175")).ok) break;
    } catch {}
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  browser = await chromium.launchPersistentContext(join(directory, "profile"), {
    executablePath: process.env.UI_CHROMIUM,
    headless: true,
    viewport: { width: 390, height: 844 },
    ignoreDefaultArgs: ["--disable-back-forward-cache"],
  });
  await browser.route("**/*", (route) =>
    new URL(route.request().url()).hostname === "127.0.0.1" ? route.continue() : route.abort(),
  );
  page = await browser.newPage();
  const errors = [];
  page.on("pageerror", () => errors.push("browser error"));
  page.on("requestfailed", (request) => {
    const url = new URL(request.url());
    if (url.port === "8000")
      console.log(
        `UI network failed: ${request.method()} ${url.pathname} ${request.failure()?.errorText}`,
      );
  });
  page.on("response", (response) => {
    const url = new URL(response.url());
    if (url.port === "8000") console.log(`UI response: ${response.status()} ${url.pathname}`);
  });
  await page.goto("http://127.0.0.1:5175");
  stage = "login form";
  await page.getByLabel("メールアドレス").fill("ui-owner@example.test");
  await page.getByLabel("パスワード", { exact: true }).fill("synthetic-password-only");
  await page.getByRole("button", { name: "サインイン", exact: true }).click();
  stage = "tenant list after sign-in";
  await page.getByRole("button", { name: "Fixture Tenant A", exact: true }).click();
  await page
    .getByRole("region", { name: "選択中のテナント" })
    .getByRole("heading", { name: "Fixture Tenant A" })
    .waitFor();
  await page.screenshot({ path: join(artifacts, "mobile-official-emulator.png"), fullPage: true });
  stage = "create tenant";
  await page.getByLabel("テナント名", { exact: true }).fill("Chromium emulator tenant");
  await page.getByRole("button", { name: "テナントを作成", exact: true }).click();
  await page
    .getByRole("region", { name: "選択中のテナント" })
    .getByRole("heading", { name: "Chromium emulator tenant" })
    .waitFor();
  stage = "cross-document signout and cached Back";
  await page.evaluate(() => {
    addEventListener("pageshow", (event) => {
      sessionStorage.setItem("ui-bfcache-restored", String(event.persisted));
    });
    addEventListener("pagehide", () => {
      sessionStorage.setItem(
        "ui-hidden-private-dom",
        String(document.querySelectorAll(".selected-tenant").length),
      );
    });
  });
  await page.goto("http://127.0.0.1:5175/?second-document=1");
  await page.getByLabel("メールアドレス").fill("ui-owner@example.test");
  await page.getByLabel("パスワード", { exact: true }).fill("synthetic-password-only");
  await page.getByRole("button", { name: "サインイン", exact: true }).click();
  await page.getByRole("button", { name: "サインアウト", exact: true }).click();
  await page.getByRole("heading", { name: "サインイン" }).waitFor();
  await page.goBack({ waitUntil: "commit" });
  await page.getByRole("heading", { name: "サインイン" }).waitFor();
  assert.equal(await page.evaluate(() => sessionStorage.getItem("ui-bfcache-restored")), "true");
  assert.equal(await page.evaluate(() => sessionStorage.getItem("ui-hidden-private-dom")), "0");
  assert.equal(await page.getByRole("region", { name: "選択中のテナント" }).count(), 0);
  assert.equal(await page.getByRole("button", { name: "サインアウト", exact: true }).count(), 0);
  await page.screenshot({
    path: join(artifacts, "mobile-official-emulator-cross-document-back.png"),
    fullPage: true,
  });
  await page.getByLabel("メールアドレス").fill("ui-owner@example.test");
  await page.getByLabel("パスワード", { exact: true }).fill("synthetic-password-only");
  await page.getByRole("button", { name: "サインイン", exact: true }).click();
  await page.getByRole("button", { name: "サインアウト", exact: true }).waitFor();
  stage = "signout";
  await page.getByRole("button", { name: "サインアウト", exact: true }).click();
  await page.goBack({ waitUntil: "commit" });
  await page.getByRole("heading", { name: "サインイン" }).waitFor();
  assert.equal(await page.getByRole("region", { name: "選択中のテナント" }).count(), 0);
  assert.equal(await page.evaluate(() => localStorage.length), 0);
  assert.deepEqual(errors, []);
  console.log(
    "Chromium official Firebase SDK/Auth/Firestore/API/CORS UI flow passed; second-document signout then Back stays signed out, pageshow.persisted=true, pagehide privateDOM=0",
  );
} catch {
  await page?.screenshot({ path: join(artifacts, "mobile-emulator-failure.png"), fullPage: true });
  console.log(`UI browser failed at: ${stage}`);
  process.exitCode = 1;
} finally {
  await browser?.close();
  child?.kill("SIGTERM");
  if (child?.exitCode === null) await new Promise((resolve) => child.once("exit", resolve));
  await rm(directory, { recursive: true, force: true });
}
