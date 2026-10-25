// Capture real generated-SDK Requests for transport-to-server round-trip tests.
// The fake fetch never opens a connection or acquires real credentials.
import assert from "node:assert/strict";
import * as sdk from "../../../packages/api-client/src/index.ts";

const tenantId = process.argv[2];
assert.ok(tenantId, "Pass the synthetic tenant ID");
const uids = [".", "..", "opaque / uid", "%2E", "u?/#+"];
const requests = [];
const client = sdk.createBrainoidClient({
  baseUrl: "https://api.example.test",
  auth: () => "synthetic-owner-token",
  fetch: async (request) => {
    assert.ok(request instanceof Request);
    const url = new URL(request.url);
    const uid = url.searchParams.get("user_id");
    assert.equal(url.pathname, `/v1/tenants/${tenantId}/memberships`);
    assert.equal(url.searchParams.getAll("user_id").length, 1);
    assert.ok(uids.includes(uid));
    assert.equal(url.hash, "");
    requests.push({
      user_id: uid,
      url: request.url,
      method: request.method,
      headers: Object.fromEntries(request.headers),
      body: await request.text(),
    });
    return request.method === "DELETE"
      ? new Response(null, { status: 204 })
      : Response.json({ user_id: uid, role: "admin", status: "active", version: 2 });
  },
});
for (const [index, uid] of uids.entries()) {
  const update = await sdk.updateMembershipByUid({
    client,
    path: { tenant_id: tenantId },
    query: { user_id: uid },
    body: { role: "admin" },
    headers: { "Idempotency-Key": `synthetic-sdk-update-${index}`, "If-Match": '"1"' },
    throwOnError: true,
  });
  assert.equal(update.data.user_id, uid);
  await sdk.revokeMembershipByUid({
    client,
    path: { tenant_id: tenantId },
    query: { user_id: uid },
    headers: { "Idempotency-Key": `synthetic-sdk-revoke-${index}`, "If-Match": '"2"' },
    throwOnError: true,
  });
  assert.equal(requests.at(-1).user_id, uid);
}
process.stdout.write(JSON.stringify(requests));
