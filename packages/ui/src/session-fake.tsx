import { useState } from "react";
import { type SessionContext, SessionController } from "./session.ts";
import { createSessionFixture } from "./session-fixture.ts";
import { browserNavigation, SessionApp } from "./session-view.tsx";

export function SessionPreview({ surface }: { surface: "mobile" | "display" }) {
  const [fixture] = useState(createSessionFixture);
  const [controller] = useState(
    () =>
      new SessionController({
        auth: fixture.auth,
        transport: fixture.transport,
        baseUrl: "https://session.invalid",
        ...browserNavigation(),
      }),
  );
  return (
    <>
      <SessionApp controller={controller} surface={surface} preview>
        {(context) => <ContextProbe context={context} />}
      </SessionApp>
      <details className="preview-tools">
        <summary>認証・切替の確認</summary>
        <div className="preview-controls">
          <label className="checkbox-label">
            <input
              type="checkbox"
              onChange={(event) => {
                fixture.controls.delayA = event.target.checked;
              }}
            />
            A の応答を遅らせる
          </label>
          <label>
            選択時の応答
            <select
              defaultValue="200"
              onChange={(event) => {
                fixture.controls.status = Number(event.target.value);
              }}
            >
              <option value="200">成功</option>
              <option value="401">401 認証失効</option>
              <option value="403">403 権限拒否</option>
              <option value="404">404 所属なし</option>
            </select>
          </label>
        </div>
      </details>
    </>
  );
}
function ContextProbe({ context }: { context: SessionContext }) {
  const [draft, setDraft] = useState("");
  return (
    <label>
      消去確認用の入力（体験用）
      <input
        aria-label="消去確認用の入力"
        value={draft}
        onChange={(event) => {
          context.keep("preview-draft", event.target.value);
          setDraft(event.target.value);
        }}
      />
    </label>
  );
}
