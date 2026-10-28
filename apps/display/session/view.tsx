import { useEffect, useState, useSyncExternalStore } from "react";
import { flushSync } from "react-dom";
import { DeviceSessionController } from "./controller.ts";

export function DeviceSession({ instance }: { instance?: DeviceSessionController }) {
  const [controller] = useState(() => instance ?? new DeviceSessionController());
  const state = useSyncExternalStore(controller.subscribe, controller.snapshot);
  useEffect(() => {
    void controller.refresh();
    const tick = setInterval(() => controller.tick(), 100),
      poll = setInterval(() => void controller.refresh(), 2_000);
    const hide = () => flushSync(() => controller.suspend());
    const show = () => {
      if (document.visibilityState !== "hidden") controller.resume();
    };
    const visibility = () => (document.visibilityState === "hidden" ? hide() : show());
    window.addEventListener("pagehide", hide);
    window.addEventListener("pageshow", show);
    document.addEventListener("visibilitychange", visibility);
    return () => {
      clearInterval(tick);
      clearInterval(poll);
      window.removeEventListener("pagehide", hide);
      window.removeEventListener("pageshow", show);
      document.removeEventListener("visibilitychange", visibility);
      controller.dispose();
    };
  }, [controller]);
  return (
    <section className="card output-target" aria-label="Piの会話出力">
      <h2>{state.session ? "個人セッション中" : "共有状態"}</h2>
      <p>発話先: このPi（音声は未接続・再生しません）。割り当てられた会話を文字で表示します。</p>
      <p>最大5分・無操作60秒。切断しても既知の期限で個人表示を消去します。</p>
      <p>このPiのID: {state.device ?? "接続後に表示します"}</p>
      <p>テナント: {state.tenant ?? "未確認"}</p>
      {state.session && (
        <>
          <p>許可範囲: {state.session.allowed_scopes.join("、")}</p>
          <p>
            最大期限: {state.session.expires_at} / 無操作期限: {state.session.idle_expires_at}
          </p>
          <button className="button secondary" type="button" onClick={() => void controller.stop()}>
            このPiでの個人表示を終了
          </button>
          <p>クラウドの個人セッションは開始した本人のスマホで終了できます。</p>
        </>
      )}
      {state.notice && <p role="status">{state.notice}</p>}
      {!state.session && (
        <p>
          個人情報を表示するには、本人のスマホでこのPiと許可範囲を確認し、個人セッションを開始して出力先を指定してください。
        </p>
      )}
      {state.runs.length > 1 && (
        <label>
          割り当てられた会話
          <select
            value={state.run ?? ""}
            onChange={(event) => void controller.select(event.target.value)}
          >
            {state.runs.map((run) => (
              <option key={run.run_id} value={run.run_id}>
                {run.run_id}
              </option>
            ))}
          </select>
        </label>
      )}
      {state.text && (
        <section aria-label="確認済みの応答">
          <p>{state.text}</p>
        </section>
      )}
      {state.provisional && (
        <section aria-label="生成途中の応答">
          <p>{state.provisional}</p>
        </section>
      )}
      <button className="button secondary" type="button" onClick={() => void controller.refresh()}>
        会話の状態を確認
      </button>
    </section>
  );
}
