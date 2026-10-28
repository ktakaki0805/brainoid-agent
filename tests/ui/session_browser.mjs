import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdir, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { chromium } from "playwright-core";

const artifacts = process.env.UI_ARTIFACTS;
if (!artifacts) throw Error("Set UI_ARTIFACTS to a dedicated evidence directory");
await mkdir(artifacts, { recursive: true });
const servers = [];
let context;
const profile = await mkdtemp(join(tmpdir(), "brainoid-ui-browser-"));
const errors = [];
async function server(app, port) {
  const outDir = join(profile, `${app}-build`);
  const build = spawn(
    process.execPath,
    ["node_modules/vite/bin/vite.js", "build", `apps/${app}`, "--mode", "fake", "--outDir", outDir],
    { stdio: ["ignore", "pipe", "pipe"] },
  );
  build.stdout.on("data", () => {});
  build.stderr.on("data", () => {});
  assert.equal(
    await new Promise((resolve) => build.once("exit", resolve)),
    0,
    "Fake build must succeed",
  );
  const child = spawn(
    process.execPath,
    [
      "node_modules/vite/bin/vite.js",
      "preview",
      `apps/${app}`,
      "--mode",
      "fake",
      "--outDir",
      outDir,
      "--host",
      "127.0.0.1",
      "--port",
      String(port),
    ],
    { stdio: ["ignore", "pipe", "pipe"] },
  );
  servers.push(child);
  child.stdout.on("data", () => {});
  child.stderr.on("data", () => {});
  const url = `http://127.0.0.1:${port}`;
  for (let attempt = 0; attempt < 100; attempt++) {
    if (child.exitCode !== null) throw Error("Vite exited early");
    try {
      if ((await fetch(url)).ok) return url;
    } catch {}
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw Error("Vite readiness timed out");
}
async function login(page) {
  await page.getByLabel("メールアドレス").fill("synthetic@example.test");
  await page.getByLabel("パスワード", { exact: true }).fill("synthetic-password");
  await page.getByRole("button", { name: "サインイン", exact: true }).click();
  await page.getByRole("heading", { name: "利用するテナント", exact: true }).waitFor();
}
try {
  const mobile = await server("mobile", 5173);
  const display = await server("display", 5174);
  context = await chromium.launchPersistentContext(profile, {
    executablePath: process.env.UI_CHROMIUM,
    headless: true,
    viewport: { width: 390, height: 844 },
    args: ["--disable-dev-shm-usage"],
    ignoreDefaultArgs: ["--disable-back-forward-cache"],
  });
  for (const [surface, url, viewport] of [
    ["mobile", mobile, { width: 390, height: 844 }],
    ["display", display, { width: 1280, height: 800 }],
  ]) {
    const page = await context.newPage();
    await page.setViewportSize(viewport);
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(url);
    await page.getByRole("heading", { name: "サインイン" }).waitFor();
    await page.screenshot({ path: join(artifacts, `${surface}-login.png`), fullPage: true });
    await login(page);
    await page.getByRole("button", { name: "テナント A", exact: true }).click();
    await page.getByRole("region", { name: "選択中のテナント" }).waitFor();
    await page.getByLabel("消去確認用の入力", { exact: true }).fill("PRIVATE-A-SYNTHETIC");
    await page.getByRole("button", { name: "テナント B", exact: true }).click();
    await page
      .getByRole("region", { name: "選択中のテナント" })
      .getByRole("heading", { name: "テナント B" })
      .waitFor();
    assert.equal(await page.getByLabel("消去確認用の入力", { exact: true }).inputValue(), "");
    await page.screenshot({ path: join(artifacts, `${surface}-tenant.png`), fullPage: true });
    assert.equal(
      await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      true,
    );
    await page.getByText("認証・切替の確認", { exact: true }).click();
    await page.getByLabel("A の応答を遅らせる").check();
    await page.getByRole("button", { name: "テナント A", exact: true }).click();
    await page.getByRole("button", { name: "テナント B", exact: true }).click();
    await page.waitForTimeout(1700);
    assert.equal(
      await page
        .getByRole("region", { name: "選択中のテナント" })
        .getByRole("heading")
        .textContent(),
      "テナント B",
    );
    await page.getByLabel("消去確認用の入力", { exact: true }).fill("PRIVATE-B-SYNTHETIC");
    await page.getByRole("button", { name: "サインアウト", exact: true }).click();
    await page.getByRole("heading", { name: "サインイン" }).waitFor();
    await page.goBack({ waitUntil: "commit" });
    assert.equal(await page.getByRole("region", { name: "選択中のテナント" }).count(), 0);
    assert.equal(await page.getByLabel("消去確認用の入力", { exact: true }).count(), 0);
    await login(page);
    for (const status of [403, 404]) {
      await page.getByLabel("選択時の応答").selectOption(String(status));
      await page.getByRole("button", { name: "テナント B", exact: true }).click();
      await page.getByRole("alert").waitFor();
      assert.equal(await page.getByRole("region", { name: "選択中のテナント" }).count(), 0);
      await page.getByRole("button", { name: "所属一覧を更新" }).click();
      await page.getByRole("button", { name: "テナント B", exact: true }).waitFor();
    }
    await page.getByLabel("選択時の応答").selectOption("401");
    await page.getByRole("button", { name: "テナント B", exact: true }).click();
    await page.getByRole("heading", { name: "サインイン" }).waitFor();
    await page.screenshot({ path: join(artifacts, `${surface}-expired.png`), fullPage: true });
    await page.goto(`${url}/?tenant=ffffffff-ffff-4fff-8fff-ffffffffffff`);
    await login(page);
    await page.getByRole("alert").waitFor();
    assert.equal(await page.getByRole("region", { name: "選択中のテナント" }).count(), 0);
    await page.getByRole("button", { name: "所属一覧を更新" }).click();
    await page.getByRole("button", { name: "テナント A", exact: true }).click();
    await page.getByLabel("消去確認用の入力", { exact: true }).fill("PRIVATE-BFCACHE-SYNTHETIC");
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
    // An actual cross-document navigation exercises pagehide and browser Back.
    await page.goto(`${url}/?away=1`);
    await login(page);
    await page.getByRole("button", { name: "サインアウト", exact: true }).click();
    await page.getByRole("heading", { name: "サインイン" }).waitFor();
    await page.goBack({ waitUntil: "commit" });
    assert.equal(await page.evaluate(() => sessionStorage.getItem("ui-bfcache-restored")), "true");
    assert.equal(await page.evaluate(() => sessionStorage.getItem("ui-hidden-private-dom")), "0");
    await page.getByRole("heading", { name: "サインイン" }).waitFor();
    assert.equal(await page.getByRole("region", { name: "選択中のテナント" }).count(), 0);
    assert.equal(await page.getByLabel("消去確認用の入力", { exact: true }).count(), 0);
    assert.equal(await page.getByRole("button", { name: "サインアウト", exact: true }).count(), 0);
    assert.equal((await page.content()).includes("PRIVATE-BFCACHE-SYNTHETIC"), false);
    await page.screenshot({
      path: join(artifacts, `${surface}-cross-document-back.png`),
      fullPage: true,
    });
    await login(page);
    await page.getByRole("button", { name: "サインアウト", exact: true }).click();
    await page.goto(`${url}/?after-signout=1`);
    await page.goBack({ waitUntil: "commit" });
    await page.getByRole("heading", { name: "サインイン" }).waitFor();
    assert.equal(await page.getByLabel("消去確認用の入力", { exact: true }).count(), 0);
    await page.close();
    console.log(
      `${surface}: login, selection, draft disposal, late A, signout Back, 401/403/404, unjoined URL, bfcache pageshow.persisted=true, pagehide privateDOM=0, second-document signout then Back stays signed out`,
    );
  }
  assert.deepEqual(errors, []);
} finally {
  await context?.close();
  for (const process of servers) {
    process.kill("SIGTERM");
    if (process.exitCode === null) await new Promise((resolve) => process.once("exit", resolve));
  }
  await rm(profile, { recursive: true, force: true });
}
