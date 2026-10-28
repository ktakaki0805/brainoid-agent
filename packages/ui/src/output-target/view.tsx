import { useEffect, useState, useSyncExternalStore } from "react";
import { flushSync } from "react-dom";
import type { PrivateSessionCreate } from "@brainoid-internal/api-client";
import type { OutputTargetController } from "./controller.ts";
import { sessionScopes } from "./lifetime.ts";

export function OutputTarget({ controller }: { controller: OutputTargetController }) {
  const state = useSyncExternalStore(controller.subscribe, controller.snapshot);
  const [device, setDevice] = useState("");
  const [scopes, setScopes] = useState<PrivateSessionCreate["allowed_scopes"]>(["conversation"]);
  const [acknowledged, acknowledge] = useState(false);
  useEffect(() => {
    const tick = setInterval(() => controller.tick(), 100);
    const poll = setInterval(() => void controller.refresh(), 2_000);
    const hide = () => {
      if (document.visibilityState === "hidden")
        flushSync(() => {
          controller.suspend();
          setDevice("");
          acknowledge(false);
          setScopes(["conversation"]);
        });
    };
    document.addEventListener("visibilitychange", hide);
    return () => {
      clearInterval(tick);
      clearInterval(poll);
      document.removeEventListener("visibilitychange", hide);
    };
  }, [controller]);
  return (
    <section className="card output-target" aria-label="Pi個人セッション">
      <h2>Piへの個人セッション</h2>
      <p>
        <strong>{state.session ? "個人セッション中" : "共有状態 · 個人セッションなし"}</strong>
      </p>
      <p>最大5分・無操作60秒。状態の確認や再接続では延長しません。</p>
      <p>
        発話先: 音声は未接続です。文字の出力先:{" "}
        {state.destination === "pi" ? `確認したPi ${state.session?.device_id}` : "このスマホ"}
      </p>
      {state.notice && <p role="status">{state.notice}</p>}
      {state.session ? (
        <>
          <dl>
            <dt>対象Pi</dt>
            <dd>{state.session.device_id}</dd>
            <dt>許可した範囲</dt>
            <dd>{state.session.allowed_scopes.join("、")}</dd>
            <dt>最大期限</dt>
            <dd>{state.session.expires_at}</dd>
            <dt>無操作期限</dt>
            <dd>{state.session.idle_expires_at}</dd>
          </dl>
          <label>
            次の会話の文字出力先
            <select
              value={state.destination}
              disabled={state.pending}
              onChange={(event) => controller.choose(event.target.value as "phone" | "pi")}
            >
              <option value="phone">このスマホ</option>
              <option value="pi" disabled={!state.session.allowed_scopes.includes("conversation")}>
                確認したPiにも表示する
              </option>
            </select>
          </label>
          <button
            type="button"
            className="button secondary"
            disabled={state.busy || state.pending}
            onClick={() => void controller.end()}
          >
            個人セッションを終了
          </button>
        </>
      ) : (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void controller.start(device.trim(), scopes, acknowledged);
            acknowledge(false);
          }}
        >
          <label>
            対象PiのID
            <input
              required
              value={device}
              disabled={state.busy || state.pending}
              onChange={(event) => {
                setDevice(event.target.value);
                acknowledge(false);
              }}
              pattern="[0-9a-fA-F-]{36}"
            />
          </label>
          <p className="hint">
            Pi画面に表示されたIDを照合してください。管理者でなくても、自分の所属先で許可されたPiを指定できます。
          </p>
          <fieldset disabled={state.busy || state.pending}>
            <legend>このPiに許可する範囲</legend>
            {sessionScopes.map((scope) => (
              <label key={scope}>
                <input
                  type="checkbox"
                  checked={scopes.includes(scope)}
                  onChange={(event) => {
                    setScopes(
                      event.target.checked
                        ? [...scopes, scope]
                        : scopes.filter((item) => item !== scope),
                    );
                    acknowledge(false);
                  }}
                />
                {scope}
              </label>
            ))}
          </fieldset>
          <p>
            テナント: {controller.context.tenantId} / 対象Pi: {device || "未指定"}
          </p>
          <label>
            <input
              type="checkbox"
              checked={acknowledged}
              disabled={state.busy || state.pending}
              onChange={(event) => acknowledge(event.target.checked)}
            />
            このPiと許可範囲を確認し、共有空間に個人情報が表示されることに同意します
          </label>
          <button
            className="button primary"
            disabled={!acknowledged || !scopes.length || state.busy || state.pending}
            type="submit"
          >
            確認したPiで開始
          </button>
        </form>
      )}
      {state.pending && (
        <div>
          <p>再確認する対象Pi: {controller.pendingTarget()}</p>
          <button
            className="button secondary"
            type="button"
            disabled={state.busy}
            onClick={() => void controller.retry()}
          >
            同じ操作を再確認
          </button>
        </div>
      )}
    </section>
  );
}
