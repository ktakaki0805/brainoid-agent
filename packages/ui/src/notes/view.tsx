import type { Note, Operation } from "@brainoid-internal/api-client";
import { useEffect, useState, useSyncExternalStore } from "react";
import type { SessionContext } from "../session.ts";
import { type NoteScope, NotesController, policies, validateDraft } from "./controller.ts";

const operationStatus: Record<Operation["status"], string> = {
  pending: "消去待ち",
  running: "消去処理中",
  succeeded: "消去完了",
  failed: "消去処理に失敗・未完了",
  outcome_unknown: "消去結果を確認中・未完了",
};
const when = (value: string) => new Date(value).toLocaleString("ja-JP", { timeZoneName: "short" });
function NoteCard({ note }: { note: Note }) {
  return (
    <div className="note-content">
      <div className="scope-badge">
        {note.visibility === "private" ? "個人" : "共有"} · {policies[note.display_policy]}
      </div>
      <h3>{note.title}</h3>
      <p className="note-body">{note.body || "本文はありません。"}</p>
      <p>更新: {when(note.updated_at)}</p>
    </div>
  );
}
export function Notes({
  context,
  surface,
}: {
  context: SessionContext;
  surface: "mobile" | "display";
}) {
  const [controller] = useState(() => new NotesController(context, surface));
  const state = useSyncExternalStore(controller.subscribe, controller.snapshot);
  const [query, setQuery] = useState("");
  const [includeDerived, setIncludeDerived] = useState(true);
  useEffect(() => {
    void controller.load();
    return () => controller.dispose();
  }, [controller]);
  if (state.closed) return null;
  const editor = state.editor,
    locked = state.busy || state.pending,
    selecting = locked || !!editor || !!state.target;
  return (
    <section className="card notes" aria-label="ノート">
      <div className="tenant-heading">
        <div>
          <p className="eyebrow">NOTES</p>
          <h2>{surface === "mobile" ? "ノートを管理" : "みんなのノート"}</h2>
        </div>
        {surface === "mobile" && (
          <button
            className="button primary"
            type="button"
            disabled={selecting}
            onClick={() => controller.create()}
          >
            ノートを作成
          </button>
        )}
      </div>
      <p>
        {surface === "mobile"
          ? "個人と共有のノートを、公開範囲と表示先を確認しながら管理できます。"
          : "この共有画面で表示を許可されたノートです。作成・編集・削除には本人確認が必要です。スマホから操作してください。"}
      </p>
      {surface === "mobile" && (
        <label>
          一覧の公開範囲
          <select
            aria-label="一覧の公開範囲"
            value={state.scope}
            disabled={selecting}
            onChange={(event) => void controller.scope(event.target.value as NoteScope)}
          >
            <option value="all">個人と共有</option>
            <option value="private">個人（自分だけ）</option>
            <option value="shared">共有（テナント内）</option>
          </select>
        </label>
      )}
      <form
        className="memory-search"
        onSubmit={(event) => {
          event.preventDefault();
          void controller.load(query.trim());
        }}
      >
        <label>
          ノートを検索
          <input
            value={query}
            maxLength={200}
            disabled={locked}
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
        <button className="button secondary" type="submit" disabled={locked}>
          検索
        </button>
        <button
          className="button secondary"
          type="button"
          disabled={locked}
          onClick={() => {
            setQuery("");
            void controller.load("");
          }}
        >
          一覧を再取得
        </button>
      </form>
      {state.query && <p>検索結果: {state.query}</p>}
      {(editor || state.target) && (
        <p>
          別のノートや公開範囲へ移るには、入力または削除確認を閉じてください。検索しても入力は保持します。
        </p>
      )}
      {state.busy && <p role="status">ノートの状態を確認しています。</p>}
      {state.notice && <p role="alert">{state.notice}</p>}
      {state.pending && (
        <div className="memory-warning">
          <p>受付済みの可能性があります。結果を確認するまで入力と別の保存・削除はできません。</p>
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
        <section className="memory-warning" aria-label="ノートの競合">
          <h3>別の更新があります</h3>
          <p>編集開始時</p>
          <NoteCard note={state.conflict.before} />
          <p>再取得した最新版</p>
          {state.conflict.current ? (
            <NoteCard note={state.conflict.current} />
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
          {editor ? (
            <>
              <p>
                下の入力は保持しています。最新版と比べ、必要な変更を入力へ反映してから保存してください。
              </p>
              <button
                className="button secondary"
                type="button"
                disabled={locked || !state.conflict.current}
                onClick={() => controller.continueEditing()}
              >
                最新版を確認して編集を続ける
              </button>
            </>
          ) : (
            <p>削除は受け付けられていません。一覧から対象を選び直して確認してください。</p>
          )}
        </section>
      )}
      {editor && (
        <form
          className="memory-editor"
          aria-label="ノートの保存フォーム"
          onSubmit={(event) => {
            event.preventDefault();
            void controller.save();
          }}
        >
          <h3>{editor.original ? "ノートを編集" : "新しいノート"}</h3>
          {editor.original && <p>公開範囲は変更できません。表示先は変更できます。</p>}
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
            <label>
              本文
              <textarea
                aria-label="本文"
                maxLength={8000}
                value={editor.draft.body}
                onChange={(event) =>
                  controller.draft({ ...editor.draft, body: event.target.value })
                }
              />
            </label>
            <label>
              公開範囲
              <select
                aria-label="公開範囲"
                disabled={!!editor.original}
                value={editor.draft.visibility}
                onChange={(event) =>
                  controller.draft({
                    ...editor.draft,
                    visibility: event.target.value as "private" | "shared",
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
                onChange={(event) =>
                  controller.draft({
                    ...editor.draft,
                    display_policy: event.target.value as Note["display_policy"],
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
            {validateDraft(editor.draft) && (
              <p className="memory-warning">{validateDraft(editor.draft)}</p>
            )}
            <button
              className="button primary"
              type="submit"
              disabled={!!validateDraft(editor.draft) || !!state.conflict}
            >
              {editor.original ? "変更を保存" : "ノートを保存"}
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
      {state.target && (
        <section className="memory-warning" aria-label="ノート削除の確認">
          <h3>このノートを削除しますか</h3>
          <NoteCard note={state.target} />
          <fieldset disabled={locked}>
            <label>
              ノートから作られた記憶
              <select
                aria-label="ノートから作られた記憶"
                value={String(includeDerived)}
                onChange={(event) => setIncludeDerived(event.target.value === "true")}
              >
                <option value="true">一緒に消去する</option>
                <option value="false">記録を残す</option>
              </select>
            </label>
            <p>
              削除受付後はノートを利用できなくなります。記憶の記録を残す場合も、このノートを出典とした回答への再利用は停止します。
            </p>
            <button
              className="button primary"
              type="button"
              onClick={() => void controller.remove(includeDerived)}
            >
              このノートの削除を依頼
            </button>
            <button
              className="button secondary"
              type="button"
              onClick={() => controller.closeEditor()}
            >
              削除をやめる
            </button>
          </fieldset>
        </section>
      )}
      <ul className="memory-list" aria-label="現在のノート">
        {state.items.map((note) => (
          <li className="card" key={note.id}>
            <NoteCard note={note} />
            {surface === "mobile" && (
              <div className="memory-actions">
                <button
                  className="button secondary"
                  type="button"
                  disabled={selecting}
                  onClick={() => void controller.select(note.id, "edit")}
                >
                  編集
                </button>
                <button
                  className="button secondary"
                  type="button"
                  disabled={selecting}
                  onClick={() => {
                    setIncludeDerived(true);
                    void controller.select(note.id, "delete");
                  }}
                >
                  削除
                </button>
              </div>
            )}
          </li>
        ))}
      </ul>
      {!state.items.length && !state.busy && <p>表示できるノートはありません。</p>}
      {!!state.operations.length && (
        <section aria-label="ノート削除の進捗">
          <h3>ノート削除の進捗</h3>
          <p>
            受付済みのノートは再利用を停止しています。消去完了は処理結果を確認してから表示します。
          </p>
          <ul>
            {state.operations.map(({ operation }) => (
              <li key={operation.operation_id}>
                <p>削除受付済み · {when(operation.created_at)}</p>
                <p role="status">{operationStatus[operation.status]}</p>
                <button
                  className="button secondary"
                  type="button"
                  disabled={locked}
                  onClick={() => void controller.refreshOperation(operation.operation_id)}
                >
                  消去状況を確認
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}
    </section>
  );
}
