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
  const mobile = await server("mobile", 5185);
  const display = await server("display", 5186);
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
  const memories = page.getByRole("region", { name: "記憶", exact: true });
  const list = memories.getByRole("list", { name: "現在の記憶" });
  const editor = memories.getByRole("form", { name: "記憶の保存フォーム" });
  const scenario = async (value) => {
    const tools = page.locator("details.preview-tools");
    await tools.evaluate((element) => {
      element.open = true;
    });
    await page.getByLabel("記憶の動作", { exact: true }).selectOption(value);
  };
  const boot = async () => {
    await page.goto(mobile);
    await login(page);
    await page.getByRole("button", { name: "テナント A", exact: true }).click();
    await list.getByText("個人だけの読書の好み", { exact: true }).waitFor();
  };
  const fill = async () => {
    await memories.getByRole("button", { name: "記憶を保存する", exact: true }).click();
    await editor.getByLabel("対象", { exact: true }).selectOption({ label: "リビング（共有）" });
    await editor
      .getByLabel("内容", { exact: true })
      .fill("新しく保存する架空の好み <script>unsafe()</script>");
  };
  const capture = async (name) => {
    await memories.scrollIntoViewIfNeeded();
    assert.equal(
      await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      true,
    );
    await memories.screenshot({ path: join(artifacts, `${name}.png`) });
  };
  await boot();
  await capture("mobile-memory-list");
  await fill();
  await editor.getByLabel("表示先", { exact: true }).selectOption("shared_display");
  assert.equal(await editor.getByRole("button", { name: "明示的に保存" }).isDisabled(), true);
  await capture("mobile-private-blocked");
  await editor.getByLabel("表示先", { exact: true }).selectOption("never");
  await scenario("reject");
  await editor.getByRole("button", { name: "明示的に保存" }).click();
  await memories.getByRole("alert").filter({ hasText: "サーバーで拒否" }).waitFor();
  await capture("mobile-api-rejection");
  await scenario("lost_save");
  await editor.getByRole("button", { name: "明示的に保存" }).click();
  await memories.getByRole("button", { name: "同じ内容で受付を再確認" }).waitFor();
  assert.equal(await editor.getByLabel("内容", { exact: true }).isDisabled(), true);
  await capture("mobile-save-response-loss");
  await memories.getByRole("button", { name: "同じ内容で受付を再確認" }).click();
  await list
    .getByText("新しく保存する架空の好み <script>unsafe()</script>", { exact: true })
    .waitFor();
  assert.equal(await editor.count(), 0);
  await memories.getByLabel("記憶を検索", { exact: true }).fill("新しく保存");
  await memories.getByRole("button", { name: "検索", exact: true }).click();
  await list.getByText("夜は穏やかな照明", { exact: true }).waitFor({ state: "hidden" });
  assert.equal(await list.locator(":scope > li").count(), 1);
  await capture("mobile-saved-search");
  await memories.getByRole("button", { name: "一覧を再取得", exact: true }).click();
  await list.getByText("夜は穏やかな照明", { exact: true }).waitFor();
  const shared = list.locator(":scope > li").filter({ hasText: "夜は穏やかな照明" });
  await shared.getByRole("button", { name: "訂正", exact: true }).click();
  await editor.getByLabel("内容", { exact: true }).fill("入力を保持したい照明設定");
  assert.equal(await editor.getByLabel("公開範囲").isDisabled(), true);
  await scenario("conflict");
  await editor.getByRole("button", { name: "訂正を保存" }).click();
  await memories.getByRole("region", { name: "記憶の競合" }).waitFor();
  await list.getByText("別の端末で更新した好み", { exact: true }).waitFor();
  assert.equal(
    await editor.getByLabel("内容", { exact: true }).inputValue(),
    "入力を保持したい照明設定",
  );
  assert.equal(await shared.count(), 0);
  await capture("mobile-conflict");
  await memories.getByRole("button", { name: "入力を閉じる" }).click();
  const latest = list.locator(":scope > li").filter({ hasText: "別の端末で更新した好み" });
  await latest.getByRole("button", { name: "訂正", exact: true }).click();
  await editor.getByLabel("内容", { exact: true }).fill("訂正後の照明設定");
  await editor.getByRole("button", { name: "訂正を保存" }).click();
  await list.getByText("訂正後の照明設定", { exact: true }).waitFor();
  assert.equal(await latest.count(), 0);
  await capture("mobile-revised-current");
  const current = list.locator(":scope > li").filter({ hasText: "訂正後の照明設定" });
  await current.getByRole("button", { name: "忘却", exact: true }).click();
  const forget = memories.getByRole("region", { name: "忘却の確認" });
  await forget.getByLabel("元の出典の扱い").selectOption("delete_source");
  await scenario("forget_failed");
  await forget.getByRole("button", { name: "この記憶の忘却を依頼" }).click();
  await memories.getByRole("button", { name: "同じ内容で受付を再確認" }).waitFor();
  assert.equal(await current.count(), 1);
  assert.equal(await memories.getByRole("region", { name: "忘却の進捗" }).count(), 0);
  await capture("mobile-forget-acceptance-failure");
  await scenario("lost_forget");
  await memories.getByRole("button", { name: "同じ内容で受付を再確認" }).click();
  await memories.getByRole("alert").filter({ hasText: "受付結果を確認できません" }).waitFor();
  await memories.getByRole("button", { name: "同じ内容で受付を再確認" }).click();
  const progress = memories.getByRole("region", { name: "忘却の進捗" });
  await progress.getByText("消去待ち", { exact: true }).waitFor();
  assert.equal(await current.count(), 0);
  await capture("mobile-forget-accepted");
  await scenario("purge_failed");
  await progress.getByRole("button", { name: "消去状況を確認" }).click();
  await progress.getByText("消去処理に失敗・未完了", { exact: true }).waitFor();
  assert.equal(await progress.getByText("消去完了", { exact: true }).count(), 0);
  await capture("mobile-purge-failed");
  await scenario("normal");
  await progress.getByRole("button", { name: "消去状況を確認" }).click();
  await progress.getByText("消去完了", { exact: true }).waitFor();
  await capture("mobile-purge-complete");
  await scenario("empty");
  await memories.getByRole("button", { name: "一覧を再取得", exact: true }).click();
  await memories.getByText("表示できる記憶はありません。", { exact: true }).waitFor();
  await memories.getByRole("button", { name: "記憶を保存する", exact: true }).click();
  await editor.getByText("新しい対象を登録", { exact: true }).click();
  await editor.getByLabel("対象の名前", { exact: true }).fill("架空の新しい話題");
  await editor.getByRole("button", { name: "この範囲で対象を登録" }).click();
  await memories.getByRole("alert").filter({ hasText: "対象を登録しました" }).waitFor();
  await editor.getByLabel("内容", { exact: true }).fill("空のテナントから保存した記憶");
  await editor.getByRole("button", { name: "明示的に保存" }).click();
  await list.getByText("空のテナントから保存した記憶", { exact: true }).waitFor();
  await capture("mobile-empty-to-saved");
  await memories.getByRole("button", { name: "記憶を保存する", exact: true }).click();
  await editor.getByLabel("内容", { exact: true }).fill("PRIVATE-MEMORY-DRAFT");
  await page.getByRole("button", { name: "テナント B", exact: true }).click();
  assert.equal((await page.content()).includes("PRIVATE-MEMORY-DRAFT"), false);
  await page.getByRole("button", { name: "サインアウト", exact: true }).click();
  await page.getByRole("heading", { name: "サインイン", exact: true }).waitFor();
  assert.equal(await memories.count(), 0);
  await page.goto(display);
  await page.setViewportSize({ width: 1280, height: 800 });
  await login(page);
  await page.getByRole("button", { name: "テナント A", exact: true }).click();
  await list.getByText("夜は穏やかな照明", { exact: true }).waitFor();
  assert.equal((await memories.textContent()).includes("個人だけの読書の好み"), false);
  for (const name of ["訂正", "忘却", "記憶を保存する"])
    assert.equal(await memories.getByRole("button", { name, exact: true }).count(), 0);
  await capture("display-readonly-private-excluded");
  await page.evaluate(() => dispatchEvent(new Event("pagehide")));
  assert.equal(await memories.count(), 0);
  await page.close();
  assert.deepEqual(errors, []);
  console.log(
    "Memory browser: mobile save/search/source, private guard/API rejection, response loss, 412 diff, current revision, forget both failure stages/completion, tenant/signout/pagehide erasure, display readonly/privacy passed",
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
