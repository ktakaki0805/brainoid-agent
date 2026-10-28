import type { Reminder } from "@brainoid-internal/api-client";
import { useEffect, useState, useSyncExternalStore } from "react";
import type { SessionContext } from "../session.ts";
import {
  policies,
  type ReminderDraft,
  type ReminderScope,
  RemindersController,
  statuses,
  validateDraft,
} from "./controller.ts";
import { deliveryStates, type NotificationHint, NotificationsController } from "./notifications.ts";

function ReminderCard({ value }: { value: Reminder }) {
  return (
    <div className="note-content">
      <div className="scope-badge">
        {value.visibility === "private" ? "個人" : "共有"} · {policies[value.display_policy]}
      </div>
      <h3>{value.title}</h3>
      <p>
        保存済みの状態: {statuses[value.status]} ({value.status})
      </p>
      <p>
        予定日時:{" "}
        {new Intl.DateTimeFormat("ja-JP", {
          timeZone: value.timezone,
          dateStyle: "medium",
          timeStyle: "long",
        }).format(new Date(value.due_at))}
      </p>
      <p>
        記録した日時: {value.due_at} / {value.timezone}
      </p>
      <p>
        配信先: {value.delivery_target.kind === "user" ? "利用者" : "端末"} ·{" "}
        {value.delivery_target.id}
      </p>
      {value.task_id && <p>関連タスク: {value.task_id}</p>}
      <p>予定版: {value.schedule_version}</p>
    </div>
  );
}
export function Reminders({
  context,
  surface,
  hint,
}: {
  context: SessionContext;
  surface: "mobile" | "display";
  hint?: NotificationHint | undefined;
}) {
  const [controller] = useState(() => new RemindersController(context, surface));
  const [inbox] = useState(() => new NotificationsController(context, surface));
  const state = useSyncExternalStore(controller.subscribe, controller.snapshot);
  const notifications = useSyncExternalStore(inbox.subscribe, inbox.snapshot);
  useEffect(() => {
    void controller.load();
    void inbox.load();
    return () => {
      controller.dispose();
      inbox.dispose();
    };
  }, [controller, inbox]);
  useEffect(() => {
    if (hint) void inbox.open(hint);
  }, [hint, inbox]);
  if (state.closed || notifications.closed) return null;
  const editor = state.editor,
    locked = state.busy || state.pending,
    selecting = locked || !!editor;
  const warning = editor ? validateDraft(editor.draft, editor.original, editor.action) : undefined;
  const change = (changes: Partial<ReminderDraft>) => {
    if (editor) controller.draft({ ...editor.draft, ...changes });
  };
  const save = async () => {
    inbox.invalidate();
    await controller.save();
    await inbox.load();
  };
  return (
    <section className="card reminders" aria-label="リマインダーと通知">
      <div className="tenant-heading">
        <div>
          <p className="eyebrow">REMINDERS</p>
          <h2>{surface === "mobile" ? "リマインダーと通知" : "みんなのリマインダー"}</h2>
        </div>
        {surface === "mobile" && (
          <button
            className="button primary"
            type="button"
            disabled={selecting}
            onClick={() => controller.create()}
          >
            リマインダーを作成
          </button>
        )}
      </div>
      <p>
        OS通知が未登録・拒否されていても、作成とアプリ内の確認を利用できます。OS通知の本文には個人の予定を載せません。
      </p>
      {surface === "display" && (
        <p>
          本人未確認の共有画面は閲覧専用です。編集・取消・スヌーズ・本人確認はスマホで操作してください。
        </p>
      )}
      <div className="memory-actions">
        {surface === "mobile" && (
          <label>
            予定の公開範囲
            <select
              aria-label="予定の公開範囲"
              value={state.scope}
              disabled={selecting}
              onChange={(event) => void controller.scope(event.target.value as ReminderScope)}
            >
              <option value="all">すべて</option>
              <option value="private">個人</option>
              <option value="shared">共有</option>
            </select>
          </label>
        )}
        <button
          className="button secondary"
          type="button"
          disabled={selecting}
          onClick={() => void controller.load()}
        >
          予定一覧を再取得
        </button>
      </div>
      {state.notice && <p role="status">{state.notice}</p>}
      {state.pending && (
        <button
          className="button secondary"
          type="button"
          disabled={state.busy}
          onClick={() => {
            inbox.invalidate();
            void controller.retry().then(() => inbox.load());
          }}
        >
          同じ保存を再確認
        </button>
      )}
      {state.conflict && (
        <div className="card" role="alert">
          <h3>他の更新があります</h3>
          <p>入力は未保存です。現在の予定を確認してから保存してください。</p>
          {state.conflict.current && <ReminderCard value={state.conflict.current} />}
          <button
            className="button secondary"
            type="button"
            disabled={locked}
            onClick={() => void controller.refreshConflict()}
          >
            最新版を取得
          </button>
          <button
            className="button secondary"
            type="button"
            disabled={locked || !state.conflict.current}
            onClick={() => controller.continueEditing()}
          >
            最新版に入力を反映
          </button>
        </div>
      )}
      {editor && (
        <form
          aria-label="リマインダーの保存フォーム"
          className="memory-editor"
          onSubmit={(event) => {
            event.preventDefault();
            void save();
          }}
        >
          <h3>
            {editor.action === "snooze"
              ? "新しい日時へスヌーズ"
              : editor.original
                ? "予定を編集・取消"
                : "新しいリマインダー"}
          </h3>
          {editor.original && <ReminderCard value={editor.original} />}
          <fieldset disabled={locked}>
            <legend>変更内容（未保存）</legend>
            <label>
              題名
              <input
                aria-label="予定の題名"
                value={editor.draft.title}
                disabled={editor.action === "snooze"}
                onChange={(event) => change({ title: event.target.value })}
              />
            </label>
            <label>
              日時（UTCオフセット付き）
              <input
                aria-label="予定日時（UTCオフセット付き）"
                value={editor.draft.due_at}
                placeholder="2027-01-01T18:00:00+09:00"
                onChange={(event) => change({ due_at: event.target.value })}
              />
            </label>
            <label>
              タイムゾーン
              <input
                aria-label="予定のタイムゾーン"
                value={editor.draft.timezone}
                placeholder="Asia/Tokyo"
                onChange={(event) => change({ timezone: event.target.value })}
              />
            </label>
            <label>
              配信先の種類
              <select
                aria-label="配信先の種類"
                value={editor.draft.delivery_target.kind}
                disabled={!!editor.original}
                onChange={(event) =>
                  change({
                    delivery_target: {
                      ...editor.draft.delivery_target,
                      kind: event.target.value as "user" | "device",
                    },
                  })
                }
              >
                <option value="user">利用者</option>
                <option value="device">端末（Pi）</option>
              </select>
            </label>
            <label>
              配信先ID
              <input
                aria-label="配信先ID"
                value={editor.draft.delivery_target.id}
                disabled={!!editor.original}
                onChange={(event) =>
                  change({
                    delivery_target: { ...editor.draft.delivery_target, id: event.target.value },
                  })
                }
              />
            </label>
            <label>
              関連タスクID（任意）
              <input
                aria-label="関連タスクID（任意）"
                value={editor.draft.task_id}
                disabled={!!editor.original}
                onChange={(event) => change({ task_id: event.target.value })}
              />
            </label>
            <label>
              公開範囲
              <select
                aria-label="予定の保存範囲"
                value={editor.draft.visibility}
                disabled={!!editor.original}
                onChange={(event) =>
                  change({ visibility: event.target.value as Reminder["visibility"] })
                }
              >
                <option value="private">個人（自分だけ）</option>
                <option value="shared">共有（テナント内）</option>
              </select>
            </label>
            <label>
              表示先
              <select
                aria-label="予定の表示先"
                value={editor.draft.display_policy}
                disabled={!!editor.original}
                onChange={(event) =>
                  change({ display_policy: event.target.value as Reminder["display_policy"] })
                }
              >
                {Object.entries(policies).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            {editor.original && editor.action !== "snooze" && (
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  aria-label="この予定を取消"
                  checked={editor.draft.status === "cancelled"}
                  disabled={editor.original.status === "cancelled"}
                  onChange={(event) =>
                    change({
                      status: event.target.checked
                        ? "cancelled"
                        : (editor.original?.status ?? "scheduled"),
                    })
                  }
                />
                この予定を取消
              </label>
            )}
            {warning && <p className="memory-warning">{warning}</p>}
            <button
              className="button primary"
              type="submit"
              disabled={!!warning || !!state.conflict}
            >
              {editor.action === "snooze" ? "スヌーズを保存" : "予定を保存"}
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
      <ul className="memory-list" aria-label="現在のリマインダー">
        {state.items.map((value) => (
          <li className="card" key={value.id}>
            <ReminderCard value={value} />
            {surface === "mobile" && (
              <div className="memory-actions">
                <button
                  className="button secondary"
                  type="button"
                  disabled={selecting}
                  onClick={() => void controller.select(value.id)}
                >
                  編集・取消
                </button>
                <button
                  className="button secondary"
                  type="button"
                  disabled={selecting}
                  onClick={() => void controller.select(value.id, "snooze")}
                >
                  スヌーズ
                </button>
              </div>
            )}
          </li>
        ))}
      </ul>
      {!state.items.length && !state.busy && <p>表示できるリマインダーはありません。</p>}
      <section aria-label="アプリ内通知">
        <h3>未確認の通知と確認履歴</h3>
        <p>配信受付・端末表示・本人確認は別の状態です。開くだけでは本人確認に変わりません。</p>
        <button
          className="button secondary"
          type="button"
          disabled={notifications.busy || notifications.pending}
          onClick={() => void inbox.load()}
        >
          通知一覧を再取得
        </button>
        {notifications.notice && <p role="status">{notifications.notice}</p>}
        {notifications.pending && (
          <button
            className="button secondary"
            type="button"
            disabled={notifications.busy}
            onClick={() => void inbox.retry()}
          >
            同じ本人確認を再確認
          </button>
        )}
        <ul className="memory-list" aria-label="現在の通知">
          {notifications.items.map((value) => (
            <li className="card" key={value.id}>
              <h4>{value.title}</h4>
              <p>{deliveryStates[value.status]}</p>
              <button
                className="button secondary"
                type="button"
                disabled={notifications.busy || notifications.pending}
                onClick={() =>
                  void inbox.open({ tenantId: context.tenantId ?? "", notificationId: value.id })
                }
              >
                現在の通知を開く
              </button>
            </li>
          ))}
        </ul>
        {notifications.selected && (
          <section className="card" aria-label="取得した通知">
            <h4>{notifications.selected.notification.title}</h4>
            <p>{deliveryStates[notifications.selected.notification.status]}</p>
            <p>現在の予定版: {notifications.selected.reminder.schedule_version}</p>
            {surface === "mobile" && (
              <button
                className="button primary"
                type="button"
                disabled={
                  notifications.busy ||
                  notifications.pending ||
                  notifications.selected.notification.status === "acknowledged"
                }
                onClick={() => void inbox.acknowledge()}
              >
                本人確認済みにする
              </button>
            )}
          </section>
        )}
        {!notifications.items.length && !notifications.selected && !notifications.busy && (
          <p>現在表示できる通知はありません。</p>
        )}
      </section>
    </section>
  );
}
