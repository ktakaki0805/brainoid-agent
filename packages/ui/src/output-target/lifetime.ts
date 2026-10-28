import type { PrivateSession } from "@brainoid-internal/api-client";
import { isId, isTimestamp } from "../chat/events.ts";

export const sessionScopes = [
  "conversation",
  "memory.read",
  "memory.write",
  "notes",
  "tasks",
  "reminders",
  "calendar.read",
  "calendar.propose",
  "home.control",
] as const;
export interface Clock {
  now(): number;
  monotonic(): number;
}
export const systemClock: Clock = { now: () => Date.now(), monotonic: () => performance.now() };
export function privateSession(value: PrivateSession): PrivateSession {
  const created = Date.parse(value.created_at),
    expires = Date.parse(value.expires_at),
    idle = Date.parse(value.idle_expires_at),
    updated = Date.parse(value.updated_at);
  if (
    Object.keys(value).some(
      (key) =>
        ![
          "id",
          "tenant_id",
          "device_id",
          "user_id",
          "version",
          "status",
          "allowed_scopes",
          "created_at",
          "updated_at",
          "expires_at",
          "idle_expires_at",
        ].includes(key),
    ) ||
    ![value.created_at, value.updated_at, value.expires_at, value.idle_expires_at].every(
      isTimestamp,
    ) ||
    !isId(value.id) ||
    !isId(value.tenant_id) ||
    !isId(value.device_id) ||
    typeof value.user_id !== "string" ||
    !value.user_id ||
    value.user_id.length > 128 ||
    !Number.isSafeInteger(value.version) ||
    value.version < 1 ||
    !["active", "ended", "expired"].includes(value.status) ||
    !Number.isFinite(created) ||
    !Number.isFinite(updated) ||
    updated < created ||
    expires !== created + 300_000 ||
    !Number.isFinite(idle) ||
    idle > expires ||
    idle < created ||
    !Array.isArray(value.allowed_scopes) ||
    value.allowed_scopes.length < 1 ||
    value.allowed_scopes.length > 9 ||
    new Set(value.allowed_scopes).size !== value.allowed_scopes.length ||
    value.allowed_scopes.some((scope) => !(sessionScopes as readonly string[]).includes(scope))
  )
    throw new Error("Invalid private session");
  return structuredClone(value);
}
export function sessionBinding(value: PrivateSession) {
  return JSON.stringify([
    value.id,
    value.tenant_id,
    value.device_id,
    value.user_id,
    value.version,
    value.created_at,
    value.expires_at,
    [...value.allowed_scopes].sort(),
  ]);
}

/** In-memory text, pending search/TTS and playback handles share the same fence.
 * No audio implementation is supplied here. Every future adapter must own its cleanup
 * and check the captured fence immediately before publishing or starting playback. */
export class OutputLifetime {
  private generation = 0;
  private cleanups = new Set<() => void>();
  private value: PrivateSession | undefined;
  private wall = 0;
  private mono = 0;
  private lastWall = 0;
  private lastMono = 0;
  private binding = "";
  private sessionId = "";
  private clockFailed = false;
  private expiredBinding = false;
  readonly clock: Clock;
  private readonly expired: () => void;
  constructor(clock: Clock = systemClock, expired = () => {}) {
    this.clock = clock;
    this.expired = expired;
  }
  get session() {
    this.tick();
    return this.value;
  }
  accept(value: PrivateSession) {
    const next = privateSession(value);
    this.tick();
    const binding = sessionBinding(next);
    if (this.binding) this.elapsedNow();
    if (next.id === this.sessionId && binding !== this.binding) {
      this.expiredBinding = true;
      this.clear();
      return false;
    }
    if (binding !== this.binding) {
      this.clear();
      this.binding = binding;
      this.sessionId = next.id;
      this.wall = this.lastWall = this.clock.now();
      this.mono = this.lastMono = this.clock.monotonic();
      this.expiredBinding = false;
    }
    if (
      next.status !== "active" ||
      this.expiredBinding ||
      this.elapsedNow() >= Math.min(Date.parse(next.expires_at), Date.parse(next.idle_expires_at))
    ) {
      this.expiredBinding = true;
      this.clear();
      return false;
    }
    // Keep the first wall/monotonic anchor across polling and output invalidation.
    // Only the actual idle timestamp can move; the original maximum never does.
    this.value = next;
    return true;
  }
  private elapsedNow() {
    const wall = this.clock.now(),
      mono = this.clock.monotonic();
    if (wall < this.lastWall || mono < this.lastMono) this.clockFailed = true;
    this.lastWall = wall;
    this.lastMono = mono;
    return this.clockFailed ? Infinity : Math.max(wall, this.wall + mono - this.mono);
  }
  tick() {
    if (
      this.value &&
      this.elapsedNow() >=
        Math.min(Date.parse(this.value.expires_at), Date.parse(this.value.idle_expires_at))
    ) {
      this.expiredBinding = true;
      this.clear();
      this.expired();
    }
  }
  capture() {
    this.tick();
    const generation = this.generation;
    return () => {
      this.tick();
      return generation === this.generation;
    };
  }
  own(cleanup: () => void) {
    this.cleanups.add(cleanup);
    return () => {
      this.cleanups.delete(cleanup);
    };
  }
  clear() {
    this.value = undefined;
    this.invalidate();
  }
  invalidate() {
    this.generation++;
    const resources = [...this.cleanups];
    this.cleanups.clear();
    for (const cleanup of resources) {
      try {
        cleanup();
      } catch {
        /* Clear every resource even if one adapter fails. */
      }
    }
  }
}
