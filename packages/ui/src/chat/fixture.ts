import type { Conversation, Message, Run, RunEvent } from "@brainoid-internal/api-client";

export const fixtureAgent = "20000000-0000-4000-8000-000000000001";
export const fixtureShared = "30000000-0000-4000-8000-000000000001";
export const fixturePrivate = "30000000-0000-4000-8000-000000000002";
const timestamp = "2026-10-28T00:00:00Z";
export type ChatScenario =
  | "normal"
  | "policy_lost"
  | "reconnect"
  | "expired"
  | "gap"
  | "waiting"
  | "failed"
  | "unknown"
  | "tools"
  | "revoked"
  | "fast_tools"
  | "fast_unknown"
  | "disconnect_tools"
  | "read_failure"
  | "message_failure";
export function createChatFixture(
  onWaiting?: (
    tenant: string,
    uid: string,
    proposal: string,
    run: string,
  ) => { expires_at: string },
) {
  const controls = {
    scenario: "normal" as ChatScenario,
    commandId: undefined as string | undefined,
    delay: 600,
    requests: [] as { path: string; cursor: string | null; authorization: boolean }[],
  };
  const conversations = new Map<string, Conversation[]>();
  const messages = new Map<string, Message[]>();
  const runs = new Map<
    string,
    {
      run: Run;
      events: RunEvent[];
      connections: number;
      final: Message;
      scenario: ChatScenario;
      revoked: boolean;
      readFailed: boolean;
      messageFailed: boolean;
    }
  >();
  const receipts = new Map<string, unknown>();
  const list = (tenant: string, uid: string) => {
    const key = `${tenant}:${uid}`;
    let values = conversations.get(key);
    if (!values) {
      const base = {
        tenant_id: tenant,
        version: 1,
        created_at: timestamp,
        updated_at: timestamp,
        agent_id: fixtureAgent,
      };
      values = [
        {
          ...base,
          id: fixtureShared,
          title: "みんなの会話",
          visibility: "shared",
          display_policy: "shared_display",
        },
        {
          ...base,
          id: fixturePrivate,
          title: "個人の会話",
          visibility: "private",
          display_policy: "never",
          owner_user_id: uid,
        },
      ];
      conversations.set(key, values);
    }
    return values;
  };
  const handle = async (request: Request, uid: string): Promise<Response | undefined> => {
    const url = new URL(request.url),
      parts = url.pathname.split("/");
    const tenant = parts[3],
      resource = parts[4],
      id = parts[5];
    if (!tenant || !["agents", "conversations", "runs"].includes(resource ?? "")) return undefined;
    controls.requests.push({
      path: url.pathname,
      cursor: request.headers.get("Last-Event-ID"),
      authorization: request.headers.has("Authorization"),
    });
    const values = list(tenant, uid);
    if (resource === "agents")
      return Response.json({
        items: [
          {
            id: fixtureAgent,
            tenant_id: tenant,
            version: 1,
            created_at: timestamp,
            updated_at: timestamp,
            name: "こもれび",
            persona: "落ち着いた日本語で話します。",
            allowed_tools: [],
          },
        ],
        next_cursor: null,
      });
    const receiptKey = `${tenant}:${uid}:${url.pathname}:${request.headers.get("Idempotency-Key")}`;
    if (resource === "conversations" && !id) {
      if (request.method === "GET") return Response.json({ items: values, next_cursor: null });
      const body = await request.json();
      let conversation = receipts.get(receiptKey) as Conversation | undefined;
      if (!conversation) {
        conversation = {
          ...body,
          id: crypto.randomUUID(),
          tenant_id: tenant,
          version: 1,
          created_at: timestamp,
          updated_at: timestamp,
          ...(body.visibility === "private" ? { owner_user_id: uid } : {}),
        };
        if (!conversation) throw new Error("Fixture conversation missing");
        values.push(conversation);
        receipts.set(receiptKey, conversation);
      }
      return Response.json(conversation, { status: 201 });
    }
    const storage = `${tenant}:${uid}:${id}`;
    if (resource === "conversations" && id) {
      const conversation = values.find((item) => item.id === id);
      if (!conversation) return Response.json({}, { status: 404 });
      if (!parts[6] && request.method === "PATCH") {
        if (!receipts.has(receiptKey)) {
          if (request.headers.get("If-Match") !== `"${conversation.version}"`)
            return Response.json({}, { status: 412 });
          const body = await request.json();
          conversation.display_policy = body.display_policy;
          conversation.version++;
          receipts.set(receiptKey, true);
          if (controls.scenario === "policy_lost")
            throw new Error("Committed policy response lost");
        }
        return Response.json(conversation, { headers: { ETag: `"${conversation.version}"` } });
      }
      if (!parts[6]) return Response.json(conversation);
      if (parts[6] === "messages") {
        const record = [...runs.values()].find(
          (item) =>
            item.run.conversation_id === id &&
            item.scenario === "message_failure" &&
            item.connections === 1 &&
            !item.messageFailed,
        );
        if (record) {
          record.messageFailed = true;
          return Response.json({}, { status: 503 });
        }
        return Response.json({ items: messages.get(storage) ?? [], next_cursor: null });
      }
      if (parts[6] === "turns") {
        const prior = receipts.get(receiptKey);
        if (prior) return Response.json(prior, { status: 202 });
        const body = await request.json(),
          runId = crypto.randomUUID();
        const finalId = crypto.randomUUID(),
          toolId = crypto.randomUUID();
        const events: RunEvent[] = [];
        const event = (type: RunEvent["type"], payload: RunEvent["payload"]) =>
          events.push({
            run_id: runId,
            seq: events.length + 1,
            type,
            occurred_at: timestamp,
            payload,
          });
        event("run.started", {});
        event("text.delta", { text: "お手伝いします。" });
        if (body.output_mode === "speech")
          event("speech.ready", {
            speech_id: crypto.randomUUID(),
            sentence_index: 0,
            expires_at: new Date(Date.now() + 120_000).toISOString(),
          });
        const scenario = controls.scenario;
        if (
          ["tools", "unknown", "fast_tools", "fast_unknown", "disconnect_tools"].includes(scenario)
        ) {
          event("tool.status", {
            tool_call_id: toolId,
            tool_name: "home_assistant.set_state",
            status: "pending",
            ...(controls.commandId ? { command_id: controls.commandId } : {}),
          });
          event("tool.status", {
            tool_call_id: toolId,
            tool_name: "home_assistant.set_state",
            status: "running",
          });
          event("tool.status", {
            tool_call_id: toolId,
            tool_name: "home_assistant.set_state",
            status: ["unknown", "fast_unknown"].includes(scenario)
              ? "outcome_unknown"
              : "succeeded",
          });
          if (["tools", "fast_tools", "disconnect_tools"].includes(scenario))
            event("tool.status", {
              tool_call_id: crypto.randomUUID(),
              tool_name: "home_assistant.read_state",
              status: "failed",
            });
        }
        const waitingId = crypto.randomUUID();
        if (scenario === "waiting")
          event("confirmation.required", {
            proposal_id: waitingId,
            kind: "calendar_write",
            expires_at:
              onWaiting?.(tenant, uid, waitingId, runId).expires_at ??
              new Date(Date.now() + 300000).toISOString(),
          });
        else if (["failed", "unknown", "fast_unknown"].includes(scenario))
          event("run.failed", {
            error_code: ["unknown", "fast_unknown"].includes(scenario)
              ? "outcome_unknown"
              : "provider_failed",
            retryable: false,
          });
        else {
          event("text.delta", { text: "一緒に進めましょう。" });
          event("run.completed", { message_id: finalId });
        }
        const user: Message = {
          id: crypto.randomUUID(),
          role: "user",
          text: body.text ?? "合成の音声入力",
          run_id: runId,
          created_at: timestamp,
          source_refs: [],
        };
        const final: Message = {
          id: finalId,
          role: "assistant",
          text: "お手伝いします。一緒に進めましょう。",
          run_id: runId,
          created_at: timestamp,
          source_refs: [],
        };
        messages.set(storage, [...(messages.get(storage) ?? []), user]);
        runs.set(`${tenant}:${uid}:${runId}`, {
          run: {
            run_id: runId,
            conversation_id: id,
            status: "running",
            version: 1,
            last_event_seq: 0,
            created_at: timestamp,
            ...(scenario === "waiting" ? { waiting_proposal_id: waitingId } : {}),
          },
          events,
          connections: 0,
          final,
          scenario,
          revoked: false,
          readFailed: false,
          messageFailed: false,
        });
        // The worker can finish before any status read or SSE connection.
        // Persist independently of stream consumption to exercise that real timing.
        const record = runs.get(`${tenant}:${uid}:${runId}`);
        if (record && ["fast_tools", "fast_unknown"].includes(scenario)) {
          record.run.last_event_seq = events.length;
          record.run.status = scenario === "fast_unknown" ? "failed" : "completed";
          if (scenario === "fast_unknown") record.run.error_code = "outcome_unknown";
          else {
            record.run.final_message_id = final.id;
            messages.set(storage, [...(messages.get(storage) ?? []), final]);
          }
        }
        const accepted = {
          run_id: runId,
          status: "queued",
          events_path: `/v1/tenants/${tenant}/runs/${runId}/events`,
          status_path: `/v1/tenants/${tenant}/runs/${runId}`,
        };
        receipts.set(receiptKey, accepted);
        return Response.json(accepted, { status: 202 });
      }
    }
    if (resource === "runs" && id) {
      const runId = id.replace(":cancel", ""),
        record = runs.get(`${tenant}:${uid}:${runId}`);
      if (!record) return Response.json({}, { status: 404 });
      if (record.revoked) return Response.json({}, { status: 403 });
      const saveFinal = () => {
        record.run = {
          ...record.run,
          status: "completed",
          final_message_id: record.final.id,
          last_event_seq: record.events.length,
        };
        const key = `${tenant}:${uid}:${record.run.conversation_id}`;
        if (!(messages.get(key) ?? []).some((item) => item.id === record.final.id))
          messages.set(key, [...(messages.get(key) ?? []), record.final]);
      };
      if (id.endsWith(":cancel")) {
        if (!["completed", "failed", "cancelled"].includes(record.run.status)) {
          record.run.status = "cancelled";
          record.run.last_event_seq = record.events.length + 1;
          record.events.push({
            run_id: runId,
            seq: record.run.last_event_seq,
            type: "run.cancelled",
            occurred_at: timestamp,
            payload: { external_action_may_have_completed: false },
          });
        }
        return Response.json(record.run);
      }
      if (!parts[6]) {
        if (record.scenario === "read_failure" && record.connections === 1 && !record.readFailed) {
          record.readFailed = true;
          return Response.json({}, { status: 503 });
        }
        return Response.json(record.run);
      }
      if (record.run.status === "cancelled") return new Response("");
      record.connections++;
      if (record.scenario === "expired" && record.connections > 1) {
        saveFinal();
        return Response.json({}, { status: 410 });
      }
      const cursor = Number(request.headers.get("Last-Event-ID") ?? 0);
      let events = record.events.filter((event) => event.seq > cursor);
      if (
        [
          "reconnect",
          "expired",
          "revoked",
          "disconnect_tools",
          "read_failure",
          "message_failure",
        ].includes(record.scenario) &&
        record.connections === 1
      )
        events = events.slice(0, 2);
      if (
        ["reconnect", "read_failure", "message_failure"].includes(record.scenario) &&
        record.connections > 1
      )
        events = record.events.filter((event) => event.seq >= cursor);
      if (record.scenario === "gap") {
        saveFinal();
        events = events.filter((event) => event.seq !== 2);
      }
      const encoder = new TextEncoder();
      let timer: ReturnType<typeof setTimeout> | undefined;
      let position = 0,
        ended = false;
      const close = () => {
        ended = true;
        clearTimeout(timer);
      };
      const body = new ReadableStream<Uint8Array>({
        start(stream) {
          const emit = () => {
            if (ended) return;
            if (request.signal.aborted || position >= events.length) {
              close();
              request.signal.removeEventListener("abort", aborted);
              if (record.scenario === "revoked") record.revoked = true;
              if (record.scenario === "disconnect_tools" && record.connections === 1) saveFinal();
              stream.close();
              return;
            }
            const event = events[position++];
            if (!event) throw new Error("Fixture event missing");
            record.run.last_event_seq = Math.max(record.run.last_event_seq, event.seq);
            if (event.type === "run.completed") saveFinal();
            if (event.type === "run.failed") {
              record.run.status = "failed";
              record.run.error_code = String(event.payload.error_code);
            }
            if (event.type === "confirmation.required") record.run.status = "waiting_confirmation";
            stream.enqueue(
              encoder.encode(
                `: heartbeat\n\nid: ${event.seq}\nevent: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`,
              ),
            );
            timer = setTimeout(emit, controls.delay);
          };
          const aborted = () => {
            if (!ended) {
              close();
              stream.close();
            }
          };
          request.signal.addEventListener("abort", aborted, { once: true });
          timer = setTimeout(emit, controls.delay);
        },
        cancel() {
          close();
        },
      });
      return new Response(body, { headers: { "Content-Type": "text/event-stream" } });
    }
    return Response.json({}, { status: 404 });
  };
  return { handle, controls };
}
