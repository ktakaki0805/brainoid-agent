import {
  type Agent,
  type Conversation,
  type Client,
  type ConversationCreate,
  type Message,
  type Run,
  RunEventsFailure,
  cancelRun,
  cancelDeviceCommand,
  getDeviceCommand,
  createConversation,
  createTurn,
  getConversation,
  getRun,
  listAgents,
  listConversations,
  listMessages,
  streamRunEvents,
  updateConversation,
} from "@brainoid-internal/api-client";
import {
  initialVoice,
  VoiceController,
  type VoiceFactory,
  type VoiceState,
} from "../voice/controller.ts";
import { OutputTargetController } from "../output-target/controller.ts";
import { RequestFailure, type SessionContext, StaleContext } from "../session.ts";
import { type EventState, initialEvents, isId, reduceEvent, validateRunEvent } from "./events.ts";

export interface ChatState {
  voice: VoiceState;
  commandResults: Record<string, string>;
  agents: readonly Agent[];
  conversations: readonly Conversation[];
  conversation?: Conversation | undefined;
  messages: readonly Message[];
  events?: EventState | undefined;
  run?: Run | undefined;
  draft: string;
  busy: boolean;
  connection: "idle" | "connecting" | "live" | "disconnected" | "settled";
  notice?: string | undefined;
  output: "current" | "unspecified";
  finalVerified: boolean;
  outputDevice?: string | undefined;
  historyUnavailable?: "expired" | "cancelled" | undefined;
}
const empty = (): ChatState => ({
  voice: initialVoice(),
  commandResults: {},
  agents: [],
  conversations: [],
  messages: [],
  draft: "",
  busy: false,
  connection: "idle",
  output: "unspecified",
  finalVerified: false,
});
const terminal = (run: Run) => ["completed", "failed", "cancelled"].includes(run.status);
const data = <T>(result: { data?: T; response?: Response }): NonNullable<T> => {
  if (!result.response?.ok || result.data == null)
    throw new RequestFailure(result.response?.status ?? 0);
  return result.data;
};

/** One instance per authenticated tenant. Selection, stream and requests all have fences. */
export class ChatController {
  private state = empty();
  private listeners = new Set<() => void>();
  private selection = 0;
  private refreshing = 0;
  private closed = false;
  private selectedAbort = new AbortController();
  private streamAbort: AbortController | undefined;
  private turn:
    | {
        text: string;
        audio?: string | undefined;
        voiceGeneration?: number | undefined;
        key: string;
        device?: string | undefined;
        binding?: string | undefined;
        session?: string | undefined;
      }
    | undefined;
  private policy: { id: string; version: number; key: string; uncertain: boolean } | undefined;
  readonly voice: VoiceController;
  readonly outputTarget: OutputTargetController;
  private outputStop: () => void;
  private outputBinding = "";
  private creation: { body: ConversationCreate; key: string } | undefined;
  private cancelKey?: string | undefined;
  private release: () => void;
  private readonly tenantId: string;
  readonly context: SessionContext;
  readonly surface: "mobile" | "display";
  constructor(context: SessionContext, surface: "mobile" | "display", voice?: VoiceFactory) {
    this.context = context;
    this.surface = surface;
    if (!context.tenantId) throw new Error("Tenant required");
    this.tenantId = context.tenantId;
    this.voice = new VoiceController(context, voice, (value) =>
      this.publish({ ...this.state, voice: value }),
    );
    this.state = { ...this.state, voice: this.voice.state };
    this.outputTarget = new OutputTargetController(context);
    this.outputStop = this.outputTarget.subscribe(() => {
      const state = this.outputTarget.snapshot();
      const binding = `${state.session?.id ?? ""}:${state.destination}:${this.outputTarget.generation}`;
      if (binding === this.outputBinding || this.closed) return;
      this.outputBinding = binding;
      this.clearOutput();
    });
    this.release = context.own(() => this.dispose());
  }
  snapshot = () => this.state;
  get pendingDisplayPolicy() {
    return this.policy?.id;
  }
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  private publish(next: ChatState) {
    this.state = next;
    for (const listener of this.listeners) listener();
  }
  private assert(epoch = this.selection) {
    this.context.assertCurrent();
    if (this.closed || epoch !== this.selection || this.selectedAbort.signal.aborted)
      throw new StaleContext();
  }
  private resetSelection() {
    this.voice.stop();
    this.selection++;
    this.refreshing++;
    this.streamAbort?.abort();
    this.selectedAbort.abort();
    this.selectedAbort = new AbortController();
    this.turn = undefined;
    this.cancelKey = undefined;
  }
  clearOutput() {
    this.resetSelection();
    this.publish({
      ...empty(),
      voice: this.voice.state,
      agents: this.state.agents,
      conversations: this.state.conversations,
    });
  }
  dispose() {
    if (this.closed) return;
    this.closed = true;
    this.resetSelection();
    this.creation = undefined;
    this.policy = undefined;
    this.outputStop();
    this.outputTarget.dispose();
    this.publish(empty());
    this.release?.();
  }
  private async request<T>(
    operation: (client: Client, signal: AbortSignal, tenantId: string) => Promise<T>,
  ): Promise<T> {
    const epoch = this.selection;
    const signal = AbortSignal.any([
      this.selectedAbort.signal,
      this.context.signal,
      AbortSignal.timeout(15_000),
    ]);
    let detach = () => {};
    try {
      const aborted = new Promise<never>((_resolve, reject) => {
        const fail = () => reject(new StaleContext());
        signal.addEventListener("abort", fail, { once: true });
        detach = () => signal.removeEventListener("abort", fail);
        if (signal.aborted) fail();
      });
      const result = await Promise.race([
        this.context.run((client, parentSignal, tenantId) =>
          operation(client, AbortSignal.any([signal, parentSignal]), tenantId ?? this.tenantId),
        ),
        aborted,
      ]);
      this.assert(epoch);
      signal.throwIfAborted();
      return result;
    } finally {
      detach();
    }
  }

