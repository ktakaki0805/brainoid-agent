import { createMemoryFixture } from "./memories/fixture.ts";
import { createChatFixture } from "./chat/fixture.ts";
import type { Tenant } from "@brainoid-internal/api-client";
import type { AuthenticationAdapter, AuthIdentity } from "../platform/auth.ts";

export const tenantA = "10000000-0000-4000-8000-000000000001";
export const tenantB = "10000000-0000-4000-8000-000000000002";
export function fixtureTenant(id: string, name: string): Tenant {
  return {
    id,
    tenant_id: id,
    name,
    timezone: "Asia/Tokyo",
    my_role: "owner",
    status: "active",
    version: 1,
    created_at: "2026-10-28T00:00:00Z",
    updated_at: "2026-10-28T00:00:00Z",
  };
}
export function createSessionFixture() {
  const chat = createChatFixture();
  const memories = createMemoryFixture();
  let user: AuthIdentity | null = null;
  const listeners = new Set<(user: AuthIdentity | null) => void>();
  const emit = (next: AuthIdentity | null) => {
    user = next;
    for (const listener of listeners) listener(user);
  };
  const auth: AuthenticationAdapter = {
    storage: "fake",
    current: async () => user,
    subscribe: (listener) => {
      listeners.add(listener);
      queueMicrotask(() => listener(user));
      return () => {
        listeners.delete(listener);
      };
    },
    signIn: async (email) => emit({ uid: email }),
    signOut: async () => emit(null),
    token: async (uid) => {
      if (user?.uid !== uid) throw new Error("Identity changed");
      return "fixture-token";
    },
  };
  const tenants = [fixtureTenant(tenantA, "テナント A"), fixtureTenant(tenantB, "テナント B")];
  const receipts = new Map<string, Tenant>();
  const controls = { delayA: false, status: 200, calls: [] as string[], refreshes: 0 };
  const originalToken = auth.token;
  auth.token = async (uid, force) => {
    if (force) controls.refreshes++;
    return originalToken(uid, force);
  };
  const transport: typeof fetch = async (input, init) => {
    const request = new Request(input, init);
    const url = new URL(request.url);
    if (url.origin !== "https://session.invalid")
      throw new Error("Fixture refuses external transport");
    controls.calls.push(`${request.method} ${url.pathname}`);
    if (!user) return Response.json({}, { status: 401 });
    const memoryResponse = await memories.handle(request, user.uid);
    if (memoryResponse) return memoryResponse;
    const chatResponse = await chat.handle(request, user.uid);
    if (chatResponse) return chatResponse;
    if (url.pathname === "/v1/tenants") {
      if (request.method === "GET") return Response.json({ items: tenants, next_cursor: null });
      if (request.method === "POST") {
        const key = request.headers.get("Idempotency-Key") ?? "";
        const body = await request.json();
        let tenant = receipts.get(key);
        if (!tenant) {
          tenant = fixtureTenant(crypto.randomUUID(), body.name);
          tenants.push(tenant);
          receipts.set(key, tenant);
        }
        return Response.json(tenant, { status: 201 });
      }
    }
    const id = url.pathname.split("/").at(-1);
    const status = controls.status;
    if (controls.delayA && id === tenantA)
      await new Promise((resolve) => setTimeout(resolve, 1400));
    // Deliberately ignore abort: the session boundary must reject stale responses itself.
    if (status !== 200) return Response.json({}, { status });
    const tenant = tenants.find((value) => value.id === id);
    return tenant ? Response.json(tenant) : Response.json({}, { status: 404 });
  };
  return { auth, transport, controls, emit, chat, memories };
}
