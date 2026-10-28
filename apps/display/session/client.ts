/** Only fixed device-local routes; the browser never receives cloud credentials. */
import { bootstrapLocalSession } from "../src/recovery/local-session.ts";
export class LocalOperationFailure extends Error {
  readonly status: number;
  constructor(status: number) {
    super("Local operation unavailable");
    this.status = status;
  }
}
export class LocalSessionClient {
  private csrf = "";
  private bootstrap: Promise<void> | undefined;
  private readonly transport: typeof fetch;
  private readonly bootstrapSession: (signal: AbortSignal) => Promise<{ csrf_token: string }>;
  constructor(transport: typeof fetch = fetch) {
    // A method call must not bind the native Window.fetch receiver to this client.
    this.transport = (input, init) => transport(input, init);
    this.bootstrapSession = (signal) => bootstrapLocalSession(signal, transport);
  }
  private async session(signal: AbortSignal) {
    if (this.csrf) return;
    if (!this.bootstrap)
      this.bootstrap = (async () => {
        const value = await this.bootstrapSession(signal);
        this.csrf = value.csrf_token;
      })().finally(() => {
        this.bootstrap = undefined;
      });
    await this.bootstrap;
  }
  private async request(path: string, signal: AbortSignal, body?: object, stop = false) {
    await this.session(signal);
    const response = await this.transport(path, {
      method: body || stop ? "POST" : "GET",
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
      signal,
      ...(stop ? { keepalive: true } : {}),
      headers: {
        "X-CSRF-Token": this.csrf,
        ...(body ? { "Content-Type": "application/json" } : {}),
      },
      ...(body ? { body: JSON.stringify(body) } : {}),
    });
    if (response.status === 401 || response.status === 403) this.csrf = "";
    if (!response.ok) throw new LocalOperationFailure(response.status);
    const raw = await response.text();
    if (new TextEncoder().encode(raw).length > 65_536) throw new Error("Local response too large");
    return JSON.parse(raw);
  }
  snapshot(signal: AbortSignal) {
    return this.request("/local/cloud/snapshot", signal);
  }
  operation(body: object, signal: AbortSignal) {
    return this.request("/local/cloud/operations", signal, body);
  }
  stop(id: string, signal: AbortSignal) {
    return this.request(`/local/cloud/private-sessions/${id}/stop`, signal, undefined, true);
  }
  async events(
    id: string,
    session: string | undefined,
    signal: AbortSignal,
    consume: (kind: string, value: unknown) => void | Promise<void>,
  ) {
    await this.session(signal);
    const response = await this.transport(`/local/cloud/runs/${id}/events`, {
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
      signal,
      headers: {
        Accept: "text/event-stream",
        ...(session ? { "X-Private-Session": session } : {}),
      },
    });
    if (
      !response.ok ||
      !response.body ||
      !response.headers.get("Content-Type")?.startsWith("text/event-stream")
    )
      throw new Error("Local stream unavailable");
    const reader = response.body.getReader(),
      decoder = new TextDecoder("utf-8", { fatal: true });
    let buffer = "";
    try {
      while (true) {
        const part = await reader.read();
        signal.throwIfAborted();
        if (part.done) break;
        buffer += decoder.decode(part.value, { stream: true }).replaceAll("\r", "");
        let end = buffer.indexOf("\n\n");
        while (end >= 0) {
          const frame = buffer.slice(0, end);
          buffer = buffer.slice(end + 2);
          if (new TextEncoder().encode(frame).length > 65_536)
            throw new Error("Stream frame too large");
          let kind = "",
            data = "";
          for (const line of frame.split("\n")) {
            if (line.startsWith("event: ")) kind = line.slice(7);
            if (line.startsWith("data: ")) data += `${data ? "\n" : ""}${line.slice(6)}`;
          }
          if (data) await consume(kind, JSON.parse(data));
          end = buffer.indexOf("\n\n");
        }
        if (new TextEncoder().encode(buffer).length > 65_536)
          throw new Error("Stream frame too large");
      }
      decoder.decode();
      if (buffer.trim()) throw new Error("Incomplete stream frame");
    } finally {
      await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  }
}
