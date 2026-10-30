import type { VoiceFactory } from "@brainoid-internal/ui/voice";
import { createNativeAudio } from "./audio-native.ts";
import { sessionAudioTransport } from "./audio-transport.ts";

/** Explicit deployment gate; a browser or missing native plugin stays text-capable. */
export function nativeVoice(enabled: unknown): VoiceFactory | undefined {
  if (enabled !== "true") return undefined;
  try {
    const adapter = createNativeAudio();
    return (session, context, changed) =>
      adapter.open(context, sessionAudioTransport(session), changed);
  } catch {
    return undefined;
  }
}
