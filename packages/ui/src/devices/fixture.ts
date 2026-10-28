import type { Device, DevicePatch, PairingConfirm } from "@brainoid-internal/api-client";
export const deviceId = "d1000000-0000-4000-8000-000000000001";
export type DeviceScenario =
  | "normal"
  | "empty"
  | "failed"
  | "lost"
  | "expired"
  | "reused"
  | "cross_tenant"
  | "denied"
  | "conflict";
export function fixtureDevice(tenant: string): Device {
  return {
    id: deviceId,
    tenant_id: tenant,
    name: "リビングのPi",
    status: "active",
    capabilities: ["display"],
    allowed_scopes: ["shared", "conversation"],
    allowed_entity_ids: [],
    allowed_room_ids: [],
    version: 1,
    authorization_version: 1,
    connection_generation: 1,
    created_at: "2026-10-28T00:00:00Z",
    updated_at: "2026-10-28T00:00:00Z",
  };
}
/** Explicit fake entry only; no secrets or device credential endpoints exist here. */
export function createDevicesFixture() {
  const controls = {
    scenario: "normal" as DeviceScenario,
    role: "owner" as "owner" | "admin" | "member",
    calls: [] as {
      method: string;
      path: string;
      key: string | null;
      match: string | null;
      body: unknown;
    }[],
  };
  const rows = new Map<string, Device>(),
    receipts = new Map<string, { binding: string; value: Device | null }>(),
    used = new Set<string>();
  const record = (tenant: string) => {
    if (!rows.has(`${tenant}:${deviceId}`))
      rows.set(`${tenant}:${deviceId}`, fixtureDevice(tenant));
  };
  const handle = async (request: Request): Promise<Response | undefined> => {
    const url = new URL(request.url),
      parts = url.pathname.split("/"),
      tenant = parts[3],
      resource = parts[4],
      id = parts[5];
    if (!tenant || !["devices", "device-pairings:confirm"].includes(resource ?? "")) return;
    const body = ["POST", "PATCH"].includes(request.method) ? await request.json() : undefined;
    const key = request.headers.get("Idempotency-Key"),
      match = request.headers.get("If-Match");
    controls.calls.push({ method: request.method, path: url.pathname, key, match, body });
    if (controls.role === "member" || controls.scenario === "denied")
      return Response.json({}, { status: 403 });
    if (controls.scenario === "failed") return Response.json({}, { status: 503 });
    record(tenant);
    const found = id ? rows.get(`${tenant}:${id}`) : undefined;
    const response = (value: Device | null) =>
      value
        ? Response.json(value, {
            status: resource === "device-pairings:confirm" ? 201 : 200,
            headers: resource === "devices" ? { ETag: `"${value.version}"` } : {},
          })
        : new Response(null, { status: 204 });
    if (request.method === "GET")
      return id
        ? found
          ? response(found)
          : Response.json({}, { status: 404 })
        : Response.json({
            items:
              controls.scenario === "empty"
                ? []
                : [...rows.values()].filter((v) => v.tenant_id === tenant),
            next_cursor: null,
          });
    const binding = JSON.stringify([request.method, url.pathname, match, body]),
      receiptKey = `${tenant}:${key}`,
      receipt = receipts.get(receiptKey);
    if (receipt)
      return receipt.binding === binding
        ? response(receipt.value)
        : Response.json({}, { status: 409 });
    if (controls.scenario === "conflict" && found) {
      controls.scenario = "normal";
      rows.set(`${tenant}:${id}`, {
        ...found,
        name: "別の管理者が更新したPi",
        version: found.version + 1,
      });
      return Response.json({}, { status: 412 });
    }
    let value: Device | null;
    if (resource === "device-pairings:confirm") {
      const input = body as PairingConfirm;
      if (
        ["expired", "reused", "cross_tenant"].includes(controls.scenario) ||
        used.has(input.user_code)
      )
        return Response.json({}, { status: 409 });
      if (
        input.replaces_device_id &&
        rows.get(`${tenant}:${input.replaces_device_id}`)?.status !== "revoked"
      )
        return Response.json({}, { status: 409 });
      value = {
        ...fixtureDevice(tenant),
        id: crypto.randomUUID(),
        name: input.device_name,
        allowed_entity_ids: input.allowed_entity_ids,
      };
      used.add(input.user_code);
    } else {
      if (!found) return Response.json({}, { status: 404 });
      if (match !== `"${found.version}"`) return Response.json({}, { status: 412 });
      value = {
        ...found,
        ...(request.method === "PATCH" ? (body as DevicePatch) : { status: "revoked" as const }),
        version: found.version + 1,
      };
      const changedAuthorization =
        request.method === "DELETE" ||
        (["allowed_scopes", "allowed_entity_ids", "allowed_room_ids"] as const).some(
          (field) =>
            JSON.stringify([...(value?.[field] ?? [])].sort()) !==
            JSON.stringify([...(found[field] ?? [])].sort()),
        );
      if (changedAuthorization) {
        value.authorization_version = (found.authorization_version ?? 1) + 1;
        value.connection_generation = found.connection_generation + 1;
      }
      rows.set(`${tenant}:${id}`, value);
      if (request.method === "DELETE") value = null;
    }
    if (value) rows.set(`${tenant}:${value.id}`, value);
    receipts.set(receiptKey, { binding, value: structuredClone(value) });
    if (controls.scenario === "lost") {
      controls.scenario = "normal";
      throw new Error("Synthetic response loss");
    }
    return response(value);
  };
  return { controls, rows, handle };
}
