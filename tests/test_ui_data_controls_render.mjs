import assert from "node:assert/strict";
import { before, after, test } from "node:test";
import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import { createServer } from "vite";
import { DataControlsController } from "../packages/ui/src/data-controls/controller.ts";
import { SessionController } from "../packages/ui/src/session.ts";
import { createSessionFixture, tenantA } from "../packages/ui/src/session-fixture.ts";
const require = createRequire(new URL("../packages/ui/package.json", import.meta.url));
const { createElement } = require("react"),
  { renderToStaticMarkup } = require("react-dom/server");
let server, OperationStatus, DataControls, cases;
before(async () => {
  server = await createServer({
    configFile: false,
    appType: "custom",
    server: { middlewareMode: true, hmr: false, watch: null },
    optimizeDeps: { noDiscovery: true },
    ssr: { optimizeDeps: { noDiscovery: true } },
  });
  ({ OperationStatus, DataControls } = await server.ssrLoadModule(
    "/packages/ui/src/data-controls/view.tsx",
  ));
  const result = spawnSync(
    "uv",
    [
      "run",
      "--all-packages",
      "--locked",
      "--offline",
      "python",
      "-B",
      "tests/ui/data_controls_api_fixture.py",
    ],
    {
      cwd: new URL("..", import.meta.url),
      env: { ...process.env, PYTHONPATH: "tests:." },
      encoding: "utf8",
    },
  );
  assert.equal(result.status, 0, result.stderr);
  cases = JSON.parse(result.stdout);
  assert.equal(cases.length, 2);
});
after(async () => {
  await server?.close();
});
for (const kind of ["leave_tenant", "delete_account"])
  test(`real ${kind} HTTP/worker response passes SDK controller and rendered progress`, async () => {
    const sample = cases.find((v) => v.body.kind === kind),
      fixture = createSessionFixture();
    const session = new SessionController({
      auth: fixture.auth,
      baseUrl: "https://session.invalid",
      transport: async (input, init) => {
        const request = new Request(input, init),
          url = new URL(request.url);
        if (!url.pathname.includes("operations")) return fixture.transport(request);
        if (request.method === "POST") {
          assert.deepEqual(await request.json(), sample.body);
          return Response.json(sample.accepted, { status: 202 });
        }
        return Response.json(sample.after, { status: sample.after_status });
      },
    });
    const stop = session.start();
    await new Promise((resolve) => setImmediate(resolve));
    await session.signIn("synthetic@example.test", "synthetic");
    await session.selectTenant(tenantA);
    const controller = new DataControlsController(session, {
      available: false,
      save: async () => {},
    });
    try {
      await controller.prepare(kind);
      await controller.confirm();
      assert.deepEqual(controller.snapshot().operation, sample.accepted);
      let html = renderToStaticMarkup(
        createElement(OperationStatus, { operation: controller.snapshot().operation }),
      );
      assert.match(html, /受付済み・処理待ち（完了ではありません）/);
      await controller.refresh();
      if (kind === "delete_account") {
        assert.equal(controller.snapshot().operation.status, "pending");
        assert.match(controller.snapshot().notice, /消去完了とは判断できません/);
      } else {
        html = renderToStaticMarkup(
          createElement(OperationStatus, { operation: controller.snapshot().operation }),
        );
        assert.match(html, /サーバーで削除処理の完了を確認/);
      }
    } finally {
      controller.dispose();
      stop();
    }
  });
for (const status of ["pending", "running", "succeeded", "failed", "outcome_unknown"])
  test(`render export ${status} without public download link`, () => {
    const html = renderToStaticMarkup(
      createElement(OperationStatus, {
        operation: {
          ...cases[0].accepted,
          kind: "export",
          status,
          download_expires_at: "2026-10-30T00:00:00Z",
        },
      }),
    );
    assert.doesNotMatch(html, /href=/);
    assert.match(html, /取得期限/);
    if (status !== "succeeded") assert.doesNotMatch(html, /生成完了/);
  });
test("shared display never instantiates a controller or renders private operation details", () => {
  const html = renderToStaticMarkup(
    createElement(DataControls, {
      surface: "display",
      session: {
        globalContext() {
          throw new Error("Must not read private context");
        },
      },
    }),
  );
  assert.match(html, /本人のスマホ/);
  assert.doesNotMatch(html, /受付番号|button|input/);
});
