import { useEffect, useState, useSyncExternalStore } from "react";
import type { SessionContext } from "../session.ts";
import { ChatController } from "./controller.ts";
import type { CharacterState, ToolStatus } from "./events.ts";

const states: Record<CharacterState, { label: string; face: string }> = {
  idle: { label: "待機中", face: "◡" },
  thinking: { label: "考えています", face: "·" },
  confirmation: { label: "確認待ち", face: "?" },
  speaking: { label: "応答を表示中", face: "○" },
  error: { label: "応答の生成に失敗しました", face: "!" },
};
const toolLabels: Record<ToolStatus, string> = {
  pending: "受付済み",
  running: "実行中",
  succeeded: "成功",
  failed: "失敗",
  outcome_unknown: "結果不明",
};
export function Character({ state }: { state: CharacterState }) {
  return (
    <div
      className={`character character-${state}`}
      role="status"
      aria-label={`アシスタント: ${states[state].label}`}
    >
      <div className="character-face" aria-hidden="true">
        <span>• •</span>
        <span>{states[state].face}</span>
      </div>
      <span>{states[state].label}</span>
    </div>
  );
}
export function Chat({
  context,
  surface,
}: {
  context: SessionContext;
  surface: "mobile" | "display";
}) {
  const [controller] = useState(() => new ChatController(context, surface));
  const state = useSyncExternalStore(controller.subscribe, controller.snapshot);
  const [agentId, setAgentId] = useState("");
  const [visibility, setVisibility] = useState<"private" | "shared">(
    surface === "display" ? "shared" : "private",
  );
  useEffect(() => {
    void controller.load();
    return () => controller.dispose();
  }, [controller]);
  const active = state.run && !["completed", "failed", "cancelled"].includes(state.run.status);
  const knownTerminal =
    state.run && ["completed", "failed", "cancelled"].includes(state.run.status);
  const selectedAgent = state.agents.find((agent) => agent.id === state.conversation?.agent_id);
  return (
    <section className="chat-workspace" aria-label="会話">
      <aside className="card chat-navigation" aria-label="会話を選択">
        <details open={!state.conversation}>
          <summary>
            {state.conversation ? "会話を切り替える・新しく作成" : "会話を選ぶ・新しく作成"}
          </summary>
          <h2>会話</h2>
          <p className="hint">
            {surface === "display"
              ? "この共有画面で表示できる会話です。"
              : "個人と共有を選んで話せます。"}
          </p>
          <ul className="conversation-list">
            {state.conversations.map((conversation) => (
              <li key={conversation.id}>
                <button
                  type="button"
                  className="button secondary"
                  aria-pressed={state.conversation?.id === conversation.id}
                  onClick={() => {
                    void controller.select(conversation.id);
                  }}
                >
                  <span className="scope-badge">
                    {conversation.visibility === "private" ? "個人" : "共有"}
                  </span>{" "}
                  {conversation.title}
                </button>
              </li>
            ))}
          </ul>
          {!state.busy && state.conversations.length === 0 && <p>まだ会話はありません。</p>}
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void controller.create(agentId || state.agents[0]?.id || "", visibility);
            }}
          >
            <label>
              エージェント
              <select
                value={agentId || state.agents[0]?.id || ""}
                onChange={(event) => setAgentId(event.target.value)}
                disabled={state.busy || state.agents.length === 0}
              >
                {state.agents.map((agent) => (
                  <option key={agent.id} value={agent.id}>
                    {agent.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              会話の公開範囲
              <select
                value={visibility}
                onChange={(event) => setVisibility(event.target.value as "private" | "shared")}
                disabled={surface === "display" || state.busy}
              >
                {surface === "mobile" && <option value="private">個人 · 自分だけ</option>}
                <option value="shared">共有 · テナントのみんな</option>
              </select>
            </label>
            <button
              className="button primary"
              disabled={state.busy || !state.agents.length}
              type="submit"
            >
              会話を作成
            </button>
          </form>
          <button
            className="button secondary"
            type="button"
            disabled={state.busy}
            onClick={() => {
              void controller.load();
            }}
          >
            会話一覧を更新
          </button>
        </details>
      </aside>
      <div className="card chat-content">
        <Character state={state.events?.character ?? "idle"} />
        {state.notice && <p role="alert">{state.notice}</p>}
        {state.busy && <p role="status">状態を確認しています。</p>}
        {state.conversation ? (
          <>
            <header className="chat-heading">
              <p className="eyebrow">{selectedAgent?.name ?? "エージェント"}</p>
              <h2>{state.conversation.title}</h2>
              <p>
                <span className="scope-badge">
                  {state.conversation.visibility === "private"
                    ? "個人 · 自分だけ"
                    : "共有 · テナントのみんな"}
                </span>
              </p>
              <p className="hint">
                共有画面への表示:{" "}
                {state.conversation.display_policy === "shared_display"
                  ? "許可"
                  : state.conversation.display_policy === "authenticated_session"
                    ? "本人の個人セッションのみ"
                    : "表示しない"}
              </p>
              <p className="output-label">
                応答先:{" "}
                {state.output === "current"
                  ? "この画面で文字表示（他端末への出力指定なし）"
                  : "履歴の応答先は不明"}
              </p>
            </header>
            <ol className="message-list" aria-label="保存済みメッセージ">
              {state.messages.map((message) => (
                <li key={message.id} className={`message message-${message.role}`}>
                  <span className="small-label">
                    {message.role === "user"
                      ? "利用者"
                      : message.role === "assistant"
                        ? (selectedAgent?.name ?? "アシスタント")
                        : "ツール"}{" "}
                    · 保存済み
                  </span>
                  <p>{message.text}</p>
                </li>
              ))}
            </ol>
            {!knownTerminal && !state.events?.terminal && state.events?.text && (
              <section className="message provisional" aria-label="生成途中の応答">
                <span className="small-label">生成途中 · 未確定</span>
                <p>{state.events.text}</p>
              </section>
            )}
            {state.run && state.events && state.run.last_event_seq > state.events.seq && (
              <p className="hint">
                操作結果の履歴は一部取得できていません。下のカードは取得済みの状態です。
              </p>
            )}
            {state.historyUnavailable && (
              <p className="hint">
                {state.historyUnavailable === "expired"
                  ? "イベント履歴の保存期限または保持範囲を超えているため、未取得の操作結果は確認できません。"
                  : "生成停止後のイベント配信は制限されています。未取得の操作結果は確認できません。"}
              </p>
            )}
            {!!state.events?.tools.length && (
              <section aria-label="操作の結果">
                <h3>操作の結果</h3>
                <ul className="tool-list">
                  {state.events.tools.map((tool) => (
                    <li key={tool.id} className={`tool-card tool-${tool.status}`}>
                      <strong>{tool.name}</strong>
                      <span>{toolLabels[tool.status]}</span>
                      {tool.status === "outcome_unknown" && (
                        <p>機器の状態を確認できませんでした。成功したかは不明です。</p>
                      )}
                      {tool.status === "pending" && <p>受付は完了を意味しません。</p>}
                    </li>
                  ))}
                </ul>
              </section>
            )}
            {!knownTerminal && state.events?.character === "confirmation" && (
              <p className="chat-notice">
                操作内容の確認または外部結果を待っています。確認の操作画面は準備中です。「状態を確認」で進捗を取得できます。
              </p>
            )}
            {state.run?.status === "failed" && (
              <p role="alert">
                応答の生成に失敗しました。
                {state.run.error_code === "outcome_unknown"
                  ? "外部処理の結果を確認できません。"
                  : "途中の回答は破棄しました。"}
                操作の結果は各カードで確認してください。
              </p>
            )}
            {state.run?.status === "cancelled" && (
              <p role="status">
                生成を停止しました。実行済みの操作を取り消したことにはなりません。
              </p>
            )}
            {state.run?.status === "completed" && state.finalVerified && (
              <p className="hint">
                保存済みの最終回答を表示しています。操作の成功・失敗は各カードで確認してください。
              </p>
            )}
            {state.connection === "disconnected" && (
              <p role="status">接続が終了しました。状態の確認または再接続を選んでください。</p>
            )}
            {state.events && (
              <div className="button-row chat-actions">
                <button
                  className="button secondary"
                  type="button"
                  disabled={state.busy}
                  onClick={() => {
                    void controller.refresh();
                  }}
                >
                  状態を確認
                </button>
                {!state.events.terminal && state.run?.status !== "cancelled" && (
                  <button
                    className="button secondary"
                    type="button"
                    disabled={
                      state.busy || state.connection === "live" || state.connection === "connecting"
                    }
                    onClick={() => {
                      void controller.connect();
                    }}
                  >
                    {knownTerminal ? "操作履歴を確認" : "再接続"}
                  </button>
                )}
                {active && (
                  <button
                    className="button secondary"
                    type="button"
                    disabled={state.busy}
                    onClick={() => {
                      void controller.cancel();
                    }}
                  >
                    生成を停止
                  </button>
                )}
              </div>
            )}
            <form
              className="chat-composer"
              onSubmit={(event) => {
                event.preventDefault();
                void controller.send();
              }}
            >
              <label>
                メッセージ
                <textarea
                  value={state.draft}
                  onChange={(event) => controller.draft(event.target.value)}
                  rows={3}
                  maxLength={8000}
                  disabled={state.busy || Boolean(active)}
                />
              </label>
              <p className="hint">送信時の応答先: この画面で文字表示。音声は再生しません。</p>
              <button
                className="button primary"
                disabled={
                  state.busy ||
                  Boolean(active) ||
                  (state.run?.status === "completed" && !state.finalVerified) ||
                  !state.draft.trim()
                }
                type="submit"
              >
                送信
              </button>
            </form>
          </>
        ) : (
          <p>会話を選ぶか、新しく作成してください。</p>
        )}
      </div>
    </section>
  );
}
