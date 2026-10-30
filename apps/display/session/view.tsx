import { VoiceControls } from "@brainoid-internal/ui/voice-controls";
import { useEffect, useState, useSyncExternalStore } from "react";
import { flushSync } from "react-dom";
import { DeviceSessionController } from "./controller.ts";

export function DeviceSession({ instance }: { instance?: DeviceSessionController }) {
  const [controller] = useState(() => instance ?? new DeviceSessionController());
  const state = useSyncExternalStore(controller.subscribe, controller.snapshot);
  const [input, setInput] = useState("");
  const [output, setOutput] = useState("");
  useEffect(() => {
    void controller.voice.stop("").then(() => controller.voice.refresh());
    void controller.refresh();
    const tick = setInterval(() => controller.tick(), 100),
      poll = setInterval(() => {
        void controller.refresh();
        void controller.voice.refresh();
      }, 2_000);
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
      <VoiceControls
        state={state.voice}
        run={state.runStatus}
        busy={state.busy}
        privateSession={Boolean(state.session)}
        destination="このPi"
        canRecord={
          Boolean(state.conversation) &&
          !state.busy &&
          !["queued", "running", "waiting_confirmation"].includes(state.runStatus ?? "")
        }
        start={() => void controller.record()}
        finish={() => void controller.voice.operation("finish", state.session?.id)}
        send={() => void controller.sendRecording()}
        discard={() =>
          void controller.voice.stop(
            "録音を破棄しました。送信開始後の処理は取り消したとは限りません。",
          )
        }
        stop={() => void controller.voice.stop()}
      />
      <details>
        <summary>音声機器と読み上げの待機</summary>
        <p>マイクの設定はスマホの端末設定で変更できます。履歴や再接続の音声は自動再生しません。</p>
        <button
          className="button secondary"
          type="button"
          onClick={() => void controller.voice.devices()}
        >
          音声機器を確認
        </button>
        <label>
          入力機器
          <select
            aria-label="入力機器"
            value={input}
            onChange={(event) => setInput(event.target.value)}
          >
            <option value="">選択してください</option>
            {state.voice.devices
              .filter((d) => d.direction === "input")
              .map((d) => (
                <option key={d.id} value={d.id}>
                  {d.id}
                </option>
              ))}
          </select>
        </label>
        <label>
          出力機器
          <select
            aria-label="出力機器"
            value={output}
            onChange={(event) => setOutput(event.target.value)}
          >
            <option value="">選択してください</option>
            {state.voice.devices
              .filter((d) => d.direction === "output")
              .map((d) => (
                <option key={d.id} value={d.id}>
                  {d.id}
                </option>
              ))}
          </select>
        </label>
        <button
          className="button secondary"
          type="button"
          disabled={!input || !output || !state.connected}
          onClick={() =>
            void controller.voice.operation("select", state.session?.id, {
              input_device: input,
              output_device: output,
            })
          }
        >
          選択した音声機器を使用
        </button>
        <button
          className="button secondary"
          type="button"
          disabled={
            !state.voice.available || !state.voice.output || !state.connected || state.voice.waiting
          }
          onClick={() => void controller.voice.wait(state.session?.id)}
        >
          次の新しい割当の読み上げを待つ
        </button>
        {state.voice.waiting && (
          <p role="status">
            新規音声を待機中 · 待機開始後にスマホで送信してください。60秒または次の1件で終了します。
          </p>
        )}
        {state.run &&
          state.runs.find((run) => run.run_id === state.run)?.output_mode === "speech" && (
            <button
              className="button secondary"
              type="button"
              disabled={
                !state.voice.available ||
                !state.voice.output ||
                !state.connected ||
                !["queued", "running", "waiting_confirmation"].includes(state.runStatus ?? "")
              }
              onClick={() => {
                if (state.run) void controller.select(state.run, "future");
              }}
            >
              この会話のこれ以降の新しい音声を再生
            </button>
          )}
      </details>
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
      {state.tools.length > 0 && (
        <section aria-label="操作の結果">
          <h3>操作の結果</h3>
          {state.tools.map((tool) => (
            <div key={tool.id}>
              <p>
                {tool.name}: {tool.status}
              </p>
              {tool.commandId && (
                <>
                  <button
                    className="button secondary"
                    type="button"
                    onClick={() => {
                      if (tool.commandId) void controller.cancelCommand(tool.commandId);
                    }}
                  >
                    未実行操作の取消・結果を確認
                  </button>
                  <p role="status">{state.commandResults[tool.commandId]}</p>
                </>
              )}
            </div>
          ))}
        </section>
      )}
      {state.runStatus === "waiting_confirmation" && (
        <p>
          操作の確認待ちです。予定の確認・未実行候補の取消は本人のスマホの確認カードで行えます。
        </p>
      )}
      {state.run &&
        ["queued", "running", "waiting_confirmation"].includes(state.runStatus ?? "") && (
          <button
            className="button secondary"
            type="button"
            onClick={() => void controller.cancelRun()}
          >
            生成を停止
          </button>
        )}
      <section aria-label="文字で会話">
        <h3>文字で会話</h3>
        <button
          className="button secondary"
          type="button"
          disabled={!state.connected}
          onClick={() => void controller.conversations()}
        >
          入力できる会話を確認
        </button>
        <label>
          入力先の会話
          <select
            aria-label="入力先の会話"
            value={state.conversation ?? ""}
            onChange={(event) => controller.chooseConversation(event.target.value)}
          >
            <option value="">会話を選択</option>
            {state.conversations.map((c) => (
              <option key={c.id} value={c.id}>
                {c.title}
              </option>
            ))}
          </select>
        </label>
        {!state.conversations.length && (
          <p>
            スマホで作成した許可済みの会話を選んでください。割り当てられた会話にも入力できます。
          </p>
        )}
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void controller.send();
          }}
        >
          <label>
            メッセージ
            <textarea
              aria-label="メッセージ"
              rows={3}
              maxLength={8000}
              value={state.draft}
              onChange={(event) => controller.draft(event.target.value)}
            />
          </label>
          <button
            className="button primary"
            type="submit"
            disabled={
              !state.connected ||
              !state.conversation ||
              !state.draft.trim() ||
              state.busy ||
              ["queued", "running", "waiting_confirmation"].includes(state.runStatus ?? "")
            }
          >
            文字を送信
          </button>
        </form>
      </section>
      <button className="button secondary" type="button" onClick={() => void controller.refresh()}>
        会話の状態を確認
      </button>
    </section>
  );
}
