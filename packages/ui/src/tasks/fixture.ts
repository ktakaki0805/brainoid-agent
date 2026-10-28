import type { Task, TaskCreate } from "@brainoid-internal/api-client";
export const sharedTaskId = "90000000-0000-4000-8000-000000000001";
export const privateTaskId = "90000000-0000-4000-8000-000000000002";
const at = "2026-10-28T00:00:00Z";
export type TaskScenario =
  | "normal"
  | "empty"
  | "reject"
  | "denied"
  | "conflict"
  | "conflict_status"
  | "changed_selection"
  | "lost_save"
  | "failed";
export function createTaskFixture() {
  const tenants = new Map<string, Map<string, Task>>();
  const receipts = new Map<string, { hash: string; id: string }>();
  const controls = {
    scenario: "normal" as TaskScenario,
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
  const make = (id: string, tenant: string, uid: string, body: TaskCreate): Task => ({
    ...body,
    id,
    tenant_id: tenant,
    status: "open",
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
          sharedTaskId,
          make(sharedTaskId, tenant, uid, {
            title: "週末の買い物",
            due_at: "2026-11-01T05:30:00Z",
            timezone: "America/New_York",
            assignee_user_id: uid,
            visibility: "shared",
            display_policy: "shared_display",
          }),
        ],
        [
          privateTaskId,
          make(privateTaskId, tenant, uid, {
            title: "個人の書類を整理",
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
      match = /^\/v1\/tenants\/([^/]+)\/tasks(?:\/([^/]+))?$/.exec(url.pathname);
    if (!match) return;
    const tenant = match[1] ?? "",
      id = match[2],
      rows = records(tenant, uid),
      body = request.method === "GET" ? undefined : await request.clone().json();
    const key = request.headers.get("Idempotency-Key"),
      ifMatch = request.headers.get("If-Match"),
      receiptKey = `${tenant}:${uid}:${key}`;
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
    if (request.method === "GET") {
      if (id) {
        const original = rows.get(id);
        if (original && controls.scenario === "changed_selection") {
          rows.set(id, {
            ...original,
            status: original.status === "done" ? "open" : "cancelled",
            title: "他の端末で変更したタスク",
            due_at: "2026-11-02T00:00:00Z",
            version: original.version + 1,
          });
          controls.scenario = "normal";
        }
        const task = rows.get(id);
        return task
          ? Response.json(task, { headers: { ETag: `"${task.version}"` } })
          : Response.json({}, { status: 404 });
      }
      if (controls.scenario === "empty") {
        rows.clear();
        controls.scenario = "normal";
      }
      const status = url.searchParams.get("status");
      return Response.json({
        items: [...rows.values()].filter((task) => !status || task.status === status),
        next_cursor: null,
      });
    }
    const hash = JSON.stringify({ path: url.pathname, method: request.method, body, ifMatch }),
      receipt = receipts.get(receiptKey);
    if (receipt) {
      if (receipt.hash !== hash) return Response.json({}, { status: 409 });
      const task = rows.get(receipt.id);
      return task
        ? Response.json(task, {
            status: request.method === "POST" ? 201 : 200,
            headers: { ETag: `"${task.version}"` },
          })
        : Response.json({}, { status: 404 });
    }
    if (controls.scenario === "failed") return Response.json({}, { status: 503 });
    if (controls.scenario === "reject") return Response.json({}, { status: 422 });
    const original = id ? rows.get(id) : undefined;
    if (id && !original) return Response.json({}, { status: 404 });
    if (original && ["conflict", "conflict_status"].includes(controls.scenario)) {
      rows.set(original.id, {
        ...original,
        title: "別の端末で更新した買い物",
        ...(controls.scenario === "conflict_status" ? { status: "cancelled" as const } : {}),
        version: original.version + 1,
      });
      controls.scenario = "normal";
    }
    if (original && ifMatch !== `"${rows.get(original.id)?.version}"`)
      return Response.json({}, { status: 412 });
    if (
      original &&
      Object.keys(body).some((field) => !["title", "status", "due_at"].includes(field))
    )
      return Response.json({}, { status: 422 });
    const task = original
      ? { ...original, ...body, version: original.version + 1 }
      : make(crypto.randomUUID(), tenant, uid, body);
    if (task.due_at === null) delete task.due_at;
    if (task.due_at) task.due_at = new Date(task.due_at).toISOString();
    rows.set(task.id, task);
    receipts.set(receiptKey, { hash, id: task.id });
    if (controls.scenario === "lost_save") {
      controls.scenario = "normal";
      throw new Error("Synthetic response loss");
    }
    return Response.json(task, {
      status: original ? 200 : 201,
      headers: { ETag: `"${task.version}"` },
    });
  }
  return { controls, handle, records };
}
