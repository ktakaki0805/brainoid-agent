import { conversationPhase, type VoiceState } from "./controller.ts";

const labels = {
  Idle: "待機中",
  Listening: "マイク入力中",
  Transcribing: "文字起こし中",
  Thinking: "応答生成中",
  Confirming: "操作の確認待ち",
  Speaking: "読み上げ中",
  Error: "音声または応答のエラー",
};
export function VoiceControls({
  state,
  run,
  busy = false,
  privateSession,
  destination,
  canRecord,
  start,
  finish,
  send,
  discard,
  stop,
  mute,
}: {
  state: VoiceState;
  run?: string | undefined;
  busy?: boolean;
  privateSession: boolean;
  destination: string;
  canRecord: boolean;
  start(): void;
  finish(): void;
  send(): void;
  discard(): void;
  stop(): void;
  mute?(): void;
}) {
  const phase = conversationPhase(state, run, busy);
  return (
    <section className="voice-controls" aria-label="音声の状態と操作">
      <p role="status">
        <strong>
          {phase} · {labels[phase]}
        </strong>
      </p>
      <p>
        接続: {state.offline ? "オフライン" : "オンライン"} / マイク:{" "}
        {state.muted === null ? "未確認" : state.muted ? "ミュート" : "ミュート解除"} /
        個人セッション: {privateSession ? "有効" : "なし"}
      </p>
      <p>発話先: {destination}</p>
      {state.notice && <p role="status">{state.notice}</p>}
      {state.phase === "permission" && (
        <p>端末のマイク権限を確認しています。中止するには録音を破棄してください。</p>
      )}
      {state.phase === "ready" && <p>録音は停止済みです。送信または破棄を選んでください。</p>}
      <div className="button-row">
        <button
          className="button primary"
          type="button"
          disabled={
            !state.available ||
            state.muted !== false ||
            state.offline ||
            !canRecord ||
            ["permission", "recording", "ready", "uploading"].includes(state.phase)
          }
          onClick={start}
        >
          録音を開始
        </button>
        {state.phase === "recording" && (
          <button className="button secondary" type="button" onClick={finish}>
            録音を終了（未送信）
          </button>
        )}
        {state.phase === "ready" && (
          <button
            className="button primary"
            type="button"
            disabled={state.offline || busy}
            onClick={send}
          >
            録音を送信
          </button>
        )}
        {["permission", "recording", "ready", "uploading"].includes(state.phase) && (
          <button className="button secondary" type="button" onClick={discard}>
            録音を破棄
          </button>
        )}
        <button className="button secondary" type="button" onClick={stop}>
          読み上げを停止
        </button>
        {mute && (
          <button
            className="button secondary"
            type="button"
            aria-pressed={state.muted ?? false}
            onClick={mute}
          >
            {state.muted ? "マイクのミュートを解除" : "マイクをミュート"}
          </button>
        )}
      </div>
      <p className="hint">
        読み上げ停止は再生と待機音声を消去します。生成や家電・予定の操作は別に取り消します。実行済みの操作は元に戻りません。
      </p>
    </section>
  );
}
