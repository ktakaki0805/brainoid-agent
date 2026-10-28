import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import { createServer } from "vite";
const require = createRequire(new URL("../packages/ui/package.json", import.meta.url));
const { createElement } = require("react"),
  { renderToStaticMarkup } = require("react-dom/server");
let server, UsageCard, boundaryCases;
before(async () => {
  server = await createServer({
    configFile: false,
    appType: "custom",
    server: { middlewareMode: true, hmr: false, watch: null },
    optimizeDeps: { noDiscovery: true },
    ssr: { optimizeDeps: { noDiscovery: true } },
  });
  ({ UsageCard } = await server.ssrLoadModule("/packages/ui/src/tenant-settings/view.tsx"));
  const result = spawnSync(
    "uv",
    [
      "run",
      "--all-packages",
      "--locked",
      "--offline",
      "python",
      "-B",
      "tests/ui/usage_scope_fixture.py",
    ],
    {
      cwd: new URL("..", import.meta.url),
      env: { ...process.env, PYTHONPATH: "tests:." },
      encoding: "utf8",
    },
  );
  assert.equal(result.status, 0, result.stderr);
  console.log("Usage API boundary inputs:", result.stdout.trim());
  boundaryCases = JSON.parse(result.stdout);
  assert.equal(boundaryCases.length, 6);
});
after(async () => {
  await server?.close();
});
const render = (role, usage) => renderToStaticMarkup(createElement(UsageCard, { role, usage }));
for (const role of ["owner", "admin", "member"]) {
  for (const quota of ["available", "limited", "exhausted"])
    test(`${role} ${quota}: rendered quota explanation matches its API aggregation scope`, () => {
      const html = render(role, {
        period: "2026-10",
        turns: 12,
        voice_seconds: 4,
        remaining_turns: quota === "exhausted" ? 0 : 88,
        quota_state: quota,
      });
      assert.doesNotMatch(html, /現在は利用できます/);
      assert.match(html, /音声は未確定の予約を含みます/);
      assert.match(html, /60秒以上/);
      if (role === "member") {
        assert.match(html, /本人とテナントの枠の小さい方/);
        assert.match(
          html,
          quota === "available"
            ? /本人とテナントの利用枠には現在、空き/
            : quota === "limited"
              ? /本人またはテナントの同時実行上限/
              : /本人またはテナントの今月の利用上限/,
        );
      } else {
        assert.match(html, /テナント枠のみの集計/);
        assert.match(html, /本人の利用可否を示しません/);
        assert.doesNotMatch(html, /本人とテナントの枠の小さい方/);
        assert.match(
          html,
          quota === "available"
            ? /テナント枠には現在、空き/
            : quota === "limited"
              ? /テナントの同時実行上限/
              : /テナントの今月の利用上限/,
        );
      }
    });
  for (const userLimit of ["turns", "concurrent_runs"])
    test(`${role} own ${userLimit} exhausted while tenant has capacity: actual API result drives rendering`, () => {
      const entry = boundaryCases.find((v) => v.role === role && v.user_limit === userLimit);
      assert.equal(entry.turn_status, 429);
      assert.equal(entry.retry_after, "60");
      const html = render(role, entry.usage);
      if (role === "member") {
        assert.equal(entry.usage.quota_state, userLimit === "turns" ? "exhausted" : "limited");
        assert.match(html, /本人またはテナントの.*上限に達しています/);
      } else {
        assert.equal(entry.usage.quota_state, "available");
        assert.equal(entry.usage.remaining_turns, 10000);
        assert.match(html, /本人の利用可否を示しません/);
        assert.match(html, /本人の月次枠・同時実行枠に達している場合は利用できません/);
      }
      assert.doesNotMatch(html, /現在は利用できます/);
    });
}