  private failure(error: unknown, epoch: number) {
    if (this.closed || epoch !== this.selection || this.context.signal.aborted) return;
    this.publish({
      ...this.state,
      busy: false,
      connection: this.state.events ? "disconnected" : "idle",
      notice:
        error instanceof RequestFailure && error.status === 409
          ? "状態が変わりました。会話を選び直して確認してください。"
          : "通信または応答を確認できません。再度状態を確認してください。",
    });
  }
  private conversation(value: Conversation): Conversation {
    if (
      !isId(value.id) ||
      value.tenant_id !== this.context.tenantId ||
      !isId(value.agent_id) ||
      typeof value.title !== "string" ||
      !["private", "shared"].includes(value.visibility) ||
      !["never", "authenticated_session", "shared_display"].includes(value.display_policy) ||
      (value.visibility === "private" &&
        (value.owner_user_id !== this.context.uid || value.display_policy === "shared_display"))
    )
      throw new Error("Invalid conversation");
    if (
      this.surface === "display" &&
      (value.visibility !== "shared" || value.display_policy !== "shared_display")
    )
      throw new Error("Not a shared display conversation");
    return value;
  }
  private async pages<T>(
    load: (cursor?: string) => Promise<{ items: T[]; next_cursor: string | null }>,
  ): Promise<T[]> {
    const items: T[] = [],
      seen = new Set<string>();
    let cursor: string | undefined;
    for (let page = 0; page < 100; page++) {
      const result = await load(cursor);
      if (!Array.isArray(result.items) || result.items.length > 100)
        throw new Error("Invalid page");
      items.push(...result.items);
      if (result.next_cursor === null) return items;
      if (
        typeof result.next_cursor !== "string" ||
        !result.next_cursor ||
        seen.has(result.next_cursor)
      )
        throw new Error("Invalid cursor");
      cursor = result.next_cursor;
      seen.add(cursor);
    }
    throw new Error("Too many pages");
  }
  async load() {
    const epoch = this.selection;
    if (this.closed || this.state.busy) return;
    this.publish({ ...this.state, busy: true, notice: undefined });
    try {
      const agents = await this.pages<Agent>((cursor) =>
        this.request(async (client, signal, tenant) =>
          data(
            await listAgents({
              client,
              signal,
              path: { tenant_id: tenant },
              query: { limit: 100, ...(cursor ? { cursor } : {}) },
            }),
          ),
        ),
      );
      if (
        agents.some(
          (agent) =>
            agent.tenant_id !== this.context.tenantId ||
            !isId(agent.id) ||
            typeof agent.name !== "string",
        )
      )
        throw new Error("Invalid agent");
      const all = await this.pages<Conversation>((cursor) =>
        this.request(async (client, signal, tenant) =>
          data(
            await listConversations({
              client,
              signal,
              path: { tenant_id: tenant },
              query: { limit: 100, ...(cursor ? { cursor } : {}) },
            }),
          ),
        ),
      );
      // The display fake has no private session. Do not fetch or expose private history.
      const conversations = all
        .filter(
          (item) =>
            this.surface !== "display" ||
            (item.visibility === "shared" && item.display_policy === "shared_display"),
        )
        .map((item) => this.conversation(item));
      this.assert(epoch);
      this.publish({ ...this.state, agents, conversations, busy: false });
    } catch (error) {
      this.failure(error, epoch);
    }
  }
  async create(agentId: string, visibility: "private" | "shared") {
    const epoch = this.selection;
    if (
      this.closed ||
      this.state.busy ||
      !this.state.agents.some((agent) => agent.id === agentId) ||
      (this.surface === "display" && visibility !== "shared")
    )
      return;
    const body: ConversationCreate = {
      agent_id: agentId,
      visibility,
      display_policy: visibility === "private" ? "never" : "shared_display",
      title: visibility === "private" ? "個人の会話" : "みんなの会話",
    };
    if (JSON.stringify(body) !== JSON.stringify(this.creation?.body))
      this.creation = { body, key: crypto.randomUUID() };
    const key = this.creation?.key;
    if (!key) return;
    this.publish({ ...this.state, busy: true, notice: undefined });
    try {
      const conversation = this.conversation(
        await this.request<Conversation>(async (client, signal, tenant) =>
          data(
            await createConversation({
              client,
              signal,
              path: { tenant_id: tenant },
              body,
              headers: { "Idempotency-Key": key },
            }),
          ),
        ),
      );
      this.assert(epoch);
      this.creation = undefined;
      this.publish({
        ...this.state,
        conversations: [
          ...this.state.conversations.filter((item) => item.id !== conversation.id),
          conversation,
        ],
        busy: false,
      });
      await this.select(conversation.id);
    } catch (error) {
      this.failure(error, epoch);
    }
  }
  async select(id: string) {
    if (this.closed || !this.state.conversations.some((item) => item.id === id)) return;
    this.resetSelection();
    const epoch = this.selection;
    this.publish({
      ...empty(),
      voice: this.voice.state,
      agents: this.state.agents,
      conversations: this.state.conversations,
      busy: true,
    });
    try {
      const conversation = this.conversation(
        await this.request<Conversation>(async (client, signal, tenant) =>
          data(
            await getConversation({
              client,
              signal,
              path: { tenant_id: tenant, conversation_id: id },
            }),
          ),
        ),
      );
      if (conversation.id !== id) throw new Error("Wrong conversation");
      const messages = await this.messages(id);
      this.assert(epoch);
      this.publish({ ...this.state, conversation, messages, busy: false });
      const runId = messages.at(-1)?.run_id;
      if (runId) {
        this.publish({ ...this.state, events: initialEvents(runId) });
        await this.refresh();
        this.assert(epoch);
        if (
          this.state.run &&
          terminal(this.state.run) &&
          this.state.run.last_event_seq > (this.state.events?.seq ?? 0)
        )
          await this.connect();
      }
    } catch (error) {
      this.failure(error, epoch);
    }
  }
  private async messages(id: string): Promise<Message[]> {
    const messages = await this.pages<Message>((cursor) =>
      this.request(async (client, signal, tenant) =>
        data(
          await listMessages({
            client,
            signal,
            path: { tenant_id: tenant, conversation_id: id },
            query: { limit: 100, ...(cursor ? { cursor } : {}) },
          }),
        ),
      ),
    );
    const seen = new Set<string>();
    for (const message of messages) {
      if (
        !isId(message.id) ||
        !isId(message.run_id) ||
        !["user", "assistant", "tool"].includes(message.role) ||
        typeof message.text !== "string" ||
        [...message.text].length > 8000 ||
        seen.has(message.id)
      )
        throw new Error("Invalid message");
      seen.add(message.id);
    }
    return messages;
  }
  draft(text: string) {
    if (!this.closed) {
      this.assert();
      this.publish({ ...this.state, draft: text });
    }
  }
  async allowPrivateDisplay() {
    const conversation = this.state.conversation,
      epoch = this.selection;
    if (
      this.closed ||
      this.state.busy ||
      (!this.policy &&
        (!conversation ||
          conversation.visibility !== "private" ||
          conversation.display_policy !== "never"))
    )
      return;
    if (!this.policy && conversation)
      this.policy = {
        id: conversation.id,
        version: conversation.version,
        key: crypto.randomUUID(),
        uncertain: false,
      };
    const pending = this.policy;
    if (!pending) return;
    this.publish({ ...this.state, busy: true, notice: undefined });
    try {
      const updated = this.conversation(
        await this.request(async (client, signal, tenant) =>
          data(
            await updateConversation({
              client,
              signal,
              path: { tenant_id: tenant, conversation_id: pending.id },
              body: { display_policy: "authenticated_session" },
              headers: { "Idempotency-Key": pending.key, "If-Match": `"${pending.version}"` },
            }),
          ),
        ),
      );
      if (
        updated.id !== pending.id ||
        updated.display_policy !== "authenticated_session" ||
        updated.version < pending.version
      )
        throw new Error("Wrong conversation policy");
      this.assert(epoch);
      this.policy = undefined;
      this.publish({
        ...this.state,
        conversation:
          this.state.conversation?.id === updated.id ? updated : this.state.conversation,
        conversations: this.state.conversations.map((value) =>
          value.id === updated.id ? updated : value,
        ),
        busy: false,
      });
    } catch (error) {
      if (
        !pending.uncertain &&
        error instanceof RequestFailure &&
        [400, 409, 412, 429].includes(error.status)
      ) {
        this.policy = undefined;
        this.failure(error, epoch);
        this.publishIfCurrent(epoch, {
          ...this.state,
          notice: "表示設定を変更できませんでした。会話を選び直して現在の設定を確認してください。",
        });
      } else {
        pending.uncertain = true;
        this.failure(error, epoch);
      }
    }
  }
  async record() {
    this.outputTarget.tick();
    if (
      !this.state.conversation ||
      this.state.busy ||
      (this.state.run && !terminal(this.state.run))
    )
      return;
    const session = this.outputTarget.snapshot().session;
    await this.voice.start(
      Math.min(
        Date.now() + 300_000,
        session ? Date.parse(session.expires_at) : Infinity,
        session ? Date.parse(session.idle_expires_at) : Infinity,
      ),
    );
  }
  async sendRecording() {
    const input = await this.voice.upload();
    if (input) await this.send(input);
  }
  async send(input?: { id: string; generation: number }) {
    this.outputTarget.tick();
    const { conversation, draft } = this.state;
    if (
      !conversation ||
      this.policy ||
      this.state.busy ||
      (this.state.run && !terminal(this.state.run)) ||
      (!this.state.run &&
        this.state.events &&
        !this.state.events.terminal &&
        this.state.connection !== "idle") ||
      (this.state.run?.status === "completed" && !this.state.finalVerified) ||
      (!input && !draft.trim()) ||
      (input && !this.voice.current(input.generation)) ||
      [...draft].length > 8000
    )
      return;
    const target = this.outputTarget.target();
    if (
      target &&
      conversation.visibility === "private" &&
      conversation.display_policy !== "authenticated_session"
    ) {
      this.publish({
        ...this.state,
        notice: "この会話はPiへの表示を許可していません。下の確認操作で許可してください。",
      });
      return;
    }
    const epoch = this.selection;
    if (!input) this.voice.stop();
    this.streamAbort?.abort();
    this.refreshing++;
    if (
      this.turn?.text !== draft ||
      this.turn?.audio !== input?.id ||
      this.turn?.device !== target?.device ||
      this.turn?.binding !== target?.binding
    )
      this.turn = {
        text: draft,
        audio: input?.id,
        voiceGeneration: input?.generation,
        key: crypto.randomUUID(),
        device: target?.device,
        binding: target?.binding,
        session: target?.session,
      };
    const pending = this.turn;
    if (!pending) return;
    this.publish({ ...this.state, busy: true, notice: undefined });
    try {
      const accepted = await this.request<{ run_id: string; status: string }>(
        async (client, signal, tenant) =>
          data(
            await createTurn({
              client,
              signal,
              path: { tenant_id: tenant, conversation_id: conversation.id },
              body: {
                ...(pending.audio ? { audio_input_id: pending.audio } : { text: pending.text }),
                output_mode: pending.audio ? "speech" : "text",
                ...(pending.device
                  ? {
                      output_device_id: pending.device,
                      expected_output_private_session_id: pending.session ?? "",
                    }
                  : {}),
              },
              headers: { "Idempotency-Key": pending.key },
            }),
          ),
      );
      if (!isId(accepted.run_id) || accepted.status !== "queued")
        throw new Error("Invalid accepted run");
      this.assert(epoch);
      if (pending.voiceGeneration !== undefined && !pending.device)
        this.voice.bindRun(accepted.run_id, pending.voiceGeneration);
      this.turn = undefined;
      this.cancelKey = undefined;
      this.publish({
        ...this.state,
        draft: "",
        busy: false,
        run: undefined,
        events: initialEvents(accepted.run_id),
        output: "current",
        outputDevice: pending.device,
        finalVerified: false,
        historyUnavailable: undefined,
      });
      await this.refresh(undefined, true);
      this.assert(epoch);
      if (this.state.run) await this.connect();
    } catch (error) {
      this.failure(error, epoch);
    }
  }
  async refresh(expectedMessageId?: string, preserveVoice = false) {
    const events = this.state.events,
      conversation = this.state.conversation;
    if (!events || !conversation || this.closed) return;
    if (!preserveVoice) this.voice.stop();
    const refreshing = ++this.refreshing;
    this.streamAbort?.abort();
    const epoch = this.selection;
    this.publish({ ...this.state, busy: true, connection: "connecting", notice: undefined });
    try {
      const run = await this.request<Run>(async (client, signal, tenant) =>
        data(await getRun({ client, signal, path: { tenant_id: tenant, run_id: events.runId } })),
      );
      if (
        run.run_id !== events.runId ||
        run.conversation_id !== conversation.id ||
        !["queued", "running", "waiting_confirmation", "completed", "failed", "cancelled"].includes(
          run.status,
        ) ||
        !Number.isSafeInteger(run.last_event_seq) ||
        run.last_event_seq < events.seq
      )
        throw new Error("Invalid run");
      if (refreshing !== this.refreshing || this.state.events?.runId !== events.runId) return;
      // Run termination is authoritative even if the final message read fails.
      // It suppresses provisional output without pretending its event history was read.
      if (["cancelled", "failed"].includes(run.status)) this.voice.stop();
      if (terminal(run)) {
        this.publish({
          ...this.state,
          run,
          finalVerified:
            this.state.finalVerified && this.state.run?.final_message_id === run.final_message_id,
          events: { ...events, text: "", character: run.status === "failed" ? "error" : "idle" },
          historyUnavailable:
            run.status === "cancelled" && run.last_event_seq > events.seq
              ? "cancelled"
              : this.state.historyUnavailable,
        });
      }
      const messages = await this.messages(conversation.id);
      this.assert(epoch);
      if (refreshing !== this.refreshing || this.state.events?.runId !== events.runId) return;
      let next = { ...(this.state.events ?? events) };
      if (run.status === "completed") {
        const final = messages.find(
          (message) =>
            message.id === run.final_message_id &&
            message.run_id === run.run_id &&
            message.role === "assistant",
        );
        if (
          !final ||
          ((expectedMessageId ?? events.finalMessageId) &&
            final.id !== (expectedMessageId ?? events.finalMessageId))
        )
          throw new Error("Final message missing or mismatched");
        next = { ...next, text: "", character: "idle" };
      } else if (run.status === "failed" || run.status === "cancelled") {
        next = { ...next, text: "", character: run.status === "failed" ? "error" : "idle" };
      } else {
        next.character = run.status === "waiting_confirmation" ? "confirmation" : "thinking";
      }

      this.publish({
        ...this.state,
        run,
        messages,
        finalVerified: run.status === "completed",
        events: next,
        busy: false,
        connection: terminal(run) ? "settled" : "disconnected",
      });
    } catch (error) {
      if (refreshing !== this.refreshing) return;
      // A recoverable read error must retain the matching prefix AND cursor.
      // Only known termination purges provisional output; permission denial disposes
      // the entire SessionContext before this branch can publish anything.
      if (this.state.events?.terminal || (this.state.run && terminal(this.state.run))) {
        this.publishIfCurrent(epoch, {
          ...this.state,
          events: this.state.events ? { ...this.state.events, text: "" } : undefined,
        });
      }
      this.failure(error, epoch);
    }
  }
  private publishIfCurrent(epoch: number, state: ChatState) {
    if (!this.closed && epoch === this.selection && !this.context.signal.aborted)
      this.publish(state);
  }
  async connect() {
    const events = this.state.events;
    const wasTerminal = this.state.run ? terminal(this.state.run) : false;
    if (
      !events ||
      this.closed ||
      this.state.busy ||
      events.terminal ||
      this.state.run?.status === "cancelled" ||
      (wasTerminal && this.state.run && this.state.run.last_event_seq <= events.seq) ||
      this.state.connection === "live"
    )
      return;
    this.streamAbort?.abort();
    const abort = new AbortController();
    this.streamAbort = abort;
    const voiceGeneration = this.voice.stream(events.runId);
    const epoch = this.selection;
    const signal = AbortSignal.any([abort.signal, this.selectedAbort.signal, this.context.signal]);
    const guard = () => {
      this.assert(epoch);
      if (signal.aborted || this.streamAbort !== abort) throw new StaleContext();
    };
    this.publish({ ...this.state, connection: "connecting", notice: undefined });
    let terminalMessage: string | undefined;
    let timedOut = false;
    const timeout = setTimeout(() => {
      timedOut = true;
      abort.abort();
    }, 30_000);
    try {
      const { stream } = await this.context.run((client) =>
        streamRunEvents({
          client,
          signal,
          path: { tenant_id: this.tenantId, run_id: events.runId },
          headers: { "Last-Event-ID": String(events.seq) },
          onSseEvent: (frame) => {
            guard();
            const event = validateRunEvent(frame.data, frame);
            const current = this.state.events;
            if (!current || current.runId !== events.runId) throw new StaleContext();
            const reduced = reduceEvent(current, event);
            if (event.type === "speech.ready" && event.seq > current.seq)
              this.voice.sentence(
                events.runId,
                voiceGeneration,
                String(event.payload.speech_id),
                Number(event.payload.sentence_index),
                Date.parse(String(event.payload.expires_at)),
              );
            if (event.type === "run.failed" || event.type === "run.cancelled") this.voice.stop();
            // Historical replay recovers tool facts, never an old answer or animation.
            const next: EventState =
              this.state.run && terminal(this.state.run)
                ? {
                    ...reduced,
                    text: "",
                    character: this.state.run.status === "failed" ? "error" : "idle",
                  }
                : reduced;
            this.publish({
              ...this.state,
              events: next,
              connection: "live",
              historyUnavailable: next.terminal ? undefined : this.state.historyUnavailable,
            });
          },
        }),
      );
      guard();
      for await (const _event of stream) {
        guard();
        if (this.state.events?.terminal) {
          terminalMessage = this.state.events.finalMessageId;
          break;
        }
      }
      guard();
    } catch (error) {
      if (
        (!timedOut && signal.aborted) ||
        epoch !== this.selection ||
        this.closed ||
        this.context.signal.aborted
      )
        return;
      if (error instanceof RunEventsFailure && error.status === 410)
        this.publish({ ...this.state, historyUnavailable: "expired" });
      // Includes 410, gaps, malformed frames and disconnects: one canonical fetch,
      // then an explicit user retry. No unlimited retry loop or skipped delta.
    } finally {
      clearTimeout(timeout);
      abort.abort();
      if (!this.state.events?.terminal && voiceGeneration === this.voice.generation)
        this.voice.stop("接続が終了したため音声を停止しました。再接続では文字だけを復元します。");
    }
    if (!this.closed && epoch === this.selection && !this.context.signal.aborted) {
      await this.refresh(terminalMessage, true);
      if (this.closed || epoch !== this.selection || this.context.signal.aborted) return;
      // At most one extra connection when the worker finished during a disconnect.
      // The second connection starts with wasTerminal=true, so cannot loop here.
      const latest = this.snapshot();
      if (
        !wasTerminal &&
        latest.run &&
        terminal(latest.run) &&
        latest.run.status !== "cancelled" &&
        !latest.historyUnavailable &&
        !latest.events?.terminal &&
        latest.run.last_event_seq > (latest.events?.seq ?? 0)
      )
        await this.connect();
    }
  }
  async cancelCommand(id: string) {
    const epoch = this.selection;
    if (!this.state.events?.tools.some((tool) => tool.commandId === id)) return;
    try {
      const command = await this.request(async (client, signal, tenant) =>
        data(
          await getDeviceCommand({ client, signal, path: { tenant_id: tenant, command_id: id } }),
        ),
      );
      if (command.id !== id) throw new Error("Wrong command");
      if (["queued", "delivered", "accepted"].includes(command.status)) {
        try {
          await this.request(async (client, signal, tenant) =>
            data(
              await cancelDeviceCommand({
                client,
                signal,
                path: { tenant_id: tenant, command_id: id },
                headers: { "Idempotency-Key": crypto.randomUUID() },
              }),
            ),
          );
        } catch {
          /* Cancellation may race the execution claim; read its actual state. */
        }
      }
      const result = await this.request(async (client, signal, tenant) =>
        data(
          await getDeviceCommand({ client, signal, path: { tenant_id: tenant, command_id: id } }),
        ),
      );
      if (result.id !== id || result.tenant_id !== this.tenantId) throw new Error("Wrong command");
      this.publishIfCurrent(epoch, {
        ...this.state,
        commandResults: {
          ...this.state.commandResults,
          [id]: `操作の現在状態: ${result.status}。実行済みの効果は元に戻りません。`,
        },
      });
    } catch {
      this.publishIfCurrent(epoch, {
        ...this.state,
        commandResults: {
          ...this.state.commandResults,
          [id]: "取消結果は未確認です。操作の現在状態を再確認してください。",
        },
      });
    }
  }
  async cancel() {
    if (!this.state.events || !this.state.run || terminal(this.state.run) || this.state.busy)
      return;
    this.voice.stop(
      "読み上げを停止し、生成の取消結果を確認しています。実行済みの操作は元に戻りません。",
    );
    this.streamAbort?.abort();
    const epoch = this.selection,
      runId = this.state.events.runId;
    this.cancelKey ??= crypto.randomUUID();
    const cancelKey = this.cancelKey;
    this.publish({ ...this.state, busy: true });
    try {
      const run = await this.request<Run>(async (client, signal, tenant) =>
        data(
          await cancelRun({
            client,
            signal,
            path: { tenant_id: tenant, run_id: runId },
            headers: { "Idempotency-Key": cancelKey },
          }),
        ),
      );
      if (run.run_id !== runId || run.conversation_id !== this.state.conversation?.id)
        throw new Error("Wrong cancelled run");
      await this.refresh();
    } catch (error) {
      this.failure(error, epoch);
    }
  }
}
