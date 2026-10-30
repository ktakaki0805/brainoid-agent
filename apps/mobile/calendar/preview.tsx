import { syntheticVoice } from "../platform/voice-fixture.ts";
import { useState } from "react";
import { SessionPreview } from "@brainoid-internal/ui/session-fake";
import { PushPreview } from "../notifications/preview.tsx";
import { nativeCalendarOAuth } from "../platform/oauth.ts";
import { syntheticBrowser } from "../platform/oauth-fixture.ts";

export function CalendarPreview() {
  const [audio] = useState(syntheticVoice);
  const [bridge] = useState(syntheticBrowser);
  const [oauth] = useState(() => nativeCalendarOAuth(bridge.browser, bridge.app, bridge.target));
  return (
    <>
      <SessionPreview
        surface="mobile"
        calendarOAuth={oauth}
        voice={audio.factory}
        extra={(controller, fixture) => (
          <PushPreview session={controller} network={fixture.push.controls} />
        )}
      />
      <details className="voice-preview-tools">
        <summary>音声の確認</summary>
        <label>
          音声の動作
          <select
            defaultValue="normal"
            onChange={(e) => {
              audio.controls.scenario = e.target.value;
            }}
          >
            <option value="normal">通常</option>
            <option value="permission">マイク拒否</option>
            <option value="silent">無音</option>
            <option value="network">音声API失敗</option>
          </select>
        </label>
      </details>
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
