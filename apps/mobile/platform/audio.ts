import { canonicalWav, fromBase64, toBase64, validateWav } from "./pcm.ts";

export type AudioIssue = "permission" | "unavailable" | "interrupted" | "invalid-audio" | "network";
export const microphoneDeniedMessage =
  "マイクを使用できません。文字で入力できます。録音するには端末の設定でこのアプリのマイクを許可し、もう一度録音をタップしてください。";
export interface AudioEvent {
  generation: number;
  reason: "limit" | "background" | "interrupted" | "error";
}
export interface NativeAudioPlugin {
  activate(options: { generation: number; expiresAt: number }): Promise<void>;
  permission(options: { generation: number }): Promise<{ granted: boolean }>;
  start(options: { generation: number }): Promise<void>;
  finish(options: { generation: number }): Promise<{ wav: string }>;
  play(options: { generation: number; wav: string; expiresAt: number }): Promise<void>;
  stop(options: { generation: number }): Promise<void>;
  addListener(
    name: "audioStopped",
    listener: (event: AudioEvent) => void,
  ): Promise<{ remove(): Promise<void> }>;
}
export interface AudioContext {
  /** New identity for each tenant / actor / session / run output binding. */
  key: string;
  signal: AbortSignal;
  expiresAt: number;
  isCurrent(): boolean;
}
export interface SpeechSentence {
  contextKey: string;
  id: string;
  index: number;
  expiresAt: number;
}
export interface AudioTransport {
  /** Explicitly authorized current-context HTTP calls; never retry or persist raw audio. */
  upload(wav: Uint8Array<ArrayBuffer>, signal: AbortSignal): Promise<string>;
  speech(id: string, signal: AbortSignal): Promise<Uint8Array<ArrayBuffer>>;
}
export interface AudioSnapshot {
  phase: "idle" | "permission" | "recording" | "ready" | "uploading" | "playing" | "closed";
  issue?: AudioIssue;
  textInputAvailable: true;
}
let generationClock = 0;
/** Generation fences are enforced again in native code, even if bridge calls arrive late. */
export function mobileAudio(plugin: NativeAudioPlugin, now = Date.now) {
  let current: { close(): Promise<void> } | undefined;
  return {
    open(
      context: AudioContext,
      transport: AudioTransport,
      changed: (state: AudioSnapshot) => void = () => {},
    ) {
      void current?.close();
      generationClock = Math.max(generationClock + 1, Date.now() * 1024);
      const generation = generationClock;
      const abort = new AbortController(),
        pending = new Map<number, SpeechSentence>();
      const seen = new Map<number, string>();
      let closed = false,
        recorded = false,
        wav: Uint8Array<ArrayBuffer> | undefined;
      let phase: AudioSnapshot["phase"] = "idle",
        next = 0,
        pumping = false;
      let finishing: Promise<void> | undefined;
      let closing: Promise<void> | undefined;
      const set = (value: AudioSnapshot["phase"], issue?: AudioIssue) => {
        phase = value;
        changed({ phase, textInputAvailable: true, ...(issue ? { issue } : {}) });
      };
      const valid = () =>
        !closed && !context.signal.aborted && context.isCurrent() && now() < context.expiresAt;
      const assert = () => {
        if (!valid()) throw new Error("Audio context ended");
      };
      const clear = () => {
        wav?.fill(0);
        wav = undefined;
        pending.clear();
        seen.clear();
      };
      const close = (): Promise<void> => {
        if (closing) return closing;
        closed = true;
        context.signal.removeEventListener("abort", onAbort);
        abort.abort();
        clear();
        clearInterval(expiry);
        set("closed");
        // Do not wait on a permission dialog, audio fetch or play completion before native stop.
        closing = plugin.stop({ generation });
        void closing.catch(() => {});
        void listener.then((handle) => handle.remove()).catch(() => {});
        return closing;
      };
      const onAbort = () => {
        void close();
      };
      const fail = (issue: AudioIssue) => {
        void close();
        set("closed", issue);
      };
      const finish = (): Promise<void> => {
        if (finishing) return finishing;
        if (!valid() || phase !== "recording") return Promise.resolve();
        finishing = (async () => {
          try {
            const result = await plugin.finish({ generation });
            assert();
            const bytes = fromBase64(result.wav);
            try {
              validateWav(bytes);
              assert();
              wav = bytes;
              set("ready");
            } catch (error) {
              bytes.fill(0);
              throw error;
            }
          } catch {
            if (!closed) fail("invalid-audio");
          }
        })();
        return finishing;
      };
      const listener = plugin.addListener("audioStopped", (event) => {
        if (event.generation !== generation || closed) return;
        if (event.reason === "limit") void finish();
        else fail(event.reason === "error" ? "unavailable" : "interrupted");
      });
      const ready = listener.then(async () => {
        assert();
        await plugin.activate({ generation, expiresAt: context.expiresAt });
        assert();
      });
      void ready.catch(() => {
        if (!closed) fail("unavailable");
      });
      const expiry = setInterval(() => {
        if (!valid()) void close();
      }, 100);
      const pump = async () => {
        if (pumping || closed) return;
        pumping = true;
        try {
          await ready;
          while (valid()) {
            const sentence = pending.get(next);
            if (!sentence) break;
            pending.delete(next);
            if (now() >= sentence.expiresAt) throw new Error("Expired speech");
            const bytes = await transport.speech(sentence.id, abort.signal);
            try {
              assert();
              if (now() >= sentence.expiresAt) throw new Error("Expired speech");
              const canonical = canonicalWav(bytes);
              try {
                set("playing");
                await plugin.play({
                  generation,
                  wav: toBase64(canonical),
                  expiresAt: Math.min(context.expiresAt, sentence.expiresAt),
                });
              } finally {
                canonical.fill(0);
              }
              assert();
              next++;
            } finally {
              bytes.fill(0);
            }
          }
          if (valid()) set("idle");
        } catch {
          if (!closed) fail("network");
        } finally {
          pumping = false;
        }
      };
      const lease = {
        /** Call only from an explicit record tap. Each lease records at most one utterance. */
        async start() {
          if (phase !== "idle" || recorded || pumping || seen.size) return;
          recorded = true;
          set("permission");
          try {
            await ready;
            assert();
            const permission = await plugin.permission({ generation });
            assert();
            if (!permission.granted) {
              fail("permission");
              return;
            }
            set("recording");
            await plugin.start({ generation });
            assert();
          } catch {
            if (!closed) fail("unavailable");
          }
        },
        finish,
        async upload(): Promise<string | undefined> {
          if (!valid() || phase !== "ready" || !wav) return undefined;
          const bytes = wav;
          wav = undefined;
          set("uploading");
          try {
            const id = await transport.upload(bytes, abort.signal);
            assert();
            set("idle");
            return id;
          } catch {
            if (!closed) fail("network");
            return undefined;
          } finally {
            bytes.fill(0);
          }
        },
        enqueue(sentence: SpeechSentence) {
          if (!valid() || !["idle", "playing"].includes(phase)) return;
          if (
            sentence.contextKey !== context.key ||
            !Number.isInteger(sentence.index) ||
            sentence.index < 0 ||
            sentence.index >= 64 ||
            !sentence.id ||
            !Number.isFinite(sentence.expiresAt) ||
            sentence.expiresAt > now() + 300_000
          )
            return;
          const previous = seen.get(sentence.index);
          if (previous) {
            if (previous !== sentence.id) fail("invalid-audio");
            return;
          }
          seen.set(sentence.index, sentence.id);
          pending.set(sentence.index, { ...sentence });
          void pump();
        },
        /** Discard, stop button, background, logout, session end and run cancellation. */
        close,
      };
      current = lease;
      context.signal.addEventListener("abort", onAbort, { once: true });
      if (!Number.isFinite(context.expiresAt) || !context.key || context.signal.aborted)
        void close();
      return lease;
    },
  };
}
