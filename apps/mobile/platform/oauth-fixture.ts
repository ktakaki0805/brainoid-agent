import type { AppPlugin } from "@capacitor/app";
import type { BrowserPlugin } from "@capacitor/browser";

/** Explicit fake-mode bridge: production OAuth adapter, no external browser or provider. */
export function syntheticBrowser() {
  const links = new Set<(v: { url: string }) => void>(),
    finishes = new Set<() => void>();
  const calls: string[] = [];
  const browser = {
    open: async () => {
      calls.push("open");
    },
    close: async () => {
      calls.push("close");
    },
    addListener: async (_name: string, fn: () => void) => {
      finishes.add(fn);
      return {
        remove: async () => {
          finishes.delete(fn);
        },
      };
    },
  } as unknown as BrowserPlugin;
  const app = {
    getLaunchUrl: async () => undefined,
    addListener: async (_name: string, fn: (v: { url: string }) => void) => {
      links.add(fn);
      return {
        remove: async () => {
          links.delete(fn);
        },
      };
    },
  } as unknown as AppPlugin;
  return {
    browser,
    app,
    calls,
    target: "https://calendar-return.example.test/mobile",
    returnURL: (url: string) => {
      for (const fn of links) fn({ url });
    },
    finish: () => {
      for (const fn of finishes) fn();
    },
    listenerCount: () => links.size + finishes.size,
  };
}
