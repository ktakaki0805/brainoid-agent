import assert from "node:assert/strict";
import { test } from "node:test";
import { SessionController } from "../packages/ui/src/session.ts";
import { createSessionFixture, tenantA, tenantB } from "../packages/ui/src/session-fixture.ts";
import { DataControlsController } from "../packages/ui/src/data-controls/controller.ts";
import { nativeExportDelivery } from "../apps/mobile/data-controls/native.ts";
const tick = () => new Promise((resolve) => setImmediate(resolve));
const deferred = () => {
  let resolve;
  const promise = new Promise((r) => {
    resolve = r;
  });
  return { promise, resolve };
};
async function using(work, wrap = (transport) => transport) {
  const fixture = createSessionFixture();
  const saved = [];
  const session = new SessionController({
    auth: fixture.auth,
    transport: wrap(fixture.transport, fixture),
    baseUrl: "https://session.invalid",
  });
  const stop = session.start();
  await tick();
  await session.signIn("synthetic@example.test", "synthetic");
  await session.selectTenant(tenantA);
  const controller = new DataControlsController(session, {
    available: true,
    save: async (blob, signal) => {
      signal.throwIfAborted();
      saved.push(blob);
    },
  });
  try {
    await work({ fixture, controller, session, saved });
  } finally {
    controller.dispose();
    stop();
  }
}
const post = (fixture) => fixture.dataControls.controls.calls.filter((v) => v.method === "POST");
const ready = async (controller) => {
  await controller.prepare("export");
  await controller.confirm();
  await controller.refresh();
  assert.equal(controller.canDownload(), true);
};
test("self account request has no target UID or tenant and acceptance is pending", () =>
  using(async ({ controller, fixture, session }) => {
    let disposed = false;
    session.snapshot().context.own(() => {
      disposed = true;
    });
    await controller.prepare("delete_account");
    assert.equal(post(fixture).length, 0);
    await controller.confirm();
    assert.equal(disposed, true);
    assert.equal(session.snapshot().context, undefined);
    assert.deepEqual(post(fixture)[0].body, { kind: "delete_account", confirm: true });
    assert.equal(post(fixture)[0].path, "/v1/operations");
    assert.equal(controller.snapshot().operation.status, "pending");
    assert.match(controller.snapshot().notice, /物理消去.*完了を意味しません/);
  }));
test("owner exact current tenant name is required, member cannot request tenant deletion", () =>
  using(async ({ controller, fixture, session }) => {
    await controller.prepare("delete_tenant", { name: "テナント B" });
    assert.equal(controller.snapshot().review, undefined);
    fixture.devices.controls.role = "member";
    await session.selectTenant(tenantA);
    await controller.prepare("delete_tenant", { name: "テナント A" });
    await controller.confirm();
    assert.equal(post(fixture).length, 0);
    fixture.devices.controls.role = "owner";
    await session.selectTenant(tenantA);
    await controller.prepare("delete_tenant", { name: "テナント A" });
    await controller.confirm();
    assert.equal(post(fixture)[0].path, `/v1/tenants/${tenantA}/operations`);
    assert.deepEqual(post(fixture)[0].body, {
      kind: "delete_tenant",
      confirmation_name: "テナント A",
    });
  }));
test("lost acceptance retries exact body and key after clearing tenant caches", () =>
  using(async ({ fixture, controller }) => {
    fixture.dataControls.controls.scenario = "lost";
    await controller.prepare("leave_tenant");
    await controller.confirm();
    assert.equal(controller.snapshot().operation, undefined);
    assert.equal(controller.snapshot().pending, true);
    await controller.retry();
    assert.equal(controller.snapshot().operation.status, "pending");
    assert.deepEqual(post(fixture)[0], post(fixture)[1]);
  }));
test("recent auth uses same identity reauthentication rather than refresh and preserves receipt", () =>
  using(async ({ fixture, controller }) => {
    fixture.dataControls.controls.scenario = "recent";
    await controller.prepare("delete_account");
    await controller.confirm();
    assert.equal(controller.snapshot().reauthentication, true);
    assert.equal(fixture.controls.refreshes, 0);
    await controller.reauthenticate("other@example.test", "synthetic");
    assert.equal(controller.snapshot().reauthentication, true);
    await controller.reauthenticate("synthetic@example.test", "synthetic");
    await controller.retry();
    assert.deepEqual(post(fixture)[0], post(fixture)[1]);
    assert.equal(controller.snapshot().operation.status, "pending");
  }));
