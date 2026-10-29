import { useEffect, useSyncExternalStore } from "react";
import { flushSync } from "react-dom";
import { DataControlsForm } from "@brainoid-internal/ui/data-controls";
import { Login } from "@brainoid-internal/ui/session-view";
import type { AccountRecovery } from "./recovery.ts";

export function WebAccountDeletion({ recovery }: { recovery: AccountRecovery }) {
  const { session } = recovery;
  const state = useSyncExternalStore(session.subscribe, session.snapshot);
  const account = useSyncExternalStore(recovery.subscribe, recovery.snapshot);
  useEffect(() => {
    const stop = session.start();
    const hide = () => flushSync(() => recovery.suspend());
    const show = (event: PageTransitionEvent) => {
      if (event.persisted) void session.resume();
    };
    window.addEventListener("pagehide", hide);
    window.addEventListener("pageshow", show);
    return () => {
      window.removeEventListener("pagehide", hide);
      window.removeEventListener("pageshow", show);
      recovery.dispose();
      stop();
    };
  }, [session, recovery]);
  return (
    <main className="account-page" aria-label="Webからのアカウント削除">
      <header className="card">
        <h1>Brainoid Agent アカウント削除</h1>
        <p>アプリをお持ちでなくても、登録済みの本人アカウントで削除を申請できます。</p>
        <p>
          再読込み後は同じ本人として再度サインインしてください。このタブに受付番号が残っていれば、現在の認証で進捗を確認します。
        </p>
        <p>削除の受付・認証の終了・進捗の取得失敗は、消去完了を意味しません。</p>
      </header>
      {state.message && (
        <p className="card" role="alert">
          {state.message}
        </p>
      )}
      {account.notice && (
        <p className="card" role="status">
          {account.notice}
        </p>
      )}
      {state.phase === "signed-out" && (
        <Login key={state.revision} controller={session} busy={state.busy ?? false} />
      )}
      {(state.phase === "starting" || account.loading) && (
        <p role="status">本人の認証を確認しています。</p>
      )}
      {account.controls && (
        <>
          <section className="card" aria-label="サインイン中の本人">
            <p>サインイン中のアカウント: {state.uid}</p>
            <button
              className="button secondary"
              type="button"
              onClick={() => void recovery.signOut()}
            >
              サインアウト
            </button>
          </section>
          <DataControlsForm key={account.controls.scope.generation} controller={account.controls} />
        </>
      )}
    </main>
  );
}
