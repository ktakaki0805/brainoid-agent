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
let activePage;
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
      "--strictPort",
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
  const mobile = await server("mobile", 5195),
    display = await server("display", 5196);
  context = await chromium.launchPersistentContext(profile, {
    executablePath: process.env.UI_CHROMIUM,
    headless: true,
    viewport: { width: 390, height: 844 },
    ignoreDefaultArgs: ["--disable-back-forward-cache"],
  });
  const page = await context.newPage();
  activePage = page;
  page.on("pageerror", (error) => errors.push(error.message));
  const region = page.getByRole("region", { name: "Piの管理", exact: true });
  let captures = 0;
  async function capture(name) {
    await region.scrollIntoViewIfNeeded();
    assert.equal(
      await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      true,
    );
    await region.screenshot({ path: join(artifacts, `${name}.png`) });
    captures++;
  }
  async function boot(url = mobile) {
    await page.goto(url);
    await login(page);
    await page.getByRole("button", { name: "テナント A", exact: true }).click();
    await region.getByRole("heading", { name: "Piの登録・管理" }).waitFor();
  }
  async function scenario(value, field = "Pi管理の動作") {
    await page.getByText("認証・切替の確認", { exact: true }).click();
    await page.getByLabel(field, { exact: true }).selectOption(value);
    await page.getByText("認証・切替の確認", { exact: true }).click();
  }
  async function pair(code = "ABCD1234", name = "画面から登録するPi") {
    await region.getByRole("button", { name: "Piを登録", exact: true }).click();
    await region.getByLabel("登録コード", { exact: true }).fill(code);
    await region.getByLabel("端末名", { exact: true }).fill(name);
    await region.getByRole("button", { name: "登録内容を確認", exact: true }).click();
    const confirm = region.getByRole("button", { name: "このテナントへ登録を承認", exact: true });
    assert.equal(await confirm.isDisabled(), true);
    await region.getByRole("checkbox", { name: /登録先「テナント A」/ }).check();
    return confirm;
  }
  await boot();
  await capture("mobile-list");
  let confirm = await pair();
  await capture("mobile-explicit-tenant-confirmation");
  await scenario("lost");
  await confirm.click();
  await region.getByRole("button", { name: "同じ操作の受付を再確認" }).waitFor();
  await capture("mobile-unknown");
  await region.getByRole("button", { name: "同じ操作の受付を再確認" }).click();
  await region
    .getByText("登録を承認しました。Pi側で接続状態を確認してください。", { exact: true })
    .waitFor();
  await capture("mobile-paired");
  await region.getByRole("button", { name: "許可設定を編集" }).click();
  await region.getByLabel("端末名", { exact: true }).fill("許可を変更するPi");
  await region.getByRole("checkbox", { name: "家電の操作", exact: true }).check();
  await scenario("conflict");
  await region.getByRole("button", { name: "この許可設定を保存" }).click();
  await region.getByRole("region", { name: "端末設定の競合" }).waitFor();
  await capture("mobile-conflict");
  await region.getByRole("button", { name: "現在の設定を確認した" }).click();
  await region
    .getByRole("checkbox", { name: "この端末のクラウド利用を停止することを確認しました" })
    .check();
  await region.getByRole("button", { name: "端末を失効", exact: true }).click();
  await region
    .getByText("クラウドで端末を失効しました。端末内の秘密の消去は未確認です。", { exact: true })
    .waitFor();
  await capture("mobile-revoked-physical-unconfirmed");
  await region.getByRole("button", { name: "失効したPiを再登録", exact: true }).click();
  await region.getByRole("heading", { name: "失効したPiの再登録" }).waitFor();
  await capture("mobile-repair");
  for (const value of ["expired", "reused", "cross_tenant"]) {
    await boot();
    confirm = await pair();
    await scenario(value);
    await confirm.click();
    await region.getByText(/登録できませんでした/).waitFor();
    assert.equal(await region.getByRole("region", { name: "端末の詳細" }).count(), 0);
    await capture(`mobile-${value}`);
  }
  await boot();
  await scenario("member", "Pi管理の役割");
  await region.getByRole("button", { name: "端末一覧を再取得" }).click();
  await region.getByText(/オーナーまたは管理者の権限が必要/).waitFor();
  assert.equal(await region.getByRole("button").count(), 0);
  await capture("mobile-member");
  await boot();
  await pair("PRIV1234", "PRIVATE-PAIRING-DRAFT");
  await page.getByRole("button", { name: "テナント B", exact: true }).click();
  assert.equal((await region.textContent()).includes("PRIVATE-PAIRING-DRAFT"), false);
  await capture("mobile-tenant-cleared");
  await boot();
  await pair("PRIV5678", "PRIVATE-BFCACHE-PAIR");
  await page.evaluate(() => {
    addEventListener("pageshow", (event) =>
      sessionStorage.setItem("device-restored", String(event.persisted)),
    );
    addEventListener("pagehide", () =>
      sessionStorage.setItem(
        "device-hidden",
        String(document.body.textContent.includes("PRIVATE-BFCACHE-PAIR")),
      ),
    );
  });
  await page.goto(`${mobile}/?new-document=1`);
  await login(page);
  await page.getByRole("button", { name: "サインアウト", exact: true }).click();
  await page.goBack({ waitUntil: "commit" });
  await page.getByRole("heading", { name: "サインイン", exact: true }).waitFor();
  assert.equal(await page.evaluate(() => sessionStorage.getItem("device-restored")), "true");
  assert.equal(await page.evaluate(() => sessionStorage.getItem("device-hidden")), "false");
  assert.equal((await page.content()).includes("PRIVATE-BFCACHE-PAIR"), false);
  await page.screenshot({ path: join(artifacts, "mobile-bfcache-cleared.png"), fullPage: true });
  captures++;
  await page.setViewportSize({ width: 1280, height: 800 });
  await boot(display);
  assert.equal(await region.getByRole("button").count(), 0);
  await capture("display-guidance");
  assert.deepEqual(errors, []);
  console.log(
    `Devices browser passed: ${captures} states, explicit tenant, lost/expired/reused/cross-tenant, conflict/revoke/re-pair, member, tenant and bfcache persisted=true.`,
  );
} catch (error) {
  await activePage?.screenshot({ path: join(artifacts, "failure.png"), fullPage: true });
  throw error;
} finally {
  await context?.close();
  for (const child of servers) {
    child.kill("SIGTERM");
    if (child.exitCode === null) await new Promise((resolve) => child.once("exit", resolve));
  }
  await rm(profile, { recursive: true, force: true });
}
