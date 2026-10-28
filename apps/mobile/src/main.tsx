import { SetupRequired } from "@brainoid-internal/ui/react";
import type { CalendarOAuth } from "@brainoid-internal/ui/calendar";
import { SessionController } from "@brainoid-internal/ui/session";
import { browserNavigation, SessionApp } from "@brainoid-internal/ui/session-view";
import { Capacitor } from "@capacitor/core";
import { createRoot } from "react-dom/client";
import { authenticationConfiguration } from "./auth-config.ts";
import "@brainoid-internal/ui/styles.css";

const element = document.getElementById("root");
if (!element) throw new Error("Missing app root");
const root = createRoot(element);
if (import.meta.env.MODE === "fake") {
  const { CalendarPreview } = await import("../calendar/preview.tsx");
  root.render(<CalendarPreview />);
} else {
  try {
    const config = authenticationConfiguration(import.meta.env, import.meta.env.MODE);
    if (!config) root.render(<SetupRequired surface="mobile" />);
    else {
      const auth = Capacitor.isNativePlatform()
        ? await (async () => {
            if (config.emulator)
              throw new Error("Native emulator requires a dedicated device configuration");
            const { FirebaseAuthentication } = await import("@capacitor-firebase/authentication");
            const { createNativeAuthentication } = await import("./firebase-native.ts");
            return createNativeAuthentication(FirebaseAuthentication);
          })()
        : (await import("./firebase-browser.ts")).createBrowserAuthentication(
            config.firebase,
            config.emulator,
          );
      const controller = new SessionController({
        auth,
        baseUrl: config.apiUrl,
        ...browserNavigation(),
      });
      let push: React.ReactNode;
      let calendarOAuth: CalendarOAuth | undefined;
      if (Capacitor.isNativePlatform()) {
        try {
          const { fixedReturnURL, nativeCalendarOAuth } = await import("../platform/oauth.ts");
          const returnURL = fixedReturnURL(import.meta.env.VITE_CALENDAR_MOBILE_RETURN_URL);
          if (returnURL) {
            const { Browser } = await import("@capacitor/browser");
            const { App } = await import("@capacitor/app");
            calendarOAuth = nativeCalendarOAuth(Browser, App, returnURL);
          }
        } catch {
          /* Calendar setup is independent of login and notifications. */
        }
      }
      if (Capacitor.isNativePlatform()) {
        try {
          const { FirebaseMessaging } = await import("@capacitor-firebase/messaging");
          const { nativePush } = await import("../platform/push.ts");
          const { PushController } = await import("../notifications/controller.ts");
          const { PushSettings } = await import("../notifications/view.tsx");
          const platform = Capacitor.getPlatform();
          if (platform === "ios" || platform === "android")
            push = (
              <PushSettings
                controller={new PushController(controller, nativePush(FirebaseMessaging), platform)}
              />
            );
        } catch {
          push = <p>OS通知を準備できません。アプリ内通知をご利用ください。</p>;
        }
      }
      root.render(
        <>
          <SessionApp controller={controller} surface="mobile" calendarOAuth={calendarOAuth} />
          {push}
        </>,
      );
    }
  } catch {
    root.render(<SetupRequired surface="mobile" />);
  }
}
