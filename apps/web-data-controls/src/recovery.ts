import {
  DataControlsController,
  unavailableDelivery,
} from "@brainoid-internal/ui/data-controls-controller";
import type { SessionContext, SessionController } from "@brainoid-internal/ui/session";

export const locatorKey = "brainoid-account-operation-v1";
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
type Locator = { version: 1; owner: string; operation: string };
type LocatorStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;
export async function ownerBinding(uid: string): Promise<string> {
  const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(uid));
  return Array.from(new Uint8Array(bytes), (v) => v.toString(16).padStart(2, "0")).join("");
}
/** The hash is only an accidental-account-switch guard, never authentication or anonymity. */
export class AccountRecovery {
  private state: { controls?: DataControlsController; loading: boolean; notice?: string } = {
    loading: false,
  };
  private scope: SessionContext | undefined;
  private listeners = new Set<() => void>();
  private stop: () => void;
  private stopControls: (() => void) | undefined;
  private preserving = false;
  private disposed = false;
  readonly session: SessionController;
  private readonly storage: () => LocatorStorage;
  constructor(session: SessionController, storage: () => LocatorStorage) {
    this.session = session;
    this.storage = storage;
    this.stop = session.subscribe(() => this.sync());
    this.sync();
  }
  snapshot = () => this.state;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  private publish(next: typeof this.state) {
    this.state = next;
    for (const listener of this.listeners) listener();
  }
  private clearLocator() {
    try {
      this.storage().removeItem(locatorKey);
    } catch {
      /* Storage may be disabled. */
    }
  }
  private read(owner: string): string | undefined {
    try {
      const raw = this.storage().getItem(locatorKey);
      if (raw === null) return;
      const value = JSON.parse(raw) as Locator;
      if (
        !value ||
        Object.keys(value).sort().join() !== "operation,owner,version" ||
        value.version !== 1 ||
        value.owner !== owner ||
        typeof value.operation !== "string" ||
        !uuid.test(value.operation)
      ) {
        this.clearLocator();
        return;
      }
      return value.operation;
    } catch {
      this.clearLocator();
      return;
    }
  }
  private sync() {
    if (this.disposed) return;
    const scope = this.session.globalContext();
    if (scope === this.scope) return;
    if (this.scope && !this.preserving) this.clearLocator();
    this.scope = scope;
    this.stopControls?.();
    this.state.controls?.dispose();
    this.publish({ loading: !!scope });
    if (scope) void this.attach(scope);
  }
  private async attach(scope: SessionContext) {
    let owner: string;
    try {
      owner = await ownerBinding(scope.uid);
      scope.assertCurrent();
      if (this.disposed || this.scope !== scope) return;
    } catch {
      return;
    }
    const controls = new DataControlsController(this.session, unavailableDelivery, {
      accountOnly: true,
      requireReauthentication: true,
    });
    const id = this.read(owner);
    let saved: string | undefined;
    this.stopControls = controls.subscribe(() => {
      const operation = controls.snapshot().operation;
      if (
        scope.signal.aborted ||
        this.scope !== scope ||
        !operation ||
        operation.operation_id === saved
      )
        return;
      saved = operation.operation_id;
      try {
        this.storage().setItem(
          locatorKey,
          JSON.stringify({ version: 1, owner, operation: saved } satisfies Locator),
        );
      } catch {
        this.publish({
          ...this.state,
          notice:
            "このブラウザーでは受付番号を保存できません。再読込み後の自動回復は利用できません。",
        });
      }
    });
    // Mark recovery before exposing the form: no fresh deletion request during its GET.
    const recovering = id ? controls.recoverAccount(id) : Promise.resolve();
    this.publish({ controls, loading: false });
    await recovering;
  }
  /** Navigation keeps only the non-credential locator, and erases auth/forms before freezing. */
  suspend() {
    this.preserving = true;
    try {
      this.session.suspend();
    } finally {
      this.preserving = false;
    }
  }
  signOut() {
    this.clearLocator();
    return this.session.signOut();
  }
  dispose() {
    this.disposed = true;
    this.stop();
    this.stopControls?.();
    this.state.controls?.dispose();
    this.publish({ loading: false });
  }
}
