import { useMemo, useRef, useState } from "react";
import type { ClientPlatformAdapter } from "../platform/index.ts";
import { AssistantHome, ConfirmationCard, Frame } from "./components.tsx";
import { createFakeDependencies, type FakeScenario } from "./fake.ts";

/** Imported only by explicit fake-mode entry builds; never a production operation. */
export function Preview({ surface }: { surface: ClientPlatformAdapter["surface"] }) {
  const [scenario, setScenario] = useState<FakeScenario>("empty");
  const [confirmation, setConfirmation] = useState(false);
  const [long, setLong] = useState(false);
  const [result, setResult] = useState("");
  const trigger = useRef<HTMLButtonElement>(null);
  const dependencies = useMemo(
    () => createFakeDependencies(surface, scenario),
    [surface, scenario],
  );
  function close(message: string) {
    setConfirmation(false);
    setResult(message);
    trigger.current?.focus();
  }
  return (
    <Frame surface={surface} preview>
      <p className="preview-notice">
        表示と操作を試すための画面です。実際のデータや機器には接続していません。
      </p>
      <AssistantHome dependencies={dependencies} />
      <details className="preview-tools">
        <summary>表示の確認</summary>
        <div className="preview-controls">
          <label>
            接続の状態
            <select
              value={scenario}
              onChange={(event) => {
                setScenario(event.target.value as FakeScenario);
                setResult("");
              }}
            >
              <option value="empty">空の画面</option>
              <option value="loading">読み込み中</option>
              <option value="error">接続エラー</option>
            </select>
          </label>
          <label className="checkbox-label">
            <input
              type="checkbox"
              checked={long}
              onChange={(event) => setLong(event.target.checked)}
            />
            長い内容を表示
          </label>
          <button
            ref={trigger}
            className="button secondary"
            type="button"
            onClick={() => {
              setConfirmation(true);
              setResult("");
            }}
          >
            確認カードを開く
          </button>
        </div>
        <p className="hint">
          読み込みは10秒でタイムアウトします。エラーの再試行では同じエラーを再現します。
        </p>
        <p role="status">{result}</p>
        {confirmation && (
          <ConfirmationCard
            title="この内容を確認しましたか？"
            confirmLabel="確認しました"
            onConfirm={() => close("確認の操作を試しました。保存や送信は行っていません。")}
            onCancel={() => close("確認を閉じました。変更はありません。")}
          >
            <p>これは確認カードの表示例です。ボタンを押しても、保存や外部への送信は行いません。</p>
            {long && (
              <p>
                {"長い説明も省略せずに確認できます。対象と内容をよく確かめてから操作してください。".repeat(
                  12,
                )}
                {"example_".repeat(35)}
              </p>
            )}
          </ConfirmationCard>
        )}
      </details>
    </Frame>
  );
}
