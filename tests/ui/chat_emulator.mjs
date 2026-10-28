import assert from "node:assert/strict";
import {
  cancelRun,
  createTurn,
  getRun,
  listMessages,
  RunEventsFailure,
  streamRunEvents,
} from "../../packages/api-client/src/index.ts";
import { SessionContext } from "../../packages/ui/src/session.ts";
import { ChatController } from "../../packages/ui/src/chat/controller.ts";
import { initialEvents, reduceEvent, validateRunEvent } from "../../packages/ui/src/chat/events.ts";
const baseUrl = process.env.UI_API_URL;
if (!baseUrl || new URL(baseUrl).hostname !== "127.0.0.1")
  throw Error("Explicit loopback API required");
const tenant = process.env.UI_TENANT,
  runId = process.env.UI_RUN,
  expired = process.env.UI_EXPIRED_RUN,
  conversation = process.env.UI_CONVERSATION;
assert.ok(tenant && runId && expired && conversation);
const context = new SessionContext({
  uid: "demo-a-owner",
  tenantId: tenant,
  generation: 1,
  baseUrl,
  current: () => true,
  token: async () => "synthetic-reader-fixture",
  transport: fetch,
  denied: () => {
    throw Error("Unexpected denial");
  },
});
const chat = new ChatController(context, "mobile");
try {
  let state = initialEvents(runId);
  await context.run(async (client, signal) => {
    const first = await streamRunEvents({
      client,
      signal,
      path: { tenant_id: tenant, run_id: runId },
      onSseEvent: (frame) => {
        state = reduceEvent(state, validateRunEvent(frame.data, frame));
      },
    });
    for await (const _event of first.stream) break;
    assert.equal(state.seq, 1);
    const resumed = await streamRunEvents({
      client,
      signal,
      path: { tenant_id: tenant, run_id: runId },
      headers: { "Last-Event-ID": String(state.seq) },
      onSseEvent: (frame) => {
        state = reduceEvent(state, validateRunEvent(frame.data, frame));
      },
    });
    for await (const _event of resumed.stream) {
    }
    assert.equal(state.terminal, "completed");
    const run = (await getRun({ client, signal, path: { tenant_id: tenant, run_id: runId } })).data;
    const messages = (
      await listMessages({
        client,
        signal,
        path: { tenant_id: tenant, conversation_id: conversation },
      })
    ).data;
    assert.equal(
      messages.items.find((message) => message.id === run.final_message_id).text,
      state.text,
    );
    const gone = await streamRunEvents({
      client,
      signal,
      path: { tenant_id: tenant, run_id: expired },
    });
    await assert.rejects(
      async () => {
        for await (const _event of gone.stream) {
        }
      },
      (error) => error instanceof RunEventsFailure && error.status === 410,
    );
    const accepted = (
      await createTurn({
        client,
        signal,
        path: { tenant_id: tenant, conversation_id: conversation },
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: { text: "Synthetic queued cancellation", output_mode: "text" },
      })
    ).data;
    const cancelled = (
      await cancelRun({
        client,
        signal,
        path: { tenant_id: tenant, run_id: accepted.run_id },
        headers: { "Idempotency-Key": crypto.randomUUID() },
      })
    ).data;
    assert.equal(cancelled.status, "cancelled");
  });
  await chat.load();
  await chat.select(conversation);
  assert.equal(chat.snapshot().run.status, "cancelled");
  assert.equal(
    chat.snapshot().messages.filter((message) => message.role === "assistant").length,
    1,
  );
  console.log(
    "UI chat real HTTP + official Firestore: schema, authenticated SDK header, disconnect/resume, final identity/text, typed 410, queued cancel and controller canonical DTO passed",
  );
} finally {
  chat.dispose();
  context.dispose();
}
