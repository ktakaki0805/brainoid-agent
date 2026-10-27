import { ConfirmationCard, Frame } from "@brainoid-internal/ui/react";
import { useEffect, useRef, useState } from "react";
import { type Diagnostics, LocalDevice } from "./client.ts";
import "./recovery.css";

const storageLabels: Record<string, string> = {
  unregistered: "未登録 — 再ペアリングが必要です",
  ready: "保存領域は正常です",
  reset_pending: "初期化は未完了です。操作は停止しています",
  recovery_required: "保存領域の復旧が必要です。操作は停止しています",
};

export function Recovery() {
  const [device] = useState(() => new LocalDevice());
  const [status, setStatus] = useState<Diagnostics | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [error, setError] = useState("");
  const [complete, setComplete] = useState(false);
  const mounted = useRef(true);
  const generation = useRef(0);
  const resetButton = useRef<HTMLButtonElement>(null);
  const hadConfirmation = useRef(false);
  const acknowledgement = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (confirm) {
      acknowledgement.current?.focus();
      acknowledgement.current?.scrollIntoView({ block: "center" });
    } else if (hadConfirmation.current && !busy) {
      resetButton.current?.focus();
      hadConfirmation.current = false;
    }
    if (confirm) hadConfirmation.current = true;
  }, [confirm, busy]);

  async function refresh() {
    const version = ++generation.current;
    setBusy(true);
    setError("");
    try {
      const result = await device.status();
      if (mounted.current && version === generation.current) setStatus(result);
    } catch {
      if (mounted.current && version === generation.current) {
        setStatus(null);
        setError("端末サービスに接続できません。サービスを確認して、もう一度お試しください。");
      }
    } finally {
      if (mounted.current && version === generation.current) setBusy(false);
    }
  }

  // biome-ignore lint/correctness/useExhaustiveDependencies: bootstrap once, guarded against unmount and stale requests.
  useEffect(() => {
    mounted.current = true;
    void refresh();
    return () => {
      mounted.current = false;
      generation.current++;
      device.cancel();
    };
  }, [device]);

  async function prepare() {
    const version = ++generation.current;
    setBusy(true);
    setError("");
    setComplete(false);
    try {
      await device.prepare();
      if (mounted.current && version === generation.current) setConfirm(true);
    } catch {
      if (mounted.current && version === generation.current)
        setError("確認を開始できませんでした。「状態を再確認」してからお試しください。");
    } finally {
      if (mounted.current && version === generation.current) setBusy(false);
    }
  }

  async function reset() {
    if (!acknowledgement.current?.checked) {
      setError("この端末の管理者であることと、消去内容を確認してください。");
      acknowledgement.current?.focus();
      return;
    }
    const version = ++generation.current;
    setStatus(null);
    setBusy(true);
    setError("");
    try {
      await device.reset();
      if (mounted.current && version === generation.current) {
        setComplete(true);
        setConfirm(false);
      }
    } catch {
      if (mounted.current && version === generation.current) {
        setConfirm(false);
        setError(
          "初期化の完了を確認できません。状態を再確認してください。未完了の場合は操作を再開せず、サービスを再起動して消去を再試行します。",
        );
      }
    } finally {
      if (mounted.current && version === generation.current) {
        // A new local session is required after reset. A lost response must not
        // turn into a success message; diagnostics remain independently observed.
        try {
          const result = await device.status();
          if (mounted.current && version === generation.current) setStatus(result);
        } catch {
          if (mounted.current && version === generation.current) setStatus(null);
        } finally {
          if (mounted.current && version === generation.current) setBusy(false);
        }
      }
    }
  }

  return (
    <Frame surface="display">
      <div className="recovery-grid">
        <section className="card recovery-status" aria-busy={busy}>
          <p className="eyebrow">端末の設定と復旧</p>
          <h2>ネットにつながらないときも</h2>
          <p>
            この画面は端末内で開きます。会話・オフラインAI・クラウド再接続はまだ利用できません。
          </p>
          {complete && <p role="status">端末内の消去が完了しました。再ペアリングが必要です。</p>}
          {error && <p role="alert">{error}</p>}
          {status ? (
            <dl className="diagnostics">
              <dt>サービス</dt>
              <dd>{status.service === "running" ? "起動しています" : "停止中です"}</dd>
              <dt>登録・保存領域</dt>
              <dd>{storageLabels[status.storage] ?? "確認できません"}</dd>
              <dt>ネットワーク</dt>
              <dd>
                {status.network === "link_up"
                  ? "リンクあり（インターネット接続は未確認）"
                  : status.network === "link_down"
                    ? "リンクなし。ケーブルやWi-Fi設定を確認してください"
                    : "確認できません"}
              </dd>
              <dt>時刻同期</dt>
              <dd>
                {status.clock === "synchronized"
                  ? "OSの同期状態：同期済み"
                  : status.clock === "unsynchronized"
                    ? "未同期。OSの時刻設定を確認してください"
                    : "確認できません。OSの時刻設定を確認してください"}
              </dd>
              <dt>空き容量</dt>
              <dd>
                {status.free_bytes === null
                  ? "確認できません"
                  : `${(status.free_bytes / 1024 ** 3).toFixed(1)} GiB`}
              </dd>
              <dt>音量・マイク</dt>
              <dd>
                音声機能は未接続です。録音・再生していません。保存設定：音量{" "}
                {status.volume_pct ?? "未確認"} / マイク{" "}
                {status.microphone_muted === null
                  ? "未確認"
                  : status.microphone_muted
                    ? "消音"
                    : "消音なし"}
                （実機の状態ではありません）
              </dd>
            </dl>
          ) : (
            <p role="status">{busy ? "端末の状態を確認しています" : "端末の状態は未確認です"}</p>
          )}
          <button
            className="button secondary"
            type="button"
            disabled={busy || confirm}
            onClick={() => void refresh()}
          >
            状態を再確認
          </button>
          <p className="hint">
            ネットワークと時刻はOSの設定で修復してください。古い待機操作を再実行して回復しないでください。
          </p>
        </section>
        <aside className="side-column" aria-label="初期化と移譲">
          <section className="info-panel">
            <h2>端末を初期化する</h2>
            <p>
              通信・実行・再生を停止し、端末内の資格情報、個人データ、待機操作を消去します。取り消せません。
            </p>
            <button
              ref={resetButton}
              className="button secondary"
              type="button"
              disabled={busy || confirm || !status}
              onClick={() => void prepare()}
            >
              初期化の内容を確認
            </button>
          </section>
          <section className="info-panel">
            <h2>外部サービスの失効は未確認です</h2>
            <p>
              ローカル消去ではクラウド登録とHome
              Assistant資格情報の失効を確認できません。端末を渡す前に、クラウド管理画面で端末登録を解除し、Home
              Assistantで資格情報を失効させてください。
            </p>
            <p>この版ではペアリング接続は未提供です。対応版を導入して再登録してください。</p>
          </section>
        </aside>
        {confirm && (
          <ConfirmationCard
            title="この端末のデータを消去しますか"
            confirmLabel="確認して端末を初期化"
            pending={busy}
            error={error}
            onConfirm={() => void reset()}
            onCancel={() => {
              generation.current++;
              device.cancel();
              setConfirm(false);
              setError("");
            }}
          >
            <p>確認の有効期限は60秒です。クラウドとHome Assistantの失効は別途必要です。</p>
            <label className="reset-ack">
              <input type="checkbox" ref={acknowledgement} />
              私はこの端末の管理者で、消去内容を確認しました
            </label>
          </ConfirmationCard>
        )}
      </div>
    </Frame>
  );
}
