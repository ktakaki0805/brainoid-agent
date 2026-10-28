import type { Entity, Memory, MemoryCreate, Operation } from "@brainoid-internal/api-client";
export const subjectId = "60000000-0000-4000-8000-000000000001";
export const sharedMemoryId = "70000000-0000-4000-8000-000000000001";
export const privateMemoryId = "70000000-0000-4000-8000-000000000002";
const at = "2026-10-28T00:00:00Z";
export type MemoryScenario =
  | "empty"
  | "normal"
  | "reject"
  | "conflict"
  | "lost_save"
  | "forget_failed"
  | "lost_forget"
  | "purge_failed";
export function createMemoryFixture() {
  const entityRows = new Map<string, Entity[]>();
  const entityReceipts = new Map<string, { hash: string; entity: Entity }>();
  const tenants = new Map<string, Map<string, Memory>>();
  const receipts = new Map<string, { hash: string; id: string; kind: "memory" | "operation" }>();
  const operations = new Map<string, { uid: string; tenant: string; operation: Operation }>();
  const controls = {
    scenario: "normal" as MemoryScenario,
    calls: [] as {
      method: string;
      path: string;
      key: string | null;
      match: string | null;
      body: unknown;
    }[],
    delay: 0,
  };
  const memory = (id: string, tenant: string, uid: string, body: MemoryCreate): Memory => ({
    ...body,
    id,
    tenant_id: tenant,
    version: 1,
    created_at: at,
    updated_at: at,
    recorded_at: at,
    created_by: uid,
    ...(body.visibility === "private" ? { owner_user_id: uid } : {}),
    origin: "user_explicit",
    status: "confirmed",
    source_refs: body.source_refs ?? [],
    valid_from: body.valid_from ?? at,
    deletion_epoch: 0,
  });
  function records(tenant: string, uid: string) {
    let rows = tenants.get(`${tenant}:${uid}`);
    if (!rows) {
      rows = new Map([
        [
          sharedMemoryId,
          memory(sharedMemoryId, tenant, uid, {
            subject_entity_id: subjectId,
            predicate: "prefers",
            value: "夜は穏やかな照明",
            visibility: "shared",
            display_policy: "shared_display",
            source_refs: [
              { kind: "note", resource_id: "80000000-0000-4000-8000-000000000001", version: 1 },
            ],
          }),
        ],
        [
          privateMemoryId,
          memory(privateMemoryId, tenant, uid, {
            subject_entity_id: subjectId,
            predicate: "prefers",
            value: "個人だけの読書の好み",
            visibility: "private",
            display_policy: "never",
          }),
        ],
      ]);
      tenants.set(`${tenant}:${uid}`, rows);
    }
    return rows;
  }
  const handle = async (request: Request, uid: string): Promise<Response | undefined> => {
    const url = new URL(request.url),
      match =
        /^\/v1\/tenants\/([^/]+)\/(entities|memories(?::search)?|operations)(?:\/([^/]+))?$/.exec(
          url.pathname,
        );
    if (!match) return;
    const tenant = match[1] ?? "",
      resource = match[2],
      id = match[3],
      rows = records(tenant, uid);
    const body = request.method === "GET" ? undefined : await request.json();
    const key = request.headers.get("Idempotency-Key"),
      scopeKey = `${tenant}:${uid}:${key}`;
    controls.calls.push({
      method: request.method,
      path: url.pathname,
      key,
      match: request.headers.get("If-Match"),
      body,
    });
    if (controls.delay) await new Promise((resolve) => setTimeout(resolve, controls.delay));
    if (
      resource === "memories:search" &&
      (!Number.isInteger(body.limit) || body.limit < 1 || body.limit > 40)
    )
      return Response.json({}, { status: 422 });
    if (resource === "entities") {
      const partition = `${tenant}:${uid}`;
      let entities = entityRows.get(partition);
      if (!entities) {
        entities = [
          {
            id: subjectId,
            tenant_id: tenant,
            version: 1,
            created_at: at,
            updated_at: at,
            kind: "room",
            label: "リビング",
            visibility: "shared",
            display_policy: "shared_display",
          },
        ];
        entityRows.set(partition, entities);
      }
      if (request.method === "GET") {
        if (controls.scenario === "empty") {
          entities.splice(0);
          rows.clear();
          controls.scenario = "normal";
        }
        return Response.json({ items: entities, next_cursor: null });
      }
      if (body.visibility === "private" && body.display_policy === "shared_display")
        return Response.json({}, { status: 422 });
      const hash = JSON.stringify(body),
        receipt = entityReceipts.get(scopeKey);
      if (receipt)
        return receipt.hash === hash
          ? Response.json(receipt.entity, { status: 201 })
          : Response.json({}, { status: 409 });
      const entity: Entity = {
        ...body,
        id: crypto.randomUUID(),
        tenant_id: tenant,
        version: 1,
        created_at: at,
        updated_at: at,
        ...(body.visibility === "private" ? { owner_user_id: uid } : {}),
      };
      entities.push(entity);
      entityReceipts.set(scopeKey, { hash, entity });
      return Response.json(entity, { status: 201 });
    }
    if (resource === "operations") {
      if (request.method === "GET") {
        const saved = operations.get(id ?? "");
        if (!saved || saved.uid !== uid || saved.tenant !== tenant)
          return Response.json({}, { status: 404 });
        saved.operation = {
          ...saved.operation,
          status: controls.scenario === "purge_failed" ? "failed" : "succeeded",
        };
        return Response.json(saved.operation);
      }
      if (controls.scenario === "forget_failed") return Response.json({}, { status: 503 });
      const hash = JSON.stringify(body),
        receipt = receipts.get(scopeKey);
      if (receipt)
        return receipt.hash === hash && receipt.kind === "operation"
          ? Response.json(operations.get(receipt.id)?.operation, { status: 202 })
          : Response.json({}, { status: 409 });
      const target = rows.get(body.target_id);
      if (!target) return Response.json({}, { status: 404 });
      if (target.version !== body.expected_version) return Response.json({}, { status: 412 });
      const operation: Operation = {
        operation_id: crypto.randomUUID(),
        kind: "forget_memory",
        status: "pending",
        created_at: at,
        updated_at: at,
      };
      operations.set(operation.operation_id, { uid, tenant, operation });
      rows.delete(target.id);
      receipts.set(scopeKey, { hash, id: operation.operation_id, kind: "operation" });
      if (controls.scenario === "lost_forget") {
        controls.scenario = "normal";
        throw new Error("Synthetic response loss");
      }
      return Response.json(operation, { status: 202 });
    }
    if (resource === "memories:search" || (request.method === "GET" && !id))
      return Response.json({
        items: [...rows.values()].filter(
          (row) =>
            row.status === "confirmed" && (!body?.query || String(row.value).includes(body.query)),
        ),
        next_cursor: null,
      });
    if (request.method === "GET") {
      const value = rows.get(id ?? "");
      return value
        ? Response.json(value, { headers: { ETag: `"${value.version}"` } })
        : Response.json({}, { status: 404 });
    }
    const input: MemoryCreate = request.method === "PATCH" ? body.replacement : body;
    if (
      controls.scenario === "reject" ||
      (input.visibility === "private" && input.display_policy === "shared_display")
    )
      return Response.json({}, { status: 422 });
    const hash = JSON.stringify({ body, id, match: request.headers.get("If-Match") }),
      receipt = receipts.get(scopeKey);
    if (receipt)
      return receipt.hash === hash && receipt.kind === "memory"
        ? Response.json(rows.get(receipt.id), { status: request.method === "POST" ? 201 : 200 })
        : Response.json({}, { status: 409 });
    const old = id ? rows.get(id) : undefined;
    if (request.method === "PATCH") {
      if (!old) return Response.json({}, { status: 404 });
      if (controls.scenario === "conflict") {
        const newer = memory(crypto.randomUUID(), tenant, uid, {
          ...input,
          value: "別の端末で更新した好み",
        });
        newer.supersedes_fact_id = old.id;
        rows.set(old.id, { ...old, status: "superseded", version: old.version + 1 });
        rows.set(newer.id, newer);
        controls.scenario = "normal";
      }
      if (
        rows.get(old.id)?.version !== Number(request.headers.get("If-Match")?.replaceAll('"', ""))
      )
        return Response.json({}, { status: 412 });
      if (input.visibility !== old.visibility || input.display_policy !== old.display_policy)
        return Response.json({}, { status: 422 });
    }
    const result = memory(crypto.randomUUID(), tenant, uid, input);
    if (old) {
      result.supersedes_fact_id = old.id;
      rows.set(old.id, { ...old, status: "superseded", version: old.version + 1 });
    }
    rows.set(result.id, result);
    receipts.set(scopeKey, { hash, id: result.id, kind: "memory" });
    if (controls.scenario === "lost_save") {
      controls.scenario = "normal";
      throw new Error("Synthetic response loss");
    }
    return Response.json(result, {
      status: request.method === "POST" ? 201 : 200,
      headers: { ETag: '"1"' },
    });
  };
  return { controls, handle, records, operations };
}
