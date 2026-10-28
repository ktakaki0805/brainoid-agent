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
  const mobile = await server("mobile", 5187);
  const display = await server("display", 5188);
  context = await chromium.launchPersistentContext(profile, {
    executablePath: process.env.UI_CHROMIUM,
    headless: true,
    viewport: { width: 390, height: 844 },
    args: ["--disable-dev-shm-usage"],
    ignoreDefaultArgs: ["--disable-back-forward-cache"],
  });
  const page = await context.newPage();
  activePage = page;
  page.on("pageerror", (error) => errors.push(error.message));
  const notes = page.getByRole("region", { name: "ノート", exact: true });
  const list = notes.getByRole("list", { name: "現在のノート", exact: true });
  const editor = notes.getByRole("form", { name: "ノートの保存フォーム" });
  const row = (title) =>
    list
      .locator(":scope > li")
      .filter({ has: page.getByRole("heading", { name: title, exact: true }) });
  const idle = async () => {
    await notes.getByRole("button", { name: "一覧を再取得", exact: true }).waitFor();
    await page.waitForFunction(
      () => !document.querySelector('section.notes button[type="submit"]')?.disabled,
    );
  };
  const scenario = async (value) => {
    await page.locator("details.preview-tools").evaluate((element) => {
      element.open = true;
    });
    await page.getByLabel("ノートの動作", { exact: true }).selectOption(value);
  };
  const capture = async (name) => {
    await notes.scrollIntoViewIfNeeded();
    assert.equal(
      await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      true,
    );
    await notes.screenshot({ path: join(artifacts, `${name}.png`) });
  };
  const boot = async (url = mobile) => {
    await page.goto(url);
    await login(page);
    await page.getByRole("button", { name: "テナント A", exact: true }).click();
    await row("週末の買い物").waitFor();
    await idle();
  };
  await boot();
  await capture("mobile-note-list");
  await notes.getByLabel("一覧の公開範囲", { exact: true }).selectOption("private");
  await row("週末の買い物").waitFor({ state: "hidden" });
  await idle();
  assert.equal(await list.locator(":scope > li").count(), 1);
  await capture("mobile-private-scope");
  await notes.getByLabel("一覧の公開範囲", { exact: true }).selectOption("all");
  await row("週末の買い物").waitFor();
  await idle();
  await notes.getByRole("button", { name: "ノートを作成", exact: true }).click();
  await editor.getByLabel("題名", { exact: true }).fill("新しい架空のノート");
  await editor.getByLabel("本文", { exact: true }).fill("保存したい本文 <script>unsafe()</script>");
  await editor.getByLabel("表示先", { exact: true }).selectOption("shared_display");
  assert.equal(
    await editor.getByRole("button", { name: "ノートを保存", exact: true }).isDisabled(),
    true,
  );
  await capture("mobile-private-display-blocked");
  await editor.getByLabel("表示先", { exact: true }).selectOption("never");
  await scenario("reject");
  await editor.getByRole("button", { name: "ノートを保存", exact: true }).click();
  await notes.getByRole("alert").filter({ hasText: "サーバーで拒否" }).waitFor();
  await capture("mobile-input-rejected");
  await scenario("lost_save");
  await editor.getByRole("button", { name: "ノートを保存", exact: true }).click();
  await notes.getByRole("button", { name: "同じ内容で受付を再確認" }).waitFor();
  assert.equal(await editor.getByLabel("本文", { exact: true }).isDisabled(), true);
  await capture("mobile-save-response-loss");
  await notes.getByRole("button", { name: "同じ内容で受付を再確認" }).click();
  await row("新しい架空のノート").waitFor();
  await idle();
  assert.equal(await editor.count(), 0);
  await notes.getByLabel("ノートを検索", { exact: true }).fill("保存したい");
  await notes.getByRole("button", { name: "検索", exact: true }).click();
  await row("週末の買い物").waitFor({ state: "hidden" });
  await idle();
  assert.equal(await list.locator(":scope > li").count(), 1);
  await capture("mobile-search");
  await notes.getByRole("button", { name: "一覧を再取得", exact: true }).click();
  await row("週末の買い物").waitFor();
  await idle();
  await row("週末の買い物").getByRole("button", { name: "編集", exact: true }).click();
  await editor.getByLabel("本文", { exact: true }).fill("競合しても残す入力");
  assert.equal(await editor.getByLabel("公開範囲", { exact: true }).isDisabled(), true);
  assert.equal(
    await notes.getByRole("button", { name: "ノートを作成", exact: true }).isDisabled(),
    true,
  );
  await notes.getByLabel("ノートを検索", { exact: true }).fill("読書");
  await notes.getByRole("button", { name: "検索", exact: true }).click();
  await row("週末の買い物").waitFor({ state: "hidden" });
  await idle();
  assert.equal(await editor.getByLabel("本文", { exact: true }).inputValue(), "競合しても残す入力");
  await scenario("conflict");
  await editor.getByRole("button", { name: "変更を保存", exact: true }).click();
  const conflict = notes.getByRole("region", { name: "ノートの競合" });
  await conflict.getByText("別の端末で更新した買い物", { exact: true }).waitFor();
  assert.equal(await editor.getByLabel("本文", { exact: true }).inputValue(), "競合しても残す入力");
  assert.equal(
    await editor.getByRole("button", { name: "変更を保存", exact: true }).isDisabled(),
    true,
  );
  await capture("mobile-conflict-retained-draft");
  await notes.getByRole("button", { name: "最新版を確認して編集を続ける" }).click();
  await editor.getByLabel("本文", { exact: true }).fill("最新版と合わせた入力");
  await editor.getByLabel("表示先", { exact: true }).selectOption("authenticated_session");
  await editor.getByRole("button", { name: "変更を保存", exact: true }).click();
  await row("週末の買い物").getByText("最新版と合わせた入力", { exact: true }).waitFor();
  await idle();
  await capture("mobile-saved-current");
  await row("週末の買い物").getByRole("button", { name: "削除", exact: true }).click();
  const confirmation = notes.getByRole("region", { name: "ノート削除の確認" });
  await confirmation.getByLabel("ノートから作られた記憶", { exact: true }).selectOption("false");
  await capture("mobile-delete-confirmation");
  await scenario("delete_failed");
  await confirmation.getByRole("button", { name: "このノートの削除を依頼" }).click();
  await notes.getByRole("button", { name: "同じ内容で受付を再確認" }).waitFor();
  assert.equal(await row("週末の買い物").count(), 1);
  await capture("mobile-delete-acceptance-failure");
  await scenario("lost_delete");
  await notes.getByRole("button", { name: "同じ内容で受付を再確認" }).click();
  await notes.getByRole("alert").filter({ hasText: "受付結果を確認できません" }).waitFor();
  await notes.getByRole("button", { name: "同じ内容で受付を再確認" }).click();
  const progress = notes.getByRole("region", { name: "ノート削除の進捗" });
  await progress.getByText("消去待ち", { exact: true }).waitFor();
  assert.equal(await row("週末の買い物").count(), 0);
  await capture("mobile-delete-accepted");
  for (const [mode, text] of [
    ["purge_failed", "消去処理に失敗・未完了"],
    ["purge_unknown", "消去結果を確認中・未完了"],
    ["normal", "消去完了"],
  ]) {
    await scenario(mode);
    await progress.getByRole("button", { name: "消去状況を確認" }).click();
    await progress.getByText(text, { exact: true }).waitFor();
    await capture(`mobile-${mode}`);
  }
  await scenario("empty");
  await notes.getByRole("button", { name: "一覧を再取得", exact: true }).click();
  await notes.getByText("表示できるノートはありません。", { exact: true }).waitFor();
  await idle();
  await capture("mobile-empty");
  await notes.getByRole("button", { name: "ノートを作成", exact: true }).click();
  await editor.getByLabel("題名", { exact: true }).fill("PRIVATE-NOTE-DRAFT");
  await editor.getByLabel("本文", { exact: true }).fill("PRIVATE-NOTE-BODY");
  await page.getByRole("button", { name: "テナント B", exact: true }).click();
  assert.equal((await page.content()).includes("PRIVATE-NOTE-BODY"), false);
  await page.goBack();
  await row("個人の読書メモ").waitFor({ state: "hidden" });
  assert.equal((await page.content()).includes("PRIVATE-NOTE-DRAFT"), false);
  await page.getByRole("button", { name: "サインアウト", exact: true }).click();
  await page.getByRole("heading", { name: "サインイン", exact: true }).waitFor();
  assert.equal(await notes.count(), 0);
  await boot();
  await scenario("denied");
  await notes.getByRole("button", { name: "一覧を再取得", exact: true }).click();
  await page.getByRole("alert").filter({ hasText: "許可されていません" }).waitFor();
  assert.equal(await notes.count(), 0);
  await page.screenshot({ path: join(artifacts, "mobile-permission-denied.png"), fullPage: true });
  await page.setViewportSize({ width: 1280, height: 800 });
  await boot(display);
  assert.equal((await notes.textContent()).includes("個人の読書メモ"), false);
  for (const name of ["編集", "削除", "ノートを作成"])
    assert.equal(await notes.getByRole("button", { name, exact: true }).count(), 0);
  await notes.getByText(/本人確認が必要/).waitFor();
  await capture("display-readonly-private-excluded");
  await page.evaluate(() => dispatchEvent(new Event("pagehide")));
  assert.equal(await notes.count(), 0);
  await page.close();
  assert.deepEqual(errors, []);
  console.log(
    "Notes browser passed: mobile create/search/scope, rejection/response-loss/412 draft/current policy, delete confirmation/acceptance/failure/unknown/completion, tenant/back/signout/pagehide erasure, shared display readonly.",
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
