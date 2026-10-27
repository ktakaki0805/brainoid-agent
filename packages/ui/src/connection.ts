import { type Client, getReadiness } from "@brainoid-internal/api-client";

/** Health is not authentication, tenant selection, or permission to perform an action. */
export async function checkConnection(api: Client, signal: AbortSignal): Promise<void> {
  const result = await getReadiness({ client: api, signal, throwOnError: true });
  if (result.data.status !== "ready") throw new Error("Unexpected readiness response");
}
