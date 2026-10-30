import { initialVoice, voiceIssue, type VoiceState } from "@brainoid-internal/ui/voice";
import type { LocalSessionClient } from "../session/client.ts";

export interface DeviceVoiceState extends VoiceState {
  devices: { id: string; direction: "input" | "output" }[];
  input: string;
  output: string;
  waiting: boolean;
}
export const initialDeviceVoice = (): DeviceVoiceState => ({
  ...initialVoice(),
  muted: null,
  devices: [],
  input: "",
  output: "",
  waiting: false,
});
export class DeviceVoice {
  state = initialDeviceVoice();
  generation = 0;
  private abort = new AbortController();
  private closing: Promise<void> = Promise.resolve();
  private closed = false;
  private waitingGeneration: number | undefined;
  private waitRecovery = false;
  private noticeOwner: "wait-preparing" | "wait-ready" | undefined;
  private client: LocalSessionClient;
  private changed: (state: DeviceVoiceState) => void;
  constructor(client: LocalSessionClient, changed: (state: DeviceVoiceState) => void) {
    this.client = client;
    this.changed = changed;
  }
  private set(patch: Partial<DeviceVoiceState>) {
    if (patch.notice !== undefined) this.noticeOwner = undefined;
    this.state = { ...this.state, ...patch };
    this.changed(this.state);
  }
  capture() {
    return { generation: this.generation, signal: this.abort.signal };
  }
  retireWait() {
    this.waitingGeneration = undefined;
    const clearNotice = this.noticeOwner || this.waitRecovery;
    this.waitRecovery = false;
    this.set({ waiting: false, ...(clearNotice ? { notice: "" } : {}) });
  }
  waitStatus(generation: number, notice: string) {
    if (this.valid(generation) && this.waitingGeneration === generation && this.state.waiting)
      this.waitNotice("wait-ready", notice);
  }
  private advance() {
    this.waitingGeneration = undefined;
    this.waitRecovery = false;
    this.abort.abort();
    this.abort = new AbortController();
    return ++this.generation;
  }
  private waitNotice(owner: "wait-preparing" | "wait-ready", notice: string) {
    this.set({ notice });
    this.noticeOwner = owner;
  }
  private valid(epoch: number) {
    return !this.closed && epoch === this.generation;
  }
  async stop(notice = "読み上げを停止しました。生成と家電・予定の操作は継続する場合があります。") {
    // Silent stops used by text restoration must retain the recovery explanation.
    const recovery = !notice && this.waitRecovery;
    if (recovery) notice = this.state.notice;
    const epoch = this.advance();
    this.waitRecovery = recovery;
    this.set({ phase: "idle", waiting: false, issue: undefined, notice });
    const previous = this.closing;
    this.closing = Promise.resolve()
      .then(() => this.client.audio(AbortSignal.timeout(5_000), { operation: "stop" }))
      .then((value) => {
        if (value.stopped !== true || value.run_cancelled !== false)
          throw new Error("Unconfirmed stop");
      })
      .catch(() => {
        if (this.valid(epoch))
          this.set({
            notice:
              "この画面の録音・読み上げの待機を解除しました。機器の停止は未確認です。端末サービスを確認してください。",
          });
      });
    this.closing = Promise.all([previous, this.closing]).then(() => {});
    await this.closing;
  }
  offline(value: boolean) {
    if (value && !this.state.offline) void this.stop();
    this.set({ offline: value });
  }
  async refresh() {
    let epoch = this.generation;
    try {
      const value = await this.client.audio(
        AbortSignal.any([this.abort.signal, AbortSignal.timeout(5_000)]),
      );
      if (!this.valid(epoch)) return;
      if (
        value.enabled !== true ||
        !["idle", "recording", "ready", "sending", "busy"].includes(value.capture) ||
        !["idle", "loading", "playing", "busy"].includes(value.playback) ||
        typeof value.muted !== "boolean"
      )
        throw new Error("Invalid audio state");
      const phase =
        value.capture === "recording"
          ? "recording"
          : value.capture === "ready"
            ? "ready"
            : value.capture === "sending"
              ? "uploading"
              : value.playback === "playing"
                ? "playing"
                : "idle";
      // A GET can observe an intention but cannot establish one. Only a successful
      // acknowledgement of this screen's current explicit wait permits attachment.
      const waiting = value.waiting === true && this.waitingGeneration === epoch;
      if (!waiting) this.waitingGeneration = undefined;
      const endedWait = this.noticeOwner === "wait-ready" && !waiting;
      this.set({
        available: value.capture !== "busy",
        phase,
        muted: value.muted,
        waiting,
        issue: value.notice && value.notice !== "capture_limit" ? String(value.notice) : undefined,
        ...(value.notice
          ? {
              notice:
                value.notice === "capture_limit"
                  ? "録音上限に達しました。送信または破棄を選んでください。"
                  : voiceIssue(String(value.notice)),
            }
          : endedWait
            ? { notice: "" }
            : {}),
        input: value.input_device ?? "",
        output: value.output_device ?? "",
      });
    } catch {
      if (!this.valid(epoch)) return;
      if (this.waitingGeneration === epoch) {
        // An unknown status is not proof of consumption/expiry. Retire the whole
        // acknowledged intention explicitly, including its candidates, and stop
        // the daemon. A later GET cannot recover this wait; a new tap is required.
        epoch = this.generation + 1;
        const stopped = this.stop(
          "音声の待機状態を確認できないため、この画面の待機を解除しました。文字入力は利用できます。接続を確認して、待機を開始し直してください。",
        );
        this.waitRecovery = true;
        this.set({ available: false, phase: "closed" });
        await stopped;
        if (!this.valid(epoch)) return;
      } else
        this.set({
          available: false,
          phase: "closed",
          waiting: false,
          ...(this.waitRecovery ? {} : { notice: voiceIssue("unavailable") }),
        });
      try {
        const diagnostics = await this.client.diagnostics(AbortSignal.timeout(5_000));
        if (this.valid(epoch))
          this.set({
            muted:
              typeof diagnostics.microphone_muted === "boolean"
                ? diagnostics.microphone_muted
                : null,
          });
      } catch {
        /* Text remains available even when the optional audio route is disabled. */
      }
    }
  }
  async devices() {
    const epoch = this.generation;
    try {
      const value = await this.client.audioDevices(AbortSignal.timeout(5_000));
      if (!this.valid(epoch)) return;
      if (
        !Array.isArray(value.devices) ||
        value.devices.length > 64 ||
        value.devices.some(
          (d: { id: string; direction: string }) =>
            typeof d.id !== "string" ||
            d.id.length > 128 ||
            !["input", "output"].includes(d.direction),
        )
      )
        throw new Error("Invalid devices");
      this.set({ devices: value.devices });
    } catch {
      if (this.valid(epoch)) this.set({ notice: voiceIssue("unavailable") });
    }
  }
  async operation(
    operation: "select" | "start" | "finish" | "send",
    session?: string,
    devices?: { input_device: string; output_device: string },
  ) {
    const clearNotice = this.noticeOwner || this.waitRecovery;
    const epoch = this.advance();
    this.set({ waiting: false, ...(clearNotice ? { notice: "" } : {}) });
    await this.closing;
    if (!this.valid(epoch) || this.state.offline) return;
    if (operation === "start") this.set({ phase: "permission", issue: undefined, notice: "" });
    if (operation === "send") this.set({ phase: "uploading" });
    try {
      const result = await this.client.audio(
        AbortSignal.any([this.abort.signal, AbortSignal.timeout(65_000)]),
        { operation, ...(session ? { private_session_id: session } : {}), ...devices },
      );
      if (!this.valid(epoch)) return;
      if (operation !== "send") await this.refresh();
      return this.valid(epoch) ? { result, generation: epoch } : undefined;
    } catch {
      if (this.valid(epoch)) {
        const stopped = this.generation + 1;
        await this.stop();
        if (this.valid(stopped))
          this.set({ phase: "closed", issue: "network", notice: voiceIssue("network") });
      }
      return undefined;
    }
  }
  async wait(session?: string) {
    const epoch = this.generation + 1;
    await this.stop("");
    if (!this.valid(epoch)) return;
    this.waitRecovery = false;
    this.waitNotice(
      "wait-preparing",
      "新規音声の待機を準備しています。待機完了後にスマホで送信してください。",
    );
    try {
      const value = await this.client.waitForSpeech(
        session,
        AbortSignal.any([this.abort.signal, AbortSignal.timeout(15_000)]),
      );
      if (!this.valid(epoch)) return;
      if (value.waiting !== true) throw new Error("Unconfirmed wait");
      // Fence polls issued before the acknowledgement, even if they return last.
      this.waitingGeneration = this.advance();
      this.set({ waiting: true });
      this.waitNotice(
        "wait-ready",
        "60秒間、次の新しい割当を待機中です。ここからスマホで録音を送信してください。",
      );
    } catch {
      // A lost response may have created a daemon binding. Invalidate it while
      // this request still owns the generation; never stop a newer intention.
      if (this.valid(epoch))
        await this.stop(
          "新規音声の待機を開始できません。接続・音声設定・サーバーの対応を確認してください。",
        );
    }
  }

  dispose() {
    void this.stop();
    this.closed = true;
  }
}
