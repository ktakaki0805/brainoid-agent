import type {
  CalendarEvent,
  CalendarProposal,
  CalendarProposalCreate,
  CalendarTime,
  Integration,
  Operation,
} from "@brainoid-internal/api-client";
import { isId } from "../chat/events.ts";
import type { SessionContext } from "../session.ts";
import { validTimestamp, validTimezone } from "../tasks/controller.ts";

export interface CalendarOAuth {
  open(
    onReturn: () => void,
    onFinish: () => void,
  ): {
    launch(url: string): Promise<void>;
    dispose(): void;
  };
}
export const opaque = (v: unknown): v is string =>
  typeof v === "string" &&
  v.length > 0 &&
  [...v].length <= 1024 &&
  [...v].every((c) => c.charCodeAt(0) >= 32 && c.charCodeAt(0) !== 127);
export const calendarIds = (v: unknown): v is string[] =>
  Array.isArray(v) && v.length <= 10 && v.every(opaque) && new Set(v).size === v.length;
const fail = (): never => {
  throw new Error("Invalid calendar response");
};
export function validDate(v: unknown): v is string {
  return (
    typeof v === "string" &&
    /^\d{4}-\d{2}-\d{2}$/.test(v) &&
    Number.isFinite(Date.parse(v)) &&
    new Date(v).toISOString().slice(0, 10) === v
  );
}
export function validTime(v: CalendarTime): boolean {
  return (
    !!v &&
    ("date" in v
      ? Object.keys(v).length === 1 && validDate(v.date)
      : Object.keys(v).length === 2 && validTimestamp(v.date_time) && validTimezone(v.timezone))
  );
}
export function ordered(start: CalendarTime, end: CalendarTime): boolean {
  if (!validTime(start) || !validTime(end)) return false;
  return "date" in start && "date" in end
    ? start.date < end.date
    : "date_time" in start &&
        "date_time" in end &&
        Date.parse(start.date_time) < Date.parse(end.date_time);
}
export function validChange(v: CalendarProposalCreate): boolean {
  if (!v || !opaque(v.calendar_id) || !["create", "update", "delete"].includes(v.action))
    return false;
  const keys = [
    "action",
    "calendar_id",
    ...(v.action === "create" ? [] : ["event_id", "provider_etag"]),
    ...(v.action === "delete" ? [] : ["summary", "start", "end", "description"]),
  ];
  if (Object.keys(v).some((k) => !keys.includes(k))) return false;
  if (
    v.action !== "create" &&
    (!opaque(v.event_id) ||
      typeof v.provider_etag !== "string" ||
      !v.provider_etag.length ||
      v.provider_etag.length > 256)
  )
    return false;
  return (
    v.action === "delete" ||
    (typeof v.summary === "string" &&
      [...v.summary].length > 0 &&
      [...v.summary].length <= 200 &&
      ordered(v.start, v.end) &&
      (v.description === undefined ||
        (typeof v.description === "string" && [...v.description].length <= 4000)))
  );
}
export function integration(v: Integration, context: SessionContext): Integration {
  if (
    !v ||
    !isId(v.id) ||
    v.tenant_id !== context.tenantId ||
    v.owner_user_id !== context.uid ||
    v.provider !== "google_calendar" ||
    !Number.isSafeInteger(v.version) ||
    v.version < 1 ||
    !["active", "reauth_required", "revoked"].includes(v.status) ||
    !calendarIds(v.allowed_calendar_ids) ||
    !validTimestamp(v.created_at) ||
    !validTimestamp(v.updated_at) ||
    (v.cleanup_status !== undefined &&
      !["not_requested", "pending", "complete"].includes(v.cleanup_status))
  )
    fail();
  return structuredClone(v);
}
export function event(v: CalendarEvent, calendar: string): CalendarEvent {
  if (
    !v ||
    !opaque(v.id) ||
    v.calendar_id !== calendar ||
    typeof v.summary !== "string" ||
    !ordered(v.start, v.end) ||
    typeof v.etag !== "string" ||
    !v.etag ||
    v.etag.length > 256 ||
    (v.fetched_at !== undefined && !validTimestamp(v.fetched_at)) ||
    (v.description !== undefined && typeof v.description !== "string") ||
    "status" in v
  )
    fail();
  return structuredClone(v);
}
export function proposal(v: CalendarProposal, expected?: string): CalendarProposal {
  if (
    !v ||
    !isId(v.proposal_id) ||
    (expected !== undefined && v.proposal_id !== expected) ||
    !isId(v.integration_id) ||
    !validChange(v.normalized_request) ||
    v.action !== v.normalized_request.action ||
    !/^[a-f0-9]{64}$/.test(v.args_hash) ||
    !validTimestamp(v.expires_at) ||
    ![
      "pending",
      "confirmed",
      "executing",
      "succeeded",
      "failed",
      "outcome_unknown",
      "expired",
      "declined",
      "cancelled",
    ].includes(v.status) ||
    (v.operation_id !== undefined && v.operation_id !== v.proposal_id) ||
    [v.operation_id, v.run_id, v.tool_call_id, v.checkpoint_id].some(
      (id) => id !== undefined && !isId(id),
    )
  )
    fail();
  return structuredClone(v);
}
export function operation(v: Operation, expected?: string): Operation {
  if (
    !v ||
    !isId(v.operation_id) ||
    (expected !== undefined && v.operation_id !== expected) ||
    v.kind !== "calendar_write" ||
    !["pending", "running", "succeeded", "failed", "outcome_unknown"].includes(v.status) ||
    !validTimestamp(v.created_at) ||
    !validTimestamp(v.updated_at)
  )
    fail();
  return structuredClone(v);
}
export function timeLabel(v: CalendarTime): string {
  if ("date" in v) return v.date;
  return `${new Intl.DateTimeFormat("ja-JP", { timeZone: v.timezone, dateStyle: "medium", timeStyle: "long" }).format(new Date(v.date_time))} (${v.timezone}) · ${v.date_time}`;
}
