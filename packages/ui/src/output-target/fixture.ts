import type { PrivateSession } from "@brainoid-internal/api-client";
export const fixturePi = "60000000-0000-4000-8000-000000000001";
export function createPrivateSessionFixture() {
  const sessions = new Map<string, PrivateSession>();
  const receipts = new Map<string, { body: string; session: string }>();
  const controls = {
    scenario: "normal",
    calls: [] as { method: string; path: string; key: string | null; body: unknown }[],
  };
  const handle = async (request: Request, uid: string): Promise<Response | undefined> => {
    const path = new URL(request.url).pathname,
      parts = path.split("/"),
      tenant = parts[3];
    if (
      !tenant ||
      !(
        parts[4] === "private-sessions" ||
        (parts[4] === "devices" && parts[6] === "private-sessions")
      )
    )
      return undefined;
    const body = request.method === "POST" ? await request.json() : null;
    const key = request.headers.get("Idempotency-Key");
    controls.calls.push({ method: request.method, path, key, body });
    if (controls.scenario === "denied") return Response.json({}, { status: 403 });
    const normalize = (session: PrivateSession) => {
      if (
        session.status === "active" &&
        Date.now() >= Math.min(Date.parse(session.expires_at), Date.parse(session.idle_expires_at))
      ) {
        session.status = "expired";
        session.version++;
      }
      return session;
    };
    let session: PrivateSession | undefined;
    if (request.method === "POST") {
      if (
        !key ||
        !body.acknowledge_shared_space ||
        !body.allowed_scopes?.length ||
        parts[5] !== fixturePi
      )
        return Response.json({}, { status: 400 });
      const receiptKey = `${tenant}:${uid}:${key}`,
        receipt = receipts.get(receiptKey);
      if (receipt) {
        if (receipt.body !== JSON.stringify(body)) return Response.json({}, { status: 409 });
        session = sessions.get(receipt.session);
      } else {
        if (
          [...sessions.values()].some(
            (value) =>
              normalize(value).status === "active" &&
              value.tenant_id === tenant &&
              value.device_id === fixturePi,
          )
        )
          return Response.json({}, { status: 409 });
        const now = Date.now();
        session = {
          id: crypto.randomUUID(),
          tenant_id: tenant,
          user_id: uid,
          device_id: fixturePi,
          allowed_scopes: body.allowed_scopes,
          created_at: new Date(now).toISOString(),
          updated_at: new Date(now).toISOString(),
          expires_at: new Date(now + 300_000).toISOString(),
          idle_expires_at: new Date(now + 60_000).toISOString(),
          status: "active",
          version: 1,
        };
        sessions.set(session.id, session);
        receipts.set(receiptKey, { body: JSON.stringify(body), session: session.id });
      }
    } else session = sessions.get(parts[5] ?? "");
    if (!session || session.user_id !== uid || session.tenant_id !== tenant)
      return Response.json({}, { status: 404 });
    normalize(session);
    if (request.method === "DELETE") {
      if (session.status === "active") {
        if (request.headers.get("If-Match") !== `"${session.version}"`)
          return Response.json({}, { status: 412 });
        session.status = "ended";
        session.version++;
      }
      return new Response(null, { status: 204 });
    }
    if (controls.scenario === "lost") {
      controls.scenario = "normal";
      throw new Error("Synthetic response loss");
    }
    return Response.json(session, {
      status: request.method === "POST" ? 201 : 200,
      headers: { ETag: `"${session.version}"` },
    });
  };
  return { controls, sessions, handle };
}
