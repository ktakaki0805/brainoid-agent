import type {
  Conversation,
  DeviceSnapshotState,
  Message,
  PrivateSession,
  Run,
  RunAssignment,
} from "@brainoid-internal/api-client";
import {
  isId,
  initialEvents,
  reduceEvent,
  validateRunEvent,
} from "@brainoid-internal/ui/chat-events";
import {
  type Clock,
  OutputLifetime,
  privateSession,
  sessionBinding,
  systemClock,
} from "@brainoid-internal/ui/output-target";
import { DeviceVoice, initialDeviceVoice, type DeviceVoiceState } from "../voice/controller.ts";
import type { EventState } from "../../../packages/ui/src/chat/events.ts";
import { LocalOperationFailure, LocalSessionClient } from "./client.ts";

interface State {
  voice: DeviceVoiceState;
  draft: string;
  conversations: Conversation[];
  conversation?: string | undefined;
  runStatus?: Run["status"] | undefined;
  tools: EventState["tools"];
  commandResults: Record<string, string>;
  busy: boolean;
  device?: string | undefined;
  tenant?: string | undefined;
  session?: PrivateSession | undefined;
  runs: RunAssignment[];
  run?: string | undefined;
  text: string;
  provisional: string;
  notice: string;
  connected: boolean;
}
const empty = (): State => ({
  voice: initialDeviceVoice(),
  draft: "",
  conversations: [],
  tools: [],
  commandResults: {},
  busy: false,
  runs: [],
  text: "",
  provisional: "",
  notice: "",
  connected: false,
});
const positive = (value: number) => Number.isSafeInteger(value) && value >= 1;
const pageCursor = (value: unknown): value is string | null | undefined =>
  value == null || (typeof value === "string" && value.length > 0 && value.length <= 2048);
interface FinalContinuation {
  binding: string;
  cursor?: string;
  anchor?: string | undefined;
  distance: number;
  power: number;
}
interface WaitingCandidates {
  generation: number;
  known: Set<string>;
  queue: { id: string; attempts: number }[];
  active: boolean;
}
function snapshot(value: unknown): DeviceSnapshotState {
  const envelope = value as { ready: boolean; snapshot: DeviceSnapshotState; play_speech: boolean };
  const state = envelope?.snapshot;
  if (
    envelope?.ready !== true ||
    envelope.play_speech !== false ||
    !state ||
    !isId(state.device_id) ||
    !isId(state.tenant_id) ||
    !positive(state.connection_generation) ||
    !/^[a-f0-9]{64}$/.test(state.snapshot_id) ||
    state.next_cursor !== null ||
    !Array.isArray(state.assigned_runs) ||
    !Array.isArray(state.pending_command_ids) ||
    !Array.isArray(state.pending_notification_ids) ||
    !Number.isSafeInteger(state.delivery_cursor) ||
    state.delivery_cursor < 0 ||
    [state.pending_command_ids, state.pending_notification_ids].some(
      (ids) => ids.some((id) => !isId(id)) || new Set(ids).size !== ids.length,
    ) ||
    state.assigned_runs.length +
      state.pending_command_ids.length +
      state.pending_notification_ids.length >
      128 ||
    new Set(state.assigned_runs.map((run) => run.run_id)).size !== state.assigned_runs.length ||
    state.assigned_runs.some(
      (run) =>
        Object.keys(run).some(
          (key) =>
            ![
              "run_id",
              "conversation_id",
              "output_mode",
              "assignment_generation",
              "last_event_seq",
              "private_session_id",
            ].includes(key),
        ) ||
        !isId(run.run_id) ||
        !isId(run.conversation_id) ||
        !positive(run.assignment_generation) ||
        !["text", "speech"].includes(run.output_mode) ||
        !Number.isSafeInteger(run.last_event_seq) ||
        run.last_event_seq < 0 ||
        (run.private_session_id !== undefined && !isId(run.private_session_id)),
    )
  )
    throw new Error("Invalid complete snapshot");
  if (state.active_private_session) {
    const session = privateSession(state.active_private_session);
    if (
      session.status !== "active" ||
      session.device_id !== state.device_id ||
      session.tenant_id !== state.tenant_id
    )
      throw new Error("Wrong session");
  }
  if (
    state.assigned_runs.some(
      (run) =>
        run.private_session_id &&
        (run.private_session_id !== state.active_private_session?.id ||
          !state.active_private_session.allowed_scopes.includes("conversation")),
    )
  )
    throw new Error("Wrong assignment session");
  return state;
}

