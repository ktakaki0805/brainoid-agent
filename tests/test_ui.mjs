import assert from "node:assert/strict";
import { test } from "node:test";
import { createBrainoidClient, getCurrentUser } from "../packages/api-client/src/index.ts";
import { createUnavailablePlatform } from "../packages/ui/platform/index.ts";
import { checkConnection } from "../packages/ui/src/connection.ts";
import { createFakeDependencies } from "../packages/ui/src/fake.ts";

test("mobile and kiosk use isolated injected clients through the generated health contract", async () => {
  const mobile = createFakeDependencies("mobile");
  const display = createFakeDependencies("display");
  assert.notEqual(mobile.api, display.api);
  assert.equal(mobile.platform.surface, "mobile");
  assert.equal(display.platform.surface, "display");
  await checkConnection(mobile.api, new AbortController().signal);
  assert.deepEqual(mobile.calls, ["GET /health/ready"]);
  assert.deepEqual(display.calls, []);
  await checkConnection(display.api, new AbortController().signal);
  assert.deepEqual(display.calls, ["GET /health/ready"]);
});

test("a substituted failing transport cannot become a successful connection", async () => {
  const fake = createFakeDependencies("mobile", "error");
  await assert.rejects(checkConnection(fake.api, new AbortController().signal));
  await assert.rejects(checkConnection(fake.api, new AbortController().signal));
  assert.equal(fake.calls.length, 2);
});

test("loading transport settles on cancellation and accepts an already cancelled signal", async () => {
  const fake = createFakeDependencies("display", "loading");
  const controller = new AbortController();
  const pending = checkConnection(fake.api, controller.signal);
  const rejection = assert.rejects(pending, { name: "AbortError" });
  await new Promise((resolve) => setImmediate(resolve));
  controller.abort();
  await rejection;
  await assert.rejects(checkConnection(fake.api, controller.signal), { name: "AbortError" });
});

test("UI fake refuses authenticated business requests without a network fallback", async () => {
  const fake = createFakeDependencies("mobile");
  await assert.rejects(getCurrentUser({ client: fake.api, throwOnError: true }));
  assert.deepEqual(fake.calls, []);
});

test("health rejects malformed and non-successful responses rather than showing ready", async () => {
  for (const response of [
    Response.json({ status: "ok" }),
    new Response("unavailable", { status: 503 }),
  ]) {
    const api = createBrainoidClient({
      baseUrl: "https://custom.invalid",
      fetch: async () => response,
    });
    await assert.rejects(checkConnection(api, new AbortController().signal));
  }
});

test("platform capabilities explicitly leave recording unavailable on both surfaces", () => {
  for (const surface of ["mobile", "display"]) {
    const adapter = createUnavailablePlatform(surface);
    assert.equal(adapter.surface, surface);
    assert.equal(adapter.voice.available, false);
    assert.ok(adapter.voice.reason.length > 0);
  }
});