test("last owner rejection remains unaccepted and can return to target selection", () =>
  using(async ({ fixture, controller }) => {
    fixture.dataControls.controls.scenario = "last_owner";
    await controller.prepare("leave_tenant");
    await controller.confirm();
    assert.equal(controller.snapshot().operation, undefined);
    assert.match(controller.snapshot().notice, /所有権を移譲/);
    assert.equal(controller.snapshot().rejected, true);
    await controller.resetRejected();
    assert.equal(controller.snapshot().pending, false);
    await controller.prepare("delete_tenant", { name: "テナント A" });
    assert.equal(controller.snapshot().review.body.kind, "delete_tenant");
  }));
test("503 never implies acceptance or erasure and cannot reset an uncertain request", () =>
  using(async ({ fixture, controller }) => {
    fixture.dataControls.controls.scenario = "unavailable";
    await controller.prepare("delete_account");
    await controller.confirm();
    assert.equal(controller.snapshot().operation, undefined);
    assert.equal(controller.snapshot().pending, true);
    fixture.dataControls.controls.scenario = "last_owner";
    await controller.retry();
    await controller.resetRejected();
    assert.equal(controller.snapshot().pending, true);
  }));
for (const scenario of ["failed", "unknown", "account_finalized"])
  test(`account ${scenario} never becomes succeeded`, () =>
    using(async ({ fixture, controller }) => {
      await controller.prepare("delete_account");
      await controller.confirm();
      fixture.dataControls.controls.scenario = scenario;
      await controller.refresh();
      assert.notEqual(controller.snapshot().operation?.status, "succeeded");
      if (scenario === "account_finalized")
        assert.match(controller.snapshot().notice, /消去完了とは判断できません/);
    }));
for (const transition of ["tenant", "identity", "logout", "pagehide", "aba"])
  test(`${transition} clears receipt and any late response`, () =>
    using(async ({ fixture, controller, session }) => {
      await controller.prepare("delete_account");
      await controller.confirm();
      if (transition === "tenant" || transition === "aba") {
        await session.selectTenant(tenantB);
        if (transition === "aba") await session.selectTenant(tenantA);
      }
      if (transition === "identity") fixture.emit({ uid: "other@example.test" });
      if (transition === "logout") await session.signOut();
      if (transition === "pagehide") {
        session.suspend();
        await tick();
      }
      const before = post(fixture).length;
      await controller.retry();
      await controller.refresh();
      assert.equal(post(fixture).length, before);
      assert.equal(controller.snapshot().operation, undefined);
      assert.equal(controller.snapshot().label, undefined);
    }));
test("export uses authenticated no-store fixed endpoint and only complete ZIP is delivered", () =>
  using(
    async ({ controller, saved }) => {
      await ready(controller);
      await controller.download();
      assert.equal(saved.length, 1);
      assert.equal(saved[0].type, "application/zip");
      controller.finish();
      await controller.prepare("export", { scope: "visible_tenant_data" });
      assert.equal(controller.snapshot().review.body.scope, "visible_tenant_data");
    },
    (transport) => async (input, init) => {
      const request = new Request(input, init);
      if (request.url.endsWith("/download")) {
        assert.equal(request.headers.get("Authorization"), "Bearer fixture-token");
        assert.equal(request.cache, "no-store");
        assert.equal(new URL(request.url).search, "");
      }
      return transport(request);
    },
  ));
for (const scenario of ["denied", "expired", "broken_stream"])
  test(`${scenario} discards export and all protected tenant state`, () =>
    using(async ({ fixture, controller, saved, session }) => {
      await ready(controller);
      fixture.dataControls.controls.scenario = scenario;
      await controller.download();
      assert.equal(saved.length, 0);
      assert.equal(controller.snapshot().operation, undefined);
      assert.equal(session.snapshot().context, undefined);
    }));
