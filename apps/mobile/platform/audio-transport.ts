import { getSpeechAudio, transcribeAudio } from "@brainoid-internal/api-client";
import type { SessionContext } from "../../../packages/ui/src/session.ts";
import type { AudioTransport } from "./audio.ts";
import { MAX_WAV_BYTES, validateWav } from "./pcm.ts";

/** Auth and tenant binding come from the existing session; no raw-audio cache or retry queue. */
export function sessionAudioTransport(context: SessionContext): AudioTransport {
  if (!context.tenantId) throw new Error("Tenant required");
  const tenantId = context.tenantId;
  return {
    upload: async (wav, signal) => {
      validateWav(wav);
      return context.run(async (client, ownerSignal) => {
        const result = await transcribeAudio({
          client,
          path: { tenant_id: tenantId },
          body: new Blob([wav], { type: "audio/wav" }),
          headers: { "Idempotency-Key": crypto.randomUUID() },
          signal: AbortSignal.any([signal, ownerSignal, AbortSignal.timeout(60_000)]),
        });
        if (
          result.response?.status !== 201 ||
          !result.data ||
          !Number.isFinite(Date.parse(result.data.expires_at)) ||
          Date.parse(result.data.expires_at) <= Date.now()
        )
          throw new Error("Audio upload failed");
        return result.data.audio_input_id;
      });
    },
    speech: (id, signal) =>
      context.run(async (client, ownerSignal) => {
        const boundedSignal = AbortSignal.any([signal, ownerSignal, AbortSignal.timeout(60_000)]);
        const result = await getSpeechAudio({
          client,
          path: { tenant_id: tenantId, speech_id: id },
          signal: boundedSignal,
          parseAs: "stream",
        });
        const response = result.response;
        if (
          !response?.ok ||
          response.headers.get("content-type")?.split(";")[0] !== "audio/wav" ||
          !response.headers
            .get("cache-control")
            ?.split(",")
            .some((value) => value.trim() === "no-store") ||
          !response.body
        ) {
          await response?.body?.cancel();
          throw new Error("Audio fetch failed");
        }
        const reader = response.body.getReader(),
          buffer = new Uint8Array(MAX_WAV_BYTES);
        let size = 0;
        try {
          while (true) {
            boundedSignal.throwIfAborted();
            context.assertCurrent();
            const chunk = await reader.read();
            boundedSignal.throwIfAborted();
            context.assertCurrent();
            if (chunk.done) break;
            if (size + chunk.value.length > MAX_WAV_BYTES) throw new Error("Audio too large");
            buffer.set(chunk.value, size);
            size += chunk.value.length;
          }
          const bytes = buffer.slice(0, size);
          try {
            validateWav(bytes);
            return bytes;
          } catch (error) {
            bytes.fill(0);
            throw error;
          }
        } finally {
          buffer.fill(0);
          await reader.cancel().catch(() => {});
          reader.releaseLock();
        }
      }),
  };
}
