import { type FormEvent, useEffect, useState, useSyncExternalStore } from "react";
import { flushSync } from "react-dom";
import { Chat } from "./chat/view.tsx";
import { Calendar } from "./calendar/view.tsx";
import type { CalendarOAuth } from "./calendar/contracts.ts";
import { Frame, StateCard } from "./components.tsx";
import { Memories } from "./memories/view.tsx";
import { Notes } from "./notes/view.tsx";
import { Reminders } from "./reminders/view.tsx";
import type { SessionContext, SessionController } from "./session.ts";
import { Devices } from "./devices/view.tsx";
import { InvitationAcceptance, TenantSettings } from "./tenant-settings/view.tsx";
import { Tasks } from "./tasks/view.tsx";
import { DataControls } from "./data-controls/view.tsx";
import type { ExportDelivery } from "./data-controls/controller.ts";

export function browserNavigation() {
  return {
    requestedTenant: () => new URL(location.href).searchParams.get("tenant") ?? undefined,
    navigate: (tenantId?: string, replace = false) => {
      const url = new URL(location.href);
      if (tenantId === undefined) url.searchParams.delete("tenant");
      else url.searchParams.set("tenant", tenantId);
      if (replace) history.replaceState(null, "", url);
      else history.pushState(null, "", url);
    },
  };
}

