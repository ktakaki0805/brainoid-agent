import assert from "node:assert/strict";
import { CalendarController } from "../../packages/ui/src/calendar/controller.ts";
import { SessionContext } from "../../packages/ui/src/session.ts";

const baseUrl = process.env.UI_API_URL,
  tenant = process.env.UI_TENANT,
  integration = process.env.UI_INTEGRATION,
  uid = process.env.UI_UID;
assert.ok(
  baseUrl &&
    new URL(baseUrl).hostname === "127.0.0.1" &&
    tenant &&
    integration &&
    uid &&
    process.env.UI_NOW,
);
// Server fixture clock is explicit and shared; no production clock override exists.
Date.now = () => Date.parse(process.env.UI_NOW);
const writes = [];
let lose = true;
const context = new SessionContext({
  uid,
  tenantId: tenant,
  generation: 1,
  baseUrl,
  current: () => true,
  token: async () => "synthetic-calendar-reader",
  denied: () => {},
  transport: async (request) => {
    if (request.method !== "GET")
      writes.push({
        path: new URL(request.url).pathname,
        body: await request.clone().text(),
        key: request.headers.get("Idempotency-Key"),
        match: request.headers.get("If-Match"),
      });
    const response = await fetch(request);
    if (lose && response.status === 202 && request.url.endsWith(":confirm")) {
      lose = false;
      await response.body?.cancel();
      throw Error("Synthetic accepted response loss");
    }
    return response;
  },
});
const calendar = new CalendarController(context, "mobile");
let unobservedContext;
try {
  await calendar.load();
  assert.ok(calendar.snapshot().integrations.some((v) => v.id === integration));
  const unrelatedIds = calendar
    .snapshot()
    .integrations.filter((v) => v.id !== integration)
    .map((v) => v.id)
    .sort();
  assert.ok(unrelatedIds.length > 0, "Fixture includes a separate authorized integration");
  await calendar.select(integration);
  assert.equal(calendar.snapshot().selected.id, integration);
  const id = calendar.snapshot().selected.allowed_calendar_ids[0];
  await calendar.events(id, { time_min: "2026-03-01T00:00:00Z", time_max: "2026-04-01T00:00:00Z" });
  assert.equal(calendar.snapshot().events[0].id, "opaque-event");
  assert.equal(calendar.snapshot().events[0].start.timezone, "America/New_York");
  calendar.edit("create");
  calendar.draft({
    ...calendar.snapshot().draft,
    summary: "HTTP Calendar UI synthetic",
    start: { date: "2026-03-08" },
    end: { date: "2026-03-09" },
  });
  await calendar.propose();
  assert.equal(calendar.snapshot().proposal.status, "pending");
  const original = calendar.snapshot().proposal;
  const conversationCard = new CalendarController(context, "mobile");
  await conversationCard.openProposal(original.proposal_id);
  await Promise.all([calendar.decide("confirm"), calendar.decide("confirm")]);
  assert.equal(calendar.snapshot().pending, true);
  await calendar.retry();
  assert.deepEqual(writes[1], writes[2]);
  assert.equal(calendar.snapshot().operation.kind, "calendar_write");
  assert.equal(calendar.snapshot().operation.status, "pending");
  const operation = calendar.snapshot().operation.operation_id;
  assert.equal(
    (await fetch(`${baseUrl}/fixture/execute/${operation}`, { method: "POST" })).status,
    200,
  );
  await calendar.refreshProposal();
  assert.equal(calendar.snapshot().operation.status, "outcome_unknown");
  assert.equal(calendar.snapshot().proposal.args_hash, original.args_hash);
  await conversationCard.refreshProposal();
  assert.equal(conversationCard.snapshot().operation.status, "outcome_unknown");
  // A separate live test context exercises cards whose metadata has never been read.
  let hold = false,
    captured = false,
    release,
    lateBodyReads = 0,
    denied = 0;
  unobservedContext = new SessionContext({
    uid,
    tenantId: tenant,
    generation: 2,
    baseUrl,
    current: () => true,
    token: async () => "synthetic-calendar-reader",
    denied: () => {
      denied++;
    },
    transport: async (request) => {
      const response = await fetch(request);
      if (hold && request.method === "GET" && request.url.includes("/calendar-proposals/")) {
        const json = response.json.bind(response);
        response.json = async () => {
          lateBodyReads++;
          return json();
        };
        captured = true;
        await new Promise((done) => {
          release = done;
        });
      }
      return response;
    },
  });
  const unobservedCard = new CalendarController(unobservedContext, "mobile"),
    lateCard = new CalendarController(unobservedContext, "mobile"),
    unobservedWorkspace = new CalendarController(unobservedContext, "mobile");
  await unobservedCard.openProposal(original.proposal_id);
  assert.equal(unobservedCard.snapshot().operation.status, "outcome_unknown");
  hold = true;
  const opening = lateCard.openProposal(original.proposal_id);
  while (!captured) await new Promise((resolve) => setTimeout(resolve, 5));
  const count = writes.length;
  calendar.edit("create");
  await calendar.decide("confirm");
  assert.equal(writes.length, count);
  assert.equal(calendar.snapshot().draft, undefined);
  await calendar.disconnect();
  assert.equal(calendar.snapshot().selected.status, "revoked");
  assert.equal(calendar.snapshot().selected.cleanup_status, "pending");
  assert.equal(calendar.snapshot().events.length, 0);
  assert.equal(calendar.snapshot().proposal, undefined);
  assert.equal(calendar.snapshot().operation, undefined);
  assert.equal(conversationCard.snapshot().proposal, undefined);
  assert.equal(conversationCard.snapshot().operation, undefined);
  await calendar.refreshIntegration();
  assert.equal(calendar.snapshot().selected.cleanup_status, "pending");
  // Remove only this synthetic Emulator seed. The subsequent list omits this ID
  // by the actual authorized API, not a fabricated transport response.
  const firestore = new URL(`http://${process.env.FIRESTORE_EMULATOR_HOST}`);
  assert.equal(firestore.hostname, "127.0.0.1");
  assert.equal(process.env.GCP_PROJECT_ID, "demo-brainoid");
  const seed = new URL(
    `/v1/projects/demo-brainoid/databases/(default)/documents/tenants/${tenant}/integrations/${integration}`,
    firestore,
  );
  // Official Emulator administrator marker, as in scripts/demo_emulator.py; not an API user token.
  assert.equal(
    (await fetch(seed, { method: "DELETE", headers: { Authorization: "Bearer owner" } })).status,
    200,
  );
  await unobservedWorkspace.load();
  await opening;
  assert.deepEqual(
    unobservedWorkspace
      .snapshot()
      .integrations.map((v) => v.id)
      .sort(),
    unrelatedIds,
  );
  assert.equal(unobservedCard.snapshot().proposal, undefined);
  assert.equal(unobservedCard.snapshot().operation, undefined);
  release();
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.equal(lateCard.snapshot().proposal, undefined);
  assert.equal(lateBodyReads, 0);
  assert.equal(denied, 0);
  context.dispose();
  assert.equal(calendar.snapshot().proposal, undefined);
  console.log(
    "Calendar HTTP Firestore controller passed: actual canonical DTO/ETag, DST event, fixed proposal, duplicate/lost confirm, outcome_unknown, disconnect pending, private erase, metadata-unobserved accepted card and late response erased by actual complete absence; synthetic reader and provider.",
  );
} finally {
  context.dispose();
  unobservedContext?.dispose();
}
