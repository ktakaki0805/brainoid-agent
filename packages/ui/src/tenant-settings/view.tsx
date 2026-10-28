import { useEffect, useState, useSyncExternalStore } from "react";
import type { Membership, Usage } from "@brainoid-internal/api-client";
import type { SessionController } from "../session.ts";
import { InvitationAcceptanceController, TenantSettingsController } from "./controller.ts";
const roleName = { owner: "owner（オーナー）", admin: "admin（管理者）", member: "member（会員）" };

export function TenantSettings({
  session,
  surface,
}: {
  session: SessionController;
  surface: "mobile" | "display";
}) {
  const [controller] = useState(() => new TenantSettingsController(session));
  const [open, setOpen] = useState(false);
  const state = useSyncExternalStore(controller.subscribe, controller.snapshot);
  useSyncExternalStore(session.subscribe, session.snapshot);
  useEffect(() => () => controller.dispose(), [controller]);
  useEffect(() => {
    if (!open) return;
    const check = () => {
      if (document.visibilityState === "visible") void controller.load();
    };
    window.addEventListener("focus", check);
    document.addEventListener("visibilitychange", check);
    const timer = setInterval(check, 60_000);
    return () => {
      window.removeEventListener("focus", check);
      document.removeEventListener("visibilitychange", check);
      clearInterval(timer);
    };
  }, [controller, open]);
  if (surface !== "mobile")
    return (
      <section className="card">
        <p>会員・招待・利用量はスマホで確認できます。</p>
      </section>
    );
  if (!session.selectedTenantId()) return null;
  const manager = state.tenant?.my_role === "owner" || state.tenant?.my_role === "admin";
  const blocked = state.busy || state.pending;
  const review = state.review;
  return (
    <section className="card tenant-settings" aria-label="会員と利用量">
      <h2>会員と利用量</h2>
      <button
        className="button secondary"
        type="button"
        disabled={state.busy}
        onClick={() => {
          setOpen(true);
          void controller.load();
        }}
      >
        会員・利用量を更新
      </button>
      {state.notice && <p role="status">{state.notice}</p>}
      {state.busy && <p role="status">現在の所属と要求を確認しています。</p>}
      {state.tenant && (
        <p>
          対象: {state.tenant.name} / {roleName[state.tenant.my_role]}
        </p>
      )}
      {state.usage && state.tenant && <UsageCard usage={state.usage} role={state.tenant.my_role} />}
      {manager && (
        <>
          <h3>招待</h3>
          <p>招待は24時間有効で、一人が一度だけ参加できます。コードはこの画面だけで保持します。</p>
          <div className="button-row">
            <button
              className="button secondary"
              type="button"
              disabled={blocked}
              onClick={() => void controller.prepare("invite", undefined, "member")}
            >
              memberを招待
            </button>
            <button
              className="button secondary"
              type="button"
              disabled={blocked}
              onClick={() => void controller.prepare("invite", undefined, "admin")}
            >
              adminを招待
            </button>
          </div>
          {state.invitation && (
            <div role="status">
              <label>
                共有する招待コード
                <input
                  readOnly
                  value={state.invitation.code}
                  autoComplete="off"
                  spellCheck={false}
                />
              </label>
              <p>有効期限: {state.invitation.expires_at}</p>
              <p>受け取る本人にだけ安全な方法で共有してください。</p>
              <button
                className="button secondary"
                type="button"
                onClick={() => controller.hideCode()}
              >
                コードを隠す
              </button>
            </div>
          )}
          <h3>会員一覧</h3>
          <p>管理権限で他人のprivate会話や個別の利用明細は閲覧できません。</p>
          {state.tenant?.my_role === "owner" && (
            <p>
              最後のownerは、他の会員へownerを移譲するか、テナント削除の手続きが必要です。テナント削除と本人のアカウント削除は別の手続きです。この画面の「所属を失効」は、その会員のこのテナントへの所属だけを変更します。
            </p>
          )}
          <ul className="tenant-list">
            {state.members.map((member) => (
              <li key={member.user_id}>
                <Member controller={controller} member={member} blocked={blocked} />
              </li>
            ))}
          </ul>
          {state.cursor && (
            <button
              className="button secondary"
              type="button"
              disabled={blocked}
              onClick={() => void controller.load(true)}
            >
              会員の続きを表示
            </button>
          )}
        </>
      )}
      {review && (
        <section className="confirmation-card" aria-label="会員操作の確認">
          <h3>操作を確認</h3>
          <p>対象テナント: {state.tenant?.name}</p>
          {review.kind === "invite" ? (
            <p>{roleName[review.role]}として参加できる招待コードを発行します。</p>
          ) : (
            <>
              <p>対象の会員ID: {review.target.user_id}</p>
              <p>
                現在: {roleName[review.target.role]} / 版 {review.target.version}
              </p>
              <p>
                {review.kind === "role"
                  ? `${roleName[review.role]}へ変更します。`
                  : review.kind === "transfer"
                    ? "この会員へownerを移譲し、あなたはmemberになります。"
                    : "このテナントの所属を失効します。本人のアカウント・他テナントの所属は削除しません。"}
              </p>
              <p>減権限・所属失効では、このテナントの既存の招待コードがすべて無効になります。</p>
            </>
          )}
          <div className="button-row">
            <button
              className="button primary"
              type="button"
              disabled={blocked}
              onClick={() => void controller.confirm()}
            >
              確認して実行
            </button>
            <button
              className="button secondary"
              type="button"
              disabled={state.busy}
              onClick={() => controller.cancel()}
            >
              取り消す
            </button>
          </div>
        </section>
      )}
      {state.conflict && (
        <p>
          現在の対象: {state.conflict.user_id} / {roleName[state.conflict.role]} / 版{" "}
          {state.conflict.version}
        </p>
      )}
      {state.pending && (
        <section aria-label="未確定の会員操作">
          <p>未確定の操作があります。対象・内容・版を変えず、同じ要求で確認します。</p>
          <button
            className="button secondary"
            type="button"
            disabled={state.busy}
            onClick={() => void controller.retry()}
          >
            同じ会員操作の結果を確認
          </button>
        </section>
      )}
      {state.reauthentication &&
        (session.canReauthenticate() ? (
          <Reauthentication controller={controller} busy={state.busy} />
        ) : (
          <p>
            この端末では本人の再認証を利用できません。対応したスマホの認証設定を確認してください。
          </p>
        ))}
    </section>
  );
}
/** The API gives managers only the tenant quota, not their own admission limits. */
export function UsageCard({ usage, role }: { usage: Usage; role: Membership["role"] }) {
  const manager = role === "owner" || role === "admin";
  const quota = manager
    ? {
        available: "テナント枠には現在、空きがあります。",
        limited: "テナントの同時実行上限に達しています。処理の完了を待って再確認してください。",
        exhausted: "テナントの今月の利用上限に達しています。月次枠の更新までお待ちください。",
      }
    : {
        available: "本人とテナントの利用枠には現在、空きがあります。",
        limited:
          "本人またはテナントの同時実行上限に達しています。処理の完了を待って再確認してください。",
        exhausted:
          "本人またはテナントの今月の利用上限に達しています。月次枠の更新までお待ちください。",
      };
  return (
    <section aria-label="利用量">
      <h3>{manager ? "テナント全体の利用量" : "本人の利用量"}</h3>
      <p>期間: {usage.period}（UTC）</p>
      <dl>
        <dt>会話回数</dt>
        <dd>{usage.turns} 回</dd>
        <dt>音声</dt>
        <dd>{usage.voice_seconds} 秒</dd>
        <dt>{manager ? "テナント枠の残り会話回数" : "本人・テナント枠の残り会話回数"}</dt>
        <dd>{usage.remaining_turns} 回</dd>
      </dl>
      <p>音声は未確定の予約を含みます。</p>
      <p>
        {manager
          ? "残り回数と状態はテナント枠のみの集計です。本人の利用可否を示しません。テナントに空きがあっても、本人の月次枠・同時実行枠に達している場合は利用できません。"
          : "残り回数は本人とテナントの枠の小さい方です。状態は本人とテナントの両方の枠を確認しています。"}
      </p>
      <p>{quota[usage.quota_state]}</p>
      <p>制限中の再確認は60秒以上お待ちください。時間の経過だけで利用枠が戻る保証はありません。</p>
    </section>
  );
}

