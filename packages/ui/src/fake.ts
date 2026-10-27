import { createBrainoidClient } from "@brainoid-internal/api-client";
import { type ClientPlatformAdapter, createUnavailablePlatform } from "../platform/index.ts";

export type FakeScenario = "empty" | "loading" | "error";

/** Explicit local-only composition. No network fallback and no authenticated operations. */
export function createFakeDependencies(
  surface: ClientPlatformAdapter["surface"],
  scenario: FakeScenario = "empty",
) {
  const calls: string[] = [];
  const transport: typeof fetch = async (input, init) => {
    const request = new Request(input, init);
    if (request.signal.aborted) throw new DOMException("Aborted", "AbortError");
    if (request.url !== "https://ui-fixture.invalid/health/ready" || request.method !== "GET") {
      throw new Error("UI fixture supports only the public readiness request");
    }
    calls.push(`${request.method} ${new URL(request.url).pathname}`);
    if (scenario === "loading") {
      return new Promise<Response>((_resolve, reject) => {
        request.signal.addEventListener(
          "abort",
          () => reject(new DOMException("Aborted", "AbortError")),
          { once: true },
        );
      });
    }
    if (scenario === "error") throw new TypeError("Synthetic connection failure");
    return Response.json({ status: "ready" });
  };
  return {
    api: createBrainoidClient({ baseUrl: "https://ui-fixture.invalid", fetch: transport }),
    platform: createUnavailablePlatform(surface),
    calls,
  };
}
