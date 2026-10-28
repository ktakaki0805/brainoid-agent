import type { SessionController } from "@brainoid-internal/ui/session";
import type { FirebaseMessagingPlugin } from "@capacitor-firebase/messaging";
import { useState } from "react";
import { nativePush } from "../platform/push.ts";
import { PushController } from "./controller.ts";
import { PushSettings } from "./view.tsx";
/** Deliberately synthetic bridge for browser permission UI; no provider or OS calls. */
export function PushPreview({
  session,
  network,
}: {
  session: SessionController;
  network: { failRotation: boolean; failRemoval: boolean };
}) {
  const [fixture] = useState(() => {
    const state = { permission: "denied", fail: false, token: "SYNTHETIC_BROWSER_FCM_ONLY" };
    const listeners = new Map<string, (event: unknown) => void>();
    const permission = async () => {
      if (state.fail) throw new Error("Synthetic unavailable bridge");
      return { receive: state.permission };
    };
    const plugin = {
      checkPermissions: permission,
      requestPermissions: permission,
      getToken: async () => ({ token: state.token }),
      deleteToken: async () => {},
      addListener: async (name: string, listener: (event: unknown) => void) => {
        listeners.set(name, listener);
        return {
          remove: async () => {
            if (listeners.get(name) === listener) listeners.delete(name);
          },
        };
      },
    } as unknown as FirebaseMessagingPlugin;
    return {
      state,
      listeners,
      controller: new PushController(session, nativePush(plugin), "android"),
    };
  });
  return (
    <>
      <PushSettings controller={fixture.controller} />
      <details className="card">
        <summary>OS通知の体験用設定</summary>
        <p>ここでの許可と通知は架空です。実機OSの確認ではありません。</p>
        <label>
          OS通知の動作
          <select
            aria-label="OS通知の動作"
            defaultValue="denied"
            onChange={(event) => {
              fixture.state.fail = event.target.value === "unavailable";
              fixture.state.permission = ["granted", "rotation_failed"].includes(event.target.value)
                ? "granted"
                : "denied";
              network.failRotation = event.target.value === "rotation_failed";
              network.failRemoval = event.target.value === "rotation_failed";
            }}
          >
            <option value="denied">拒否</option>
            <option value="granted">許可</option>
            <option value="unavailable">初期化失敗</option>
            <option value="rotation_failed">更新・解除の通信失敗</option>
          </select>
        </label>
        <button
          className="button secondary"
          type="button"
          onClick={() => {
            fixture.state.token = "SYNTHETIC_BROWSER_FCM_ROTATED";
            fixture.listeners.get("tokenReceived")?.({ token: fixture.state.token });
          }}
        >
          架空のトークンを更新
        </button>
        <button
          className="button secondary"
          type="button"
          onClick={() =>
            fixture.listeners.get("notificationActionPerformed")?.({
              notification: {
                data: {
                  tenant_id: "10000000-0000-4000-8000-000000000001",
                  notification_id: "b0000000-0000-4000-8000-000000000001",
                  body: "PRIVATE-UNTRUSTED-PAYLOAD",
                  url: "https://untrusted.invalid",
                },
              },
            })
          }
        >
          架空の通知を開く
        </button>
      </details>
    </>
  );
}
