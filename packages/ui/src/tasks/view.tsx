import type { Task } from "@brainoid-internal/api-client";
import { useEffect, useState, useSyncExternalStore } from "react";
import type { SessionContext } from "../session.ts";
import {
  policies,
  statuses,
  type TaskFilter,
  type TaskScope,
  TasksController,
  validateDraft,
  validTimezone,
} from "./controller.ts";

function TaskCard({ task, zone }: { task: Task; zone: string }) {
  return (
    <div className="note-content">
      <div className="scope-badge">
        {task.visibility === "private" ? "個人" : "共有"} · {policies[task.display_policy]}
      </div>
      <h3>{task.title}</h3>
      <p className="task-status">
        保存済みの状態: {statuses[task.status]} ({task.status})
      </p>
      <p>
        期限:{" "}
        {task.due_at
          ? new Intl.DateTimeFormat("ja-JP", {
              timeZone: zone,
              dateStyle: "medium",
              timeStyle: "long",
            }).format(new Date(task.due_at))
          : "期限なし"}
      </p>
      <p>担当者: {task.assignee_user_id || "未指定"}</p>
      <p>タスクに記録したタイムゾーン: {task.timezone || "未指定"}</p>
    </div>
  );
}
export function Tasks({
  context,
  surface,
}: {
  context: SessionContext;
  surface: "mobile" | "display";
}) {
  const [controller] = useState(() => new TasksController(context, surface));
  const state = useSyncExternalStore(controller.subscribe, controller.snapshot);
  const [zone, setZone] = useState(() => Intl.DateTimeFormat().resolvedOptions().timeZone);
  const [zoneInput, setZoneInput] = useState(zone);
  useEffect(() => {
    void controller.load();
    return () => controller.dispose();
  }, [controller]);
  if (state.closed) return null;
  const editor = state.editor,
    locked = state.busy || state.pending,
    selecting = locked || !!editor;
  const warning = editor ? validateDraft(editor.draft, editor.original) : undefined;
  return (
    <section className="card tasks" aria-label="タスク">
      <div className="tenant-heading">
        <div>
          <p className="eyebrow">TASKS</p>
          <h2>{surface === "mobile" ? "タスクを管理" : "みんなのタスク"}</h2>
        </div>
        {surface === "mobile" && (
          <button
            className="button primary"
            type="button"
            disabled={selecting}
            onClick={() => controller.create()}
          >
            タスクを作成
          </button>
        )}
      </div>
      <p>
        {surface === "mobile"
          ? "保存された期限・担当・状態を確認できます。AIの成功文だけでは完了に変わりません。"
          : "本人未確認の共有画面では更新できません。スマホで操作するか、スマホからtasksを許可した個人セッションを開始してください。この画面は閲覧専用です。"}
      </p>
      <form
        className="memory-search"
        onSubmit={(event) => {
          event.preventDefault();
          if (validTimezone(zoneInput)) setZone(zoneInput);
        }}
      >
        <label>
          日時の表示タイムゾーン
          <input
            aria-label="日時の表示タイムゾーン"
            value={zoneInput}
            maxLength={64}
            onChange={(event) => setZoneInput(event.target.value)}
          />
        </label>
        <button className="button secondary" type="submit" disabled={!validTimezone(zoneInput)}>
          表示に適用
        </button>
      </form>
      <p>
        選択中の表示タイムゾーン: <strong>{zone}</strong>（初期値はこのブラウザーの設定）
      </p>
      {!validTimezone(zoneInput) && (
        <p role="alert">Asia/TokyoなどのIANAタイムゾーン名を入力してください。</p>
      )}
      <div className="memory-search">
        {surface === "mobile" && (
          <label>
            一覧の公開範囲
            <select
              aria-label="タスク一覧の公開範囲"
              value={state.scope}
              disabled={selecting}
              onChange={(event) => void controller.scope(event.target.value as TaskScope)}
            >
              <option value="all">個人と共有</option>
              <option value="private">個人（自分だけ）</option>
              <option value="shared">共有（テナント内）</option>
            </select>
          </label>
        )}
        <label>
          一覧の状態
          <select
            aria-label="タスク一覧の状態"
            value={state.filter}
            disabled={selecting}
            onChange={(event) => void controller.filter(event.target.value as TaskFilter)}
          >
            <option value="all">すべて</option>
            {Object.entries(statuses).map(([value, label]) => (
              <option key={value} value={value}>
                {label} ({value})
              </option>
            ))}
          </select>
        </label>
        <button
          className="button secondary"
          type="button"
          disabled={locked}
          onClick={() => void controller.load()}
        >
          一覧を再取得
        </button>
      </div>
      {state.busy && <p role="status">タスクの状態を確認しています。</p>}
      {state.notice && <p role="alert">{state.notice}</p>}
      {state.pending && (
        <div className="memory-warning">
          <p>
            保存済みの可能性がありますが、結果は未確認です。確認が終わるまで入力と別の保存はできません。下の一覧は最後に確認した保存済みの状態です。
          </p>
          <button
            className="button secondary"
            type="button"
            disabled={state.busy}
            onClick={() => void controller.retry()}
          >
            同じ内容で受付を再確認
          </button>
        </div>
      )}
      {state.conflict && (
        <section className="memory-warning" aria-label="タスクの競合">
          <h3>別の更新があります</h3>
          <p>編集開始時</p>
          <TaskCard task={state.conflict.before} zone={zone} />
          <p>再取得した最新版</p>
          {state.conflict.current ? (
            <TaskCard task={state.conflict.current} zone={zone} />
          ) : (
            <p>最新版はまだ取得できていません。</p>
          )}
          <button
            className="button secondary"
            type="button"
            disabled={locked}
            onClick={() => void controller.refreshConflict()}
          >
            最新版を再取得
          </button>
          <p>
            下の未保存の入力は保持しています。状態と期限を最新版と比較し、必要な変更を入力へ反映してください。
          </p>
          <button
            className="button secondary"
            type="button"
            disabled={locked || !state.conflict.current}
            onClick={() => controller.continueEditing()}
          >
            最新版を確認して編集を続ける
          </button>
        </section>
      )}
      {editor && (
        <form
          className="memory-editor"
          aria-label="タスクの保存フォーム"
          onSubmit={(event) => {
            event.preventDefault();
            void controller.save();
          }}
        >
          <h3>{editor.original ? "タスクを編集" : "新しいタスク"}</h3>
          <p>以下は未保存の入力です。保存結果を確認するまで一覧の状態は変わりません。</p>
          {editor.original && (
            <>
              <p>
                編集できる項目は題名・状態・期限です。担当者・記録したタイムゾーン・公開範囲・表示先は変更できません。
              </p>
              <p>
                編集対象の保存済み状態: {statuses[editor.original.status]} ({editor.original.status}
                )
              </p>
            </>
          )}
          <fieldset disabled={locked}>
            <label>
              題名
              <input
                required
                maxLength={200}
                value={editor.draft.title}
                onChange={(event) =>
                  controller.draft({ ...editor.draft, title: event.target.value })
                }
              />
            </label>
            {editor.original && (
              <label>
                変更後の状態（未保存）
                <select
                  aria-label="変更後の状態（未保存）"
                  value={editor.draft.status}
                  onChange={(event) =>
                    controller.draft({
                      ...editor.draft,
                      status: event.target.value as Task["status"],
                    })
                  }
                >
                  {Object.entries(statuses).map(([value, label]) => (
                    <option key={value} value={value}>
                      {label} ({value})
                    </option>
                  ))}
                </select>
              </label>
            )}
            <label>
              期限（UTCオフセット付き）
              <input
                aria-label="期限（UTCオフセット付き）"
                placeholder="2026-10-28T18:00:00+09:00"
                value={editor.draft.due_at}
                onChange={(event) =>
                  controller.draft({ ...editor.draft, due_at: event.target.value })
                }
              />
            </label>
            <p>
              空欄は期限なし。日時を秒まで入力し、+09:00やZで時点を指定してください。夏時間の重複する時刻もオフセットで区別します。記録したタイムゾーンから期限を自動計算しません。
            </p>
            <label>
              記録するタイムゾーン（任意）
              <input
                aria-label="記録するタイムゾーン（任意）"
                placeholder="Asia/Tokyo"
                value={editor.draft.timezone}
                disabled={!!editor.original}
                maxLength={64}
                onChange={(event) =>
                  controller.draft({ ...editor.draft, timezone: event.target.value })
                }
              />
            </label>
            <label>
              担当者のユーザーID（任意）
              <input
                aria-label="担当者のユーザーID（任意）"
                value={editor.draft.assignee_user_id}
                disabled={!!editor.original}
                maxLength={128}
                onChange={(event) =>
                  controller.draft({ ...editor.draft, assignee_user_id: event.target.value })
                }
              />
            </label>
            <p>
              担当者は同じテナントの有効なユーザーIDをそのまま指定します。担当者にしても個人タスクの閲覧権は増えません。
            </p>
            <label>
              公開範囲
              <select
                aria-label="公開範囲"
                value={editor.draft.visibility}
                disabled={!!editor.original}
                onChange={(event) =>
                  controller.draft({
                    ...editor.draft,
                    visibility: event.target.value as Task["visibility"],
                  })
                }
              >
                <option value="private">個人（自分だけ）</option>
                <option value="shared">共有（テナント内）</option>
              </select>
            </label>
            <label>
              表示先
              <select
                aria-label="表示先"
                value={editor.draft.display_policy}
                disabled={!!editor.original}
                onChange={(event) =>
                  controller.draft({
                    ...editor.draft,
                    display_policy: event.target.value as Task["display_policy"],
                  })
                }
              >
                {Object.entries(policies).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            {warning && <p className="memory-warning">{warning}</p>}
            <button
              className="button primary"
              type="submit"
              disabled={!!warning || !!state.conflict}
            >
              {editor.original ? "変更を保存" : "タスクを保存"}
            </button>
          </fieldset>
          <button
            className="button secondary"
            type="button"
            disabled={locked}
            onClick={() => controller.closeEditor()}
          >
            入力を破棄して閉じる
          </button>
        </form>
      )}
      <ul className="memory-list" aria-label="現在のタスク">
        {state.items.map((task) => (
          <li className="card" key={task.id}>
            <TaskCard task={task} zone={zone} />
            {surface === "mobile" && (
              <div className="memory-actions">
                <button
                  className="button secondary"
                  type="button"
                  disabled={selecting}
                  onClick={() => void controller.select(task.id)}
                >
                  編集・完了・取消
                </button>
              </div>
            )}
          </li>
        ))}
      </ul>
      {!state.items.length && !state.busy && <p>表示できるタスクはありません。</p>}
    </section>
  );
}
