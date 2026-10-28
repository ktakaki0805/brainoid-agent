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
  const mobile = await server("mobile", 5193),
    display = await server("display", 5194);
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
  const region = page.getByRole("region", { name: "Calendar", exact: true });
  const card = region.getByRole("region", { name: "Calendar確認カード", exact: true });
  const editor = region.getByRole("form", { name: "Calendar候補の入力" });
  let captures = 0;
  async function capture(name, area = region) {
    await area.scrollIntoViewIfNeeded();
    assert.equal(
      await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      true,
    );
    await area.screenshot({ path: join(artifacts, `${name}.png`) });
    captures++;
  }
  async function boot(url = mobile) {
    await page.goto(url);
    await login(page);
    await page.getByRole("button", { name: "テナント A", exact: true }).click();
    await region.getByRole("heading", { name: "カレンダー", exact: true }).waitFor();
  }
  async function select() {
    await region.getByRole("button", { name: /Google Calendar · 接続済み/ }).click();
    await region.getByRole("heading", { name: "連携の設定", exact: true }).waitFor();
  }
  async function events() {
    await region.getByRole("button", { name: / の予定を取得$/ }).click();
    await region.getByRole("heading", { name: "架空の終日予定", exact: true }).waitFor();
  }
  async function scenario(value) {
    await page.getByText("認証・切替の確認", { exact: true }).click();
    await page.getByLabel("Calendarの動作", { exact: true }).selectOption(value);
    await page.getByText("認証・切替の確認", { exact: true }).click();
  }
  async function create(title) {
    await region.getByRole("button", { name: "予定の作成内容を入力", exact: true }).click();
    await editor.getByLabel("予定の題名", { exact: true }).fill(title);
    await editor.getByLabel("日時の種類").selectOption("date");
    await editor
      .getByLabel("開始日（終日は終了日を含みません）", { exact: true })
      .fill("2099-11-01");
    await editor
      .getByLabel("終了日（終日は終了日を含みません）", { exact: true })
      .fill("2099-11-02");
    await editor.getByRole("button", { name: "固定された確認候補を作成", exact: true }).click();
    await card.getByRole("heading", { name: title, exact: true }).waitFor();
  }
  async function openConversationProposal() {
    await page.getByText("認証・切替の確認", { exact: true }).click();
    await page.getByLabel("会話イベント").selectOption("waiting");
    await page.getByText("認証・切替の確認", { exact: true }).click();
    await page.getByRole("button", { name: "個人 個人の会話", exact: true }).click();
    await page.getByLabel("メッセージ", { exact: true }).fill("架空の予定を提案して");
    await page.getByRole("button", { name: "送信", exact: true }).click();
    await page.getByRole("button", { name: "Calendarの確認内容を開く", exact: true }).click();
    const conversation = page
      .getByRole("region", { name: "会話", exact: true })
      .getByRole("region", { name: "Calendar確認カード", exact: true });
    await conversation
      .getByRole("heading", { name: "会話からの架空の予定", exact: true })
      .waitFor();
    return conversation;
  }
  await boot();
  await capture("mobile-connected");
  await scenario("empty");
  await region.getByRole("button", { name: "連携一覧を再取得", exact: true }).click();
  await region.getByText("連携はありません。", { exact: true }).waitFor();
  await capture("mobile-empty");
  await scenario("failed");
  await region.getByRole("button", { name: "連携一覧を再取得", exact: true }).click();
  await region.getByRole("alert").waitFor();
  await capture("mobile-unavailable");
  await scenario("normal");
  await region.getByRole("button", { name: "連携一覧を再取得", exact: true }).click();

  await select();
  await events();
  await capture("mobile-all-day-timezone");
  await region
    .getByRole("listitem")
    .filter({ has: page.getByRole("heading", { name: "架空の終日予定", exact: true }) })
    .getByRole("button", { name: "削除内容を確認", exact: true })
    .click();
  await editor.getByRole("button", { name: "固定された確認候補を作成", exact: true }).click();
  await card.getByRole("region", { name: "削除する予定の取得時情報" }).waitFor();
  await capture("mobile-delete-target", card);
  await card.getByRole("button", { name: "拒否する", exact: true }).click();
  await card.getByText("状態: 拒否済み", { exact: true }).waitFor();
  await capture("mobile-rejected", card);
  await create("PRIVATE-CALENDAR-PREVIEW");
  await capture("mobile-fixed-preview", card);
  await create("PRIVATE-CALENDAR-CHANGED");
  assert.equal((await card.textContent()).includes("PRIVATE-CALENDAR-PREVIEW"), false);
  await scenario("lost");
  await card.getByRole("button", { name: "この内容で実行する", exact: true }).click();
  await region.getByRole("button", { name: "同じ操作の受付を再確認", exact: true }).waitFor();
  await capture("mobile-lost-confirm");
  await region.getByRole("button", { name: "同じ操作の受付を再確認", exact: true }).click();
  await card.getByText("書込処理: 受付済み（未完了）", { exact: true }).waitFor();
  await capture("mobile-accepted-pending", card);
  assert.equal(
    await card.getByRole("button", { name: "この内容で実行する", exact: true }).isEnabled(),
    false,
  );
  await boot();
  await select();
  await events();
  await create("結果不明の架空予定");
  await scenario("unknown");
  await card.getByRole("button", { name: "この内容で実行する", exact: true }).click();
  await card.getByText("書込処理: 結果不明", { exact: true }).waitFor();
  await capture("mobile-outcome-unknown", card);
  assert.equal((await card.textContent()).includes("状態: 確認待ち"), false);
  assert.equal(await card.getByText("確認: 受付済み", { exact: true }).count(), 1);
  await boot();
  await select();
  await events();
  await create("競合する架空予定");
  await scenario("provider_conflict");
  await card.getByRole("button", { name: "この内容で実行する", exact: true }).click();
  await card.getByText("書込処理: 失敗 (conflict)", { exact: true }).waitFor();
  await capture("mobile-provider-conflict", card);
  await boot();
  await select();
  await events();
  await create("期限切れの架空予定");
  await scenario("expired");
  await card.getByRole("button", { name: "確認・書込状態を再取得", exact: true }).click();
  await card.getByText("状態: 確認期限切れ", { exact: true }).waitFor();
  await capture("mobile-expired", card);
  await scenario("normal");
  await region
    .getByLabel("参照するカレンダーID（1行に1個、最大10個）")
    .fill("changed@example.test");
  await scenario("conflict");
  await region.getByRole("button", { name: "カレンダーの選択を保存", exact: true }).click();
  await region.getByRole("heading", { name: "連携の変更が競合しました", exact: true }).waitFor();
  await capture("mobile-selection-conflict");
  await region.getByRole("button", { name: "最新版を基準に入力を見直す", exact: true }).click();
  await region.getByRole("button", { name: "カレンダーの選択を保存", exact: true }).click();
  await region.getByRole("button", { name: "この連携を解除", exact: true }).click();
  await capture("mobile-disconnect-confirm");
  await region.getByRole("button", { name: "連携を停止して消去を依頼", exact: true }).click();
  await region.getByText("取得済みデータ・秘密の消去: 処理中", { exact: true }).waitFor();
  await capture("mobile-cleanup-pending");
  await scenario("cleanup");
  await region.getByRole("button", { name: "連携・消去状態を再取得", exact: true }).click();
  await region.getByText("取得済みデータ・秘密の消去: 完了", { exact: true }).waitFor();
  await capture("mobile-cleanup-complete");
  await boot();
  await select();
  await scenario("reauth");
  await region.getByRole("button", { name: "連携・消去状態を再取得", exact: true }).click();
  await region.getByText("状態: 権限を再取得してください", { exact: true }).waitFor();
  await capture("mobile-reauth");
  await scenario("normal");
  await region.getByRole("button", { name: "Google Calendarに接続・再接続", exact: true }).click();
  await region.getByText("システムブラウザーで同意を完了してください。", { exact: true }).waitFor();
  await capture("mobile-oauth-await");
  await page.getByText("Calendarブラウザーの確認", { exact: true }).click();
  await page.getByRole("button", { name: "合成の不正URLで復帰", exact: true }).click();
  assert.equal(
    await region.getByText("システムブラウザーで同意を完了してください。", { exact: true }).count(),
    1,
  );
  await page.getByRole("button", { name: "合成の許可済みURLで復帰", exact: true }).click();
  await region.getByRole("button", { name: "連携一覧を再取得", exact: true }).waitFor();
  assert.equal(
    await region.getByText("システムブラウザーで同意を完了してください。", { exact: true }).count(),
    0,
  );
  await capture("mobile-oauth-return");
  await boot();
  await select();
  await events();
  await create("PRIVATE-CALENDAR-CLEAR");
  await page.getByRole("button", { name: "テナント B", exact: true }).click();
  await region.getByRole("heading", { name: "カレンダー", exact: true }).waitFor();
  assert.equal((await page.content()).includes("PRIVATE-CALENDAR-CLEAR"), false);
  await page.goBack();
  await region.getByRole("heading", { name: "カレンダー", exact: true }).waitFor();
  assert.equal((await page.content()).includes("PRIVATE-CALENDAR-CLEAR"), false);
  await select();
  await events();
  await create("PRIVATE-CALENDAR-BFCACHE");
  await page.evaluate(() => {
    addEventListener("pageshow", (event) =>
      sessionStorage.setItem("calendar-test-restored", String(event.persisted)),
    );
    addEventListener("pagehide", () =>
      sessionStorage.setItem(
        "calendar-test-cleared",
        String(document.querySelectorAll(".calendar-proposal").length),
      ),
    );
  });
  await page.goto(`${mobile}/?second-document=1`);
  await login(page);
  await page.getByRole("button", { name: "サインアウト", exact: true }).click();
  await page.goBack({ waitUntil: "commit" });
  await page.getByRole("heading", { name: "サインイン", exact: true }).waitFor();
  assert.equal(await page.evaluate(() => sessionStorage.getItem("calendar-test-restored")), "true");
  assert.equal(await page.evaluate(() => sessionStorage.getItem("calendar-test-cleared")), "0");
  assert.equal((await page.content()).includes("PRIVATE-CALENDAR-BFCACHE"), false);
  await page.screenshot({ path: join(artifacts, "mobile-bfcache-erased.png") });
  captures++;
  await boot();
  await page.getByText("認証・切替の確認", { exact: true }).click();
  await page.getByLabel("会話イベント").selectOption("waiting");
  await page.getByText("認証・切替の確認", { exact: true }).click();
  await page.getByRole("button", { name: "個人 個人の会話", exact: true }).click();
  await page.getByLabel("メッセージ", { exact: true }).fill("架空の予定を提案して");
  await page.getByRole("button", { name: "送信", exact: true }).click();
  await page.getByRole("button", { name: "Calendarの確認内容を開く", exact: true }).waitFor();
  await page.getByRole("button", { name: "Calendarの確認内容を開く", exact: true }).click();
  const chatCard = page
    .getByRole("region", { name: "会話", exact: true })
    .getByRole("region", { name: "Calendar確認カード", exact: true });
  await chatCard.getByRole("heading", { name: "会話からの架空の予定", exact: true }).waitFor();
  await capture("mobile-chat-waiting-proposal", chatCard);
  await chatCard.getByRole("button", { name: "拒否する", exact: true }).click();
  await chatCard.getByText("状態: 拒否済み", { exact: true }).waitFor();
  await capture("mobile-chat-rejected", chatCard);
  for (const change of ["disconnect", "selection", "reauth"]) {
    await boot();
    await select();
    await events();
    await create(`PRIVATE-WORKSPACE-${change}`);
    await openConversationProposal();
    assert.equal(
      await page.getByRole("region", { name: "Calendar確認カード", exact: true }).count(),
      2,
    );
    if (change === "disconnect") {
      await card.getByRole("button", { name: "この内容で実行する", exact: true }).click();
      await card.getByText("書込処理: 受付済み（未完了）", { exact: true }).waitFor();
      await region.getByRole("button", { name: "この連携を解除", exact: true }).click();
      await region.getByRole("button", { name: "連携を停止して消去を依頼", exact: true }).click();
      await region.getByText("状態: 停止済み", { exact: true }).waitFor();
    } else if (change === "selection") {
      await region.getByLabel("参照するカレンダーID（1行に1個、最大10個）").fill("");
      await region.getByRole("button", { name: "カレンダーの選択を保存", exact: true }).click();
      await region.getByText("参照するカレンダーを保存しました。", { exact: true }).waitFor();
    } else {
      await scenario("reauth");
      await region.getByRole("button", { name: "連携・消去状態を再取得", exact: true }).click();
      await region.getByText("状態: 権限を再取得してください", { exact: true }).waitFor();
    }
    assert.equal(
      await page.getByRole("region", { name: "Calendar確認カード", exact: true }).count(),
      0,
    );
    assert.equal((await page.content()).includes(`PRIVATE-WORKSPACE-${change}`), false);
    assert.equal((await page.content()).includes("会話からの架空の予定"), false);
    assert.equal(await page.getByRole("button", { name: "サインアウト", exact: true }).count(), 1);
    await page.screenshot({
      path: join(artifacts, `mobile-authority-${change}-both-erased.png`),
      fullPage: true,
    });
    captures++;
  }
  for (const phase of ["displayed", "accepted", "uncertain"]) {
    // No Integration DTO has succeeded in this context when the conversation opens.
    await page.goto(mobile);
    await login(page);
    await scenario("failed");
    await page.getByRole("button", { name: "テナント A", exact: true }).click();
    await region.getByRole("alert").waitFor();
    await scenario("normal");
    const conversation = await openConversationProposal();
    if (phase === "displayed") await capture("mobile-conversation-before-metadata", conversation);
    if (phase === "uncertain") await scenario("lost");
    if (phase !== "displayed")
      await conversation.getByRole("button", { name: "この内容で実行する", exact: true }).click();
    if (phase === "accepted")
      await conversation.getByText("書込処理: 受付済み（未完了）", { exact: true }).waitFor();
    if (phase === "uncertain")
      await page.getByRole("button", { name: "同じ確認操作の受付を再確認", exact: true }).waitFor();
    await scenario("empty");
    await region.getByRole("button", { name: "連携一覧を再取得", exact: true }).click();
    await region.getByText("連携はありません。", { exact: true }).waitFor();
    assert.equal(
      await page.getByRole("region", { name: "Calendar確認カード", exact: true }).count(),
      0,
    );
    assert.equal(
      await page.getByRole("button", { name: "同じ確認操作の受付を再確認", exact: true }).count(),
      0,
    );
    assert.equal((await page.content()).includes("会話からの架空の予定"), false);
    assert.equal(await page.getByRole("button", { name: "サインアウト", exact: true }).count(), 1);
    await page.screenshot({
      path: join(artifacts, `mobile-unobserved-${phase}-absence-erased.png`),
      fullPage: true,
    });
    captures++;
  }
  await page.setViewportSize({ width: 1280, height: 800 });
  await boot(display);
  assert.equal(await region.getByRole("button").count(), 0);
  assert.equal((await region.textContent()).includes("架空の"), false);
  await capture("display-guidance");
  assert.deepEqual(errors, []);
  console.log(
    `Calendar browser passed: ${captures} states; fixed/changed/delete/reject/expiry/duplicate/lost/unknown/conflict, selection/disconnect/cleanup, native synthetic return, privacy tenant/history/bfcache persisted=true, display readonly.`,
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
