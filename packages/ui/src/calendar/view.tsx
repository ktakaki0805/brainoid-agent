import { useEffect, useState, useSyncExternalStore } from "react";
import type { CalendarProposalCreate } from "@brainoid-internal/api-client";
import type { SessionContext } from "../session.ts";
import { type CalendarOAuth, timeLabel } from "./contracts.ts";
import { CalendarController } from "./controller.ts";

const statuses: Record<string, string> = {
  pending: "確認待ち",
  confirmed: "確認受付済み",
  executing: "実行中",
  succeeded: "成功",
  failed: "失敗",
  outcome_unknown: "結果不明",
  expired: "確認期限切れ",
  declined: "拒否済み",
  cancelled: "取消済み",
  running: "実行中",
};
const actions = { create: "予定を作成", update: "予定を変更", delete: "予定を削除" };
export function ProposalCard({ controller }: { controller: CalendarController }) {
  const state = useSyncExternalStore(controller.subscribe, controller.snapshot);
  const value = state.proposal;
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  if (!value || state.closed) return null;
  const body = value.normalized_request;
  const target =
    body.action !== "create" && state.selected?.id === value.integration_id
      ? state.events.find(
          (v) =>
            v.id === body.event_id &&
            v.calendar_id === body.calendar_id &&
            v.etag === body.provider_etag,
        )
      : undefined;
  const expired = now >= Date.parse(value.expires_at);
  const blocked =
    state.busy ||
    state.pending ||
    !!state.draft ||
    value.status !== "pending" ||
    !!state.operation ||
    expired;
  return (
    <section className="card calendar-proposal" aria-label="Calendar確認カード">
      <h3>{actions[value.action]} · 固定された確認内容</h3>
      <p>
        {state.operation ? "確認: 受付済み" : `状態: ${statuses[value.status]}`}
        {!state.operation && expired && value.status === "pending"
          ? "（確認期限を過ぎています）"
          : ""}
      </p>
      <p>
        カレンダー: <code>{body.calendar_id}</code>
      </p>
      <p>
        連携: <code>{value.integration_id}</code>
      </p>
      {body.action !== "create" && (
        <>
          <p>
            対象予定ID: <code>{body.event_id}</code>
          </p>
          <p>
            取得時の予定の版: <code>{body.provider_etag}</code>
          </p>
        </>
      )}
      {body.action !== "delete" && (
        <>
          <h4>{body.summary}</h4>
          <p>
            {"date" in body.start ? "終日" : "時刻指定"} · 開始: {timeLabel(body.start)}
          </p>
          <p>
            終了: {timeLabel(body.end)}
            {"date" in body.end ? "（この日は含みません）" : ""}
          </p>
          {body.description !== undefined && (
            <p className="calendar-description">{body.description}</p>
          )}
        </>
      )}
      {body.action === "delete" &&
        (target ? (
          <section aria-label="削除する予定の取得時情報">
            <h4>{target.summary || "予定"}</h4>
            <p>
              {"date" in target.start ? "終日" : "時刻指定"} · {timeLabel(target.start)} 〜{" "}
              {timeLabel(target.end)}
              {"date" in target.end ? "（終了日は含みません）" : ""}
            </p>
            <p className="hint">
              この予定IDと版に一致する取得時の情報です。確認後に予定が変更されていた場合は実行されません。
            </p>
          </section>
        ) : (
          <p>
            このIDの予定を削除します。題名と日時はGoogle
            Calendarで確認するか、下のカレンダー一覧から予定を取得して対象を照合してください。
          </p>
        ))}
      <p>確認期限: {value.expires_at}</p>
      <details>
        <summary>確認内容の識別子</summary>
        <p>
          Proposal: <code>{value.proposal_id}</code>
        </p>
        <p>
          内容の固定hash: <code>{value.args_hash}</code>
        </p>
      </details>
      {value.error_code && (
        <p role="alert">
          {value.error_code === "conflict"
            ? "Google側で予定が変更されました。予定を再取得し、内容を確認してください。"
            : `処理を完了できませんでした (${value.error_code})。状態を確認してください。`}
        </p>
      )}
      {state.operation && (
        <p role="status">
          書込処理:{" "}
          {state.operation.status === "pending"
            ? "受付済み（未完了）"
            : statuses[state.operation.status]}
          {state.operation.error_code ? ` (${state.operation.error_code})` : ""}
        </p>
      )}
      {(value.status === "outcome_unknown" || state.operation?.status === "outcome_unknown") && (
        <p role="alert">
          Googleへの反映結果が不明です。新しい候補で再実行せず、Googleの予定と処理状態を確認してください。
        </p>
      )}
      {controller.surface === "mobile" ? (
        <div className="button-row">
          <button
            className="button primary"
            type="button"
            disabled={blocked}
            onClick={() => void controller.decide("confirm")}
          >
            この内容で実行する
          </button>
          <button
            className="button secondary"
            type="button"
            disabled={blocked}
            onClick={() => void controller.decide("reject")}
          >
            拒否する
          </button>
          <button
            className="button secondary"
            type="button"
            disabled={state.busy || state.pending}
            onClick={() => void controller.refreshProposal()}
          >
            確認・書込状態を再取得
          </button>
        </div>
      ) : (
        <p>本人のスマホで内容を確認してください。この画面から確認・拒否はできません。</p>
      )}
      <p className="hint">
        確認の受付は書込完了を意味しません。取消や連携解除で、実行済みの変更が元に戻ることはありません。
      </p>
    </section>
  );
}
export function WaitingProposal({
  context,
  surface,
  id,
}: {
  context: SessionContext;
  surface: "mobile" | "display";
  id: string;
}) {
  const [controller] = useState(() => new CalendarController(context, surface));
  const state = useSyncExternalStore(controller.subscribe, controller.snapshot);
  useEffect(() => () => controller.dispose(), [controller]);
  if (state.closed) return null;
  if (surface === "display") return <p>Calendarの書込確認は本人のスマホで行ってください。</p>;
  return (
    <div>
      <button
        className="button secondary"
        type="button"
        disabled={state.busy || state.pending}
        onClick={() => void controller.openProposal(id)}
      >
        Calendarの確認内容を開く
      </button>
      {state.notice && <p role="alert">{state.notice}</p>}
      {state.pending && (
        <button
          className="button secondary"
          type="button"
          disabled={state.busy}
          onClick={() => void controller.retry()}
        >
          同じ確認操作の受付を再確認
        </button>
      )}
      <ProposalCard controller={controller} />
    </div>
  );
}
export function Calendar({
  context,
  surface,
  oauth,
}: {
  context: SessionContext;
  surface: "mobile" | "display";
  oauth?: CalendarOAuth | undefined;
}) {
  const [controller] = useState(() => new CalendarController(context, surface, oauth));
  const state = useSyncExternalStore(controller.subscribe, controller.snapshot);
  const [scope, setScope] = useState<"read" | "read_write">("read");
  const [range, setRange] = useState(state.range);
  const [disconnect, setDisconnect] = useState(false);
  useEffect(() => {
    void controller.load();
    return () => controller.dispose();
  }, [controller]);
  if (state.closed) return null;
  if (surface === "display")
    return (
      <section className="card calendar-workspace" aria-label="Calendar">
        <h2>カレンダー</h2>
        <p>連携設定と書込確認は本人のスマホで行ってください。</p>
      </section>
    );
  const disabled = state.busy || state.pending || state.oauthWaiting;
  const editingBlocked =
    disabled ||
    (!!state.operation &&
      ["pending", "running", "outcome_unknown"].includes(state.operation.status)) ||
    (!!state.proposal &&
      ["confirmed", "executing", "outcome_unknown"].includes(state.proposal.status));
  return (
    <section className="card calendar-workspace" aria-label="Calendar">
      <h2>カレンダー</h2>
      <p>自分のGoogle Calendarと連携します。参照するカレンダーを選び、書込内容は毎回確認します。</p>
      {state.notice && <p role="alert">{state.notice}</p>}
      {state.busy && <p role="status">Calendarの状態を確認しています。</p>}
      {state.pending && (
        <button
          className="button primary"
          type="button"
          disabled={state.busy}
          onClick={() => void controller.retry()}
        >
          同じ操作の受付を再確認
        </button>
      )}
      <div className="button-row">
        <label>
          連携時に要求する権限
          <select
            value={scope}
            disabled={disabled}
            onChange={(e) => setScope(e.target.value as typeof scope)}
          >
            <option value="read">予定の参照</option>
            <option value="read_write">予定の参照と書込</option>
          </select>
        </label>
        <button
          className="button primary"
          type="button"
          disabled={disabled || !oauth}
          onClick={() => void controller.authorize(scope)}
        >
          Google Calendarに接続・再接続
        </button>
        <button
          className="button secondary"
          type="button"
          disabled={disabled}
          onClick={() => void controller.load()}
        >
          連携一覧を再取得
        </button>
      </div>
      {!oauth && (
        <p>
          この端末のCalendar接続設定がありません。設定済みの本人スマホから接続してください。既存の連携は下で確認できます。
        </p>
      )}
      <p className="hint">
        要求した書込権限がすべて付与されるとは限りません。現在の権限は操作時に確認します。
      </p>
      {state.oauthWaiting && (
        <div>
          <p>システムブラウザーで同意を完了してください。</p>
          <button
            className="button secondary"
            type="button"
            disabled={state.busy}
            onClick={() => controller.finishOAuth()}
          >
            ブラウザー操作を終了して戻る
          </button>
        </div>
      )}
      {!state.busy && !state.integrations.length && <p>連携はありません。</p>}
      <ul className="calendar-list">
        {state.integrations.map((v) => (
          <li key={v.id}>
            <button
              className="button secondary"
              type="button"
              disabled={disabled}
              aria-pressed={state.selected?.id === v.id}
              onClick={() => {
                setDisconnect(false);
                void controller.select(v.id);
              }}
            >
              Google Calendar ·{" "}
              {v.status === "active"
                ? "接続済み"
                : v.status === "reauth_required"
                  ? "再接続が必要"
                  : "停止済み"}{" "}
              · {v.id}
            </button>
          </li>
        ))}
      </ul>
      {state.selected && (
        <section aria-label="選択したCalendar連携">
          <h3>連携の設定</h3>
          <p>
            状態:{" "}
            {state.selected.status === "active"
              ? "接続済み"
              : state.selected.status === "reauth_required"
                ? "権限を再取得してください"
                : "停止済み"}
          </p>
          <p>
            取得済みデータ・秘密の消去:{" "}
            {state.selected.cleanup_status === "pending"
              ? "処理中"
              : state.selected.cleanup_status === "complete"
                ? "完了"
                : "未要求"}
          </p>
          {state.selected.stop_reason && (
            <p>
              停止理由:{" "}
              {state.selected.stop_reason === "user_disconnected"
                ? "本人による解除"
                : state.selected.stop_reason === "permission_revoked"
                  ? "カレンダー権限の失効"
                  : "認可情報の期限切れ・失効"}
            </p>
          )}
          {state.selected.reconnect_allowed === false && (
            <p>現在、この連携の再接続は許可されていません。</p>
          )}
          {state.selected.status !== "active" && (
            <p>
              予定の参照・新しい書込は停止しています。再接続が許可される場合は上の接続ボタンから進めてください。
            </p>
          )}
          <button
            className="button secondary"
            type="button"
            disabled={disabled}
            onClick={() => void controller.refreshIntegration()}
          >
            連携・消去状態を再取得
          </button>
          {state.selected.status === "active" && (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void controller.saveSelection();
              }}
            >
              <label>
                参照するカレンダーID（1行に1個、最大10個）
                <textarea
                  value={(state.selectionDraft ?? []).join("\n")}
                  disabled={disabled}
                  onChange={(e) =>
                    controller.selectionDraft(
                      e.target.value === "" ? [] : e.target.value.split("\n"),
                    )
                  }
                />
              </label>
              <p className="hint">
                Google
                Calendarの設定でIDを確認して入力してください。空欄で保存すると参照対象がなくなります。
              </p>
              <button
                className="button secondary"
                disabled={disabled || !!state.conflict}
                type="submit"
              >
                カレンダーの選択を保存
              </button>
            </form>
          )}
          {state.conflict && (
            <section role="alert">
              <h4>連携の変更が競合しました</h4>
              <p>
                編集開始時: {state.conflict.before.allowed_calendar_ids?.join("、") || "選択なし"}
              </p>
              <p>
                最新版:{" "}
                {state.conflict.current?.allowed_calendar_ids?.join("、") || "選択なし／未取得"}
              </p>
              <button
                className="button secondary"
                type="button"
                disabled={disabled || !state.conflict.current}
                onClick={() => controller.acceptCurrentSelection()}
              >
                最新版を基準に入力を見直す
              </button>
            </section>
          )}
          {!disconnect ? (
            <button
              className="button secondary"
              type="button"
              disabled={disabled || state.selected.status === "revoked"}
              onClick={() => setDisconnect(true)}
            >
              この連携を解除
            </button>
          ) : (
            <section aria-label="連携解除の確認">
              <p>
                この連携を停止し、保存した秘密・派生データを消去します。Google全体の同意取消は行いません。実行済みの予定は元に戻りません。
              </p>
              <button
                className="button danger"
                type="button"
                disabled={disabled || !!state.conflict}
                onClick={() => {
                  setDisconnect(false);
                  void controller.disconnect();
                }}
              >
                連携を停止して消去を依頼
              </button>
              <button
                className="button secondary"
                type="button"
                disabled={disabled}
                onClick={() => setDisconnect(false)}
              >
                戻る
              </button>
            </section>
          )}
          {state.selected.status === "active" && (
            <>
              <h3>直近の予定</h3>
              <label>
                開始日時（UTCオフセット付き）
                <input
                  value={range.time_min}
                  disabled={disabled}
                  onChange={(e) => setRange({ ...range, time_min: e.target.value })}
                />
              </label>
              <label>
                終了日時（UTCオフセット付き、31日以内）
                <input
                  value={range.time_max}
                  disabled={disabled}
                  onChange={(e) => setRange({ ...range, time_max: e.target.value })}
                />
              </label>
              <div className="button-row">
                {state.selected.allowed_calendar_ids?.map((id) => (
                  <button
                    className="button secondary"
                    type="button"
                    key={id}
                    disabled={disabled}
                    aria-pressed={id === state.calendar}
                    onClick={() => void controller.events(id, range)}
                  >
                    {id} の予定を取得
                  </button>
                ))}
              </div>
              {!state.selected.allowed_calendar_ids?.length && (
                <p>参照するカレンダーIDを設定してください。</p>
              )}
            </>
          )}
        </section>
      )}
      {state.calendar && (
        <section aria-label="カレンダーの予定">
          <h3>{state.calendar}</h3>
          <p>
            {state.range.time_min} 〜 {state.range.time_max}
          </p>
          {!state.busy && !state.events.length && <p>取得済みの予定はありません。</p>}
          <button
            className="button primary"
            type="button"
            disabled={editingBlocked}
            onClick={() => controller.edit("create")}
          >
            予定の作成内容を入力
          </button>
          <ul className="calendar-list">
            {state.events.map((v) => (
              <li className="card" key={v.id}>
                <h4>{v.summary || "予定"}</h4>
                <p>
                  {"date" in v.start ? "終日" : "時刻指定"} · {timeLabel(v.start)} 〜{" "}
                  {timeLabel(v.end)}
                  {"date" in v.end ? "（終了日は含みません）" : ""}
                </p>
                {v.description !== undefined && (
                  <p className="calendar-description">{v.description}</p>
                )}
                <p>
                  予定ID: <code>{v.id}</code>
                </p>
                {v.fetched_at && <p>取得日時: {v.fetched_at}</p>}
                <div className="button-row">
                  <button
                    className="button secondary"
                    type="button"
                    disabled={editingBlocked}
                    onClick={() => controller.edit("update", v.id)}
                  >
                    変更内容を入力
                  </button>
                  <button
                    className="button secondary"
                    type="button"
                    disabled={editingBlocked}
                    onClick={() => controller.edit("delete", v.id)}
                  >
                    削除内容を確認
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}
      {state.draft && <Editor controller={controller} draft={state.draft} disabled={disabled} />}
      <ProposalCard controller={controller} />
    </section>
  );
}
function Editor({
  controller,
  draft,
  disabled,
}: {
  controller: CalendarController;
  draft: CalendarProposalCreate;
  disabled: boolean;
}) {
  return (
    <form
      className="card calendar-editor"
      aria-label="Calendar候補の入力"
      onSubmit={(e) => {
        e.preventDefault();
        void controller.propose();
      }}
    >
      <h3>{actions[draft.action]} · 候補の入力</h3>
      <p>カレンダー: {draft.calendar_id}</p>
      {draft.action !== "create" && <p>対象予定ID: {draft.event_id}</p>}
      {draft.action !== "delete" && (
        <>
          <label>
            予定の題名
            <input
              maxLength={200}
              value={draft.summary}
              disabled={disabled}
              onChange={(e) => controller.draft({ ...draft, summary: e.target.value })}
            />
          </label>
          <label>
            説明
            <textarea
              maxLength={4000}
              value={draft.description ?? ""}
              disabled={disabled}
              onChange={(e) => controller.draft({ ...draft, description: e.target.value })}
            />
          </label>
          <label>
            日時の種類
            <select
              value={"date" in draft.start ? "date" : "time"}
              disabled={disabled}
              onChange={(e) =>
                controller.draft(
                  e.target.value === "date"
                    ? { ...draft, start: { date: "" }, end: { date: "" } }
                    : {
                        ...draft,
                        start: { date_time: "", timezone: "Asia/Tokyo" },
                        end: { date_time: "", timezone: "Asia/Tokyo" },
                      },
                )
              }
            >
              <option value="time">時刻とタイムゾーン</option>
              <option value="date">終日</option>
            </select>
          </label>
          {(["start", "end"] as const).map((key) => {
            const point = draft[key];
            return (
              <div key={key}>
                <label>
                  {key === "start" ? "開始" : "終了"}
                  {"date" in point
                    ? "日（終日は終了日を含みません）"
                    : "日時（例: 2027-01-01T18:00:00+09:00）"}
                  <input
                    type={"date" in point ? "date" : "text"}
                    value={"date" in point ? point.date : point.date_time}
                    disabled={disabled}
                    onChange={(e) =>
                      controller.draft({
                        ...draft,
                        [key]:
                          "date" in point
                            ? { date: e.target.value }
                            : { ...point, date_time: e.target.value },
                      })
                    }
                  />
                </label>
                {"date_time" in point && (
                  <label>
                    {key === "start" ? "開始" : "終了"}タイムゾーン
                    <input
                      value={point.timezone}
                      disabled={disabled}
                      onChange={(e) =>
                        controller.draft({
                          ...draft,
                          [key]: { ...point, timezone: e.target.value },
                        })
                      }
                    />
                  </label>
                )}
              </div>
            );
          })}
        </>
      )}
      <p>内容を変えるたびに、新しい確認候補を作成します。以前の確認は使いません。</p>
      <div className="button-row">
        <button className="button primary" type="submit" disabled={disabled}>
          固定された確認候補を作成
        </button>
        <button
          className="button secondary"
          type="button"
          disabled={disabled}
          onClick={() => controller.closeDraft()}
        >
          入力を閉じる
        </button>
      </div>
    </form>
  );
}
