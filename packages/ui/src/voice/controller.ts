import type { SessionContext } from "../session.ts";

export type VoicePhase =
  | "idle"
  | "permission"
  | "recording"
  | "ready"
  | "uploading"
  | "playing"
  | "closed";
export interface VoiceSnapshot {
  phase: VoicePhase;
  issue?: string | undefined;
}
export interface VoiceLease {
  start(): Promise<void>;
  finish(): Promise<void>;
  upload(): Promise<string | undefined>;
  enqueue(sentence: { contextKey: string; id: string; index: number; expiresAt: number }): void;
  close(): Promise<void>;
}
export interface VoiceContext {
  key: string;
  signal: AbortSignal;
  expiresAt: number;
  isCurrent(): boolean;
}
/** Applications supply hardware and transport. Shared UI never imports an app adapter. */
export type VoiceFactory = (
  context: SessionContext,
  binding: VoiceContext,
  changed: (state: VoiceSnapshot) => void,
) => VoiceLease;
export interface VoiceState extends VoiceSnapshot {
  available: boolean;
  muted: boolean | null;
  offline: boolean;
  notice: string;
}
export const voiceIssue = (issue: string) => {
  if (issue === "permission" || issue === "microphone_muted")
    return "マイクを使用できません。文字で入力できます。端末の設定でマイクを許可し、ミュートを解除して再度録音してください。";
  if (issue === "invalid-audio" || issue === "audio_silent")
    return "音声を認識できませんでした。文字で入力するか、マイクを確認して録音し直してください。";
  return "音声を利用できません。文字で入力できます。接続と音声設定を確認して、もう一度操作してください。";
};
export const initialVoice = (available = false): VoiceState => ({
  available,
  muted: false,
  offline: false,
  phase: "idle",
  notice: available
    ? ""
    : "音声は無効です。文字で入力できます。音声設定は端末の管理者に確認してください。",
});

export class VoiceController {
  state: VoiceState;
  generation = 0;
  private lease: VoiceLease | undefined;
  private abort: AbortController | undefined;
  private binding: VoiceContext | undefined;
  private run: string | undefined;
  private streamed = false;
  private context: SessionContext;
  private factory: VoiceFactory | undefined;
  private changed: (state: VoiceState) => void;
  constructor(
    context: SessionContext,
    factory: VoiceFactory | undefined,
    changed: (state: VoiceState) => void,
  ) {
    this.context = context;
    this.factory = factory;
    this.changed = changed;
    this.state = initialVoice(Boolean(factory));
  }
  private set(patch: Partial<VoiceState>) {
    this.state = { ...this.state, ...patch };
    this.changed(this.state);
  }
  stop(notice = "") {
    this.generation++;
    const lease = this.lease;
    this.lease = undefined;
    this.binding = undefined;
    this.run = undefined;
    this.streamed = false;
    this.abort?.abort();
    this.abort = undefined;
    const generation = this.generation;
    void lease?.close().catch(() => {
      if (generation === this.generation && !this.context.signal.aborted)
        this.set({ notice: "音声停止を確認できません。端末の音声設定を確認してください。" });
    });
    this.set({
      phase: "idle",
      issue: undefined,
      notice: notice || (this.factory ? "" : initialVoice().notice),
    });
  }
  mute() {
    const muted = !this.state.muted;
    this.stop();
    this.set({ muted });
  }
  network(offline: boolean) {
    if (offline) this.stop("オフラインです。接続を確認してください。文字は入力しておけます。");
    this.set({
      offline,
      ...(!offline && this.state.offline
        ? { notice: "接続が戻りました。録音は明示操作で開始してください。" }
        : {}),
    });
  }
  async start(expiresAt: number) {
    if (!this.factory || this.state.muted || this.state.offline) return;
    this.stop();
    const generation = this.generation;
    this.abort = new AbortController();
    const binding: VoiceContext = {
      key: `${this.context.generation}:${generation}:${crypto.randomUUID()}`,
      expiresAt,
      signal: AbortSignal.any([this.context.signal, this.abort.signal]),
      isCurrent: () => generation === this.generation && !this.context.signal.aborted,
    };
    this.binding = binding;
    try {
      this.lease = this.factory(this.context, binding, (value) => {
        if (generation !== this.generation || binding.signal.aborted) return;
        this.set({ ...value, notice: value.issue ? voiceIssue(value.issue) : "" });
      });
      await this.lease.start();
    } catch {
      if (generation === this.generation) {
        this.stop();
        this.set({ phase: "closed", issue: "unavailable", notice: voiceIssue("unavailable") });
      }
    }
  }
  finish() {
    return this.lease?.finish();
  }
  async upload() {
    const generation = this.generation;
    const id = await this.lease?.upload();
    return id && this.current(generation) ? { id, generation } : undefined;
  }
  current(generation: number) {
    return (
      generation === this.generation &&
      Boolean(this.lease) &&
      Boolean(this.binding?.isCurrent()) &&
      !this.binding?.signal.aborted &&
      Date.now() < (this.binding?.expiresAt ?? 0)
    );
  }
  bindRun(run: string, generation: number) {
    if (this.current(generation)) this.run = run;
  }
  /** Exactly one SSE connection may carry an explicit audio intention. */
  stream(run: string) {
    if (this.run !== run || this.streamed) return undefined;
    this.streamed = true;
    return this.generation;
  }
  sentence(
    run: string,
    generation: number | undefined,
    id: string,
    index: number,
    expiresAt: number,
  ) {
    if (generation === undefined || this.run !== run || !this.current(generation) || !this.binding)
      return;
    this.lease?.enqueue({ contextKey: this.binding.key, id, index, expiresAt });
  }
}

export type ConversationPhase =
  | "Idle"
  | "Listening"
  | "Transcribing"
  | "Thinking"
  | "Confirming"
  | "Speaking"
  | "Error";
export function conversationPhase(
  voice: VoiceSnapshot,
  run?: string,
  busy = false,
): ConversationPhase {
  if (voice.issue || run === "failed") return "Error";
  if (voice.phase === "recording" || voice.phase === "permission") return "Listening";
  if (voice.phase === "uploading") return "Transcribing";
  if (voice.phase === "playing") return "Speaking";
  if (run === "waiting_confirmation") return "Confirming";
  if (busy || run === "running" || run === "queued") return "Thinking";
  return "Idle";
}
