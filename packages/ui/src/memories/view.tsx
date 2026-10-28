import { useEffect, useState, useSyncExternalStore } from "react";
import type { Entity, Memory, MemoryCreate, Operation } from "@brainoid-internal/api-client";
import type { SessionContext } from "../session.ts";
import {
  type ForgetPolicy,
  MemoryController,
  policies,
  predicates,
  validateDraft,
} from "./controller.ts";

const status = {
  candidate: "候補・未確定",
  confirmed: "確定",
  superseded: "訂正済み・過去の記憶",
  retracted: "撤回済み",
};
const operationStatus: Record<Operation["status"], string> = {
  pending: "消去待ち",
  running: "消去処理中",
  succeeded: "消去完了",
  failed: "消去処理に失敗・未完了",
  outcome_unknown: "消去結果を確認中・未完了",
};
const when = (value: string) => new Date(value).toLocaleString("ja-JP", { timeZoneName: "short" });
const entityName = (entities: readonly Entity[], id: string) =>
  entities.find((entity) => entity.id === id)?.label ?? `対象 ${id}`;
export function MemoryCard({
  memory,
  entities,
  uid,
}: {
  memory: Memory;
  entities: readonly Entity[];
  uid: string;
}) {
  const period =
    Date.parse(memory.valid_from) > Date.now()
      ? "開始前"
      : memory.valid_to && Date.parse(memory.valid_to) <= Date.now()
        ? "有効期間終了"
        : "有効期間内";
  return (
    <div className="memory-fact">
      <div className="scope-badge">
        {memory.visibility === "private" ? "個人" : "共有"} · {policies[memory.display_policy]}
      </div>
      <h3>
        {entityName(entities, memory.subject_entity_id)}の{predicates[memory.predicate]}
      </h3>
      <p className="memory-value">
        {memory.object_entity_id
          ? entityName(entities, memory.object_entity_id)
          : String(memory.value)}
      </p>
      <p>
        {status[memory.status]} · {period}
        {memory.origin === "inferred" ? " · AIによる候補" : ""}
      </p>
      {memory.qualifiers?.context && <p>場面: {memory.qualifiers.context}</p>}
      {memory.qualifiers?.brightness_pct !== undefined && (
        <p>明るさ: {memory.qualifiers.brightness_pct}%</p>
      )}
      {memory.qualifiers?.temperature_c !== undefined && (
        <p>温度: {memory.qualifiers.temperature_c}℃</p>
      )}
      <p>
        有効期間: {when(memory.valid_from)} 〜{" "}
        {memory.valid_to ? when(memory.valid_to) : "終了指定なし"}
      </p>
      <div className="memory-source">
        <strong>出典</strong>
        {memory.source_refs.length ? (
          <ul>
            {memory.source_refs.map((source) => (
              <li key={`${source.kind}:${source.resource_id}:${source.version}`}>
                {source.kind === "message"
                  ? "会話"
                  : source.kind === "note"
                    ? "ノート"
                    : "取り込み資料"}{" "}
                · {source.resource_id}（版 {source.version}）
              </li>
            ))}
          </ul>
        ) : (
          <p>
            {memory.created_by === uid ? "あなた" : memory.created_by}が明示保存 ·{" "}
            {when(memory.recorded_at)}
          </p>
        )}
      </div>
      {memory.supersedes_fact_id && <p>以前の記憶を訂正した内容です。</p>}
    </div>
  );
}
export function Memories({
  context,
  surface,
}: {
  context: SessionContext;
  surface: "mobile" | "display";
}) {
  const [controller] = useState(() => new MemoryController(context, surface));
  const state = useSyncExternalStore(controller.subscribe, controller.snapshot);
  const [query, setQuery] = useState("");
  const [subjectName, setSubjectName] = useState("");
  const [subjectKind, setSubjectKind] = useState<Entity["kind"]>("topic");
  const [policy, setPolicy] = useState<ForgetPolicy>("suppress_reuse");
  useEffect(() => {
    void controller.load();
    return () => controller.dispose();
  }, [controller]);
  if (state.closed) return null;
  const editor = state.editor;
  return (
    <section className="card memories" aria-label="記憶">
      <div className="tenant-heading">
        <div>
          <p className="eyebrow">MEMORIES</p>
          <h2>{surface === "mobile" ? "記憶を管理" : "みんなの記憶"}</h2>
        </div>
        {surface === "mobile" && (
          <button
            className="button primary"
            type="button"
            disabled={state.busy || state.pending}
            onClick={() => controller.create()}
          >
            記憶を保存する
          </button>
        )}
      </div>
      <p>
        {surface === "mobile"
          ? "保存した内容と出典を確認し、訂正や忘却ができます。"
          : "この画面で表示を許可された記憶です。管理はスマホから行ってください。"}
      </p>
      <form
        className="memory-search"
        onSubmit={(event) => {
          event.preventDefault();
          void controller.load(query.trim());
        }}
      >
        <label>
          記憶を検索
          <input
            value={query}
            maxLength={1000}
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
        <button className="button secondary" type="submit" disabled={state.busy}>
          検索
        </button>
        <button
          className="button secondary"
          type="button"
          disabled={state.busy}
          onClick={() => {
            setQuery("");
            void controller.load("");
          }}
        >
          一覧を再取得
        </button>
      </form>
      {state.busy && <p role="status">記憶の状態を確認しています。</p>}
      {state.notice && <p role="alert">{state.notice}</p>}
      {state.pending && (
        <div className="memory-warning">
          <p>この要求の結果を確認するまで、内容の変更と別の保存・忘却はできません。</p>
          <button
            className="button secondary"
            disabled={state.busy}
            type="button"
            onClick={() => void controller.retry()}
          >
            同じ内容で受付を再確認
          </button>
        </div>
      )}
      {state.conflict && (
        <section className="memory-warning" aria-label="記憶の競合">
          <h3>更新の差分</h3>
          <p>編集開始時</p>
          <MemoryCard memory={state.conflict.before} entities={state.entities} uid={context.uid} />
          <p>再取得した状態</p>
          {state.conflict.current ? (
            <MemoryCard
              memory={state.conflict.current}
              entities={state.entities}
              uid={context.uid}
            />
          ) : (
            <p>現在の状態を取得できていません。一覧を再取得してください。</p>
          )}
          <p>
            入力中の内容は下に保持しています。過去の記憶を上書きせず、現行の記憶を一覧から選び直してください。
          </p>
        </section>
      )}
      {editor && (
        <form
          className="memory-editor"
          aria-label="記憶の保存フォーム"
          onSubmit={(event) => {
            event.preventDefault();
            void controller.save();
          }}
        >
          <h3>{editor.original ? "記憶を訂正" : "新しい記憶"}</h3>
          {editor.original && <p>公開範囲と表示先は元の記憶から引き継ぎます。</p>}
          <fieldset disabled={state.busy || state.pending || !!state.conflict}>
            <label>
              対象
              <select
                aria-label="対象"
                required
                value={editor.draft.subject_entity_id}
                onChange={(event) =>
                  controller.draft({ ...editor.draft, subject_entity_id: event.target.value })
                }
              >
                <option value="">対象を選ぶ</option>
                {state.entities.map((entity) => (
                  <option key={entity.id} value={entity.id}>
                    {entity.label}（{entity.visibility === "private" ? "個人" : "共有"}）
                  </option>
                ))}
              </select>
            </label>
            {!state.entities.length && (
              <p>利用できる対象がありません。下の「新しい対象を登録」で追加できます。</p>
            )}
            <label>
              記憶の種類
              <select
                aria-label="記憶の種類"
                value={editor.draft.predicate}
                onChange={(event) =>
                  controller.draft({
                    ...editor.draft,
                    predicate: event.target.value as MemoryCreate["predicate"],
                    qualifiers: editor.draft.qualifiers?.context
                      ? { context: editor.draft.qualifiers.context }
                      : {},
                  })
                }
              >
                {Object.entries(predicates).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              内容の形式
              <select
                aria-label="内容の形式"
                value={
                  editor.draft.object_entity_id !== undefined ? "entity" : typeof editor.draft.value
                }
                onChange={(event) => {
                  const { value: _value, object_entity_id: _object, ...rest } = editor.draft;
                  controller.draft({
                    ...rest,
                    ...(event.target.value === "entity"
                      ? { object_entity_id: "" }
                      : {
                          value:
                            event.target.value === "number"
                              ? 0
                              : event.target.value === "boolean"
                                ? true
                                : "",
                        }),
                  });
                }}
              >
                <option value="string">文章</option>
                <option value="number">数値</option>
                <option value="boolean">はい・いいえ</option>
                <option value="entity">別の対象との関係</option>
              </select>
            </label>
            {editor.draft.object_entity_id !== undefined ? (
              <label>
                関係先
                <select
                  aria-label="関係先"
                  required
                  value={editor.draft.object_entity_id}
                  onChange={(event) =>
                    controller.draft({ ...editor.draft, object_entity_id: event.target.value })
                  }
                >
                  <option value="">関係先を選ぶ</option>
                  {state.entities.map((entity) => (
                    <option key={entity.id} value={entity.id}>
                      {entity.label}
                    </option>
                  ))}
                </select>
              </label>
            ) : typeof editor.draft.value === "boolean" ? (
              <label>
                内容
                <select
                  aria-label="内容"
                  value={String(editor.draft.value)}
                  onChange={(event) =>
                    controller.draft({ ...editor.draft, value: event.target.value === "true" })
                  }
                >
                  <option value="true">はい</option>
                  <option value="false">いいえ</option>
                </select>
              </label>
            ) : typeof editor.draft.value === "number" ? (
              <label>
                内容
                <input
                  type="number"
                  step="any"
                  required
                  value={Number.isFinite(editor.draft.value) ? editor.draft.value : ""}
                  onChange={(event) =>
                    controller.draft({ ...editor.draft, value: event.target.valueAsNumber })
                  }
                />
              </label>
            ) : (
              <label>
                内容
                <textarea
                  aria-label="内容"
                  required
                  maxLength={4000}
                  value={editor.draft.value ?? ""}
                  onChange={(event) =>
                    controller.draft({ ...editor.draft, value: event.target.value })
                  }
                />
              </label>
            )}
            <label>
              場面（任意）
              <input
                maxLength={200}
                value={editor.draft.qualifiers?.context ?? ""}
                onChange={(event) => {
                  const { context: _context, ...rest } = editor.draft.qualifiers ?? {};
                  controller.draft({
                    ...editor.draft,
                    qualifiers: {
                      ...rest,
                      ...(event.target.value ? { context: event.target.value } : {}),
                    },
                  });
                }}
              />
            </label>
            {editor.draft.predicate === "prefers_setting" &&
              (["brightness_pct", "temperature_c"] as const).map((key) => (
                <label key={key}>
                  {key === "brightness_pct" ? "明るさ（%・任意）" : "温度（℃・任意）"}
                  <input
                    type="number"
                    min={key === "brightness_pct" ? 0 : 16}
                    max={key === "brightness_pct" ? 100 : 30}
                    step={key === "brightness_pct" ? 1 : "any"}
                    value={editor.draft.qualifiers?.[key] ?? ""}
                    onChange={(event) => {
                      const qualifiers = { ...editor.draft.qualifiers };
                      delete qualifiers[key];
                      if (event.target.value) qualifiers[key] = event.target.valueAsNumber;
                      controller.draft({ ...editor.draft, qualifiers });
                    }}
                  />
                </label>
              ))}
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
                disabled={!!editor.original}
                value={editor.draft.display_policy}
                onChange={(event) =>
                  controller.draft({
                    ...editor.draft,
                    display_policy: event.target.value as MemoryCreate["display_policy"],
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
            <details className="memory-subject">
              <summary>新しい対象を登録</summary>
              <p>
                この記憶の公開範囲と表示先で、別の対象として登録します。同じ名前でも自動的に統合しません。
              </p>
              <label>
                対象の名前
                <input
                  aria-label="対象の名前"
                  maxLength={200}
                  value={subjectName}
                  onChange={(event) => setSubjectName(event.target.value)}
                />
              </label>
              <label>
                対象の種類
                <select
                  aria-label="対象の種類"
                  value={subjectKind}
                  onChange={(event) => setSubjectKind(event.target.value as Entity["kind"])}
                >
                  <option value="topic">話題</option>
                  <option value="person">人</option>
                  <option value="room">部屋</option>
                  <option value="appliance">機器</option>
                  <option value="activity">活動</option>
                </select>
              </label>
              <button
                className="button secondary"
                type="button"
                disabled={
                  !subjectName.trim() ||
                  (editor.draft.visibility === "private" &&
                    editor.draft.display_policy === "shared_display")
                }
                onClick={() => void controller.createSubject(subjectName, subjectKind)}
              >
                この範囲で対象を登録
              </button>
            </details>
            <DateInput
              label="有効開始（空欄は保存時刻）"
              value={editor.draft.valid_from}
              change={(value) => {
                const { valid_from: _start, ...rest } = editor.draft;
                controller.draft({ ...rest, ...(value ? { valid_from: value } : {}) });
              }}
            />
            <DateInput
              label="有効終了（任意）"
              value={editor.draft.valid_to ?? undefined}
              change={(value) => controller.draft({ ...editor.draft, valid_to: value || null })}
            />
            <p>この入力を、あなたと保存時刻を出典として記録します。</p>
            {validateDraft(editor.draft) && (
              <p className="memory-warning">{validateDraft(editor.draft)}</p>
            )}
            <button
              className="button primary"
              type="submit"
              disabled={!!validateDraft(editor.draft)}
            >
              {editor.original ? "訂正を保存" : "明示的に保存"}
            </button>
          </fieldset>
          <button
            className="button secondary"
            type="button"
            disabled={state.busy || state.pending}
            onClick={() => controller.closeEditor()}
          >
            入力を閉じる
          </button>
        </form>
      )}
      {state.target && (
        <section className="memory-warning" aria-label="忘却の確認">
          <h3>この記憶を忘却しますか</h3>
          <MemoryCard memory={state.target} entities={state.entities} uid={context.uid} />
          <fieldset disabled={state.busy || state.pending}>
            <label>
              元の出典の扱い
              <select
                aria-label="元の出典の扱い"
                value={policy}
                onChange={(event) => setPolicy(event.target.value as ForgetPolicy)}
              >
                <option value="suppress_reuse">出典は残し、再利用を停止する</option>
                <option value="delete_source">削除権限のある元出典も削除する</option>
              </select>
            </label>
            <p>
              出典を残しても、回答・検索・再抽出への再利用を停止します。元出典の削除はサーバーで権限を確認します。
            </p>
            <button
              className="button primary"
              type="button"
              onClick={() => void controller.forget(policy)}
            >
              この記憶の忘却を依頼
            </button>
            <button
              className="button secondary"
              type="button"
              onClick={() => controller.closeEditor()}
            >
              やめる
            </button>
          </fieldset>
        </section>
      )}
      <ul className="memory-list" aria-label="現在の記憶">
        {state.items.map((memory) => (
          <li className="card" key={memory.id}>
            <MemoryCard memory={memory} entities={state.entities} uid={context.uid} />
            {surface === "mobile" && (
              <div className="memory-actions">
                <button
                  className="button secondary"
                  type="button"
                  disabled={state.busy || state.pending}
                  onClick={() => void controller.select(memory.id, "revise")}
                >
                  訂正
                </button>
                <button
                  className="button secondary"
                  type="button"
                  disabled={state.busy || state.pending}
                  onClick={() => {
                    setPolicy("suppress_reuse");
                    void controller.select(memory.id, "forget");
                  }}
                >
                  忘却
                </button>
              </div>
            )}
          </li>
        ))}
      </ul>
      {!state.items.length && !state.busy && <p>表示できる記憶はありません。</p>}
      {!!state.operations.length && (
        <section aria-label="忘却の進捗">
          <h3>忘却の進捗</h3>
          <p>
            受付済みの記憶は再利用を停止しています。消去完了は処理結果を確認してから表示します。
          </p>
          <ul>
            {state.operations.map(({ operation }) => (
              <li key={operation.operation_id}>
                <p>忘却受付済み · {when(operation.created_at)}</p>
                <p role="status">{operationStatus[operation.status]}</p>
                <button
                  className="button secondary"
                  type="button"
                  disabled={state.busy}
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
function DateInput({
  label,
  value,
  change,
}: {
  label: string;
  value: string | undefined;
  change: (value: string) => void;
}) {
  const local = value
    ? new Date(Date.parse(value) - new Date(value).getTimezoneOffset() * 60_000)
        .toISOString()
        .slice(0, 16)
    : "";
  return (
    <label>
      {label}
      <input
        aria-label={label}
        type="datetime-local"
        value={local}
        onChange={(event) =>
          change(event.target.value ? new Date(event.target.value).toISOString() : "")
        }
      />
    </label>
  );
}
