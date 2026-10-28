import type { Integration } from "@brainoid-internal/api-client";
import { type SessionContext, StaleContext } from "../session.ts";

export type CalendarAuthorityChange =
  | { kind: "collection" }
  | {
      kind: "integration";
      id: string;
      current: Integration | undefined;
      previous: Integration | undefined;
      incompatible: boolean;
    };
type Observer = (change: CalendarAuthorityChange) => void;
interface Authority {
  sequence: number;
  proposalDependencies: Set<string>;
  records: Map<
    string,
    { value: Integration | undefined; latest: Integration | undefined; revision: number }
  >;
  listeners: Set<Observer>;
}
const contexts = new WeakMap<SessionContext, Authority>();
function authority(context: SessionContext): Authority {
  context.assertCurrent();
  let value = contexts.get(context);
  if (!value) {
    value = {
      sequence: 0,
      proposalDependencies: new Set(),
      records: new Map(),
      listeners: new Set(),
    };
    contexts.set(context, value);
    const owned = value;
    context.own(() => {
      owned.records.clear();
      owned.proposalDependencies.clear();
      owned.listeners.clear();
      contexts.delete(context);
    });
  }
  return value;
}
const stamp = (value: Integration | undefined) =>
  value
    ? JSON.stringify([value.version, value.status, [...(value.allowed_calendar_ids ?? [])].sort()])
    : "removed";
export function observeCalendarAuthority(context: SessionContext, listener: Observer) {
  const value = authority(context);
  value.listeners.add(listener);
  return () => {
    value.listeners.delete(listener);
  };
}
/** Start before I/O. Resolve a proposal's integration only from the authenticated server DTO. */
export function calendarAuthorityGuard(context: SessionContext, id?: string) {
  const value = authority(context),
    before = value.sequence;
  return (resolved = id) => {
    context.assertCurrent();
    if (resolved && (value.records.get(resolved)?.revision ?? 0) > before) throw new StaleContext();
  };
}
export function assertCalendarScope(context: SessionContext, id: string, calendar: string) {
  const record = authority(context).records.get(id);
  if (
    record &&
    (record.value?.status !== "active" || !record.value?.allowed_calendar_ids?.includes(calendar))
  )
    throw new StaleContext();
}
/** Only a validated authenticated proposal/acceptance may register this dependency.
 * This records an ID and observation order, never an Integration DTO or write grant. */
export function observeCalendarProposal(context: SessionContext, id: string) {
  const value = authority(context);
  value.proposalDependencies.add(id);
  value.sequence++;
}
/** Canonical observations only. Failed/unknown mutations must never call this function. */
export function publishCalendarAuthority(
  context: SessionContext,
  id: string,
  current?: Integration,
) {
  const value = authority(context),
    saved = value.records.get(id),
    previous = saved?.value,
    latest = saved?.latest;
  if (
    current &&
    latest &&
    (current.version < latest.version ||
      (current.version === latest.version &&
        latest.status !== "active" &&
        current.status === "active"))
  )
    throw new StaleContext();
  const incompatible = !!saved && stamp(previous) !== stamp(current);
  const revision = !saved || incompatible ? ++value.sequence : saved.revision;
  value.records.set(id, { value: current, latest: current ?? latest, revision });
  for (const listener of value.listeners)
    listener({ kind: "integration", id, current, previous, incompatible });
}
/** A complete list may prove absence only against observations predating its first page. */
export function calendarListPublication(context: SessionContext) {
  const before = authority(context).sequence;
  return (items: readonly Integration[]) => publishCalendarList(context, items, before);
}
function publishCalendarList(
  context: SessionContext,
  items: readonly Integration[],
  before: number,
) {
  const value = authority(context),
    present = new Set(items.map((v) => v.id));
  if (value.sequence !== before) throw new StaleContext();
  // Validate the complete page set against newer observations before publishing any of it.
  for (const current of items) {
    const latest = value.records.get(current.id)?.latest;
    if (
      latest &&
      (current.version < latest.version ||
        (current.version === latest.version &&
          latest.status !== "active" &&
          current.status === "active"))
    )
      throw new StaleContext();
  }
  for (const item of items) publishCalendarAuthority(context, item.id, item);
  for (const id of new Set([...value.records.keys(), ...value.proposalDependencies]))
    if (!present.has(id)) publishCalendarAuthority(context, id);
  // An empty first list is still a canonical collection observation. Pending proposal
  // reads have no integration ID yet; notify their owners so they can abort before parsing.
  value.sequence++;
  for (const listener of value.listeners) listener({ kind: "collection" });
}
