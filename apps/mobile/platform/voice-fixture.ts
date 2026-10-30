import type { VoiceFactory } from "@brainoid-internal/ui/voice";
import { mobileAudio, type AudioEvent } from "./audio.ts";
import { encodeWav, toBase64 } from "./pcm.ts";

/** Only imported by the explicit fake entry. No microphone, account or paid provider. */
export function syntheticVoice() {
  const controls = { scenario: "normal" };
  const listeners = new Set<(value: AudioEvent) => void>();
  let finishPlayback = () => {};
  const adapter = mobileAudio({
    activate: async () => {},
    permission: async () => ({ granted: controls.scenario !== "permission" }),
    start: async () => {},
    finish: async () => ({
      wav: toBase64(
        encodeWav(
          controls.scenario === "silent" ? new Int16Array(100) : Int16Array.of(1000, -1000, 2000),
        ),
      ),
    }),
    play: async () =>
      new Promise<void>((resolve) => {
        const timer = setTimeout(resolve, 2000);
        finishPlayback = () => {
          clearTimeout(timer);
          resolve();
        };
      }),
    stop: async () => {
      finishPlayback();
    },
    addListener: async (_name, listener) => {
      listeners.add(listener);
      return {
        remove: async () => {
          listeners.delete(listener);
        },
      };
    },
  });
  const factory: VoiceFactory = (_session, binding, changed) =>
    adapter.open(
      binding,
      {
        upload: async () => {
          await new Promise((resolve) => setTimeout(resolve, 350));
          if (controls.scenario === "network") throw new Error("synthetic");
          return crypto.randomUUID();
        },
        speech: async () => {
          if (controls.scenario === "network") throw new Error("synthetic");
          return encodeWav(Int16Array.of(1000, -1000, 2000));
        },
      },
      changed,
    );
  return { factory, controls };
}
