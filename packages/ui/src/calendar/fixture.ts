import type {
  CalendarEvent,
  CalendarProposal,
  CalendarProposalCreate,
  Integration,
  Operation,
} from "@brainoid-internal/api-client";

export const integrationId = "a1000000-0000-4000-8000-000000000001";
export const calendarId = "synthetic/calendar+calendar@example.test";
export type CalendarScenario =
  | "normal"
  | "empty"
  | "failed"
  | "lost"
  | "conflict"
  | "expired"
  | "provider_conflict"
  | "unknown"
  | "reauth"
  | "cleanup";
/** Synthetic transport only; never used by production entry points. */
export function createCalendarFixture() {
  const controls = {
    scenario: "normal" as CalendarScenario,
    calls: [] as {
      method: string;
      path: string;
      query: string;
      key: string | null;
      match: string | null;
      body: unknown;
    }[],
  };
  const rows = new Map<string, Integration>();
  const proposals = new Map<string, CalendarProposal>();
  const owners = new Map<string, string>();
  const operations = new Map<string, Operation>();
  const receipts = new Map<string, { status: number; data: unknown; etag?: string }>();
  const stamp = () => new Date().toISOString();
  const owner = (tenant: string, uid: string) => `${tenant}:${uid}`;
  const record = (tenant: string, uid: string) => {
    const key = owner(tenant, uid);
    let row = rows.get(key);
    if (!row) {
      row = {
        id: integrationId,
        tenant_id: tenant,
        owner_user_id: uid,
        version: 1,
        provider: "google_calendar",
        status: "active",
        allowed_calendar_ids: [calendarId],
        created_at: stamp(),
        updated_at: stamp(),
        cleanup_status: "not_requested",
        reconnect_allowed: true,
      };
      rows.set(key, row);
    }
    return row;
  };
  const seedProposal = (tenant: string, uid: string, id: string, run?: string) => {
    const value: CalendarProposal = {
      proposal_id: id,
      integration_id: integrationId,
      action: "create",
      normalized_request: {
        action: "create",
        calendar_id: calendarId,
        summary: "会話からの架空の予定",
        start: { date: "2099-11-01" },
        end: { date: "2099-11-02" },
      },
      args_hash: "a".repeat(64),
      expires_at: new Date(Date.now() + 300000).toISOString(),
      status: "pending",
      ...(run ? { run_id: run } : {}),
    };
    proposals.set(id, value);
    owners.set(id, owner(tenant, uid));
    return value;
  };
  const handle = async (request: Request, uid: string): Promise<Response | undefined> => {
    const url = new URL(request.url),
      parts = url.pathname.split("/"),
      tenant = parts[3],
      resource = parts[4],
      id = parts[5],
      tail = parts[6];
    if (!tenant || !["integrations", "calendar-proposals", "operations"].includes(resource ?? ""))
      return;
    if (resource === "operations" && !operations.has(id ?? "")) return;
    const body: unknown =
      request.method === "GET" || request.method === "DELETE" || id?.endsWith(":reject")
        ? undefined
        : await request.json();
    const key = request.headers.get("Idempotency-Key"),
      scope = owner(tenant, uid);
    controls.calls.push({
      method: request.method,
      path: url.pathname,
      query: url.search,
      key,
      match: request.headers.get("If-Match"),
      body,
    });
    if (controls.scenario === "failed") return Response.json({}, { status: 503 });
    const receipt = `${scope}:${url.pathname}:${key}`;
    const reply = (value: { status: number; data: unknown; etag?: string }) =>
      value.status === 204
        ? new Response(null, { status: 204 })
        : Response.json(value.data, {
            status: value.status,
            headers: value.etag ? { ETag: value.etag } : {},
          });
    const replay = receipts.get(receipt);
    if (request.method !== "GET" && replay) return reply(replay);
    const saved = (data: unknown, status: number, etag?: string) => {
      const value = { data, status, ...(etag ? { etag } : {}) };
      receipts.set(receipt, structuredClone(value));
      if (controls.scenario === "lost") {
        controls.scenario = "normal";
        throw new Error("Synthetic response loss");
      }
      return reply(value);
    };
    const row = record(tenant, uid);
    if (resource === "integrations" && !id)
      return Response.json({
        items: controls.scenario === "empty" ? [] : [row],
        next_cursor: null,
      });
    if (id === "google-calendar" && tail === "authorizations")
      return saved(
        {
          authorization_url: "https://accounts.google.com/o/oauth2/v2/auth?state=SYNTHETIC_ONLY",
          expires_at: new Date(Date.now() + 600000).toISOString(),
        },
        201,
      );
    if (resource === "integrations" && id !== integrationId)
      return Response.json({}, { status: 404 });
    if (resource === "integrations" && !tail) {
      if (request.method === "GET") {
        if (controls.scenario === "reauth") row.status = "reauth_required";
        if (controls.scenario === "cleanup") row.cleanup_status = "complete";
        return reply({ status: 200, data: row, etag: `"${row.version}"` });
      }
      if (controls.scenario === "conflict") {
        controls.scenario = "normal";
        row.version++;
        row.allowed_calendar_ids = ["other@example.test"];
        return Response.json({}, { status: 412 });
      }
      if (request.headers.get("If-Match") !== `"${row.version}"`)
        return Response.json({}, { status: 412 });
      row.version++;
      if (request.method === "DELETE") {
        row.status = "revoked";
        row.allowed_calendar_ids = [];
        row.cleanup_status = "pending";
      } else
        row.allowed_calendar_ids = (
          body as { allowed_calendar_ids: string[] }
        ).allowed_calendar_ids;
      return saved(row, request.method === "DELETE" ? 202 : 200, `"${row.version}"`);
    }
    if (tail === "calendar-events") {
      const calendar = url.searchParams.get("calendar_id") ?? "";
      const startDate = (url.searchParams.get("time_min") ?? stamp()).slice(0, 10);
      const endDate = new Date(Date.parse(startDate) + 2 * 86400000).toISOString().slice(0, 10);
      const items: CalendarEvent[] = [
        {
          id: "opaque/event+one",
          calendar_id: calendar,
          summary: "架空の終日予定",
          start: { date: startDate },
          end: { date: endDate },
          etag: '"provider-v1"',
          fetched_at: stamp(),
        },
        {
          id: "opaque:event:two",
          calendar_id: calendar,
          summary: "架空の時刻付き予定",
          start: { date_time: `${startDate}T14:00:00Z`, timezone: "America/New_York" },
          end: { date_time: `${startDate}T15:00:00Z`, timezone: "America/New_York" },
          etag: '"provider-v2"',
          fetched_at: stamp(),
        },
      ];
      return Response.json({
        items: controls.scenario === "empty" ? [] : items,
        next_cursor: null,
      });
    }
    if (tail === "calendar-proposals") {
      const change = body as CalendarProposalCreate,
        value = seedProposal(tenant, uid, crypto.randomUUID());
      value.normalized_request = change;
      value.action = change.action;
      value.args_hash = crypto.randomUUID().replaceAll("-", "").repeat(2);
      return saved(value, 201);
    }
    if (resource === "operations") {
      const value = operations.get(id ?? "");
      if (owners.get(id ?? "") !== scope) return Response.json({}, { status: 404 });
      return Response.json(value);
    }
    if (resource === "calendar-proposals" && id) {
      const [pid, action] = id.split(":"),
        value = proposals.get(pid ?? "");
      if (!value || owners.get(pid ?? "") !== scope) return Response.json({}, { status: 404 });
      if (controls.scenario === "expired" && value.status === "pending") value.status = "expired";
      if (Date.parse(value.expires_at) <= Date.now() && value.status === "pending")
        value.status = "expired";
      if (!action) return Response.json(value);
      if (action === "confirm" && value.operation_id)
        return saved(operations.get(value.operation_id), 202);
      if (value.status !== "pending") return Response.json({}, { status: 409 });
      if (action === "reject") {
        value.status = "declined";
        return saved(value, 200);
      }
      if ((body as { args_hash?: string })?.args_hash !== value.args_hash)
        return Response.json({}, { status: 409 });
      const op: Operation = {
        operation_id: value.proposal_id,
        kind: "calendar_write",
        status:
          controls.scenario === "unknown"
            ? "outcome_unknown"
            : controls.scenario === "provider_conflict"
              ? "failed"
              : "pending",
        created_at: stamp(),
        updated_at: stamp(),
        ...(controls.scenario === "provider_conflict" ? { error_code: "conflict" } : {}),
      };
      operations.set(op.operation_id, op);
      owners.set(op.operation_id, scope);
      value.operation_id = op.operation_id;
      value.status =
        op.status === "pending" ? "confirmed" : op.status === "running" ? "executing" : op.status;
      return saved(op, 202);
    }
    return Response.json({}, { status: 404 });
  };
  return { handle, controls, record, proposals, operations, seedProposal };
}
