import { type ReactNode, useEffect, useId, useState } from "react";
import type { ClientPlatformAdapter, UiDependencies } from "../platform/index.ts";
import { checkConnection } from "./connection.ts";

export function StateCard({
  state,
  title,
  children,
}: {
  state: "empty" | "loading" | "ready";
  title: string;
  children: ReactNode;
}) {
  return (
    <section className={`card state-card state-${state}`} aria-busy={state === "loading"}>
      <span className="state-symbol" aria-hidden="true">
        {state === "loading" ? "◌" : "○"}
      </span>
      <div role="status" aria-live="polite">
        <p className="eyebrow">
          {state === "loading" ? "準備中" : state === "empty" ? "待機中" : "接続済み"}
        </p>
        <h2>{title}</h2>
        <div className="card-copy">{children}</div>
      </div>
    </section>
  );
}

export function ErrorCard({ onRetry }: { onRetry: () => void }) {
  return (
    <section className="card error-card" role="alert">
      <p className="eyebrow">接続エラー</p>
      <h2>接続を確認できませんでした</h2>
      <p>通信環境を確認して、もう一度お試しください。</p>
      <button className="button primary" type="button" onClick={onRetry}>
        もう一度試す
      </button>
    </section>
  );
}

/** Controlled, inline confirmation. The caller owns pending/error state and the operation. */
export function ConfirmationCard({
  title,
  children,
  confirmLabel,
  onConfirm,
  onCancel,
  pending = false,
  error,
}: {
  title: string;
  children: ReactNode;
  confirmLabel: string;
  onConfirm: () => void;
  onCancel: () => void;
  pending?: boolean;
  error?: string;
}) {
  const heading = useId();
  return (
    <section className="card confirmation-card" aria-labelledby={heading} aria-busy={pending}>
      <p className="eyebrow">内容の確認</p>
      <h2 id={heading}>{title}</h2>
      <div className="card-copy">{children}</div>
      {error && <p role="alert">{error}</p>}
      <div className="button-row">
        <button className="button secondary" type="button" disabled={pending} onClick={onCancel}>
          戻る
        </button>
        <button className="button primary" type="button" disabled={pending} onClick={onConfirm}>
          {pending ? "処理中です" : confirmLabel}
        </button>
      </div>
    </section>
  );
}

export function Frame({
  surface,
  children,
  preview = false,
}: {
  surface: ClientPlatformAdapter["surface"];
  children: ReactNode;
  preview?: boolean;
}) {
  return (
    <div className={`app surface-${surface}`}>
      <a className="skip-link" href="#main">
        本文へ移動
      </a>
      <header className="topbar">
        <a className="brand" href="./" aria-label="Brainoid ホーム">
          <span className="brand-mark" aria-hidden="true">
            b.
          </span>
          <span>
            brainoid<span className="brand-caption">暮らしのアシスタント</span>
          </span>
        </a>
        <span className="mode-badge">
          {preview ? "体験用の画面" : surface === "display" ? "ディスプレイ" : "モバイル"}
        </span>
      </header>
      <main id="main" tabIndex={-1}>
        <div className="intro">
          <p className="eyebrow">YOUR EVERYDAY COMPANION</p>
          <h1>暮らしのそばに。</h1>
          <p>いつもの場所から、ひとつずつ。</p>
        </div>
        {children}
      </main>
      <footer>
        Brainoid Agent <span aria-hidden="true">·</span>{" "}
        {surface === "display" ? "みんなの画面" : "手元の画面"}
      </footer>
    </div>
  );
}

export function SetupRequired({ surface }: { surface: ClientPlatformAdapter["surface"] }) {
  return (
    <Frame surface={surface}>
      <StateCard state="empty" title="利用の準備が必要です">
        <p>接続先が設定されていません。セットアップが完了すると、この画面を利用できます。</p>
      </StateCard>
    </Frame>
  );
}

export function AssistantHome({ dependencies }: { dependencies: UiDependencies }) {
  const { api, platform } = dependencies;
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  // biome-ignore lint/correctness/useExhaustiveDependencies: an explicit retry starts a new request even when api is unchanged.
  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    setState("loading");
    const timeout = setTimeout(() => {
      if (active) setState("error");
      controller.abort();
    }, 10_000);
    void checkConnection(api, controller.signal)
      .then(
        () => {
          if (active && !controller.signal.aborted) setState("ready");
        },
        () => {
          if (active && !controller.signal.aborted) setState("error");
        },
      )
      .finally(() => clearTimeout(timeout));
    return () => {
      active = false;
      clearTimeout(timeout);
      controller.abort();
    };
  }, [api, attempt]);

  return (
    <div className="home-grid">
      <div className="primary-column">
        {state === "loading" ? (
          <StateCard state="loading" title="接続を確認しています">
            <p>このまま、少しお待ちください。</p>
          </StateCard>
        ) : state === "error" ? (
          <ErrorCard onRetry={() => setAttempt((value) => value + 1)} />
        ) : (
          <StateCard state="empty" title="まだ会話はありません">
            <p>
              ここに会話が表示されます。
              <br />
              会話の機能は準備中です。
            </p>
          </StateCard>
        )}
        <div className="composer" aria-describedby="composer-note">
          <span>ことばを届ける</span>
          <button className="button secondary" type="button" disabled>
            メッセージを送る
          </button>
        </div>
        <p className="hint" id="composer-note">
          会話はまだ利用できません。
        </p>
      </div>
      <aside className="side-column" aria-label="この画面について">
        <section className="info-panel">
          <p className="eyebrow">この画面</p>
          <h2>{platform.surface === "display" ? "みんなの場所に" : "手元で、ゆっくり"}</h2>
          <p>
            {platform.surface === "display"
              ? "離れた場所からも、状態を確認しやすい画面です。"
              : "内容を見ながら、自分のペースで操作できます。"}
          </p>
        </section>
        <section className="info-panel voice-panel">
          <span className="quiet-dot" aria-hidden="true" />
          <div>
            <h2>音声について</h2>
            <p>{platform.voice.reason}</p>
            <span className="small-label">録音していません</span>
          </div>
        </section>
      </aside>
    </div>
  );
}
