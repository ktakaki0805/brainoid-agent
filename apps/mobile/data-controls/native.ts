import { Directory, type FilesystemPlugin } from "@capacitor/filesystem";
import type { SharePlugin } from "@capacitor/share";
import type { ExportDelivery } from "../../../packages/ui/src/data-controls/controller.ts";

const prefix = /^brainoid-export-v1-[0-9a-f-]{36}\.zip$/;
/** Only this adapter's cache files, never credentials or another feature's storage. */
export function nativeExportDelivery(
  filesystem: Pick<FilesystemPlugin, "readdir" | "writeFile" | "deleteFile">,
  share: Pick<SharePlugin, "canShare" | "share">,
) {
  let ready = false,
    busy = false,
    warning = "";
  const listeners = new Set<() => void>();
  const failedCleanup = () => {
    ready = false;
    warning =
      "エクスポートの一時ファイルを削除できませんでした。アプリを再起動して清掃を再試行してください。保存・共有先のコピーは別途削除が必要です。";
    for (const listener of listeners) listener();
  };
  const erase = async (path: string) => {
    try {
      await filesystem.deleteFile({ path, directory: Directory.Cache });
    } catch (error) {
      // Plugin's documented not-found code means a previous cleanup already won.
      if ((error as { code?: string })?.code !== "OS-PLUG-FILE-0008") {
        failedCleanup();
        throw new Error("Export temporary file cleanup failed");
      }
    }
  };
  const adapter: ExportDelivery & {
    initialize(): Promise<void>;
    subscribe(listener: () => void): () => void;
    snapshot(): string;
  } = {
    get available() {
      return ready;
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    snapshot: () => warning,
    async initialize() {
      ready = false;
      try {
        const files = await filesystem.readdir({ path: "", directory: Directory.Cache });
        for (const file of files.files)
          if (file.type === "file" && prefix.test(file.name)) await erase(file.name);
        ready = (await share.canShare()).value;
        if (ready) warning = "";
        if (!ready) warning = "この端末ではファイル共有を利用できません。";
      } catch {
        failedCleanup();
      }
      for (const listener of listeners) listener();
    },
    async save(blob, signal) {
      if (
        !ready ||
        busy ||
        blob.type !== "application/zip" ||
        blob.size > 4 * 1024 * 1024 ||
        blob.size < 22
      )
        throw new Error("Export delivery unavailable");
      signal.throwIfAborted();
      busy = true;
      const path = `brainoid-export-v1-${crypto.randomUUID()}.zip`;
      let wrote = false,
        cleanup: Promise<void> | undefined;
      const remove = () => (cleanup ??= erase(path));
      const aborted = () => {
        if (wrote) void remove().catch(() => {});
      };
      signal.addEventListener("abort", aborted);
      try {
        const bytes = new Uint8Array(await blob.arrayBuffer());
        signal.throwIfAborted();
        let binary = "";
        for (let offset = 0; offset < bytes.length; offset += 8192)
          binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
        // No token, API endpoint or server-generated URL crosses the native bridge.
        const result = await filesystem.writeFile({
          path,
          directory: Directory.Cache,
          data: btoa(binary),
        });
        wrote = true;
        signal.throwIfAborted();
        if (!result.uri.startsWith("file://")) throw new Error("Invalid native file URI");
        await share.share({
          title: "Brainoid エクスポート",
          dialogTitle: "保存・共有先を選択",
          files: [result.uri],
        });
        signal.throwIfAborted();
      } finally {
        signal.removeEventListener("abort", aborted);
        // write may have created a partial file even when it rejects. Attempt exact cleanup.
        try {
          await remove();
        } finally {
          busy = false;
        }
      }
    },
  };
  return adapter;
}
