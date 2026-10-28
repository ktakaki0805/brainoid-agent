/** Recovery and conversation output share an in-flight cookie bootstrap. Starting
 * two initial local sessions concurrently can pair one cookie with the other's CSRF. */
const pending = new WeakMap<typeof fetch, Promise<{ csrf_token: string }>>();
export function bootstrapLocalSession(signal: AbortSignal, transport: typeof fetch = fetch) {
  const existing = pending.get(transport);
  if (existing) return existing;
  const request = (async () => {
    const response = await transport("/local/session", {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
      headers: { "X-Brainoid-Local": "1" },
      signal,
    });
    if (!response.ok) throw new Error("Local session unavailable");
    const value = await response.json();
    if (typeof value.csrf_token !== "string" || !value.csrf_token)
      throw new Error("Invalid local session");
    return { csrf_token: value.csrf_token as string };
  })().finally(() => {
    pending.delete(transport);
  });
  pending.set(transport, request);
  return request;
}
