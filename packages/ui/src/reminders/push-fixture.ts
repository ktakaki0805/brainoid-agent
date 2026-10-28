import type { PushInstallation } from "@brainoid-internal/api-client";
/** Synthetic HTTP installation registry; production never imports the session fixture. */
export function createPushFixture() {
  const rows = new Map<string, { owner: string; value: PushInstallation }>();
  const receipts = new Map<string, { hash: string; id: string; status: number }>();
  const controls = { failRotation: false, failRemoval: false };
  const handle = async (request: Request, uid: string): Promise<Response | undefined> => {
    const path = new URL(request.url).pathname;
    const match = /^\/v1\/users\/me\/push-installations(?:\/([^/]+))?$/.exec(path);
    if (!match) return;
    if (
      (controls.failRotation && request.method === "PATCH") ||
      (controls.failRemoval && request.method === "DELETE")
    )
      throw new TypeError("Synthetic push network failure");
    const body = request.method === "DELETE" ? undefined : await request.json();
    const key = request.headers.get("Idempotency-Key");
    if (!key) return Response.json({}, { status: 400 });
    const hash = JSON.stringify([request.method, path, body, request.headers.get("If-Match")]);
    const receipt = receipts.get(`${uid}:${key}`),
      id = receipt?.id ?? match[1] ?? crypto.randomUUID();
    const previous = rows.get(id);
    if (receipt && receipt.hash !== hash) return Response.json({}, { status: 409 });
    if (receipt?.status === 204) return new Response(null, { status: 204 });
    if (match[1] && (!previous || previous.owner !== uid))
      return Response.json({}, { status: 404 });
    if (!receipt && match[1] && request.headers.get("If-Match") !== `"${previous?.value.version}"`)
      return Response.json({}, { status: 412 });
    const status =
      receipt?.status ??
      (request.method === "POST" ? 201 : request.method === "DELETE" ? 204 : 200);
    if (status === 204) {
      rows.delete(id);
      receipts.set(`${uid}:${key}`, { hash, id, status });
      return new Response(null, { status });
    }
    const value =
      receipt && previous
        ? previous.value
        : {
            id,
            platform: previous?.value.platform ?? body.platform,
            version: (previous?.value.version ?? 0) + 1,
            created_at: "2026-10-28T00:00:00Z",
          };
    rows.set(id, { owner: uid, value });
    receipts.set(`${uid}:${key}`, { hash, id, status });
    return Response.json(value, { status, headers: { ETag: `"${value.version}"` } });
  };
  return Object.assign(handle, { controls });
}
