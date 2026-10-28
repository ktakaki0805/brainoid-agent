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
  const mobile = await server("mobile", 5191),
    display = await server("display", 5192);
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
  const region = page.getByRole("region", { name: "リマインダーと通知", exact: true }),
    list = region.getByRole("list", { name: "現在のリマインダー", exact: true }),
    inbox = region.getByRole("region", { name: "アプリ内通知", exact: true }),
    editor = region.getByRole("form", { name: "リマインダーの保存フォーム" });
  const row = (title) =>
    list
      .locator(":scope > li")
      .filter({ has: page.getByRole("heading", { name: title, exact: true }) });
  let captures = 0;
  const capture = async (name) => {
    await region.scrollIntoViewIfNeeded();
    assert.equal(
      await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      true,
    );
    await region.screenshot({ path: join(artifacts, `${name}.png`) });
    captures++;
  };
  const boot = async (url = mobile) => {
    await page.goto(url);
    await login(page);
    await page.getByRole("button", { name: "テナント A", exact: true }).click();
    await row("みんなの防災確認").waitFor();
  };
  const scenario = async (value) => {
    await page.locator("details.preview-tools").evaluate((e) => {
      e.open = true;
    });
    await page.getByLabel("リマインダーの動作", { exact: true }).selectOption(value);
  };
  const saved = async () => {
    await editor.waitFor({ state: "hidden" });
    await region.getByRole("button", { name: "予定一覧を再取得", exact: true }).waitFor();
  };
  await boot();
  const push = page.getByRole("region", { name: "OS通知設定", exact: true });
  await push.getByRole("button", { name: "OS通知を許可・登録", exact: true }).click();
  await push
    .getByText("OS通知は許可されていません。アプリ内の未確認一覧をご利用ください。", {
      exact: true,
    })
    .waitFor();
  await push.screenshot({ path: join(artifacts, "mobile-push-permission-denied.png") });
  captures++;
  await page.getByText("OS通知の体験用設定", { exact: true }).click();
  await page.getByRole("button", { name: "架空の通知を開く", exact: true }).click();
  await inbox.getByLabel("取得した通知").waitFor();
  assert.equal((await page.content()).includes("PRIVATE-UNTRUSTED-PAYLOAD"), false);
  await inbox.getByRole("button", { name: "通知一覧を再取得", exact: true }).click();
  await scenario("audio_expired");
  await inbox.getByRole("button", { name: "通知一覧を再取得", exact: true }).click();
  await inbox.getByRole("heading", { name: "個人の受診予定", exact: true }).waitFor();
  await capture("mobile-past-audio-deadline-inbox");
  await capture("mobile-list-unregistered");
  const notification = inbox
    .getByRole("list", { name: "現在の通知" })
    .locator(":scope > li")
    .filter({ has: page.getByRole("heading", { name: "みんなの防災確認", exact: true }) });
  await notification.getByRole("button", { name: "現在の通知を開く", exact: true }).click();
  await inbox
    .getByLabel("取得した通知")
    .getByText("配信受付済み（端末表示は未確認）", { exact: true })
    .waitFor();
  await capture("mobile-open-not-ack");
  await inbox.getByRole("button", { name: "本人確認済みにする", exact: true }).click();
  await inbox.getByLabel("取得した通知").getByText("本人確認済み", { exact: true }).waitFor();
  await row("みんなの防災確認")
    .getByText("保存済みの状態: 本人確認済み (acknowledged)", { exact: true })
    .waitFor();
  await capture("mobile-acknowledged");
  await scenario("normal");
  await row("みんなの防災確認").getByRole("button", { name: "スヌーズ", exact: true }).click();
  await editor
    .getByLabel("予定日時（UTCオフセット付き）", { exact: true })
    .fill("2099-11-02T01:30:00-04:00");
  await capture("mobile-snooze-draft");
  await editor.getByRole("button", { name: "スヌーズを保存", exact: true }).click();
  await saved();
  await row("みんなの防災確認").getByText("予定版: 2", { exact: true }).waitFor();
  assert.equal(await inbox.getByText("みんなの防災確認", { exact: true }).count(), 0);
  await capture("mobile-snoozed-old-notification-removed");
  await region.getByRole("button", { name: "リマインダーを作成", exact: true }).click();
  await editor.getByLabel("予定の題名", { exact: true }).fill("PRIVATE-REMINDER-NEW");
  await editor
    .getByLabel("予定日時（UTCオフセット付き）", { exact: true })
    .fill("2099-02-30T00:00:00Z");
  await editor.getByLabel("予定のタイムゾーン", { exact: true }).fill("Asia/Tokyo");
  assert.equal(
    await editor.getByRole("button", { name: "予定を保存", exact: true }).isDisabled(),
    true,
  );
  await capture("mobile-invalid-date");
  await editor
    .getByLabel("予定日時（UTCオフセット付き）", { exact: true })
    .fill("2099-11-01T18:00:00+09:00");
  await scenario("lost_save");
  await editor.getByRole("button", { name: "予定を保存", exact: true }).click();
  await region.getByRole("button", { name: "同じ保存を再確認", exact: true }).waitFor();
  await capture("mobile-uncertain-save");
  await region.getByRole("button", { name: "同じ保存を再確認", exact: true }).click();
  await saved();
  await row("PRIVATE-REMINDER-NEW").waitFor();
  await capture("mobile-canonical-saved");
  await row("PRIVATE-REMINDER-NEW")
    .getByRole("button", { name: "編集・取消", exact: true })
    .click();
  await editor.getByLabel("予定の題名", { exact: true }).fill("PRIVATE-REMINDER-DRAFT");
  await scenario("conflict");
  await editor.getByRole("button", { name: "予定を保存", exact: true }).click();
  await region.getByRole("heading", { name: "他の更新があります", exact: true }).waitFor();
  assert.equal(
    await editor.getByLabel("予定の題名", { exact: true }).inputValue(),
    "PRIVATE-REMINDER-DRAFT",
  );
  await capture("mobile-412-retained-draft");
  await region.getByRole("button", { name: "最新版に入力を反映", exact: true }).click();
  await editor.getByRole("button", { name: "予定を保存", exact: true }).click();
  await saved();
  await row("PRIVATE-REMINDER-DRAFT")
    .getByRole("button", { name: "編集・取消", exact: true })
    .click();
  await editor.getByLabel("この予定を取消", { exact: true }).check();
  await editor.getByRole("button", { name: "予定を保存", exact: true }).click();
  await saved();
  await row("PRIVATE-REMINDER-DRAFT")
    .getByText("保存済みの状態: 取消済み (cancelled)", { exact: true })
    .waitFor();
  await capture("mobile-cancelled");
  await scenario("empty");
  await region.getByRole("button", { name: "予定一覧を再取得", exact: true }).click();
  await inbox.getByRole("button", { name: "通知一覧を再取得", exact: true }).click();
  await region.getByText("表示できるリマインダーはありません。", { exact: true }).waitFor();
  await capture("mobile-empty");
  await scenario("normal");
  await region.getByRole("button", { name: "予定一覧を再取得", exact: true }).click();
  await row("個人の受診予定").waitFor();
  await row("個人の受診予定").getByRole("button", { name: "編集・取消", exact: true }).click();
  await editor.getByLabel("予定の題名", { exact: true }).fill("PRIVATE-REMINDER-HISTORY");
  await page.getByRole("button", { name: "テナント B", exact: true }).click();
  await row("みんなの防災確認").waitFor();
  assert.equal((await page.content()).includes("PRIVATE-REMINDER-HISTORY"), false);
  await page.goBack();
  await row("みんなの防災確認").waitFor();
  assert.equal((await page.content()).includes("PRIVATE-REMINDER-HISTORY"), false);
  await page.getByRole("button", { name: "サインアウト", exact: true }).click();
  await page.getByRole("heading", { name: "サインイン", exact: true }).waitFor();
  assert.equal(await region.count(), 0);
  assert.equal((await page.content()).includes("個人の受診予定"), false);
  await page.screenshot({ path: join(artifacts, "mobile-signout-cleared.png") });
  captures++;
  await login(page);
  await page.getByRole("button", { name: "テナント A", exact: true }).click();
  await row("みんなの防災確認").waitFor();
  await page.getByLabel("OS通知の動作", { exact: true }).selectOption("unavailable");
  await push.getByRole("button", { name: "OS通知を許可・登録", exact: true }).click();
  await push
    .getByText("OS通知を準備できません。リマインダー作成とアプリ内通知は利用できます。", {
      exact: true,
    })
    .waitFor();
  await push.screenshot({ path: join(artifacts, "mobile-push-unavailable.png") });
  captures++;
  assert.equal(
    await region.getByRole("button", { name: "リマインダーを作成", exact: true }).isEnabled(),
    true,
  );
  await page.getByLabel("OS通知の動作", { exact: true }).selectOption("granted");
  await push.getByRole("button", { name: "OS通知を許可・登録", exact: true }).click();
  await push
    .getByText("OS通知の配信先を登録しました。配信や表示の保証ではありません。", { exact: true })
    .waitFor();
  await push.screenshot({ path: join(artifacts, "mobile-push-registered.png") });
  captures++;
  await page.getByLabel("OS通知の動作", { exact: true }).selectOption("rotation_failed");
  await page.getByRole("button", { name: "架空のトークンを更新", exact: true }).click();
  await push.getByRole("button", { name: "同じ登録を再確認", exact: true }).waitFor();
  await push.getByRole("button", { name: "OS通知を解除", exact: true }).click();
  await push.getByRole("button", { name: "配信先を再確認・登録", exact: true }).waitFor();
  assert.equal(
    await push.getByRole("button", { name: "配信先を再確認・登録", exact: true }).isEnabled(),
    true,
  );
  assert.equal(
    await push.getByRole("button", { name: "同じ登録を再確認", exact: true }).count(),
    0,
  );
  await push.screenshot({
    path: join(artifacts, "mobile-push-removal-unconfirmed-recoverable.png"),
  });
  captures++;
  await page.getByLabel("OS通知の動作", { exact: true }).selectOption("granted");
  await push.getByRole("button", { name: "配信先を再確認・登録", exact: true }).click();
  await push
    .getByText("OS通知の配信先を登録しました。配信や表示の保証ではありません。", { exact: true })
    .waitFor();
  await push.screenshot({ path: join(artifacts, "mobile-push-recovered.png") });
  captures++;
  await push.getByRole("button", { name: "OS通知を解除", exact: true }).click();
  await push
    .getByText("OS通知を解除しました。アプリ内通知は利用できます。", { exact: true })
    .waitFor();
  await page.setViewportSize({ width: 1280, height: 800 });
  await boot(display);
  assert.equal((await region.textContent()).includes("個人の受診予定"), false);
  assert.equal(
    await region.getByRole("button", { name: "リマインダーを作成", exact: true }).count(),
    0,
  );
  assert.equal(await region.getByRole("button", { name: "スヌーズ", exact: true }).count(), 0);
  assert.equal(
    await region.getByRole("button", { name: "本人確認済みにする", exact: true }).count(),
    0,
  );
  await capture("display-shared-readonly");
  await page.evaluate(() => dispatchEvent(new Event("pagehide")));
  assert.equal(await region.count(), 0);
  assert.deepEqual(errors, []);
  console.log(
    `Reminders browser passed: ${captures} captures; mobile canonical/draft/412/lost response/explicit time/cancel/snooze/old notification, separate ack, empty/unregistered, tenant/history/signout/pagehide erasure, display shared readonly.`,
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
