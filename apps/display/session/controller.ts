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
import { LocalOperationFailure, LocalSessionClient } from "./client.ts";

interface State {
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
const empty = (): State => ({ runs: [], text: "", provisional: "", notice: "", connected: false });
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
  readonly lifetime: OutputLifetime;
  readonly client: LocalSessionClient;
  constructor(client: LocalSessionClient = new LocalSessionClient(), clock: Clock = systemClock) {
    this.client = client;
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
    this.finalContinuation = undefined;
    this.revision++;
    this.selection++;
    this.stream?.abort();
    this.stream = undefined;
    this.abort.abort();
    this.abort = new AbortController();
    this.lifetime.clear();
    this.binding = "";
    this.publish({ ...empty(), device: this.state.device, tenant: this.state.tenant, notice });
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
        runs.map((run) => [
          run.run_id,
          run.conversation_id,
          run.private_session_id,
          run.assignment_generation,
        ]),
      ]);
      const changed = binding !== this.binding;
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
      if (changed || (!this.stream && !this.state.text)) {
        const selected = runs.find((run) => run.run_id === this.state.run) ?? runs.at(-1);
        if (selected) void this.select(selected.run_id);
      }
    } catch {
      if (!this.closed && revision === this.revision) {
        // Network failure also clears provisional/audio work immediately. The
        // independent lease remains responsible for the known offline deadline.
        this.selection++;
        this.finalContinuation = undefined;
        this.stream?.abort();
        this.stream = undefined;
        this.lifetime.invalidate();
        this.publish({
          ...this.state,
          text: "",
          provisional: "",
          connected: false,
          notice: "接続を確認できません。既知の期限を超えて個人表示を続けません。",
        });
      }
    } finally {
      this.syncing = false;
    }
  }
  async select(id: string) {
    this.tick();
    const assignment = this.state.runs.find((run) => run.run_id === id);
    if (!assignment || !this.state.connected || this.closed || this.hidden) return;
    if (id !== this.state.run) this.finalContinuation = undefined;
    this.stream?.abort();
    const stream = new AbortController();
    this.stream = stream;
    const revision = this.revision,
      selection = ++this.selection,
      lifetime = this.lifetime.capture();
    const signal = AbortSignal.any([stream.signal, this.abort.signal]);
    const valid = () =>
      !this.closed &&
      !this.hidden &&
      revision === this.revision &&
      selection === this.selection &&
      !signal.aborted &&
      lifetime() &&
      (!assignment.private_session_id ||
        this.lifetime.session?.id === assignment.private_session_id);
    const guard = () => {
      if (!valid()) throw new Error("Stale output");
    };
    const detach = this.lifetime.own(() => stream.abort());
    this.publish({ ...this.state, run: id, text: "", provisional: "" });
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
    try {
      const run = await read();
      if (["completed", "cancelled", "failed"].includes(run.status)) return;
      let events = initialEvents(id);
      await this.client.events(id, assignment.private_session_id, signal, (kind, value) => {
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
        const event = validateRunEvent(value, { id: String(candidate.seq), event: candidate.type });
        events = reduceEvent(events, event);
        this.publish({ ...this.state, provisional: events.terminal ? "" : events.text });
        if (events.terminal)
          return (async () => {
            const current = await read();
            guard();
            if (current.status !== events.terminal) throw new Error("Unconfirmed terminal event");
            stream.abort();
          })();
        // speech.ready is deliberately never played (including reconnect history).
      });
    } catch {
      if (valid())
        this.publish({
          ...this.state,
          text: "",
          provisional: "",
          notice: "出力の現在状態を確認できません。再同期を待っています。",
        });
    } finally {
      detach();
      if (this.stream === stream) this.stream = undefined;
    }
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
  }
}
