import type { Membership, Tenant, Usage } from "@brainoid-internal/api-client";
export function createTenantSettingsFixture(authority: { role: Tenant["my_role"] }) {
  const members = new Map<string, Membership>();
  const receipts = new Map<string, { status: number; value?: unknown }>();
  const controls = {
    scenario: "normal" as "normal" | "lost" | "conflict" | "denied" | "recent" | "rate" | "failed",
    quota: "available" as Usage["quota_state"],
    calls: [] as {
      method: string;
      path: string;
      uid: string | null;
      key: string | null;
      match: string | null;
      body: unknown;
    }[],
    members,
  };
  function response(value: { status: number; value?: unknown }) {
    return value.status === 204
      ? new Response(null, { status: 204 })
      : Response.json(value.value ?? {}, { status: value.status });
  }
  const handle = async (request: Request, uid: string): Promise<Response | undefined> => {
    const url = new URL(request.url),
      path = url.pathname;
    if (
      !/\/memberships(?::transfer-owner)?$|\/usage$|\/invitations$|\/invitations:accept$/.test(path)
    )
      return;
    const targetId = url.searchParams.get("user_id");
    const body =
      request.method === "GET" || request.method === "DELETE" ? undefined : await request.json();
    const key = request.headers.get("Idempotency-Key");
    controls.calls.push({
      method: request.method,
      path,
      uid: targetId,
      key,
      match: request.headers.get("If-Match"),
      body,
    });
    if (!members.has(uid)) {
      members.set(uid, { user_id: uid, role: authority.role, status: "active", version: 7 });
      members.set("u?/#+", { user_id: "u?/#+", role: "member", status: "active", version: 3 });
      members.set("..", { user_id: "..", role: "admin", status: "active", version: 4 });
    }
    if (controls.scenario === "failed") throw new Error("Synthetic transport unavailable");
    if (controls.scenario === "rate") return Response.json({}, { status: 429 });
    if (controls.scenario === "denied") return Response.json({}, { status: 403 });
    if (path.endsWith("/usage"))
      return Response.json({
        period: "2026-10",
        turns: authority.role === "member" ? 2 : 12,
        voice_seconds: 4,
        remaining_turns: controls.quota === "exhausted" ? 0 : 88,
        quota_state: controls.quota,
      });
    if (request.method === "GET") {
      if (authority.role === "member") return Response.json({}, { status: 403 });
      return Response.json({
        items: [...members.values()].map((v) =>
          v.user_id === uid ? { ...v, role: authority.role } : v,
        ),
        next_cursor: null,
      });
    }
    if (
      controls.scenario === "recent" &&
      (request.method === "DELETE" || path.endsWith(":transfer-owner"))
    )
      return Response.json({}, { status: 403 });
    if (controls.scenario === "conflict") return Response.json({}, { status: 412 });
    if (key && receipts.has(key))
      return response(receipts.get(key) as { status: number; value?: unknown });
    let result: { status: number; value?: unknown };
    if (path.endsWith("invitations:accept"))
      result = {
        status: 201,
        value: { user_id: uid, role: "member", status: "active", version: 1 },
      };
    else {
      if (authority.role === "member") return Response.json({}, { status: 403 });
      if (path.endsWith("/invitations"))
        result = {
          status: 201,
          value: {
            id: "10000000-0000-4000-8000-000000000099",
            role: body.role,
            code: "a".repeat(64),
            expires_at: "2027-01-01T00:00:00Z",
          },
        };
      else {
        const transfer = path.endsWith(":transfer-owner"),
          target = members.get(transfer ? body.target_user_id : (targetId ?? ""));
        if (!target) return Response.json({}, { status: 404 });
        if (
          authority.role === "admin" &&
          (transfer || target.user_id === uid || target.role === "owner" || body?.role === "owner")
        )
          return Response.json({}, { status: 403 });
        const expected = transfer ? members.get(uid)?.version : target.version;
        if (
          request.headers.get("If-Match") !== `"${expected}"` ||
          (transfer && body.target_version !== target.version)
        )
          return Response.json({}, { status: 412 });
        const updated: Membership = {
          ...target,
          version: target.version + 1,
          role: transfer ? "owner" : (body?.role ?? target.role),
          status: request.method === "DELETE" ? "revoked" : "active",
        };
        members.set(target.user_id, updated);
        if (transfer) {
          const actor = members.get(uid) as Membership;
          members.set(uid, { ...actor, role: "member", version: actor.version + 1 });
          authority.role = "member";
        } else if (target.user_id === uid) authority.role = updated.role;
        result = request.method === "DELETE" ? { status: 204 } : { status: 200, value: updated };
      }
    }
    if (key) receipts.set(key, result);
    if (controls.scenario === "lost") {
      controls.scenario = "normal";
      throw new Error("Synthetic response lost after commit");
    }
    return response(result);
  };
  return { controls, handle };
}
