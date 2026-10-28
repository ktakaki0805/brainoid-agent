import { type RunEvent, RunEventCursor } from "@brainoid-internal/api-client";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export const isId = (value: unknown): value is string =>
  typeof value === "string" && uuid.test(value);
const object = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);
const date = (value: unknown) => {
  if (typeof value !== "string") return false;
  const match =
    /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(Z|[+-]\d{2}:\d{2})$/i.exec(value);
  if (!match || !Number.isFinite(Date.parse(value))) return false;
  const year = Number(match[1]),
    month = Number(match[2]),
    day = Number(match[3]);
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  return (
    month >= 1 &&
    month <= 12 &&
    day >= 1 &&
    day <= (days[month - 1] ?? 0) &&
    Number(match[4]) <= 23 &&
    Number(match[5]) <= 59 &&
    Number(match[6]) <= 59
  );
};
const string = (value: unknown, max: number, min = 0) =>
  typeof value === "string" && [...value].length >= min && [...value].length <= max;
const integer = (value: unknown, min: number) =>
  Number.isSafeInteger(value) && Number(value) >= min;
const keys = (value: Record<string, unknown>, required: string[], optional: string[] = []) =>
  required.every((key) => Object.hasOwn(value, key)) &&
  Object.keys(value).every((key) => required.includes(key) || optional.includes(key));

/** Exact public run-events.schema.json boundary; payloads never come from model text. */
export function validateRunEvent(value: unknown, wire: { id?: string; event?: string }): RunEvent {
  if (
    !object(value) ||
    !keys(value, ["run_id", "seq", "type", "occurred_at", "payload"]) ||
    !isId(value.run_id) ||
    !integer(value.seq, 1) ||
    !date(value.occurred_at) ||
    wire.id !== String(value.seq) ||
    wire.event !== value.type ||
    !object(value.payload)
  )
    throw new Error("Invalid run event envelope");
  const p = value.payload;
  let valid = false;
  switch (value.type) {
    case "run.started":
      valid = keys(p, []);
      break;
    case "text.delta":
      valid = keys(p, ["text"]) && string(p.text, 2000, 1);
      break;
    case "speech.ready":
      valid =
        keys(p, ["speech_id", "sentence_index", "expires_at"]) &&
        isId(p.speech_id) &&
        integer(p.sentence_index, 0) &&
        date(p.expires_at);
      break;
    case "tool.status":
      valid =
        keys(p, ["tool_call_id", "tool_name", "status"], ["command_id"]) &&
        isId(p.tool_call_id) &&
        string(p.tool_name, 80) &&
        typeof p.status === "string" &&
        ["pending", "running", "succeeded", "failed", "outcome_unknown"].includes(p.status) &&
        (p.command_id === undefined || isId(p.command_id));
      break;
    case "confirmation.required":
      valid =
        keys(p, ["proposal_id", "kind", "expires_at"]) &&
        isId(p.proposal_id) &&
        p.kind === "calendar_write" &&
        date(p.expires_at);
      break;
    case "run.completed":
      valid = keys(p, ["message_id"]) && isId(p.message_id);
      break;
    case "run.failed":
      valid =
        keys(p, ["error_code", "retryable"]) &&
        string(p.error_code, 80) &&
        typeof p.retryable === "boolean";
      break;
    case "run.cancelled":
      valid =
        keys(p, ["external_action_may_have_completed"]) &&
        typeof p.external_action_may_have_completed === "boolean";
      break;
  }
  if (!valid) throw new Error("Invalid run event payload");
  return value as unknown as RunEvent;
}
export type CharacterState = "idle" | "thinking" | "confirmation" | "speaking" | "error";
export type ToolStatus = "pending" | "running" | "succeeded" | "failed" | "outcome_unknown";
export interface ToolCard {
  id: string;
  name: string;
  status: ToolStatus;
  commandId?: string;
}
export interface EventState {
  runId: string;
  seq: number;
  text: string;
  character: CharacterState;
  tools: readonly ToolCard[];
  terminal?: "completed" | "failed" | "cancelled";
  finalMessageId?: string;
  externalActionMayHaveCompleted?: boolean;
}
export const initialEvents = (runId: string): EventState => ({
  runId,
  seq: 0,
  text: "",
  character: "thinking",
  tools: [],
});
export function reduceEvent(state: EventState, event: RunEvent): EventState {
  let next = state;
  new RunEventCursor(state.runId, state.seq).apply(event, () => {
    if (state.terminal) throw new Error("Event after terminal state");
    const p = event.payload;
    next = { ...state, seq: event.seq };
    switch (event.type) {
      case "run.started":
        next.character = "thinking";
        break;
      case "text.delta":
        next.text += String(p.text);
        if ([...next.text].length > 8000) throw new Error("Run text exceeds limit");
        next.character = "speaking";
        break;
      // Speech availability is not playback; no audio is fetched or played here.
      case "speech.ready":
        break;
      case "confirmation.required":
        next.character = "confirmation";
        break;
      case "tool.status": {
        const card: ToolCard = {
          id: String(p.tool_call_id),
          name: String(p.tool_name),
          status: p.status as ToolStatus,
          ...(p.command_id ? { commandId: String(p.command_id) } : {}),
        };
        next.tools = [...state.tools.filter((tool) => tool.id !== card.id), card];
        break;
      }
      case "run.completed":
        next.terminal = "completed";
        next.finalMessageId = String(p.message_id);
        next.character = "thinking";
        break;
      case "run.failed":
        next.terminal = "failed";
        next.character = "error";
        next.text = "";
        break;
      case "run.cancelled":
        next.terminal = "cancelled";
        next.character = "idle";
        next.text = "";
        next.externalActionMayHaveCompleted = Boolean(p.external_action_may_have_completed);
        break;
    }
  });
  return next;
}
