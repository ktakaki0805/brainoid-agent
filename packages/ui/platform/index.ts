import type { Client } from "@brainoid-internal/api-client";

/** Presentation capabilities only; these never grant authorization or OS permission.
 * Credential operations use AuthenticationAdapter; notification and recording have later adapters.
 */
export interface ClientPlatformAdapter {
  readonly surface: "mobile" | "display";
  readonly voice: { readonly available: false; readonly reason: string };
}

export interface UiDependencies {
  readonly api: Client;
  readonly platform: ClientPlatformAdapter;
}

export function createUnavailablePlatform(
  surface: ClientPlatformAdapter["surface"],
): ClientPlatformAdapter {
  return { surface, voice: { available: false, reason: "音声はまだ利用できません" } };
}
