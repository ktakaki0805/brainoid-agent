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
  const mobile = await server("mobile", 5183);
  const display = await server("display", 5184);
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
    activePage = page;
    await page.setViewportSize(viewport);
    page.on("pageerror", (error) => errors.push(error.message));
    const saved = page.getByRole("list", { name: "保存済みメッセージ" });
    for (const scenario of [
      "normal",
      "reconnect",
      "expired",
      "gap",
      "waiting",
      "failed",
      "unknown",
      "tools",
      "revoked",
      "fast_tools",
      "fast_unknown",
      "disconnect_tools",
      "read_failure",
      "message_failure",
    ]) {
      await page.goto(url);
      await login(page);
      await page.getByRole("button", { name: "テナント A", exact: true }).click();
      await page.getByRole("button", { name: "共有 みんなの会話", exact: true }).waitFor();
      if (surface === "display")
        assert.equal(
          await page.getByRole("button", { name: "個人 個人の会話", exact: true }).count(),
          0,
        );
      await page.getByText("認証・切替の確認", { exact: true }).click();
      await page.getByRole("combobox", { name: /会話イベント/ }).selectOption(scenario);
      await page.getByRole("button", { name: "共有 みんなの会話", exact: true }).click();
      await page.getByLabel("メッセージ", { exact: true }).waitFor();
      await page.getByRole("region", { name: "会話", exact: true }).scrollIntoViewIfNeeded();
      if (scenario === "normal") {
        await page.screenshot({ path: join(artifacts, `${surface}-idle.png`), fullPage: true });
        assert.match(
          await page.getByText("履歴の応答先は不明", { exact: false }).textContent(),
          /不明/,
        );
      }
      await page
        .getByLabel("メッセージ", { exact: true })
        .fill("架空のお願い <script>unsafe()</script>");
      await page.evaluate(() => {
        window.chatTerminalViolations = [];
        window.chatObserver?.disconnect();
        window.chatObserver = new MutationObserver(() => {
          const root = document.querySelector(".chat-content");
          const text = root?.textContent ?? "";
          if (
            text.includes("保存済みの最終回答を表示しています") ||
            text.includes("応答の生成に失敗しました。")
          ) {
            const character = root?.querySelector(".character")?.getAttribute("aria-label") ?? "";
            if (root?.querySelector(".provisional") || /応答を表示中|考えています/.test(character))
              window.chatTerminalViolations.push(character);
          }
        });
        window.chatObserver.observe(document.body, {
          subtree: true,
          childList: true,
          attributes: true,
        });
      });
      await page.getByRole("button", { name: "送信", exact: true }).click();
      if (scenario === "normal") {
        await page.getByRole("status", { name: "アシスタント: 考えています" }).waitFor();
        await page.screenshot({
          path: join(artifacts, `${surface}-thinking.png`),
          fullPage: false,
        });
        await page.getByLabel("生成途中の応答").waitFor();
        await page.screenshot({
          path: join(artifacts, `${surface}-text-delta.png`),
          fullPage: false,
        });
      }
      if (["read_failure", "message_failure"].includes(scenario)) {
        await page
          .getByRole("alert")
          .filter({ hasText: "通信または応答を確認できません" })
          .waitFor();
        assert.equal(
          await page
            .getByLabel("生成途中の応答")
            .getByText("お手伝いします。", { exact: true })
            .count(),
          1,
        );
        await page.screenshot({
          path: join(artifacts, `${surface}-${scenario}-failed-read.png`),
          fullPage: true,
        });
        await page.getByRole("button", { name: "再接続", exact: true }).click();
        await page
          .getByLabel("生成途中の応答")
          .getByText("お手伝いします。一緒に進めましょう。", { exact: true })
          .waitFor();
      }
      if (["reconnect", "expired"].includes(scenario)) {
        await page
          .getByText("接続が終了しました。状態の確認または再接続を選んでください。", {
            exact: true,
          })
          .waitFor();
        assert.equal(
          await page
            .getByLabel("生成途中の応答")
            .getByText("お手伝いします。", { exact: true })
            .count(),
          1,
        );
        await page.screenshot({
          path: join(artifacts, `${surface}-${scenario}-disconnected.png`),
          fullPage: true,
        });
        await page.getByRole("button", { name: "再接続", exact: true }).click();
      }
      if (
        [
          "normal",
          "reconnect",
          "expired",
          "gap",
          "tools",
          "fast_tools",
          "disconnect_tools",
          "read_failure",
          "message_failure",
        ].includes(scenario)
      ) {
        await page
          .getByText(
            "保存済みの最終回答を表示しています。操作の成功・失敗は各カードで確認してください。",
            { exact: true },
          )
          .waitFor();
        assert.equal(
          await saved.getByText("お手伝いします。一緒に進めましょう。", { exact: true }).count(),
          1,
        );
        assert.equal(await page.getByLabel("生成途中の応答").count(), 0);
        assert.match(await page.locator(".output-label").textContent(), /この画面/);
      }
      if (scenario === "waiting") {
        await page.getByRole("status", { name: "アシスタント: 確認待ち" }).waitFor();
        await page
          .getByText("接続が終了しました。状態の確認または再接続を選んでください。", {
            exact: true,
          })
          .waitFor();
        await page.screenshot({
          path: join(artifacts, `${surface}-confirmation.png`),
          fullPage: true,
        });
        await page.getByRole("button", { name: "生成を停止", exact: true }).click();
        await page
          .getByText("生成を停止しました。実行済みの操作を取り消したことにはなりません。", {
            exact: true,
          })
          .waitFor();
      }
      if (["failed", "unknown", "fast_unknown"].includes(scenario)) {
        await page
          .getByRole("status", { name: "アシスタント: 応答の生成に失敗しました" })
          .waitFor();
        await page.getByRole("alert").filter({ hasText: "応答の生成に失敗しました。" }).waitFor();
        assert.equal(await page.getByLabel("生成途中の応答").count(), 0);
        assert.equal(
          await saved.getByText("お手伝いします。一緒に進めましょう。", { exact: true }).count(),
          0,
        );
        if (["unknown", "fast_unknown"].includes(scenario))
          await page.getByText("結果不明", { exact: true }).waitFor();
      }
      if (["tools", "fast_tools", "disconnect_tools"].includes(scenario)) {
        await page
          .getByRole("region", { name: "操作の結果" })
          .getByText("成功", { exact: true })
          .waitFor();
        await page.getByText("失敗", { exact: true }).waitFor();
      }
      if (scenario === "revoked") {
        await page.getByRole("alert").waitFor();
        assert.equal(await page.getByRole("region", { name: "会話", exact: true }).count(), 0);
      }
      assert.equal(
        await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
        true,
      );
      await page.screenshot({
        path: join(artifacts, `${surface}-${scenario}.png`),
        fullPage: true,
      });
      assert.deepEqual(await page.evaluate(() => window.chatTerminalViolations), []);
      console.log(`${surface}: ${scenario} accepted`);
    }
    if (surface === "mobile") {
      await page.goto(url);
      await login(page);
      await page.getByRole("button", { name: "テナント A", exact: true }).click();
      await page.getByRole("button", { name: "個人 個人の会話", exact: true }).click();
      await page.getByLabel("メッセージ", { exact: true }).fill("PRIVATE-CHAT-SYNTHETIC");
      await page.getByRole("button", { name: "送信", exact: true }).click();
      await saved.getByText("PRIVATE-CHAT-SYNTHETIC", { exact: true }).waitFor();
      await page.getByRole("button", { name: "テナント B", exact: true }).click();
      assert.equal((await page.content()).includes("PRIVATE-CHAT-SYNTHETIC"), false);
      await page.getByRole("button", { name: "サインアウト", exact: true }).click();
      await page.getByRole("heading", { name: "サインイン", exact: true }).waitFor();
      assert.equal(await page.getByRole("region", { name: "会話", exact: true }).count(), 0);
    }
    await page.close();
  }
  assert.deepEqual(errors, []);
} catch (error) {
  await activePage?.screenshot({ path: join(artifacts, "failure.png"), fullPage: true });
  throw error;
} finally {
  await context?.close();
  for (const process of servers) {
    process.kill("SIGTERM");
    if (process.exitCode === null) await new Promise((resolve) => process.once("exit", resolve));
  }
  await rm(profile, { recursive: true, force: true });
}
