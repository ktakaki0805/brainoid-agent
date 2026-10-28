import type { Operation } from "@brainoid-internal/api-client";
export type DataScenario =
  | "normal"
  | "recent"
  | "last_owner"
  | "lost"
  | "unavailable"
  | "denied"
  | "expired"
  | "failed"
  | "unknown"
  | "account_finalized"
  | "broken_stream";
export function createDataControlsFixture() {
  const operations = new Map<
    string,
    { operation: Operation; tenant: string | undefined; uid: string }
  >();
  const receipts = new Map<string, { body: string; id: string }>();
  const controls = {
    scenario: "normal" as DataScenario,
    calls: [] as { path: string; method: string; key: string | null; body?: unknown }[],
    lost: false,
  };
  // Valid uncompressed synthetic ZIP containing an empty file, no private data.
  const zip = Uint8Array.from(
    atob(
      "UEsDBBQAAAAAAPccXV3a0dOODgAAAA4AAAAJAAAAZGF0YS5qc29ueyJyZWNvcmRzIjpbXX1QSwECFAMUAAAAAAD3HF1d2tHTjg4AAAAOAAAACQAAAAAAAAAAAAAAgAEAAAAAZGF0YS5qc29uUEsFBgAAAAABAAEANwAAADUAAAAAAA==",
    ),
    (v) => v.charCodeAt(0),
  );
  async function handle(request: Request, uid: string): Promise<Response | undefined> {
    const url = new URL(request.url),
      match = /^\/v1\/(?:tenants\/([^/]+)\/)?operations(?:\/([^/]+))?(\/download)?$/.exec(
        url.pathname,
      );
    if (!match) return;
    const tenant = match[1],
      id = match[2];
    const body = request.method === "POST" ? await request.clone().json() : undefined;
    if (
      request.method === "POST" &&
      !["export", "leave_tenant", "delete_tenant", "delete_account"].includes(body?.kind) &&
      !(body?.kind === "delete_resource" && body.target_type === "conversation")
    )
      return;
    if (request.method === "GET" && !operations.has(id ?? "")) return;
    const key = request.headers.get("Idempotency-Key");
    controls.calls.push({ path: url.pathname, method: request.method, key, body });
    if (controls.scenario === "denied") return Response.json({}, { status: 403 });
    if (request.method === "POST") {
      if (controls.scenario === "recent") return Response.json({}, { status: 403 });
      if (controls.scenario === "last_owner") return Response.json({}, { status: 409 });
      if (controls.scenario === "unavailable") return Response.json({}, { status: 503 });
      if (!key || (body.kind === "delete_account") !== (tenant === undefined))
        return Response.json({}, { status: 422 });
      const binding = `${tenant}:${uid}:${key}`,
        previous = receipts.get(binding);
      if (previous && previous.body !== JSON.stringify(body))
        return Response.json({}, { status: 409 });
      const value: Operation = previous
        ? (operations.get(previous.id) as { operation: Operation }).operation
        : {
            operation_id: crypto.randomUUID(),
            kind: body.kind,
            status: "pending",
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
          };
      operations.set(value.operation_id, { operation: value, tenant, uid });
      receipts.set(binding, { body: JSON.stringify(body), id: value.operation_id });
      if (controls.scenario === "lost" && !controls.lost) {
        controls.lost = true;
        throw new TypeError("Synthetic response lost");
      }
      return Response.json(value, { status: 202 });
    }
    const saved = operations.get(id ?? "");
    if (
      !saved ||
      saved.tenant !== tenant ||
      saved.uid !== uid ||
      controls.scenario === "account_finalized"
    )
      return Response.json({}, { status: 404 });
    if (controls.scenario === "expired") return Response.json({}, { status: 410 });
    if (match[3]) {
      if (saved.operation.kind !== "export" || saved.operation.status !== "succeeded")
        return Response.json({}, { status: 409 });
      const body =
        controls.scenario === "broken_stream"
          ? new ReadableStream({
              start(controller) {
                controller.enqueue(zip.slice(0, 30));
                controller.error(new TypeError("Synthetic stream interrupted"));
              },
            })
          : zip;
      return new Response(body, {
        headers: {
          "content-type": "application/zip",
          "cache-control": "no-store",
          "content-length": String(zip.length),
        },
      });
    }
    saved.operation = {
      ...saved.operation,
      status:
        controls.scenario === "failed"
          ? "failed"
          : controls.scenario === "unknown"
            ? "outcome_unknown"
            : "succeeded",
      ...(saved.operation.kind === "export"
        ? { download_expires_at: new Date(Date.now() + 86400_000).toISOString() }
        : {}),
    };
    return Response.json(saved.operation);
  }
  return { controls, handle, operations, zip };
}
