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
  const mobile = await server("mobile", 5189),
    display = await server("display", 5190);
  context = await chromium.launchPersistentContext(profile, {
    executablePath: process.env.UI_CHROMIUM,
    headless: true,
    viewport: { width: 390, height: 844 },
    timezoneId: "Asia/Tokyo",
    args: ["--disable-dev-shm-usage"],
    ignoreDefaultArgs: ["--disable-back-forward-cache"],
  });
  const page = await context.newPage();
  activePage = page;
  page.on("pageerror", (error) => errors.push(error.message));
  const tasks = page.getByRole("region", { name: "タスク", exact: true });
  const list = tasks.getByRole("list", { name: "現在のタスク", exact: true });
  const editor = tasks.getByRole("form", { name: "タスクの保存フォーム" });
  const row = (title) =>
    list
      .locator(":scope > li")
      .filter({ has: page.getByRole("heading", { name: title, exact: true }) });
  const status = (title, value) =>
    row(title).getByText(`保存済みの状態: ${value}`, { exact: true });
  const idle = async () => {
    await page.waitForFunction(
      () =>
        !Array.from(document.querySelectorAll("section.tasks button")).find(
          (button) => button.textContent === "一覧を再取得",
        )?.disabled,
    );
  };
  const scenario = async (value) => {
    await page.locator("details.preview-tools").evaluate((element) => {
      element.open = true;
    });
    await page.getByLabel("タスクの動作", { exact: true }).selectOption(value);
  };
  const capture = async (name) => {
    await tasks.scrollIntoViewIfNeeded();
    assert.equal(
      await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      true,
    );
    await tasks.screenshot({ path: join(artifacts, `${name}.png`) });
  };
  const boot = async (url = mobile) => {
    await page.goto(url);
    await login(page);
    await page.getByRole("button", { name: "テナント A", exact: true }).click();
    await row("週末の買い物").waitFor();
    await idle();
  };
  const select = async (title = "週末の買い物") => {
    await row(title).getByRole("button", { name: "編集・完了・取消", exact: true }).click();
    await editor.waitFor();
  };
  const setStatus = (value) =>
    editor.getByLabel("変更後の状態（未保存）", { exact: true }).selectOption(value);
  await boot();
  await status("週末の買い物", "未完了 (open)").waitFor();
  await row("個人の書類を整理").getByText("期限: 期限なし", { exact: true }).waitFor();
  await tasks.getByText("Asia/Tokyo", { exact: true }).waitFor();
  await capture("mobile-list-tokyo");
  await tasks.getByLabel("日時の表示タイムゾーン", { exact: true }).fill("America/New_York");
  await tasks.getByRole("button", { name: "表示に適用", exact: true }).click();
  const due = "2026-11-01T05:30:00Z";
  const renderedDue = new Intl.DateTimeFormat("ja-JP", {
    timeZone: "America/New_York",
    dateStyle: "medium",
    timeStyle: "long",
  }).format(new Date(due));
  await row("週末の買い物").getByText(`期限: ${renderedDue}`, { exact: true }).waitFor();
  await select();
  assert.equal(
    await editor.getByLabel("期限（UTCオフセット付き）", { exact: true }).inputValue(),
    due,
  );
  await tasks.getByLabel("日時の表示タイムゾーン", { exact: true }).fill("Asia/Tokyo");
  await tasks.getByRole("button", { name: "表示に適用", exact: true }).click();
  assert.equal(
    await editor.getByLabel("期限（UTCオフセット付き）", { exact: true }).inputValue(),
    due,
  );
  for (const name of [
    "担当者のユーザーID（任意）",
    "記録するタイムゾーン（任意）",
    "公開範囲",
    "表示先",
  ])
    assert.equal(await editor.getByLabel(name, { exact: true }).isDisabled(), true);
  await setStatus("done");
  await status("週末の買い物", "未完了 (open)").waitFor();
  await scenario("reject");
  await editor.getByRole("button", { name: "変更を保存", exact: true }).click();
  await tasks.getByRole("alert").filter({ hasText: "サーバーで拒否" }).waitFor();
  await status("週末の買い物", "未完了 (open)").waitFor();
  await capture("mobile-rejected-done-still-open");
  await scenario("conflict");
  await editor.getByRole("button", { name: "変更を保存", exact: true }).click();
  const conflict = tasks.getByRole("region", { name: "タスクの競合", exact: true });
  await conflict.getByText("再取得した最新版", { exact: true }).waitFor();
  assert.equal(
    await editor.getByLabel("変更後の状態（未保存）", { exact: true }).inputValue(),
    "done",
  );
  await status("別の端末で更新した買い物", "未完了 (open)").waitFor();
  await capture("mobile-conflict-retained-done");
  await tasks.getByRole("button", { name: "最新版を確認して編集を続ける", exact: true }).click();
  await scenario("failed");
  await editor.getByRole("button", { name: "変更を保存", exact: true }).click();
  await tasks.getByRole("alert").filter({ hasText: "受付結果を確認できません" }).waitFor();
  await status("別の端末で更新した買い物", "未完了 (open)").waitFor();
  assert.equal(
    await editor.getByLabel("変更後の状態（未保存）", { exact: true }).isDisabled(),
    true,
  );
  await capture("mobile-failed-done-uncertain");
  await scenario("lost_save");
  await tasks.getByRole("button", { name: "同じ内容で受付を再確認" }).click();
  await tasks.getByRole("alert").filter({ hasText: "受付結果を確認できません" }).waitFor();
  await status("別の端末で更新した買い物", "未完了 (open)").waitFor();
  await capture("mobile-response-loss-still-unconfirmed");
  await tasks.getByRole("button", { name: "同じ内容で受付を再確認" }).click();
  await status("週末の買い物", "完了 (done)").waitFor();
  await idle();
  await capture("mobile-confirmed-done");
  await scenario("changed_selection");
  await select();
  await status("他の端末で変更したタスク", "未完了 (open)").waitFor();
  assert.equal(
    await editor.getByLabel("題名", { exact: true }).inputValue(),
    "他の端末で変更したタスク",
  );
  assert.equal(
    await editor.getByLabel("期限（UTCオフセット付き）", { exact: true }).inputValue(),
    "2026-11-02T00:00:00Z",
  );
  await editor.getByLabel("題名", { exact: true }).fill("未保存の題名");
  await setStatus("done");
  await status("他の端末で変更したタスク", "未完了 (open)").waitFor();
  await capture("mobile-selected-reopened-canonical");
  await editor.getByRole("button", { name: "入力を破棄して閉じる", exact: true }).click();
  await status("他の端末で変更したタスク", "未完了 (open)").waitFor();
  assert.equal(await row("週末の買い物").count(), 0);
  await select("他の端末で変更したタスク");
  await editor.getByLabel("題名", { exact: true }).fill("週末の買い物");
  await setStatus("cancelled");
  await editor.getByLabel("期限（UTCオフセット付き）", { exact: true }).fill("");
  await editor.getByRole("button", { name: "変更を保存" }).click();
  await status("週末の買い物", "取消済み (cancelled)").waitFor();
  await row("週末の買い物").getByText("期限: 期限なし", { exact: true }).waitFor();
  await idle();
  await capture("mobile-cancelled-due-cleared");
  await tasks.getByLabel("タスク一覧の状態", { exact: true }).selectOption("done");
  await tasks.getByText("表示できるタスクはありません。", { exact: true }).waitFor();
  await idle();
  await capture("mobile-empty-status-filter");
  await tasks.getByLabel("タスク一覧の状態", { exact: true }).selectOption("all");
  await row("週末の買い物").waitFor();
  await idle();
  await tasks.getByRole("button", { name: "タスクを作成", exact: true }).click();
  await editor.getByLabel("題名", { exact: true }).fill("DSTのタスク");
  await editor.getByLabel("期限（UTCオフセット付き）", { exact: true }).fill("2026-11-01T01:30:00");
  assert.equal(
    await editor.getByRole("button", { name: "タスクを保存", exact: true }).isDisabled(),
    true,
  );
  await editor
    .getByLabel("期限（UTCオフセット付き）", { exact: true })
    .fill("2026-02-30T09:00:00Z");
  assert.equal(
    await editor.getByRole("button", { name: "タスクを保存", exact: true }).isDisabled(),
    true,
  );
  await capture("mobile-invalid-date-blocked");
  await editor
    .getByLabel("期限（UTCオフセット付き）", { exact: true })
    .fill("2026-11-01T01:30:00-05:00");
  await editor.getByLabel("記録するタイムゾーン（任意）", { exact: true }).fill("America/New_York");
  await editor
    .getByLabel("担当者のユーザーID（任意）", { exact: true })
    .fill("synthetic@example.test");
  await editor.getByRole("button", { name: "タスクを保存", exact: true }).click();
  await status("DSTのタスク", "未完了 (open)").waitFor();
  await idle();
  await tasks.getByLabel("日時の表示タイムゾーン", { exact: true }).fill("America/New_York");
  await tasks.getByRole("button", { name: "表示に適用", exact: true }).click();
  const foldDue = new Intl.DateTimeFormat("ja-JP", {
    timeZone: "America/New_York",
    dateStyle: "medium",
    timeStyle: "long",
  }).format(new Date("2026-11-01T06:30:00Z"));
  await row("DSTのタスク").getByText(`期限: ${foldDue}`, { exact: true }).waitFor();
  await capture("mobile-dst-offset-created");
  await tasks.getByRole("button", { name: "タスクを作成", exact: true }).click();
  await editor.getByLabel("題名", { exact: true }).fill("PRIVATE-TASK-DRAFT");
  await page.getByRole("button", { name: "テナント B", exact: true }).click();
  assert.equal((await page.content()).includes("PRIVATE-TASK-DRAFT"), false);
  await page.goBack();
  await tasks.waitFor();
  assert.equal((await page.content()).includes("PRIVATE-TASK-DRAFT"), false);
  await page.getByRole("button", { name: "サインアウト", exact: true }).click();
  await page.getByRole("heading", { name: "サインイン", exact: true }).waitFor();
  assert.equal(await tasks.count(), 0);
  await boot();
  await tasks.getByLabel("タスク一覧の状態", { exact: true }).selectOption("open");
  await idle();
  await scenario("changed_selection");
  await select();
  assert.equal(
    await editor.getByLabel("変更後の状態（未保存）", { exact: true }).inputValue(),
    "cancelled",
  );
  assert.equal(await row("週末の買い物").count(), 0);
  assert.equal(await row("他の端末で変更したタスク").count(), 0);
  await capture("mobile-selected-outside-open-filter");
  await editor.getByRole("button", { name: "入力を破棄して閉じる", exact: true }).click();
  assert.equal(await row("週末の買い物").count(), 0);
  await tasks.getByLabel("タスク一覧の状態", { exact: true }).selectOption("all");
  await status("他の端末で変更したタスク", "取消済み (cancelled)").waitFor();
  await boot();
  await tasks.getByLabel("タスク一覧の状態", { exact: true }).selectOption("open");
  await idle();
  await select();
  await setStatus("done");
  await scenario("conflict_status");
  await editor.getByRole("button", { name: "変更を保存", exact: true }).click();
  await conflict.getByText("保存済みの状態: 取消済み (cancelled)", { exact: true }).waitFor();
  assert.equal(await row("別の端末で更新した買い物").count(), 0);
  assert.equal(
    await editor.getByLabel("変更後の状態（未保存）", { exact: true }).inputValue(),
    "done",
  );
  await capture("mobile-conflict-outside-open-filter");
  await boot();
  await scenario("denied");
  await tasks.getByRole("button", { name: "一覧を再取得", exact: true }).click();
  await page.getByRole("alert").filter({ hasText: "許可されていません" }).waitFor();
  assert.equal(await tasks.count(), 0);
  await page.screenshot({ path: join(artifacts, "mobile-permission-denied.png"), fullPage: true });
  await boot();
  await tasks.getByRole("button", { name: "タスクを作成", exact: true }).click();
  await editor.getByLabel("題名", { exact: true }).fill("PRIVATE-TASK-BFCACHE");
  await page.evaluate(() => {
    addEventListener("pageshow", (event) =>
      sessionStorage.setItem("tasks-bfcache", String(event.persisted)),
    );
    addEventListener("pagehide", () =>
      sessionStorage.setItem("tasks-at-hide", String(document.querySelectorAll(".tasks").length)),
    );
  });
  await page.goto(`${mobile}/?away=1`);
  await login(page);
  await page.getByRole("button", { name: "サインアウト", exact: true }).click();
  await page.getByRole("heading", { name: "サインイン", exact: true }).waitFor();
  await page.goBack({ waitUntil: "commit" });
  assert.equal(await page.evaluate(() => sessionStorage.getItem("tasks-bfcache")), "true");
  assert.equal(await page.evaluate(() => sessionStorage.getItem("tasks-at-hide")), "0");
  await page.getByRole("heading", { name: "サインイン", exact: true }).waitFor();
  assert.equal(await tasks.count(), 0);
  assert.equal((await page.content()).includes("PRIVATE-TASK-BFCACHE"), false);
  await page.setViewportSize({ width: 1280, height: 800 });
  await boot(display);
  assert.equal((await tasks.textContent()).includes("個人の書類を整理"), false);
  await tasks.getByText(/本人未確認/).waitFor();
  assert.equal(
    await tasks.getByRole("button", { name: "編集・完了・取消", exact: true }).count(),
    0,
  );
  assert.equal(await tasks.getByRole("button", { name: "タスクを作成", exact: true }).count(), 0);
  await capture("display-readonly-public-only");
  await page.evaluate(() => dispatchEvent(new Event("pagehide")));
  assert.equal(await tasks.count(), 0);
  await page.close();
  assert.deepEqual(errors, []);
  console.log(
    "Tasks browser passed: mobile explicit timezone/DST/create/edit/complete/cancel/due clear, rejection/failure/response-loss/412 preserve canonical status, status filter, tenant/back/signout/real bfcache erasure, display readonly/public filter.",
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
