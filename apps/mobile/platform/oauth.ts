import type { AppPlugin } from "@capacitor/app";
import type { BrowserPlugin } from "@capacitor/browser";
import type { CalendarOAuth } from "@brainoid-internal/ui/calendar";

/** Deployment configuration, never a return URL supplied by a callback or an API response. */
export function fixedReturnURL(value: unknown): string | undefined {
  if (typeof value !== "string" || !value || value.length > 2048) return undefined;
  try {
    const url = new URL(value);
    if (
      url.protocol !== "https:" ||
      url.username ||
      url.password ||
      url.search ||
      url.hash ||
      url.port ||
      url.href !== value ||
      url.hostname === "localhost" ||
      !url.hostname.includes(".")
    )
      return undefined;
    return url.href;
  } catch {
    return undefined;
  }
}
export function authorizationURL(value: string): boolean {
  try {
    const url = new URL(value);
    return (
      url.origin === "https://accounts.google.com" &&
      url.pathname === "/o/oauth2/v2/auth" &&
      !url.username &&
      !url.password &&
      !url.hash
    );
  } catch {
    return false;
  }
}
/** Concrete Capacitor system-browser/App adapter. Each listener and browser operation has an owner. */
export function nativeCalendarOAuth(
  browser: BrowserPlugin,
  app: AppPlugin,
  configuredReturn: string,
): CalendarOAuth {
  const target = fixedReturnURL(configuredReturn);
  if (!target) throw new Error("Calendar return URL is not configured");
  let queue: Promise<unknown> = Promise.resolve();
  let owner: object | undefined;
  const serial = <T>(work: () => Promise<T>) => {
    const next = queue.then(work);
    queue = next.catch(() => {});
    return next;
  };
  return {
    open(onReturn, onFinish) {
      const identity = {};
      let active = true,
        launched = false,
        consumed = false;
      const listen = <T>(work: () => Promise<T>) => {
        try {
          return work();
        } catch (error) {
          return Promise.reject(error);
        }
      };
      const handles = [
        listen(() =>
          app.addListener("appUrlOpen", ({ url }) => {
            if (active && launched && owner === identity && !consumed && url === target) {
              consumed = true;
              onReturn();
            }
          }),
        ),
        listen(() =>
          browser.addListener("browserFinished", () => {
            if (active && launched && owner === identity && !consumed) {
              consumed = true;
              onFinish();
            }
          }),
        ),
      ];
      const ready = Promise.all(handles);
      void ready.catch(() => {});
      // Cold launch has no live, identity-bound authorization. Never retain or act on its URL.
      void listen(() => app.getLaunchUrl()).then(
        () => {},
        () => {},
      );
      const close = () =>
        serial(async () => {
          if (owner !== identity) return;
          owner = undefined;
          try {
            await browser.close();
          } catch {
            /* Android close is unavailable; listeners are already fenced. */
          }
        });
      return {
        launch: (url) =>
          serial(async () => {
            await ready;
            if (!active || !authorizationURL(url)) throw new Error("Calendar browser unavailable");
            owner = identity;
            launched = true;
            try {
              await browser.open({ url, presentationStyle: "fullscreen" });
            } catch {
              launched = false;
              throw new Error("Calendar browser could not open");
            }
            if (!active) throw new Error("Calendar browser lease ended");
          }),
        dispose() {
          if (!active) return;
          active = false;
          launched = false;
          for (const handle of handles) void handle.then((h) => h.remove()).catch(() => {});
          void close();
        },
      };
    },
  };
}