export function SessionApp({
  controller,
  surface,
  preview = false,
  children,
  calendarOAuth,
  exportDelivery,
}: {
  controller: SessionController;
  surface: "mobile" | "display";
  preview?: boolean;
  children?: (context: SessionContext) => React.ReactNode;
  calendarOAuth?: CalendarOAuth | undefined;
  exportDelivery?: ExportDelivery | undefined;
}) {
  const state = useSyncExternalStore(controller.subscribe, controller.snapshot);
  useEffect(() => {
    const stop = controller.start();
    const back = () => {
      void controller.selectTenant(browserNavigation().requestedTenant(), false);
    };
    const hide = () => {
      flushSync(() => controller.suspend());
    };
    const show = (event: PageTransitionEvent) => {
      if (event.persisted) void controller.resume();
    };
    window.addEventListener("popstate", back);
    window.addEventListener("pagehide", hide);
    window.addEventListener("pageshow", show);
    return () => {
      stop();
      window.removeEventListener("popstate", back);
      window.removeEventListener("pagehide", hide);
      window.removeEventListener("pageshow", show);
    };
  }, [controller]);
  return (
    <Frame surface={surface} preview={preview}>
      {preview && (
        <p className="preview-notice">体験用の認証とテナントです。実際のデータには接続しません。</p>
      )}
      {state.message && (
        <p className="card" role="alert">
          {state.message}
        </p>
      )}
      {controller.globalContext() && surface === "mobile" && (
        <InvitationAcceptance
          key={`invite:${controller.globalContext()?.generation}`}
          session={controller}
        />
      )}
      {controller.globalContext() && (
        <DataControls
          key={`data:${controller.globalContext()?.generation}`}
          session={controller}
          surface={surface}
          delivery={exportDelivery}
        />
      )}
      {controller.globalContext() && (
        <TenantSettings
          key={`settings:${controller.globalContext()?.generation}`}
          session={controller}
          surface={surface}
        />
      )}
      {state.context && (
        <Chat key={state.context.generation} context={state.context} surface={surface} />
      )}
      {state.context && (
        <Calendar
          key={`calendar:${state.context.generation}`}
          context={state.context}
          surface={surface}
          oauth={calendarOAuth}
        />
      )}
      {state.context && (
        <Memories
          key={`memories:${state.context.generation}`}
          context={state.context}
          surface={surface}
        />
      )}
      {state.context && (
        <Notes
          key={`notes:${state.context.generation}`}
          context={state.context}
          surface={surface}
        />
      )}
      {state.context && (
        <Tasks
          key={`tasks:${state.context.generation}`}
          context={state.context}
          surface={surface}
        />
      )}
      {state.context && (
        <Reminders
          key={`reminders:${state.context.generation}`}
          context={state.context}
          surface={surface}
          hint={state.notificationHint}
        />
      )}
      {state.context && (
        <Devices
          key={`devices:${state.context.generation}`}
          context={state.context}
          surface={surface}
        />
      )}
      {state.phase === "starting" ? (
        <StateCard state="loading" title="認証を確認しています">
          <p>少しお待ちください。</p>
        </StateCard>
      ) : state.phase === "signed-out" ? (
        <Login key={state.revision} controller={controller} busy={state.busy ?? false} />
      ) : (
        <div className="home-grid">
          <section className="card primary-column" aria-label="テナントを選択">
            <div className="tenant-heading">
              <h2>利用するテナント</h2>
              <button
                className="button secondary"
                type="button"
                onClick={() => {
                  void controller.signOut();
                }}
              >
                サインアウト
              </button>
            </div>
            <p>所属先を選んでください。選択のたびに現在の所属を確認します。</p>
            <button
              className="button secondary"
              type="button"
              onClick={() => {
                void controller.loadTenants();
              }}
            >
              所属一覧を更新
            </button>
            {state.phase === "loading" && <p role="status">所属を確認しています。</p>}
            {state.phase !== "loading" && state.tenants.length === 0 && (
              <p>選択できる所属はありません。一覧を更新するか、新しく作成してください。</p>
            )}
            <ul className="tenant-list">
              {state.tenants.map((tenant) => (
                <li key={tenant.id}>
                  <button
                    className="button secondary"
                    type="button"
                    aria-pressed={state.tenant?.id === tenant.id}
                    onClick={() => {
                      void controller.selectTenant(tenant.id);
                    }}
                  >
                    {tenant.name}
                  </button>
                </li>
              ))}
            </ul>
            {state.nextCursor && (
              <button
                className="button secondary"
                type="button"
                onClick={() => {
                  void controller.loadTenants(true);
                }}
              >
                続きを表示
              </button>
            )}
            {state.phase === "selecting" && <p role="status">テナントの所属を確認しています。</p>}
            {state.context && state.tenant && (
              <section
                key={state.context.generation}
                className="selected-tenant"
                aria-label="選択中のテナント"
              >
                <p className="eyebrow">選択中</p>
                <h2>{state.tenant.name}</h2>
                <p>{state.tenant.timezone}</p>
                {children ? (
                  children(state.context)
                ) : (
                  <p>このテナントで利用する準備ができました。</p>
                )}
              </section>
            )}
          </section>
          <CreateTenant
            key={`${state.uid}:${state.context?.generation ?? state.phase}`}
            controller={controller}
            busy={state.busy ?? false}
          />
        </div>
      )}
    </Frame>
  );
}
function Login({ controller, busy }: { controller: SessionController; busy: boolean }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const submit = (event: FormEvent) => {
    event.preventDefault();
    const secret = password;
    setPassword("");
    void controller.signIn(email.trim(), secret);
  };
  return (
    <section className="card auth-card">
      <h2>サインイン</h2>
      <p>登録済みのメールアドレスでサインインしてください。</p>
      <form onSubmit={submit}>
        <label>
          メールアドレス
          <input
            type="email"
            autoComplete="username"
            required
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            disabled={busy}
          />
        </label>
        <label>
          パスワード
          <input
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            disabled={busy}
          />
        </label>
        <button className="button primary" type="submit" disabled={busy}>
          {busy ? "処理中です" : "サインイン"}
        </button>
        {busy && (
          <button
            className="button secondary"
            type="button"
            onClick={() => {
              void controller.signOut();
            }}
          >
            サインアウトを再試行
          </button>
        )}
      </form>
    </section>
  );
}
function CreateTenant({ controller, busy }: { controller: SessionController; busy: boolean }) {
  const [name, setName] = useState("");
  const [timezone, setTimezone] = useState("Asia/Tokyo");
  return (
    <section className="card side-column">
      <h2>新しく作成</h2>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void controller.createTenant(name, timezone);
        }}
      >
        <label>
          テナント名
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            required
            maxLength={100}
            disabled={busy}
          />
        </label>
        <label>
          タイムゾーン
          <input
            value={timezone}
            onChange={(event) => setTimezone(event.target.value)}
            required
            maxLength={64}
            disabled={busy}
          />
        </label>
        <button className="button primary" type="submit" disabled={busy}>
          テナントを作成
        </button>
      </form>
    </section>
  );
}
