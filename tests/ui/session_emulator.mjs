import assert from "node:assert/strict";
import { createBrowserAuthentication } from "../../apps/mobile/src/firebase-browser.ts";
import { SessionController } from "../../packages/ui/src/session.ts";

const baseUrl = process.env.UI_API_URL;
if (!baseUrl || new URL(baseUrl).hostname !== "127.0.0.1") throw Error("Loopback API required");
const auth = createBrowserAuthentication(
  {
    projectId: "demo-brainoid",
    apiKey: "fixture-key",
    appId: "fixture-app",
    authDomain: "demo-brainoid.firebaseapp.com",
  },
  true,
);
const controller = new SessionController({ auth, baseUrl });
const stop = controller.start();
const tick = () => new Promise((resolve) => setTimeout(resolve, 20));
async function waitFor(predicate) {
  for (let i = 0; i < 250; i++) {
    if (predicate(controller.snapshot())) return;
    await tick();
  }
  throw Error("UI state timed out");
}
try {
  await waitFor((state) => state.phase === "signed-out");
  await controller.signIn("ui-owner@example.test", "synthetic-password-only");
  await waitFor((state) => state.tenants.length === 1);
  const a = controller.snapshot().tenants[0];
  assert.equal(a.name, "Fixture Tenant A");
  await controller.selectTenant(a.id);
  assert.equal(controller.snapshot().context.tenantId, a.id);
  await auth.token("demo-a-owner", true);
  await controller.selectTenant("ffffffff-ffff-4fff-8fff-ffffffffffff");
  assert.equal(controller.snapshot().tenant, undefined);
  assert.match(controller.snapshot().message, /利用できません/);
  await controller.createTenant("UI emulator household", "Asia/Tokyo");
  assert.equal(controller.snapshot().tenant.name, "UI emulator household");
  assert.equal(controller.snapshot().tenants.length, 2);
  const old = controller.snapshot().context;
  await controller.signOut();
  assert.equal(old.signal.aborted, true);
  assert.equal(await auth.current(), null);
  await controller.signIn("ui-new@example.test", "synthetic-password-only");
  await waitFor((state) => state.phase === "ready");
  assert.equal(controller.snapshot().tenants.length, 0);
  await controller.createTenant("Profile not yet created", "Asia/Tokyo");
  assert.equal(controller.snapshot().tenant.name, "Profile not yet created");
  const token = await auth.token("ui-no-profile", false);
  const profile = await fetch(`${baseUrl}/v1/users/me`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  assert.equal(profile.status, 404);
  assert.equal(controller.snapshot().uid, "ui-no-profile");
  await controller.signOut();
  console.log(
    "UI official Firebase SDK + Auth/Firestore + API: sign-in, refresh, membership, creation, no-profile onboarding, signout passed",
  );
} finally {
  stop();
  await auth.signOut();
}
