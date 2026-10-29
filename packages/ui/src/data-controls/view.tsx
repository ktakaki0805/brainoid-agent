import { useEffect, useState, useSyncExternalStore } from "react";
import type { Operation } from "@brainoid-internal/api-client";
import type { SessionController } from "../session.ts";
import { DataControlsController, unavailableDelivery, type ExportDelivery } from "./controller.ts";

export const browserExportDelivery: ExportDelivery = {
  available: true,
  async save(blob, signal) {
    signal.throwIfAborted();
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    try {
      signal.throwIfAborted();
      anchor.href = url;
      anchor.download = "brainoid-export.zip";
      anchor.click();
    } finally {
      anchor.removeAttribute("href");
      anchor.remove();
      URL.revokeObjectURL(url);
    }
  },
};
const labels: Record<Operation["kind"], string> = {
  export: "エクスポート",
  delete_resource: "会話の削除",
  forget_memory: "記憶の忘却",
  leave_tenant: "テナントから退出",
  delete_account: "本人アカウントの削除",
  delete_tenant: "テナント全体の削除",
  calendar_write: "Calendar操作",
};
export function OperationStatus({ operation }: { operation: Operation }) {
  const state = {
    pending: "受付済み・処理待ち（完了ではありません）",
    running: "処理中（完了ではありません）",
    succeeded: operation.kind === "export" ? "生成完了" : "サーバーで削除処理の完了を確認しました",
    failed: "処理に失敗しました。消去完了ではありません。",
    outcome_unknown: "処理結果を確認できません。消去完了ではありません。",
  }[operation.status];
  return (
    <section aria-label="要求の進捗">
      <h3>{labels[operation.kind]}</h3>
      <p role="status">{state}</p>
      <p>受付番号: {operation.operation_id}</p>
      {operation.download_expires_at && (
        <p>
          取得期限:{" "}
          <time dateTime={operation.download_expires_at}>{operation.download_expires_at}</time>
        </p>
      )}
    </section>
  );
}
export function DataControls({
  session,
  surface,
  delivery = unavailableDelivery,
}: {
  session: SessionController;
  surface: "mobile" | "display";
  delivery?: ExportDelivery | undefined;
}) {
  return surface === "display" ? (
    <section className="card" aria-label="データの管理">
      <h2>データの管理</h2>
      <p>
        本人のスマホでエクスポート・退出・削除を行ってください。共有画面には個人の受付情報を表示しません。
      </p>
    </section>
  ) : (
    <MobileDataControls session={session} delivery={delivery} />
  );
}
function MobileDataControls({
  session,
  delivery,
}: {
  session: SessionController;
  delivery: ExportDelivery;
}) {
  const [controller] = useState(() => new DataControlsController(session, delivery));
  useEffect(() => () => controller.dispose(), [controller]);
  return <DataControlsForm controller={controller} />;
}
/** The public entry supplies an account-only controller; confirmation/progress stay shared. */
export function DataControlsForm({ controller }: { controller: DataControlsController }) {
  const { session, delivery } = controller;
  const accountOnly = controller.options.accountOnly === true;
  const state = useSyncExternalStore(controller.subscribe, controller.snapshot);
  const current = useSyncExternalStore(session.subscribe, session.snapshot);
  const [name, setName] = useState("");
  const [scope, setScope] = useState<"my_data" | "visible_tenant_data">("my_data");
  const [derived, setDerived] = useState(true);
  const [, tick] = useState(0);
  useEffect(() => {
    setName("");
    setDerived(current.context?.generation !== undefined);
  }, [current.context?.generation]);
  useEffect(() => {
    const timer = setInterval(() => tick((v) => v + 1), 1000);
    return () => clearInterval(timer);
  }, []);
  const locked =
    state.busy || state.pending || !!state.operation || !!state.recovery || !!state.review;
  return (
    <section className="card" aria-label="データの管理">
      <h2>{accountOnly ? "本人アカウントの削除申請" : "データの管理"}</h2>
      {accountOnly ? (
        <p>
          アカウント削除は現在サインインしている本人のすべての所属・本人だけに公開したデータ・個人連携・通知経路が対象です。共有寄稿は自動的に全消去されません。テナント全体の削除とは異なります。
        </p>
      ) : (
        <ul>
          <li>
            会話削除は会話の本文・応答の履歴・音声への参照を削除します。派生記憶の同時削除は既定で有効です。
          </li>
          <li>
            記憶の忘却は記憶とその検索用データ・要約・一時保存を削除します。元出典は既定でモデル再利用を止め、出典削除は別途選択します。下の記憶一覧で対象と範囲を確認してください。
          </li>
          <li>
            退出は現在のテナントの所属と本人だけに公開したデータを削除します。共有した寄稿は残る場合があります。
          </li>
          <li>
            アカウント削除は本人のすべての所属・本人だけに公開したデータ・個人連携・通知経路が対象です。共有寄稿は自動的に全消去されません。
          </li>
          <li>
            テナント削除は所有者（owner）が現在のテナント全体を削除します。他のテナントは対象になりません。
          </li>
        </ul>
      )}
      <p>
        退出・アカウント削除・テナント削除には5分以内の本人認証が必要です。最後の所有者（owner）は会員設定で先に所有権を移譲するか、テナント削除を選んでください。
      </p>
      <p>
        削除要求の受付後は対象の利用を止め、実際のデータ消去を順次進めます。受付や認証終了だけでは消去完了を確認できません。
      </p>
      {!accountOnly && current.context && current.tenant && (
        <>
          <h3>現在の対象: {current.tenant.name}</h3>
          <label>
            エクスポートの範囲
            <select
              value={scope}
              disabled={locked}
              onChange={(e) => setScope(e.target.value as typeof scope)}
            >
              <option value="my_data">本人だけに公開したデータと本人の共有寄稿</option>
              <option value="visible_tenant_data">現在閲覧できる共有データ</option>
            </select>
          </label>
          <p>
            どちらも他人だけに公開されたデータは含みません。生成後も取得時に権限と期限を確認します。忘却・退出・削除・権限変更で失効します。
          </p>
          <button
            className="button secondary"
            type="button"
            disabled={locked}
            onClick={() => void controller.prepare("export", { scope })}
          >
            エクスポートを確認
          </button>
          {!delivery.available && (
            <p>
              この端末ではファイル保存を準備できませんでした。アプリの設定と対応状況を確認してください。
            </p>
          )}
          <button
            className="button secondary"
            type="button"
            disabled={locked}
            onClick={() => void controller.loadConversations()}
          >
            削除する会話を選ぶ
          </button>
          {state.conversations.length > 0 && (
            <>
              <label>
                <input
                  type="checkbox"
                  checked={derived}
                  disabled={locked}
                  onChange={(e) => setDerived(e.target.checked)}
                />
                派生記憶も同時に削除する
              </label>
              <ul>
                {state.conversations.map((v) => (
                  <li key={v.id}>
                    <button
                      className="button secondary"
                      type="button"
                      disabled={locked}
                      onClick={() =>
                        void controller.prepare("conversation", { conversation: v.id, derived })
                      }
                    >
                      会話「{v.title ?? v.id}」の削除を確認
                    </button>
                  </li>
                ))}
              </ul>
              {state.cursor && (
                <button
                  className="button secondary"
                  type="button"
                  disabled={locked}
                  onClick={() => void controller.loadConversations(true)}
                >
                  会話の続きを表示
                </button>
              )}
            </>
          )}
          <button
            className="button secondary"
            type="button"
            disabled={locked}
            onClick={() => void controller.prepare("leave_tenant")}
          >
            このテナントからの退出を確認
          </button>
          {current.tenant.my_role === "owner" && (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void controller.prepare("delete_tenant", { name });
              }}
            >
              <label>
                削除するテナント名を入力
                <input
                  required
                  maxLength={200}
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  disabled={locked}
                />
              </label>
              <button
                className="button secondary"
                disabled={locked || name !== current.tenant.name}
                type="submit"
              >
                テナント全体の削除を確認
              </button>
            </form>
          )}
        </>
      )}
      <button
        className="button secondary"
        type="button"
        disabled={locked}
        onClick={() => void controller.prepare("delete_account")}
      >
        本人アカウントの削除を確認
      </button>
      {state.review && !state.reauthentication && (
        <section aria-label="データ操作の確認">
          <h3>{labels[state.review.body.kind]}</h3>
          <p>対象: {state.review.label}</p>
          {state.review.body.kind === "delete_resource" && (
            <>
              <p>
                派生記憶:{" "}
                {state.review.body.include_derived_memories
                  ? "同時に削除"
                  : "保持（元の会話は削除）"}
              </p>
              <p>共有会話の削除では、この会話内の他の参加者の発言も削除対象です。</p>
            </>
          )}
          <p>この対象と範囲で要求を送信します。</p>
          {state.review.body.kind !== "export" && <p>削除したデータは元に戻せません。</p>}
          <button
            className="button primary"
            type="button"
            disabled={state.busy}
            onClick={() => void controller.confirm()}
          >
            対象と範囲を確認して要求
          </button>
          <button
            className="button secondary"
            type="button"
            disabled={state.busy}
            onClick={() => controller.cancel()}
          >
            戻る
          </button>
        </section>
      )}
      {state.label && <p>要求の対象: {state.label}</p>}
      {state.notice && <p role="status">{state.notice}</p>}
      {state.pending && (
        <button
          className="button secondary"
          type="button"
          disabled={state.busy}
          onClick={() => void controller.retry()}
        >
          同じ要求の受付を確認
        </button>
      )}
      {state.rejected && (
        <button
          className="button secondary"
          type="button"
          disabled={state.busy}
          onClick={() => void controller.resetRejected()}
        >
          拒否された要求を閉じて対象を選び直す
        </button>
      )}
      {state.reauthentication &&
        (session.canReauthenticate() ? (
          <Reauthentication controller={controller} busy={state.busy} />
        ) : (
          <p>この認証方式ではアプリ内の再認証を利用できません。本人として再ログインが必要です。</p>
        ))}
      {state.recovery && !state.operation && (
        <button
          className="button secondary"
          type="button"
          disabled={state.busy}
          onClick={() => void controller.refresh()}
        >
          受付済みの進捗を再確認
        </button>
      )}
      {state.operation && (
        <>
          <OperationStatus operation={state.operation} />
          {state.operation.kind !== "delete_account" &&
            ["succeeded", "failed"].includes(state.operation.status) && (
              <button
                className="button secondary"
                type="button"
                disabled={state.busy}
                onClick={() => controller.finish()}
              >
                結果を確認して閉じる
              </button>
            )}
          <button
            className="button secondary"
            type="button"
            disabled={state.busy}
            onClick={() => void controller.refresh()}
          >
            進捗を確認
          </button>
          {state.operation.kind === "export" && (
            <>
              <button
                className="button primary"
                type="button"
                disabled={state.busy || !controller.canDownload()}
                onClick={() => void controller.download()}
              >
                認証してZIPを取得
              </button>
              <p>
                保存したファイルは失効後も端末から自動回収できません。端末のファイル管理で削除してください。
              </p>
            </>
          )}
        </>
      )}
    </section>
  );
}
function Reauthentication({
  controller,
  busy,
}: {
  controller: DataControlsController;
  busy: boolean;
}) {
  const [email, setEmail] = useState(""),
    [password, setPassword] = useState("");
  return (
    <form
      aria-label="削除操作の本人確認"
      onSubmit={(e) => {
        e.preventDefault();
        const secret = password;
        setPassword("");
        void controller.reauthenticate(email, secret);
      }}
    >
      <label>
        削除確認のメールアドレス
        <input
          type="email"
          autoComplete="username"
          required
          value={email}
          disabled={busy}
          onChange={(e) => setEmail(e.target.value)}
        />
      </label>
      <label>
        削除確認のパスワード
        <input
          type="password"
          autoComplete="current-password"
          required
          value={password}
          disabled={busy}
          onChange={(e) => setPassword(e.target.value)}
        />
      </label>
      <button className="button primary" disabled={busy} type="submit">
        削除の本人確認をする
      </button>
    </form>
  );
}

export { DataControlsController, unavailableDelivery } from "./controller.ts";
