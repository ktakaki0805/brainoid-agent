import { useState } from "react";
import { SessionPreview } from "@brainoid-internal/ui/session-fake";
import { PushPreview } from "../notifications/preview.tsx";
import { nativeCalendarOAuth } from "../platform/oauth.ts";
import { syntheticBrowser } from "../platform/oauth-fixture.ts";

export function CalendarPreview() {
  const [bridge] = useState(syntheticBrowser);
  const [oauth] = useState(() => nativeCalendarOAuth(bridge.browser, bridge.app, bridge.target));
  return (
    <>
      <SessionPreview
        surface="mobile"
        calendarOAuth={oauth}
        extra={(controller, fixture) => (
          <PushPreview session={controller} network={fixture.push.controls} />
        )}
      />
      <details className="calendar-preview-tools">
        <summary>Calendarブラウザーの確認</summary>
        <div className="button-row">
          <button
            className="button secondary"
            type="button"
            onClick={() => bridge.returnURL(bridge.target)}
          >
            合成の許可済みURLで復帰
          </button>
          <button
            className="button secondary"
            type="button"
            onClick={() => bridge.returnURL(`${bridge.target}?code=synthetic`)}
          >
            合成の不正URLで復帰
          </button>
          <button className="button secondary" type="button" onClick={() => bridge.finish()}>
            ブラウザーを取り消す
          </button>
        </div>
      </details>
    </>
  );
}
