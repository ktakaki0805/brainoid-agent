import { useEffect, useSyncExternalStore } from "react";
import type { PushController } from "./controller.ts";
export function PushSettings({ controller }: { controller: PushController }) {
  const state = useSyncExternalStore(controller.subscribe, controller.snapshot);
  useEffect(() => controller.start(), [controller]);
  return (
    <section className="card" aria-label="OS通知設定">
      <h2>OS通知</h2>
      <p>
        許可状態: {state.permission} / 配信先:{" "}
        {state.removalUnconfirmed ? "解除未確認" : state.registered ? "登録済み" : "未登録"}
      </p>
      {state.message && <p role="status">{state.message}</p>}
      <div className="memory-actions">
        <button
          className="button secondary"
          type="button"
          disabled={state.busy || state.pending}
          onClick={() => void controller.enable()}
        >
          {state.removalUnconfirmed ? "配信先を再確認・登録" : "OS通知を許可・登録"}
        </button>
        {state.pending && (
          <button
            className="button secondary"
            type="button"
            disabled={state.busy}
            onClick={() => void controller.retry()}
          >
            同じ登録を再確認
          </button>
        )}
        <button
          className="button secondary"
          type="button"
          disabled={state.busy || !state.registered}
          onClick={() => void controller.disable()}
        >
          OS通知を解除
        </button>
      </div>
    </section>
  );
}
