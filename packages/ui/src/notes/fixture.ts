import type { Note, NoteCreate, Operation } from "@brainoid-internal/api-client";
export const sharedNoteId = "80000000-0000-4000-8000-000000000001";
export const privateNoteId = "80000000-0000-4000-8000-000000000002";
const at = "2026-10-28T00:00:00Z";
export type NoteScenario =
  | "normal"
  | "empty"
  | "reject"
  | "denied"
  | "conflict"
  | "lost_save"
  | "delete_failed"
  | "lost_delete"
  | "purge_failed"
  | "purge_unknown";
export function createNoteFixture() {
  const tenants = new Map<string, Map<string, Note>>();
  const receipts = new Map<string, { hash: string; id: string; kind: "note" | "operation" }>();
  const operations = new Map<string, { uid: string; tenant: string; operation: Operation }>();
  const controls = {
    scenario: "normal" as NoteScenario,
    delay: 0,
    calls: [] as {
      method: string;
      path: string;
      query: string;
      key: string | null;
      match: string | null;
      body: unknown;
    }[],
  };
  const make = (id: string, tenant: string, uid: string, body: NoteCreate): Note => ({
    ...body,
    id,
    tenant_id: tenant,
    version: 1,
    created_at: at,
    updated_at: at,
    ...(body.visibility === "private" ? { owner_user_id: uid } : {}),
  });
  function records(tenant: string, uid: string) {
    let rows = tenants.get(`${tenant}:${uid}`);
    if (!rows) {
      rows = new Map([
        [
          sharedNoteId,
          make(sharedNoteId, tenant, uid, {
            title: "週末の買い物",
            body: "牛乳とパンを買う",
            visibility: "shared",
            display_policy: "shared_display",
          }),
        ],
        [
          privateNoteId,
          make(privateNoteId, tenant, uid, {
            title: "個人の読書メモ",
            body: "自分だけの架空の読書メモ",
            visibility: "private",
            display_policy: "never",
          }),
        ],
      ]);
      tenants.set(`${tenant}:${uid}`, rows);
    }
    return rows;
  }
  async function handle(request: Request, uid: string): Promise<Response | undefined> {
    const url = new URL(request.url),
      match = /^\/v1\/tenants\/([^/]+)\/(notes|operations)(?:\/([^/]+))?$/.exec(url.pathname);
    if (!match) return;
    const tenant = match[1] ?? "",
      resource = match[2],
      id = match[3];
    const body = request.method === "GET" ? undefined : await request.clone().json();
    if (
      resource === "operations" &&
      (request.method === "GET"
        ? !operations.has(id ?? "")
        : body?.kind !== "delete_resource" || body?.target_type !== "note")
    )
      return;
    const rows = records(tenant, uid),
      key = request.headers.get("Idempotency-Key"),
      receiptKey = `${tenant}:${uid}:${key}`,
      ifMatch = request.headers.get("If-Match");
    controls.calls.push({
      method: request.method,
      path: url.pathname,
      query: url.search,
      key,
      match: ifMatch,
      body,
    });
    if (controls.delay) await new Promise((resolve) => setTimeout(resolve, controls.delay));
    if (controls.scenario === "denied") return Response.json({}, { status: 403 });
    if (resource === "operations" && request.method === "GET") {
      const saved = operations.get(id ?? "");
      if (!saved || saved.uid !== uid || saved.tenant !== tenant)
        return Response.json({}, { status: 404 });
      saved.operation = {
        ...saved.operation,
        status:
          controls.scenario === "purge_failed"
            ? "failed"
            : controls.scenario === "purge_unknown"
              ? "outcome_unknown"
              : "succeeded",
      };
      return Response.json(saved.operation);
    }
    if (resource === "notes" && request.method === "GET") {
      if (id) {
        const note = rows.get(id);
        return note
          ? Response.json(note, { headers: { ETag: `"${note.version}"` } })
          : Response.json({}, { status: 404 });
      }
      if (controls.scenario === "empty") {
        rows.clear();
        controls.scenario = "normal";
      }
      const query = url.searchParams.get("query")?.toLowerCase() ?? "";
      return Response.json({
        items: [...rows.values()].filter((note) =>
          `${note.title}\n${note.body}`.toLowerCase().includes(query),
        ),
        next_cursor: null,
      });
    }
    const hash = JSON.stringify({ path: url.pathname, method: request.method, body, ifMatch }),
      receipt = receipts.get(receiptKey);
    if (receipt) {
      if (receipt.hash !== hash) return Response.json({}, { status: 409 });
      if (receipt.kind === "operation")
        return Response.json(operations.get(receipt.id)?.operation, { status: 202 });
      const note = rows.get(receipt.id);
      return note
        ? Response.json(note, {
            status: request.method === "POST" ? 201 : 200,
            headers: { ETag: `"${note.version}"` },
          })
        : Response.json({}, { status: 404 });
    }
    if (resource === "operations") {
      if (controls.scenario === "delete_failed") return Response.json({}, { status: 503 });
      const note = rows.get(body.target_id);
      if (!note) return Response.json({}, { status: 404 });
      if (note.version !== body.expected_version) return Response.json({}, { status: 412 });
      const operation: Operation = {
        operation_id: crypto.randomUUID(),
        kind: "delete_resource",
        status: "pending",
        created_at: at,
        updated_at: at,
      };
      operations.set(operation.operation_id, { uid, tenant, operation });
      rows.delete(note.id);
      receipts.set(receiptKey, { hash, id: operation.operation_id, kind: "operation" });
      if (controls.scenario === "lost_delete") {
        controls.scenario = "normal";
        throw new Error("Synthetic response loss");
      }
      return Response.json(operation, { status: 202 });
    }
    const original = id ? rows.get(id) : undefined;
    if (id && !original) return Response.json({}, { status: 404 });
    if (original && controls.scenario === "conflict") {
      rows.set(original.id, {
        ...original,
        body: "別の端末で更新した買い物",
        version: original.version + 1,
      });
      controls.scenario = "normal";
    }
    if (original && ifMatch !== `"${rows.get(original.id)?.version}"`)
      return Response.json({}, { status: 412 });
    if (
      controls.scenario === "reject" ||
      ((original?.visibility ?? body.visibility) === "private" &&
        body.display_policy === "shared_display")
    )
      return Response.json({}, { status: 422 });
    const note = original
      ? { ...original, ...body, version: original.version + 1 }
      : make(crypto.randomUUID(), tenant, uid, body);
    rows.set(note.id, note);
    receipts.set(receiptKey, { hash, id: note.id, kind: "note" });
    if (controls.scenario === "lost_save") {
      controls.scenario = "normal";
      throw new Error("Synthetic response loss");
    }
    return Response.json(note, {
      status: original ? 200 : 201,
      headers: { ETag: `"${note.version}"` },
    });
  }
  return { controls, handle, records, operations };
}
