import { useSyncExternalStore } from "react";
export function ExportCleanupNotice({
  adapter,
}: {
  adapter: { subscribe(listener: () => void): () => void; snapshot(): string };
}) {
  const notice = useSyncExternalStore(adapter.subscribe, adapter.snapshot);
  return notice ? (
    <p className="card" role="alert">
      {notice}
    </p>
  ) : null;
}
