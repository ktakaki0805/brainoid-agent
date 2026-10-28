import { useState } from "react";
import type { CalendarOAuth } from "./calendar/contracts.ts";
import type { CalendarScenario } from "./calendar/fixture.ts";
import type { ChatScenario } from "./chat/fixture.ts";
import type { MemoryScenario } from "./memories/fixture.ts";
import type { NoteScenario } from "./notes/fixture.ts";
import type { ReminderScenario } from "./reminders/fixture.ts";
import { type SessionContext, SessionController } from "./session.ts";
import { createSessionFixture } from "./session-fixture.ts";
import { browserNavigation, SessionApp } from "./session-view.tsx";
import type { TaskScenario } from "./tasks/fixture.ts";

export function SessionPreview({
  surface,
  extra,
  calendarOAuth,
}: {
  surface: "mobile" | "display";
  calendarOAuth?: CalendarOAuth | undefined;
  extra?: (
    controller: SessionController,
    fixture: ReturnType<typeof createSessionFixture>,
  ) => React.ReactNode;
}) {
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
      <SessionApp controller={controller} surface={surface} preview calendarOAuth={calendarOAuth}>
        {(context) => <ContextProbe context={context} />}
      </SessionApp>
      {extra?.(controller, fixture)}
      <details className="preview-tools">
        <summary>認証・切替の確認</summary>
        <div className="preview-controls">
          <label>
            Calendarの動作
            <select
              aria-label="Calendarの動作"
              defaultValue="normal"
              onChange={(event) => {
                fixture.calendar.controls.scenario = event.target.value as CalendarScenario;
              }}
            >
              <option value="normal">通常</option>
              <option value="empty">空の一覧</option>
              <option value="failed">通信失敗</option>
              <option value="lost">受付の応答喪失</option>
              <option value="conflict">連携の競合</option>
              <option value="expired">確認期限切れ</option>
              <option value="provider_conflict">Google側の競合</option>
              <option value="unknown">書込結果不明</option>
              <option value="reauth">再認可が必要</option>
              <option value="cleanup">消去完了</option>
            </select>
          </label>
          <label>
            リマインダーの動作
            <select
              aria-label="リマインダーの動作"
              defaultValue="normal"
              onChange={(event) => {
                fixture.reminders.controls.scenario = event.target.value as ReminderScenario;
              }}
            >
              <option value="normal">通常</option>
              <option value="empty">空の一覧</option>
              <option value="conflict">編集の競合</option>
              <option value="lost_save">保存の応答喪失</option>
              <option value="failed">通信失敗</option>
              <option value="denied">権限拒否</option>
              <option value="expired">通知の失効</option>
              <option value="audio_expired">音声期限超過（一覧は保持）</option>
            </select>
          </label>
          <label>
            タスクの動作
            <select
              aria-label="タスクの動作"
              defaultValue="normal"
              onChange={(event) => {
                fixture.tasks.controls.scenario = event.target.value as TaskScenario;
              }}
            >
              <option value="normal">通常</option>
              <option value="empty">空の一覧</option>
              <option value="reject">入力のAPI拒否</option>
              <option value="denied">権限拒否</option>
              <option value="conflict">編集の競合</option>
              <option value="conflict_status">状態変更の競合</option>
              <option value="changed_selection">選択直前に別の端末で状態変更</option>
              <option value="lost_save">保存の応答喪失</option>
              <option value="failed">保存の通信失敗</option>
            </select>
          </label>
          <label>
            ノートの動作
            <select
              aria-label="ノートの動作"
              defaultValue="normal"
              onChange={(event) => {
                fixture.notes.controls.scenario = event.target.value as NoteScenario;
              }}
            >
              <option value="normal">通常</option>
              <option value="empty">空の一覧</option>
              <option value="reject">入力のAPI拒否</option>
              <option value="denied">権限拒否</option>
              <option value="conflict">編集の競合</option>
              <option value="lost_save">保存の応答喪失</option>
              <option value="delete_failed">削除受付の失敗</option>
              <option value="lost_delete">削除の応答喪失</option>
              <option value="purge_failed">消去処理の失敗</option>
              <option value="purge_unknown">消去結果不明</option>
            </select>
          </label>
          <label>
            記憶の動作
            <select
              aria-label="記憶の動作"
              defaultValue="normal"
              onChange={(event) => {
                fixture.memories.controls.scenario = event.target.value as MemoryScenario;
              }}
            >
              <option value="normal">通常</option>
              <option value="empty">新規テナントの空状態</option>
              <option value="reject">入力のAPI拒否</option>
              <option value="conflict">訂正の競合</option>
              <option value="lost_save">保存の応答喪失</option>
              <option value="forget_failed">忘却受付の失敗</option>
              <option value="lost_forget">忘却の応答喪失</option>
              <option value="purge_failed">消去処理の失敗</option>
            </select>
          </label>
          <label>
            会話イベント
            <select
              defaultValue="normal"
              onChange={(event) => {
                fixture.chat.controls.scenario = event.target.value as ChatScenario;
              }}
            >
              <option value="normal">通常の回答</option>
              <option value="reconnect">切断と重複再開</option>
              <option value="expired">履歴期限切れ 410</option>
              <option value="gap">seq 欠番</option>
              <option value="waiting">確認待ち</option>
              <option value="failed">run 失敗</option>
              <option value="unknown">家電の結果不明</option>
              <option value="tools">操作の受付・成功・失敗</option>
              <option value="revoked">表示権限の失効</option>
              <option value="fast_tools">worker先行完了・操作結果</option>
              <option value="fast_unknown">worker先行失敗・結果不明</option>
              <option value="disconnect_tools">切断中に完了・操作結果</option>
              <option value="read_failure">切断後の状態取得503</option>
              <option value="message_failure">切断後の履歴取得503</option>
            </select>
          </label>
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
