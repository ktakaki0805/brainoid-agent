import type { Reminder } from "@brainoid-internal/api-client";
import type { SessionContext } from "../session.ts";

type Observer = (value: Reminder) => void;
const observers = new WeakMap<SessionContext, Set<Observer>>();
/** Synchronous canonical observations, scoped to this identity/tenant generation; no cache. */
export function observeReminders(context: SessionContext, observer: Observer) {
  context.assertCurrent();
  let listeners = observers.get(context);
  if (!listeners) {
    listeners = new Set();
    observers.set(context, listeners);
  }
  listeners.add(observer);
  return () => {
    listeners.delete(observer);
    if (!listeners.size) observers.delete(context);
  };
}
export function publishReminder(context: SessionContext, value: Reminder) {
  context.assertCurrent();
  for (const observer of observers.get(context) ?? []) observer(value);
}
