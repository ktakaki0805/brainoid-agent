import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdir, mkdtemp, readFile, writeFile, rm } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { chromium } from "playwright-core";
const artifacts = process.env.UI_ARTIFACTS;
assert.ok(artifacts && process.env.UI_CHROMIUM);
await mkdir(artifacts, { recursive: true });
const directory = await mkdtemp(join(tmpdir(), "ba-ui-12-writer-browser-"));
const children = [],
  ownership = [];
let browser,
  page,
  stage = "build";
async function launch(args) {
  const child = spawn(process.execPath, args, {
    env: process.env,
    stdio: ["ignore", "pipe", "pipe"],
  });
  const record = {
    argv: args,
    pid: child.pid,
    birth: await readFile(`/proc/${child.pid}/stat`, "utf8"),
    start: new Date().toISOString(),
  };
  ownership.push(record);
  children.push(child);
  child.stdout.on("data", (chunk) => process.stdout.write(chunk));
  child.stderr.on("data", (chunk) => process.stderr.write(chunk));
  child.once("exit", (code, signal) => {
    record.exit = code;
    record.signal = signal;
    record.end = new Date().toISOString();
  });
  return child;
}
async function serve(surface, port) {
  const out = join(directory, surface);
  const build = await launch([
    "node_modules/vite/bin/vite.js",
    "build",
    `apps/${surface}`,
    "--mode",
    "fake",
    "--outDir",
    out,
  ]);
  assert.equal(await new Promise((resolve) => build.once("exit", resolve)), 0);
  const server = await launch([
    "node_modules/vite/bin/vite.js",
    "preview",
    `apps/${surface}`,
    "--mode",
    "fake",
    "--outDir",
    out,
    "--host",
    "127.0.0.1",
    "--strictPort",
    "--port",
    String(port),
  ]);
  for (let i = 0; i < 100; i++) {
    try {
      if ((await fetch(`http://127.0.0.1:${port}`)).ok) return;
    } catch {}
    assert.equal(server.exitCode, null);
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("Preview did not start");
}
const stageAt = (value) => {
  stage = value;
  console.log(`Data controls browser: ${stage}`);
};
try {
  await serve("mobile", 5198);
  await serve("display", 5199);
  browser = await chromium.launchPersistentContext(join(directory, "profile"), {
    executablePath: process.env.UI_CHROMIUM,
    headless: true,
    viewport: { width: 390, height: 844 },
    acceptDownloads: true,
  });
  await browser.route("**/*", (route) =>
    new URL(route.request().url()).hostname === "127.0.0.1" ? route.continue() : route.abort(),
  );
  page = await browser.newPage();
  const errors = [];
  page.on("pageerror", () => errors.push("pageerror"));
  await page.goto("http://127.0.0.1:5198");
  const login = async () => {
    await page.getByLabel("メールアドレス", { exact: true }).fill("synthetic@example.test");
    await page.getByLabel("パスワード", { exact: true }).fill("synthetic-only");
    await page.getByRole("button", { name: "サインイン", exact: true }).click();
    await page.getByRole("heading", { name: "利用するテナント", exact: true }).waitFor();
  };
  await login();
  await page.getByRole("button", { name: "テナント A", exact: true }).click();
  const controls = page.getByRole("region", { name: "データの管理", exact: true });
  const scenario = async (value) => {
    const details = page.locator("details.preview-tools");
    if (!(await details.evaluate((e) => e.open))) await details.locator("summary").click();
    await page.getByLabel("データ管理の動作", { exact: true }).selectOption(value);
  };
  const reset = async () => {
    await scenario("normal");
    await page.getByRole("button", { name: "テナント B", exact: true }).click();
    await page.getByRole("button", { name: "テナント A", exact: true }).click();
    await controls.getByRole("heading", { name: "現在の対象: テナント A" }).waitFor();
  };
  const confirm = async () =>
    controls.getByRole("button", { name: "対象と範囲を確認して要求", exact: true }).click();
  const ready = async () => {
    await controls.getByRole("button", { name: "エクスポートを確認", exact: true }).click();
    assert.equal(await controls.getByText(/この会話内の他の参加者/).count(), 0);
    assert.equal(await controls.getByText(/削除したデータは元に戻せません/).count(), 0);
    await confirm();
    await controls.getByText(/受付済み・処理待ち/).waitFor();
    await controls.getByRole("button", { name: "進捗を確認", exact: true }).click();
    await controls.getByText("生成完了", { exact: true }).waitFor();
  };
  stageAt("export-confirmation-progress-download");
  await ready();
  assert.ok(await controls.evaluate((e) => e.scrollWidth <= e.clientWidth));
  await controls.screenshot({ path: join(artifacts, "export-ready.png") });
  const downloadEvent = page.waitForEvent("download");
  await controls.getByRole("button", { name: "認証してZIPを取得", exact: true }).click();
  const downloaded = await downloadEvent;
  assert.equal(downloaded.suggestedFilename(), "brainoid-export.zip");
  assert.equal(await downloaded.failure(), null);
  const content = await readFile(await downloaded.path());
  assert.equal(content.readUInt32LE(0), 0x04034b50);
  assert.equal(content.readUInt32LE(content.length - 22), 0x06054b50);
  assert.equal(await controls.locator("a[href]").count(), 0);
  assert.equal(
    await page.evaluate(
      () =>
        JSON.stringify(localStorage).includes("fixture-token") ||
        JSON.stringify(sessionStorage).includes("fixture-token") ||
        location.href.includes("token"),
    ),
    false,
  );
  await controls.getByRole("button", { name: "結果を確認して閉じる" }).click();
  await ready();
  stageAt("revoked-completed-export");
  await scenario("expired");
  await controls.getByRole("button", { name: "認証してZIPを取得" }).click();
  await controls.getByText(/取得・保存・共有を完了できません/).waitFor();
  assert.equal(await controls.getByRole("region", { name: "要求の進捗" }).count(), 0);
  await controls.screenshot({ path: join(artifacts, "export-revoked.png") });
  stageAt("account-reauth-and-finalization-unknown");
  await reset();
  await scenario("recent");
  await controls.getByRole("button", { name: "本人アカウントの削除を確認", exact: true }).click();
  await confirm();
  const auth = controls.getByRole("form", { name: "削除操作の本人確認" });
  await auth.waitFor();
  await auth.getByLabel("削除確認のメールアドレス").fill("synthetic@example.test");
  await auth.getByLabel("削除確認のパスワード").fill("synthetic-secret-only");
  await auth.getByRole("button", { name: "削除の本人確認をする" }).click();
  await controls.getByText(/本人確認ができました/).waitFor();
  assert.equal(await controls.getByLabel("削除確認のパスワード").count(), 0);
  await controls.getByRole("button", { name: "同じ要求の受付を確認" }).click();
  await controls.getByText(/受付済み・処理待ち/).waitFor();
  await scenario("account_finalized");
  await controls.getByRole("button", { name: "進捗を確認" }).click();
  await controls.getByText(/消去完了とは判断できません/).waitFor();
  assert.equal(
    await controls.getByText("サーバーで削除処理の完了を確認しました", { exact: true }).count(),
    0,
  );
  await controls.screenshot({ path: join(artifacts, "account-progress-unavailable.png") });
  stageAt("last-owner-and-current-tenant-confirmation");
  await reset();
  await scenario("last_owner");
  await controls.getByRole("button", { name: "このテナントからの退出を確認" }).click();
  await confirm();
  await controls.getByText(/最後の所有者（owner）は先に/).waitFor();
  await controls.getByRole("button", { name: "拒否された要求を閉じて対象を選び直す" }).click();
  await scenario("normal");
  await controls.getByLabel("削除するテナント名を入力").fill("テナント B");
  assert.equal(
    await controls.getByRole("button", { name: "テナント全体の削除を確認" }).isEnabled(),
    false,
  );
  await controls.getByLabel("削除するテナント名を入力").fill("テナント A");
  await controls.getByRole("button", { name: "テナント全体の削除を確認" }).click();
  assert.equal(await controls.getByText(/この会話内の他の参加者/).count(), 0);
  await controls.screenshot({ path: join(artifacts, "tenant-confirmation.png") });
  await controls.getByRole("button", { name: "戻る", exact: true }).click();
  stageAt("conversation-scope-and-private-display-clearing");
  await controls.getByRole("button", { name: "削除する会話を選ぶ" }).click();
  const derived = controls.getByLabel("派生記憶も同時に削除する");
  await derived.waitFor();
  assert.equal(await derived.isChecked(), true);
  await derived.uncheck();
  const target = controls.getByRole("button", { name: /^会話「.*」の削除を確認$/ }).last();
  const title = (await target.textContent()).match(/会話「(.*)」/)[1];
  await target.click();
  await controls.getByText(/派生記憶: 保持/).waitFor();
  await controls.getByText(/この会話内の他の参加者の発言も削除対象/).waitFor();
  await confirm();
  await controls.getByText(/受付済み・処理待ち/).waitFor();
  assert.equal((await controls.textContent()).includes(title), false);
  assert.equal(await page.getByRole("region", { name: "選択中のテナント" }).count(), 0);
  await controls.screenshot({ path: join(artifacts, "conversation-accepted.png") });
  await page.getByRole("button", { name: "サインアウト", exact: true }).click();
  await page.getByRole("heading", { name: "サインイン", exact: true }).waitFor();
  assert.equal(await page.getByRole("region", { name: "データの管理", exact: true }).count(), 0);
  stageAt("shared-display-no-personal-controls");
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto("http://127.0.0.1:5199");
  await login();
  const display = page.getByRole("region", { name: "データの管理", exact: true });
  await display.waitFor();
  assert.equal(await display.locator("button,input,a").count(), 0);
  assert.match(await display.textContent(), /本人のスマホ/);
  await display.screenshot({ path: join(artifacts, "display-guidance.png") });
  assert.deepEqual(errors, []);
  console.log("Data controls browser: 6 flows passed on mobile/display");
} catch (error) {
  console.log(`Data controls failed at ${stage}`);
  throw error;
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
