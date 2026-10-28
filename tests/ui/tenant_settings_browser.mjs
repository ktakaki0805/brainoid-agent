import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdir, mkdtemp, readFile, writeFile, rm } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { chromium } from "playwright-core";
const artifacts = process.env.UI_ARTIFACTS;
assert.ok(artifacts && process.env.UI_CHROMIUM);
await mkdir(artifacts, { recursive: true });
const emulator = !!process.env.UI_API_URL;
const directory = await mkdtemp(join(tmpdir(), "ba-ui-11-writer-browser-"));
const env = emulator
  ? {
      ...process.env,
      VITE_API_URL: process.env.UI_API_URL,
      VITE_FIREBASE_API_KEY: "fixture-key",
      VITE_FIREBASE_AUTH_DOMAIN: "demo-brainoid.firebaseapp.com",
      VITE_FIREBASE_PROJECT_ID: "demo-brainoid",
      VITE_FIREBASE_APP_ID: "fixture-app",
      VITE_AUTH_EMULATOR: "http://127.0.0.1:9099",
    }
  : process.env;
const ownership = [],
  children = [];
let browser,
  page,
  stage = "build";
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
  child.stdout.on("data", (chunk) => process.stdout.write(chunk));
  child.stderr.on("data", (chunk) => process.stderr.write(chunk));
  child.once("exit", (code, signal) => {
    record.exit = code;
    record.signal = signal;
    record.end = new Date().toISOString();
  });
  return child;
}
const stageAt = (value) => {
  stage = value;
  console.log(`Tenant settings browser stage: ${stage}`);
};
try {
  const out = join(directory, "build"),
    mode = emulator ? "emulator" : "fake";
  const build = await launch([
    "node_modules/vite/bin/vite.js",
    "build",
    "apps/mobile",
    "--mode",
    mode,
    "--outDir",
    out,
  ]);
  assert.equal(await new Promise((resolve) => build.once("exit", resolve)), 0);
  const server = await launch([
    "node_modules/vite/bin/vite.js",
    "preview",
    "apps/mobile",
    "--mode",
    mode,
    "--outDir",
    out,
    "--host",
    "127.0.0.1",
    "--strictPort",
    "--port",
    "5197",
  ]);
  for (let i = 0; i < 100; i++) {
    try {
      if ((await fetch("http://127.0.0.1:5197")).ok) break;
    } catch {}
    assert.equal(server.exitCode, null);
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  browser = await chromium.launchPersistentContext(join(directory, "profile"), {
    executablePath: process.env.UI_CHROMIUM,
    headless: true,
    viewport: { width: 390, height: 844 },
  });
  await browser.route("**/*", (route) =>
    new URL(route.request().url()).hostname === "127.0.0.1" ? route.continue() : route.abort(),
  );
  page = await browser.newPage();
  const errors = [];
  page.on("pageerror", () => errors.push("pageerror"));
  await page.goto("http://127.0.0.1:5197");
  const login = async (email) => {
    await page.getByLabel("メールアドレス", { exact: true }).fill(email);
    await page.getByLabel("パスワード", { exact: true }).fill("synthetic-password-only");
    await page.getByRole("button", { name: "サインイン", exact: true }).click();
    await page.getByRole("heading", { name: "利用するテナント", exact: true }).waitFor();
  };
  stageAt("owner-and-invitation");
  await login(emulator ? "ui-owner@example.test" : "synthetic@example.test");
  await page
    .getByRole("button", { name: emulator ? "Fixture Tenant A" : "テナント A", exact: true })
    .click();
  const settings = page.getByRole("region", { name: "会員と利用量", exact: true });
  const refresh = async () => {
    await settings.getByRole("button", { name: "会員・利用量を更新", exact: true }).click();
    await settings.getByRole("heading", { name: /利用量$/, level: 3 }).waitFor();
  };
  await refresh();
  await settings.getByRole("heading", { name: "テナント全体の利用量" }).waitFor();
  assert.equal(await settings.getByRole("link").count(), 0);
  await settings.getByRole("button", { name: "memberを招待", exact: true }).click();
  await settings.getByRole("button", { name: "確認して実行", exact: true }).click();
  const codeField = settings.getByLabel("共有する招待コード");
  await codeField.waitFor();
  const code = await codeField.inputValue();
  assert.match(code, /^[a-f0-9]{64}$/);
  assert.equal(
    await page.evaluate(
      (value) =>
        location.href.includes(value) ||
        JSON.stringify(localStorage).includes(value) ||
        JSON.stringify(sessionStorage).includes(value),
      code,
    ),
    false,
  );
  await settings.getByRole("button", { name: "コードを隠す" }).click();
  await settings.screenshot({ path: join(artifacts, "owner-settings.png") });
  await page.screenshot({ path: join(artifacts, "owner.png"), fullPage: true });
  if (!emulator) {
    await page.getByText("認証・切替の確認", { exact: true }).click();
    stageAt("role-quota-scope");
    const usage = settings.getByRole("region", { name: "利用量", exact: true });
    for (const role of ["owner", "admin", "member"]) {
      for (const quota of ["available", "limited", "exhausted"]) {
        await page.getByLabel("Pi管理の役割", { exact: true }).selectOption(role);
        await page.getByLabel("利用量の状態").selectOption(quota);
        await refresh();
        const text = await usage.textContent();
        assert.equal(text.includes("現在は利用できます"), false);
        if (role === "member") {
          assert.ok(text.includes("本人とテナントの枠の小さい方"));
          assert.ok(
            text.includes(
              quota === "available"
                ? "本人とテナントの利用枠には現在、空き"
                : quota === "limited"
                  ? "本人またはテナントの同時実行上限"
                  : "本人またはテナントの今月の利用上限",
            ),
          );
        } else {
          assert.ok(text.includes("本人の利用可否を示しません"));
          assert.equal(text.includes("本人とテナントの枠の小さい方"), false);
          assert.ok(
            text.includes(
              quota === "available"
                ? "テナント枠には現在、空き"
                : quota === "limited"
                  ? "テナントの同時実行上限"
                  : "テナントの今月の利用上限",
            ),
          );
        }
        assert.ok(await usage.evaluate((element) => element.scrollWidth <= element.clientWidth));
        await usage.screenshot({ path: join(artifacts, `usage-${role}-${quota}.png`) });
      }
    }
    stageAt("admin-boundary");
    await page.getByLabel("Pi管理の役割", { exact: true }).selectOption("admin");
    await refresh();
    assert.equal(
      await settings.getByRole("button", { name: "ownerを移譲", exact: true }).count(),
      0,
    );
    assert.equal(
      await settings.getByRole("button", { name: "ownerに変更", exact: true }).count(),
      0,
    );
    const own = settings.getByRole("article").filter({ hasText: "synthetic@example.test" });
    assert.equal(await own.getByRole("button").count(), 0);
    await settings.screenshot({ path: join(artifacts, "admin-settings.png") });
    stageAt("member-quota");
    await page.getByLabel("Pi管理の役割", { exact: true }).selectOption("member");
    await page.getByLabel("利用量の状態").selectOption("exhausted");
    await refresh();
    await settings.getByRole("heading", { name: "本人の利用量" }).waitFor();
    assert.equal(await settings.getByRole("heading", { name: "会員一覧" }).count(), 0);
    await settings.getByText(/今月の利用上限/).waitFor();
    await settings.screenshot({ path: join(artifacts, "member-settings.png") });
    await page.screenshot({ path: join(artifacts, "member.png"), fullPage: true });
    stageAt("reauthentication");
    await page.getByLabel("Pi管理の役割", { exact: true }).selectOption("owner");
    await page.getByLabel("利用量の状態").selectOption("limited");
    await refresh();
    const dot = settings.getByRole("article").filter({ hasText: "会員ID: .." });
    await dot.getByRole("button", { name: "所属を失効", exact: true }).click();
    await page.getByLabel("会員管理の動作").selectOption("recent");
    await settings.getByRole("button", { name: "確認して実行", exact: true }).click();
    const auth = settings.getByRole("form", { name: "管理操作の本人確認" });
    await auth.waitFor();
    await settings.screenshot({ path: join(artifacts, "reauthentication.png") });
    await auth.getByLabel("本人確認のメールアドレス").fill("synthetic@example.test");
    await auth.getByLabel("本人確認のパスワード").fill("synthetic-password-only");
    await auth.getByRole("button", { name: "本人確認する" }).click();
    await settings.getByText(/本人確認が完了/).waitFor();
    await settings.getByRole("button", { name: "同じ会員操作の結果を確認" }).click();
    await settings.getByText(/会員の変更を確認しました/).waitFor();
    stageAt("lost-transfer");
    const target = settings.getByRole("article").filter({ hasText: "u?/#+" });
    await target.getByRole("button", { name: "ownerを移譲", exact: true }).click();
    await page.getByLabel("会員管理の動作").selectOption("lost");
    await settings.getByRole("button", { name: "確認して実行", exact: true }).click();
    await settings.getByRole("button", { name: "同じ会員操作の結果を確認" }).waitFor();
    await settings.screenshot({ path: join(artifacts, "lost-transfer.png") });
    assert.equal(await settings.getByRole("heading", { name: "会員一覧" }).count(), 0);
    await settings.getByRole("button", { name: "同じ会員操作の結果を確認" }).click();
    await settings.getByText(/会員の変更を確認しました/).waitFor();
  }
  stageAt("accept-without-membership");
  await page.getByRole("button", { name: "サインアウト", exact: true }).click();
  await login(emulator ? "ui-new@example.test" : "new@example.test");
  const accept = page.getByRole("region", { name: "招待を受け取った方" });
  await accept.getByLabel("受け取った招待コード").fill(code);
  await accept.getByRole("button", { name: "招待を受諾して参加", exact: true }).click();
  await accept.getByText(/参加を確認しました/).waitFor();
  assert.equal(await accept.getByLabel("受け取った招待コード").inputValue(), "");
  if (emulator) {
    await page.getByRole("button", { name: "Fixture Tenant A", exact: true }).click();
    await refresh();
    await settings.getByRole("heading", { name: "本人の利用量" }).waitFor();
  }
  await accept.screenshot({ path: join(artifacts, "acceptance.png") });
  await page.screenshot({ path: join(artifacts, "accepted.png"), fullPage: true });
  await page.getByRole("button", { name: "サインアウト", exact: true }).click();
  await page.getByRole("heading", { name: "サインイン", exact: true }).waitFor();
  assert.deepEqual(errors, []);
  console.log(
    `Tenant settings browser ${emulator ? "real Auth/Firestore/API 3" : "fixture 6 flows + 9 role/quota states"} scenarios passed`,
  );
} catch (error) {
  console.log(`Tenant settings browser failed at: ${stage}`);
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