for (const variant of ["type", "length", "oversize", "truncated", "nostore"])
  test(`invalid export ${variant} is never delivered`, () =>
    using(
      async ({ controller, saved }) => {
        await ready(controller);
        await controller.download();
        assert.equal(saved.length, 0);
      },
      (transport) => async (input, init) => {
        const request = new Request(input, init);
        const response = await transport(request);
        if (!request.url.endsWith("/download")) return response;
        const headers = new Headers(response.headers);
        let bytes = new Uint8Array(await response.arrayBuffer());
        if (variant === "type") headers.set("content-type", "application/json");
        if (variant === "length") headers.set("content-length", String(bytes.length + 1));
        if (variant === "oversize") {
          bytes = new Uint8Array(4 * 1024 * 1024 + 1);
          headers.delete("content-length");
        }
        if (variant === "truncated") {
          bytes = bytes.slice(0, 30);
          headers.delete("content-length");
        }
        if (variant === "nostore") headers.delete("cache-control");
        return new Response(bytes, { headers });
      },
    ));
test("tenant switch during unfinished stream cancels download without retaining bytes", () => {
  const gate = deferred();
  return using(
    async ({ controller, session, saved }) => {
      await ready(controller);
      const pending = controller.download();
      await gate.promise;
      await session.selectTenant(tenantB);
      await pending;
      assert.equal(saved.length, 0);
      assert.equal(controller.snapshot().operation, undefined);
    },
    (transport) => async (input, init) => {
      const request = new Request(input, init);
      if (!request.url.endsWith("/download")) return transport(request);
      return new Response(
        new ReadableStream({
          start(controller) {
            controller.enqueue(new Uint8Array([0x50, 0x4b, 3, 4]));
            gate.resolve();
          },
          cancel() {},
        }),
        { headers: { "content-type": "application/zip", "cache-control": "no-store" } },
      );
    },
  );
});
function nativeFixture() {
  const files = new Map([
    ["brainoid-export-v1-00000000-0000-4000-8000-000000000001.zip", "stale"],
    ["other-feature.txt", "keep"],
  ]);
  const calls = [],
    control = { write: undefined, sharing: undefined, cleanupFails: false };
  const fs = {
    async readdir() {
      return { files: [...files.keys()].map((name) => ({ name, type: "file" })) };
    },
    async writeFile(value) {
      calls.push(["write", value.path]);
      await control.write?.promise;
      files.set(value.path, value.data);
      return { uri: `file:///cache/${value.path}` };
    },
    async deleteFile(value) {
      calls.push(["delete", value.path]);
      if (control.cleanupFails) throw new Error("Synthetic disk failure");
      files.delete(value.path);
    },
  };
  const share = {
    async canShare() {
      return { value: true };
    },
    async share(value) {
      calls.push(["share", value.files]);
      await control.sharing?.promise;
    },
  };
  return { files, calls, control, adapter: nativeExportDelivery(fs, share) };
}
test("native startup and successful sharing clean only this adapter cache", async () => {
  const f = nativeFixture();
  await f.adapter.initialize();
  assert.deepEqual([...f.files.keys()], ["other-feature.txt"]);
  await f.adapter.save(
    new Blob([new Uint8Array(22)], { type: "application/zip" }),
    new AbortController().signal,
  );
  assert.equal(f.calls.filter((v) => v[0] === "share").length, 1);
  assert.deepEqual([...f.files.keys()], ["other-feature.txt"]);
});
for (const stage of ["write", "sharing"])
  test(`native abort during ${stage} cleans the completed write without retaining a file`, async () => {
    const f = nativeFixture();
    await f.adapter.initialize();
    const gate = deferred();
    f.control[stage] = gate;
    const abort = new AbortController();
    const saving = f.adapter.save(
      new Blob([new Uint8Array(22)], { type: "application/zip" }),
      abort.signal,
    );
    await tick();
    abort.abort();
    gate.resolve();
    await assert.rejects(saving);
    assert.deepEqual([...f.files.keys()], ["other-feature.txt"]);
    if (stage === "write")
      assert.equal(
        f.calls.some((v) => v[0] === "share"),
        false,
      );
  });
test("native cleanup failure is visible and prevents another export", async () => {
  const f = nativeFixture();
  await f.adapter.initialize();
  f.control.cleanupFails = true;
  await assert.rejects(
    f.adapter.save(
      new Blob([new Uint8Array(22)], { type: "application/zip" }),
      new AbortController().signal,
    ),
  );
  assert.equal(f.adapter.available, false);
  assert.match(f.adapter.snapshot(), /一時ファイルを削除できません/);
  f.control.cleanupFails = false;
  await f.adapter.initialize();
  assert.equal(f.adapter.available, true);
  assert.deepEqual([...f.files.keys()], ["other-feature.txt"]);
});
