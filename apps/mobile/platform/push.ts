import type { FirebaseMessagingPlugin } from "@capacitor-firebase/messaging";
export type PushPermission = "prompt" | "prompt-with-rationale" | "granted" | "denied";
export interface PushLease {
  permission(request: boolean): Promise<PushPermission>;
  token(): Promise<string>;
  close(): Promise<void>;
}
/** Serializes native token operations across identity generations, including late deletes. */
export function nativePush(plugin: FirebaseMessagingPlugin) {
  let queue: Promise<unknown> = Promise.resolve();
  function serial<T>(work: () => Promise<T>): Promise<T> {
    const result = queue.then(work);
    queue = result.catch(() => {});
    return result;
  }
  return {
    open(
      onToken: (token: string) => void,
      onOpen: (data: unknown) => void,
      onReceived: () => void,
    ): PushLease {
      let active = true,
        enabled = false;
      let latest: string | undefined,
        received = 0;
      const assert = () => {
        if (!active) throw new Error("Push lease closed");
      };
      const listen = <T>(work: () => Promise<T>) => {
        try {
          return work();
        } catch (error) {
          return Promise.reject(error);
        }
      };
      const handles = [
        listen(() =>
          plugin.addListener("tokenReceived", (event) => {
            if (active && enabled) {
              latest = event.token;
              received++;
              onToken(event.token);
            }
          }),
        ),
        listen(() =>
          plugin.addListener("notificationActionPerformed", (event) => {
            if (active) onOpen(event.notification.data);
          }),
        ),
        listen(() =>
          plugin.addListener("notificationReceived", () => {
            if (active) onReceived();
          }),
        ),
      ];
      // Preserve rejection as an initialization failure without unhandled promises.
      const ready = Promise.all(handles);
      void ready.catch(() => {});
      let closing: Promise<void> | undefined;
      return {
        permission: (request) =>
          serial(async () => {
            assert();
            await ready;
            assert();
            const result = await (request
              ? plugin.requestPermissions()
              : plugin.checkPermissions());
            assert();
            return result.receive;
          }),
        token: () =>
          serial(async () => {
            assert();
            await ready;
            assert();
            enabled = true;
            const before = received;
            const result = await plugin.getToken();
            assert();
            const token = received !== before ? latest : result.token;
            if (!token || token.length > 4096) throw new Error("Invalid token");
            return token;
          }),
        close() {
          if (closing) return closing;
          active = false;
          enabled = false;
          for (const handle of handles) void handle.then((value) => value.remove()).catch(() => {});
          closing = serial(() => plugin.deleteToken());
          void closing.catch(() => {});
          return closing;
        },
      };
    },
  };
}
export type NativePush = ReturnType<typeof nativePush>;