export class DeviceSessionController {
  private state = empty();
  private listeners = new Set<() => void>();
  private closed = false;
  private hidden = false;
  private syncing = false;
  private revision = 0;
  private selection = 0;
  private credential = 0;
  private binding = "";
  private abort = new AbortController();
  private stream: AbortController | undefined;
  private retired = new Set<string>();
  private assignments = new Map<string, { generation: number; binding: string }>();
  private finalContinuation: FinalContinuation | undefined;
  private waitingCandidates: WaitingCandidates | undefined;
  readonly voice: DeviceVoice;
  private streamDone: Promise<void> = Promise.resolve();
  private turn: { text: string; audio?: string; key: string; conversation: string } | undefined;
  readonly lifetime: OutputLifetime;
  readonly client: LocalSessionClient;
  constructor(client: LocalSessionClient = new LocalSessionClient(), clock: Clock = systemClock) {
    this.client = client;
    this.voice = new DeviceVoice(client, (voice) => {
      if (!voice.waiting || this.waitingCandidates?.generation !== this.voice.generation)
        this.waitingCandidates = undefined;
      this.publish({ ...this.state, voice });
    });
    this.lifetime = new OutputLifetime(clock, () => {
      if (this.state.session) this.retired.add(this.state.session.id);
      this.clear("個人セッションの期限に達しました。個人表示と待機中の出力を消去しました。");
    });
  }
  snapshot = () => this.state;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  private publish(next: State) {
    this.state = next;
    for (const listener of this.listeners) listener();
  }
  tick() {
    this.lifetime.tick();
  }
  private clear(notice = "") {
    void this.voice.stop("");
    this.turn = undefined;
    this.waitingCandidates = undefined;
    this.finalContinuation = undefined;
    this.revision++;
    this.selection++;
    this.stream?.abort();
    this.stream = undefined;
    this.abort.abort();
    this.abort = new AbortController();
    this.lifetime.clear();
    this.binding = "";
    this.publish({
      ...empty(),
      voice: this.voice.state,
      device: this.state.device,
      tenant: this.state.tenant,
      notice,
    });
  }
  async refresh() {
    this.tick();
    if (this.closed || this.hidden || this.syncing) return;
    this.syncing = true;
    const revision = this.revision;
    const signal = AbortSignal.any([this.abort.signal, AbortSignal.timeout(12_000)]);
    try {
      const next = snapshot(await this.client.snapshot(signal));
      this.tick();
      if (this.closed || this.hidden || revision !== this.revision || signal.aborted) return;
      if (
        this.state.device &&
        (next.device_id !== this.state.device || next.tenant_id !== this.state.tenant)
      ) {
        this.clear("端末の登録先が変わりました。ページを開き直してください。");
        this.hidden = true;
        return;
      }
      if (next.connection_generation < this.credential)
        throw new Error("Old credential generation");
      if (
        next.active_private_session &&
        this.state.session?.id === next.active_private_session.id &&
        (sessionBinding(this.state.session) !== sessionBinding(next.active_private_session) ||
          next.connection_generation !== this.credential)
      )
        throw new Error("Changed session binding");
      let session = next.active_private_session ?? undefined;
      if (
        session &&
        (this.retired.has(session.id) ||
          this.lifetime.clock.now() >=
            Math.min(Date.parse(session.expires_at), Date.parse(session.idle_expires_at)))
      ) {
        this.retired.add(session.id);
        session = undefined;
      }
      const runs = next.assigned_runs.filter(
        (run) => !run.private_session_id || run.private_session_id === session?.id,
      );
      for (const run of runs) {
        const binding = `${run.conversation_id}:${run.private_session_id ?? "shared"}`;
        const previous = this.assignments.get(run.run_id);
        if (!previous && this.assignments.size >= 1024) throw new Error("Assignment history limit");
        if (
          previous &&
          (run.assignment_generation < previous.generation ||
            (run.assignment_generation === previous.generation && binding !== previous.binding))
        )
          throw new Error("Old assignment");
        this.assignments.set(run.run_id, { generation: run.assignment_generation, binding });
      }
      const binding = JSON.stringify([
        next.device_id,
        next.tenant_id,
        next.connection_generation,
        session ? sessionBinding(session) : null,
      ]);
      const changed =
        binding !== this.binding ||
        this.state.runs.some(
          (old) =>
            !runs.some(
              (run) =>
                run.run_id === old.run_id &&
                run.assignment_generation === old.assignment_generation &&
                run.conversation_id === old.conversation_id &&
                run.private_session_id === old.private_session_id,
            ),
        );
      // Enumeration is not consumption: keep every untried candidate under the
      // acknowledged wait, even after all rows have been installed in state.runs.
      if (this.voice.state.waiting && !changed) {
        const pending: WaitingCandidates = this.waitingCandidates ?? {
          generation: this.voice.generation,
          known: new Set(this.state.runs.map((run) => run.run_id)),
          queue: [],
          active: false,
        };
        for (const run of runs) {
          if (pending.known.has(run.run_id)) continue;
          pending.known.add(run.run_id);
          if (run.output_mode === "speech") pending.queue.push({ id: run.run_id, attempts: 0 });
        }
        pending.queue = pending.queue.filter((item) =>
          runs.some((run) => run.run_id === item.id && run.output_mode === "speech"),
        );
        this.waitingCandidates = pending;
      } else this.waitingCandidates = undefined;
      if (changed && this.state.session && this.state.session.id !== session?.id)
        this.retired.add(this.state.session.id);
      if (this.retired.size > 128) throw new Error("Session history limit");
      if (changed) this.clear();
      this.credential = next.connection_generation;
      this.binding = binding;
      if (session && !this.lifetime.accept(session)) session = undefined;
      this.publish({
        ...this.state,
        device: next.device_id,
        tenant: next.tenant_id,
        session,
        runs,
        connected: true,
        notice: "",
      });
      this.voice.offline(false);
      if (this.voice.state.waiting && !changed) {
        this.tryWaitingCandidate();
      } else if (changed || (!this.stream && !this.state.text && !this.voice.state.waiting)) {
        const selected = runs.find((run) => run.run_id === this.state.run) ?? runs.at(-1);
        if (selected) void this.select(selected.run_id);
      }
    } catch {
      if (!this.closed && revision === this.revision) {
        // Network failure also clears provisional/audio work immediately. The
        // independent lease remains responsible for the known offline deadline.
        this.voice.offline(true);
        this.selection++;
        this.turn = undefined;
        this.finalContinuation = undefined;
        this.stream?.abort();
        this.stream = undefined;
        this.lifetime.invalidate();
        this.publish({
          ...this.state,
          text: "",
          provisional: "",
          connected: false,
          busy: false,
          notice: "接続を確認できません。既知の期限を超えて個人表示を続けません。",
        });
      }
    } finally {
      this.syncing = false;
    }
  }
  private tryWaitingCandidate() {
    const pending = this.waitingCandidates;
    if (
      !pending ||
      pending.active ||
      !this.voice.state.waiting ||
      pending.generation !== this.voice.generation
    )
      return;
    const item = pending.queue.shift();
    if (!item) return;
    pending.active = true;
    item.attempts++;
    void (async () => {
      const result = await this.select(item.id, "waiting", false, pending);
      if (this.waitingCandidates !== pending || pending.generation !== this.voice.generation)
        return;
      // A completed/failed POST may already have consumed the single daemon wait.
      // Confirm its current state before considering any second candidate.
      await this.voice.refresh();
      if (
        this.waitingCandidates !== pending ||
        !this.voice.state.waiting ||
        pending.generation !== this.voice.generation
      )
        return;
      pending.active = false;
      if (
        result === "retry" &&
        item.attempts < 2 &&
        this.state.runs.some((run) => run.run_id === item.id)
      )
        pending.queue.push(item);
      this.voice.waitStatus(
        pending.generation,
        pending.queue.length
          ? "受信した音声を確認できませんでした。残りの候補を確認します。文字で会話を確認できます。"
          : "受信した音声を再生できませんでした。残りの待機時間内の新しい割当を待ちます。文字で会話を確認するか、待機を開始し直してください。",
      );
    })();
  }
  async select(
    id: string,
    speech?: "future" | "waiting",
    preserveAudio = false,
    candidate?: WaitingCandidates,
  ): Promise<"retry" | undefined> {
    this.tick();
    const assignment = this.state.runs.find((run) => run.run_id === id);
    if (!assignment || !this.state.connected || this.closed || this.hidden) return;
    if (candidate !== this.waitingCandidates) this.waitingCandidates = undefined;
    if (speech === "future") this.voice.retireWait();
    if (id !== this.state.run) this.finalContinuation = undefined;
    this.stream?.abort();
    const previous = this.streamDone;
    let done = () => {};
    this.streamDone = new Promise<void>((resolve) => {
      done = resolve;
    });
    const stream = new AbortController();
    this.stream = stream;
    const revision = this.revision,
      selection = ++this.selection,
      lifetime = this.lifetime.capture();
    // The explicit playback tap owns every prerequisite and the eventual POST.
    // Stopping or starting another audio operation invalidates this entire path.
    const audioIntent = speech ? this.voice.capture() : undefined;
    const signal = AbortSignal.any([
      stream.signal,
      this.abort.signal,
      ...(audioIntent ? [audioIntent.signal] : []),
    ]);
    const valid = () =>
      !this.closed &&
      !this.hidden &&
      revision === this.revision &&
      selection === this.selection &&
      (!audioIntent || audioIntent.generation === this.voice.generation) &&
      !signal.aborted &&
      lifetime() &&
      (!assignment.private_session_id ||
        this.lifetime.session?.id === assignment.private_session_id);
    const guard = () => {
      if (!valid()) throw new Error("Stale output");
    };
    const detach = this.lifetime.own(() => stream.abort());
    this.turn = undefined;
    this.publish({
      ...this.state,
      run: id,
      busy: false,
      conversation: assignment.conversation_id,
      text: "",
      provisional: "",
      tools: [],
      runStatus: undefined,
    });
    const operation = async (body: object) => {
      guard();
      const result = await this.client.operation(
        {
          ...body,
          ...(assignment.private_session_id
            ? { private_session_id: assignment.private_session_id }
            : {}),
        },
        AbortSignal.any([signal, AbortSignal.timeout(12_000)]),
      );
      guard();
      return result;
    };
    const read = async () => {
      const conversation: Conversation = await operation({
        operation: "get_conversation",
        conversation_id: assignment.conversation_id,
      });
      if (
        conversation.id !== assignment.conversation_id ||
        conversation.tenant_id !== this.state.tenant ||
        (conversation.visibility === "private"
          ? !assignment.private_session_id ||
            conversation.display_policy !== "authenticated_session" ||
            conversation.owner_user_id !== this.state.session?.user_id
          : conversation.visibility !== "shared" ||
            conversation.display_policy !== "shared_display")
      )
        throw new Error("Wrong display policy");
      const run: Run = await operation({ operation: "get_run", run_id: id });
      if (
        run.run_id !== id ||
        run.conversation_id !== assignment.conversation_id ||
        !["queued", "running", "waiting_confirmation", "completed", "failed", "cancelled"].includes(
          run.status,
        )
      )
        throw new Error("Wrong run");
      this.publish({ ...this.state, runStatus: run.status });
      if (run.status === "completed") {
        if (!isId(run.final_message_id)) throw new Error("Missing final message");
        const binding = JSON.stringify([
          this.binding,
          id,
          assignment.conversation_id,
          assignment.assignment_generation,
          run.final_message_id,
        ]);
        if (this.finalContinuation?.binding !== binding)
          this.finalContinuation = { binding, distance: 0, power: 1 };
        const progress = this.finalContinuation;
        for (let page = 0; page < 100; page++) {
          let messages: { items: Message[]; next_cursor?: string | null };
          try {
            messages = await operation({
              operation: "list_messages",
              conversation_id: assignment.conversation_id,
              ...(progress.cursor ? { cursor: progress.cursor } : {}),
            });
          } catch (error) {
            if (
              error instanceof LocalOperationFailure &&
              error.status >= 400 &&
              error.status < 500 &&
              error.status !== 429 &&
              this.finalContinuation === progress
            )
              this.finalContinuation = undefined;
            throw error;
          }
          if (
            !Array.isArray(messages.items) ||
            messages.items.length > 100 ||
            !pageCursor(messages.next_cursor) ||
            messages.items.some(
              (message) =>
                !isId(message.id) ||
                !isId(message.run_id) ||
                typeof message.text !== "string" ||
                [...message.text].length > 8000,
            )
          )
            throw new Error("Invalid messages");
          const final = messages.items.find(
            (message) =>
              message.id === run.final_message_id &&
              message.run_id === id &&
              message.role === "assistant",
          );
          if (final) {
            guard();
            this.finalContinuation = undefined;
            this.publish({ ...this.state, text: final.text, provisional: "", notice: "" });
            return run;
          }
          if (
            !messages.next_cursor ||
            messages.next_cursor === progress.cursor ||
            messages.next_cursor === progress.anchor
          )
            throw new Error("Final message unavailable");
          // Brent's cycle detector retains two bounded cursors, not the history.
          // Keep progress across batches under the current output binding only.
          if (progress.distance === progress.power) {
            progress.anchor = progress.cursor;
            progress.power = Math.min(Number.MAX_SAFE_INTEGER, progress.power * 2);
            progress.distance = 0;
          }
          progress.distance++;
          progress.cursor = messages.next_cursor;
        }
        this.publish({
          ...this.state,
          notice: "保存済み応答の続きを確認しています。次の再同期で続けます。",
        });
        return run;
      }
      this.finalContinuation = undefined;
      return run;
    };
    let streamRequested = false;
    try {
      if (!speech && !preserveAudio) await this.voice.stop("");
      if (speech) {
        let timer: ReturnType<typeof setTimeout> | undefined;
        try {
          await Promise.race([
            previous,
            new Promise<never>((_resolve, reject) => {
              timer = setTimeout(() => reject(new Error("Previous stream still closing")), 2000);
            }),
          ]);
        } finally {
          clearTimeout(timer);
        }
      }
      guard();
      const run = await read();
      if (
        ["cancelled", "failed"].includes(run.status) ||
        (run.status === "completed" && speech !== "waiting" && !preserveAudio)
      )
        return;
      let events = initialEvents(id);
      streamRequested = true;
      await this.client.events(
        id,
        assignment.private_session_id,
        signal,
        (kind, value) => {
          guard();
          if (kind === "clear") throw new Error("Output cleared");
          if (kind === "history") {
            const history = value as {
              run: Run;
              messages: { items: Message[]; next_cursor?: string | null };
              replace: boolean;
              play_speech: boolean;
            };
            if (
              history?.replace !== true ||
              history.play_speech !== false ||
              history.run?.run_id !== id ||
              history.run.conversation_id !== assignment.conversation_id ||
              ![
                "queued",
                "running",
                "waiting_confirmation",
                "completed",
                "failed",
                "cancelled",
              ].includes(history.run.status) ||
              !Number.isSafeInteger(history.run.last_event_seq) ||
              history.run.last_event_seq < events.seq ||
              !Array.isArray(history.messages?.items) ||
              history.messages.items.length > 100 ||
              history.messages.items.some(
                (message) =>
                  !isId(message.id) ||
                  !isId(message.run_id) ||
                  typeof message.text !== "string" ||
                  [...message.text].length > 8000,
              ) ||
              !pageCursor(history.messages.next_cursor)
            )
              throw new Error("Invalid replacement history");
            // Reconcile on this stream. Opening another stream here would receive
            // the same durable-cursor history prefix and abort/reopen indefinitely.
            this.publish({ ...this.state, text: "", provisional: "" });
            return (async () => {
              const current = await read();
              guard();
              if (
                !Number.isSafeInteger(current.last_event_seq) ||
                current.last_event_seq < history.run.last_event_seq
              )
                throw new Error("Old canonical run");
              events = { ...initialEvents(id), seq: history.run.last_event_seq };
              if (["completed", "failed", "cancelled"].includes(current.status)) stream.abort();
            })();
          }
          if (kind !== "run") throw new Error("Unknown local event");
          const candidate = value as { seq: number; type: string };
          const event = validateRunEvent(value, {
            id: String(candidate.seq),
            event: candidate.type,
          });
          events = reduceEvent(events, event);
          this.publish({
            ...this.state,
            provisional: events.terminal ? "" : events.text,
            tools: events.tools,
            runStatus:
              events.character === "confirmation" ? "waiting_confirmation" : this.state.runStatus,
          });
          if (events.terminal)
            return (async () => {
              const current = await read();
              guard();
              if (current.status !== events.terminal) throw new Error("Unconfirmed terminal event");
              stream.abort();
            })();
          // Only the daemon consumes authorised live speech.ready; the browser never fetches PCM.
        },
        speech,
      );
    } catch {
      if (valid())
        this.publish({
          ...this.state,
          text: "",
          provisional: "",
          notice: "出力の現在状態を確認できません。再同期を待っています。",
        });
      if (valid() && !streamRequested) return "retry";
    } finally {
      detach();
      if (this.stream === stream) this.stream = undefined;
      done();
    }
  }
  draft(value: string) {
    if (!this.closed && !this.hidden) this.publish({ ...this.state, draft: value });
  }
  async conversations() {
    const revision = this.revision;
    try {
      const items: Conversation[] = [];
      let cursor: string | undefined;
      const seen = new Set<string>();
      for (let page = 0; page < 100; page++) {
        const value = await this.client.operation(
          {
            operation: "list_conversations",
            ...(cursor ? { cursor } : {}),
            ...(this.state.session ? { private_session_id: this.state.session.id } : {}),
          },
          AbortSignal.any([this.abort.signal, AbortSignal.timeout(12_000)]),
        );
        if (revision !== this.revision || this.closed || this.hidden) return;
        if (!Array.isArray(value.items) || value.items.length > 100)
          throw new Error("Invalid conversations");
        for (const item of value.items as Conversation[]) {
          if (
            !isId(item.id) ||
            item.tenant_id !== this.state.tenant ||
            typeof item.title !== "string"
          )
            throw new Error("Wrong conversation");
          if (
            (item.visibility === "shared" && item.display_policy === "shared_display") ||
            (item.visibility === "private" &&
              item.display_policy === "authenticated_session" &&
              this.state.session?.allowed_scopes.includes("conversation") &&
              item.owner_user_id === this.state.session.user_id)
          )
            items.push(item);
        }
        if (!value.next_cursor) {
          this.publish({
            ...this.state,
            conversations: items,
            conversation: items.some((c) => c.id === this.state.conversation)
              ? this.state.conversation
              : items[0]?.id,
          });
          return;
        }
        if (typeof value.next_cursor !== "string" || seen.has(value.next_cursor))
          throw new Error("Invalid cursor");
        cursor = value.next_cursor;
        seen.add(value.next_cursor);
      }
      throw new Error("Too many conversations");
    } catch {
      if (revision === this.revision && !this.closed)
        this.publish({
          ...this.state,
          notice: "利用可能な会話を取得できません。接続と許可範囲を確認してください。",
        });
    }
  }
  chooseConversation(id: string) {
    if (!this.state.conversations.some((c) => c.id === id)) return;
    this.selection++;
    this.waitingCandidates = undefined;
    this.stream?.abort();
    this.turn = undefined;
    void this.voice.stop("");
    this.publish({
      ...this.state,
      conversation: id,
      busy: false,
      draft: "",
      run: undefined,
      runStatus: undefined,
      text: "",
      provisional: "",
      tools: [],
      commandResults: {},
    });
  }
  async record() {
    if (
      !this.state.conversation ||
      this.state.busy ||
      ["running", "queued", "waiting_confirmation"].includes(this.state.runStatus ?? "")
    )
      return;
    await this.voice.operation("start", this.state.session?.id);
  }
  async sendRecording() {
    const result = await this.voice.operation("send", this.state.session?.id);
    if (!result || result.generation !== this.voice.generation) return;
    const value = result.result;
    if (
      !isId(value.audio_input_id) ||
      !Number.isFinite(Date.parse(value.expires_at)) ||
      Date.parse(value.expires_at) <= Date.now()
    ) {
      await this.voice.stop();
      return;
    }
    await this.send(value.audio_input_id, result.generation);
  }
  async send(audio?: string, voiceGeneration?: number) {
    this.tick();
    const conversation = this.state.conversation,
      draft = this.state.draft,
      revision = this.revision,
      selection = this.selection;
    if (
      !conversation ||
      !this.state.connected ||
      this.closed ||
      this.hidden ||
      this.state.busy ||
      (!audio && !draft.trim()) ||
      draft.length > 8000 ||
      ["queued", "running", "waiting_confirmation"].includes(this.state.runStatus ?? "") ||
      (audio && voiceGeneration !== this.voice.generation)
    )
      return;
    if (!audio) await this.voice.stop("");
    if (revision !== this.revision || selection !== this.selection) return;
    if (
      !this.turn ||
      this.turn.text !== draft ||
      this.turn.audio !== audio ||
      this.turn.conversation !== conversation
    )
      this.turn = {
        text: draft,
        ...(audio ? { audio } : {}),
        conversation,
        key: crypto.randomUUID(),
      };
    const pending = this.turn;
    this.publish({ ...this.state, busy: true, notice: "" });
    try {
      const result = await this.client.operation(
        {
          operation: "create_turn",
          operation_id: pending.key,
          conversation_id: conversation,
          turn: {
            ...(audio ? { audio_input_id: audio } : { text: pending.text }),
            output_mode: audio ? "speech" : "text",
          },
          ...(this.state.session ? { private_session_id: this.state.session.id } : {}),
        },
        AbortSignal.any([this.abort.signal, AbortSignal.timeout(15_000)]),
      );
      this.tick();
      if (revision !== this.revision || selection !== this.selection || this.closed || this.hidden)
        return;
      if (!isId(result.run_id) || result.status !== "queued") throw new Error("Wrong run receipt");
      this.turn = undefined;
      const assignment: RunAssignment = {
        run_id: result.run_id,
        conversation_id: conversation,
        output_mode: audio ? "speech" : "text",
        assignment_generation: 1,
        last_event_seq: 0,
        ...(this.state.session ? { private_session_id: this.state.session.id } : {}),
      };
      this.publish({
        ...this.state,
        draft: "",
        busy: false,
        runs: [...this.state.runs.filter((r) => r.run_id !== result.run_id), assignment],
        run: result.run_id,
        runStatus: "queued",
      });
      void this.select(result.run_id, undefined, true);
    } catch {
      if (revision === this.revision && selection === this.selection && !this.closed)
        this.publish({
          ...this.state,
          busy: false,
          notice: "送信結果は未確認です。同じ入力で再確認するか、会話の状態を確認してください。",
        });
    }
  }
  async cancelRun() {
    const id = this.state.run,
      revision = this.revision,
      selection = this.selection;
    if (!id || this.closed || this.hidden) return;
    await this.voice.stop();
    this.stream?.abort();
    try {
      const result = await this.client.operation(
        {
          operation: "cancel_run",
          run_id: id,
          operation_id: crypto.randomUUID(),
          ...(this.state.session ? { private_session_id: this.state.session.id } : {}),
        },
        AbortSignal.any([this.abort.signal, AbortSignal.timeout(12_000)]),
      );
      if (revision !== this.revision || selection !== this.selection || this.closed) return;
      if (result.run_id !== id) throw new Error("Wrong run");
      this.publish({
        ...this.state,
        runStatus: result.status,
        notice:
          result.status === "cancelled"
            ? "生成を停止しました。実行済みの操作は元に戻りません。"
            : `生成の現在状態: ${result.status}。取消と完了が競合した可能性があります。`,
      });
      void this.select(id);
    } catch {
      if (revision === this.revision && !this.closed)
        this.publish({
          ...this.state,
          notice: "生成の取消結果は未確認です。会話の状態を確認してください。",
        });
    }
  }
  async cancelCommand(id: string) {
    const revision = this.revision,
      selection = this.selection;
    if (!this.state.tools.some((tool) => tool.commandId === id)) return;
    const body = {
      command_id: id,
      ...(this.state.session ? { private_session_id: this.state.session.id } : {}),
    };
    let status = "未確認";
    try {
      const command = await this.client.operation(
        { ...body, operation: "get_command" },
        AbortSignal.any([this.abort.signal, AbortSignal.timeout(12_000)]),
      );
      if (revision !== this.revision || selection !== this.selection) return;
      if (command.id !== id || command.tenant_id !== this.state.tenant)
        throw new Error("Wrong command");
      if (["queued", "delivered", "accepted"].includes(command.status)) {
        try {
          await this.client.operation(
            { ...body, operation: "cancel_command", operation_id: crypto.randomUUID() },
            AbortSignal.any([this.abort.signal, AbortSignal.timeout(12_000)]),
          );
        } catch {
          /* A claim can win. Read the actual result below. */
        }
      }
      const current = await this.client.operation(
        { ...body, operation: "get_command" },
        AbortSignal.any([this.abort.signal, AbortSignal.timeout(12_000)]),
      );
      if (current.id !== id || current.tenant_id !== this.state.tenant)
        throw new Error("Wrong command");
      status = current.status;
    } catch {
      /* Do not report cancellation without a canonical result. */
    }
    if (revision === this.revision && selection === this.selection && !this.closed)
      this.publish({
        ...this.state,
        commandResults: {
          ...this.state.commandResults,
          [id]: `操作の現在状態: ${status}。本人のスマホでの操作はスマホから取り消してください。`,
        },
      });
  }
  async stop() {
    const session = this.state.session;
    if (session) this.retired.add(session.id);
    this.clear("この画面の個人表示を停止しました。Pi全体の停止を確認しています。");
    const revision = this.revision;
    if (!session) return;
    try {
      const result = await this.client.stop(session.id, AbortSignal.timeout(12_000));
      if (result.status !== "stopped" || result.cloud_session_ended !== false)
        throw new Error("Unconfirmed stop");
      if (!this.closed && revision === this.revision)
        this.publish({
          ...this.state,
          notice:
            "このPiでの個人表示を終了しました。クラウドの委任はスマホで終了するか、最大5分の期限で終了します。",
        });
    } catch {
      if (!this.closed && revision === this.revision)
        this.publish({
          ...this.state,
          notice:
            "この画面は消去しました。Pi全体の停止は未確認です。スマホで個人セッションを終了してください。",
        });
    }
  }
  suspend() {
    this.hidden = true;
    void this.stop();
  }
  resume() {
    this.hidden = false;
    void this.refresh();
  }
  dispose() {
    this.closed = true;
    this.clear();
    this.abort.abort();
    this.retired.clear();
    this.assignments.clear();
    this.voice.dispose();
  }
}
