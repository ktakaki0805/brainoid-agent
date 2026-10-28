import assert from "node:assert/strict";
import { createBrowserAuthentication } from "../../apps/mobile/src/firebase-browser.ts";
import { SessionController } from "../../packages/ui/src/session.ts";
import {
  TenantSettingsController,
  InvitationAcceptanceController,
} from "../../packages/ui/src/tenant-settings/controller.ts";
const baseUrl = process.env.UI_API_URL;
assert.equal(new URL(baseUrl).hostname, "127.0.0.1");
const credentials = {
  projectId: "demo-brainoid",
  apiKey: "fixture-key",
  appId: "fixture-app",
  authDomain: "demo-brainoid.firebaseapp.com",
};
const auth = createBrowserAuthentication(credentials, true);
let loseTransfer = false;
const calls = [];
const transport = async (request) => {
  if (request.method !== "GET")
    calls.push({
      url: request.url,
      method: request.method,
      match: request.headers.get("If-Match"),
      key: request.headers.get("Idempotency-Key"),
      body: request.method === "DELETE" ? null : await request.clone().json(),
    });
  const response = await fetch(request);
  if (loseTransfer && request.url.endsWith("memberships:transfer-owner") && response.ok) {
    loseTransfer = false;
    await response.body.cancel();
    throw Error("Synthetic response loss");
  }
  return response;
};
const session = new SessionController({ auth, baseUrl, transport }),
  stop = session.start();
let settings, accept, otherStop, otherAuth;
let stage = "sign-in";
const at = (value) => {
  stage = value;
  console.log(`Tenant settings controller stage: ${value}`);
};
try {
  await session.signIn("ui-owner@example.test", "synthetic-password-only");
  await session.loadTenants(false, false);
  at("tenant-list");
  const tenant = session.snapshot().tenants.find((v) => v.name === "Fixture Tenant A");
  assert.ok(tenant);
  await session.selectTenant(tenant.id);
  settings = new TenantSettingsController(session);
  await settings.load();
  at("settings-loaded");
  assert.equal(settings.snapshot().tenant?.my_role, "owner");
  assert.ok(settings.snapshot().usage);
  at("invite");
  await settings.prepare("invite", undefined, "member");
  await settings.confirm();
  const code = settings.snapshot().invitation?.code;
  assert.ok(code, "Invitation was not returned");
  settings.hideCode();
  otherAuth = createBrowserAuthentication(credentials, true);
  const other = new SessionController({ auth: otherAuth, baseUrl });
  otherStop = other.start();
  await other.signIn("ui-new@example.test", "synthetic-password-only");
  assert.equal(other.snapshot().tenants.length, 0);
  accept = new InvitationAcceptanceController(other);
  await accept.accept(code);
  assert.match(accept.snapshot().notice, /参加を確認/);
  assert.equal(other.selectedTenantId(), undefined);
  await other.selectTenant(tenant.id);
  const memberSettings = new TenantSettingsController(other);
  await memberSettings.load();
  assert.equal(memberSettings.snapshot().tenant.my_role, "member");
  assert.equal(memberSettings.snapshot().members.length, 0);
  memberSettings.dispose();
  at("opaque-uid-mutations");
  for (const uid of [".", "..", "opaque / uid", "%2E", "u?/#+"]) {
    await settings.prepare("role", uid, "admin");
    assert.equal(settings.snapshot().review.target.user_id, uid);
    await settings.confirm();
    assert.equal(settings.snapshot().members.find((v) => v.user_id === uid).role, "admin");
    await settings.prepare("revoke", uid);
    await settings.confirm();
    assert.equal(settings.snapshot().members.find((v) => v.user_id === uid).status, "revoked");
    const actual = calls.filter((v) => new URL(v.url).searchParams.get("user_id") === uid);
    assert.equal(actual.length, 2);
    assert.equal(actual[0].match, '"1"');
    assert.equal(actual[1].match, '"2"');
  }
  at("reauth-transfer");
  assert.equal(
    await session.reauthenticate("ui-owner@example.test", "synthetic-password-only"),
    true,
  );
  await settings.load();
  await settings.prepare("transfer", "demo-a-admin");
  const actorVersion = settings.snapshot().review.actorVersion;
  loseTransfer = true;
  await settings.confirm();
  assert.equal(session.snapshot().tenant.my_role, "member");
  assert.equal(settings.snapshot().pending, true);
  assert.equal(settings.snapshot().members.length, 0);
  await settings.retry();
  assert.equal(settings.snapshot().pending, false);
  assert.match(settings.snapshot().notice, /会員の変更を確認/);
  const transfers = calls.filter((v) => v.url.endsWith("memberships:transfer-owner"));
  assert.equal(transfers.length, 2);
  assert.deepEqual(transfers[0], transfers[1]);
  assert.equal(transfers[0].match, `"${actorVersion}"`);
  console.log(
    "Tenant settings actual Auth/Firestore/API: 5 opaque UID role/revoke pairs, no-membership acceptance, same-user reauthentication, lost transfer exact recovery passed",
  );
} catch (error) {
  console.log(`Tenant settings controller failed at: ${stage}`);
  console.log(`Tenant settings controller notice: ${settings?.snapshot().notice}`);
  console.error(
    error.stack
      .split("\n")
      .filter((v) => v.includes("tests/ui/tenant_settings"))
      .join("\n"),
  );
  process.exitCode = 1;
} finally {
  settings?.dispose();
  accept?.dispose();
  stop();
  otherStop?.();
  await auth.signOut();
  await otherAuth?.signOut();
}