function Member({
  controller,
  member,
  blocked,
}: {
  controller: TenantSettingsController;
  member: Membership;
  blocked: boolean;
}) {
  const state = controller.snapshot();
  return (
    <article>
      <p>
        会員ID: <bdi>{member.user_id}</bdi>
        {member.user_id === controller.scope.uid ? "（本人）" : ""}
      </p>
      <p>
        {roleName[member.role]} / {member.status} / 版 {member.version}
      </p>
      <div className="button-row">
        {(["owner", "admin", "member"] as const)
          .filter((role) => controller.canChange(member, role))
          .map((role) => (
            <button
              className="button secondary"
              type="button"
              disabled={blocked}
              key={role}
              onClick={() => void controller.prepare("role", member.user_id, role)}
            >
              {role}に変更
            </button>
          ))}
        {controller.canChange(member) && member.user_id !== controller.scope.uid && (
          <button
            className="button secondary"
            type="button"
            disabled={blocked}
            onClick={() => void controller.prepare("revoke", member.user_id)}
          >
            所属を失効
          </button>
        )}
        {state.tenant?.my_role === "owner" &&
          member.status === "active" &&
          member.role !== "owner" &&
          member.user_id !== controller.scope.uid && (
            <button
              className="button secondary"
              type="button"
              disabled={blocked}
              onClick={() => void controller.prepare("transfer", member.user_id)}
            >
              ownerを移譲
            </button>
          )}
      </div>
    </article>
  );
}
function Reauthentication({
  controller,
  busy,
}: {
  controller: TenantSettingsController;
  busy: boolean;
}) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  return (
    <form
      aria-label="管理操作の本人確認"
      onSubmit={(event) => {
        event.preventDefault();
        const secret = password;
        setPassword("");
        void controller.reauthenticate(email, secret);
      }}
    >
      <h3>本人の再認証</h3>
      <p>現在のアカウントのパスワードで本人確認します。</p>
      <label>
        本人確認のメールアドレス
        <input
          type="email"
          required
          value={email}
          autoComplete="username"
          onChange={(event) => setEmail(event.target.value)}
          disabled={busy}
        />
      </label>
      <label>
        本人確認のパスワード
        <input
          type="password"
          required
          value={password}
          autoComplete="current-password"
          onChange={(event) => setPassword(event.target.value)}
          disabled={busy}
        />
      </label>
      <button className="button primary" type="submit" disabled={busy}>
        本人確認する
      </button>
    </form>
  );
}
export function InvitationAcceptance({ session }: { session: SessionController }) {
  const [controller] = useState(() => new InvitationAcceptanceController(session));
  const state = useSyncExternalStore(controller.subscribe, controller.snapshot);
  const [code, setCode] = useState("");
  useEffect(() => () => controller.dispose(), [controller]);
  return (
    <section className="card" aria-label="招待を受け取った方">
      <h2>招待から参加</h2>
      <p>
        招待を受けた本人として、コードを入力して参加を受諾してください。テナントの選択は不要です。
      </p>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          const secret = code;
          setCode("");
          void controller.accept(secret);
        }}
      >
        <label>
          受け取った招待コード
          <input
            type="password"
            value={code}
            autoComplete="off"
            spellCheck={false}
            maxLength={64}
            minLength={64}
            required
            disabled={state.busy || state.pending}
            onChange={(event) => setCode(event.target.value)}
          />
        </label>
        <button className="button primary" type="submit" disabled={state.busy || state.pending}>
          招待を受諾して参加
        </button>
      </form>
      {state.notice && <p role="status">{state.notice}</p>}
      {state.pending && (
        <button
          className="button secondary"
          type="button"
          disabled={state.busy}
          onClick={() => void controller.accept()}
        >
          同じ参加要求の結果を確認
        </button>
      )}
    </section>
  );
}
