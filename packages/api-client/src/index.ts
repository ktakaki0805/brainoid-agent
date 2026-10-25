import { createParser, type EventSourceMessage } from "eventsource-parser";
import { client as defaultClient } from "./generated/client.gen.js";
import { type Client, type Config, createClient } from "./generated/client/index.js";
import { type Options, streamRunEvents as generatedStreamRunEvents } from "./generated/sdk.gen.js";
import type { StreamRunEventsData } from "./generated/types.gen.js";

export * from "./generated/sdk.gen.js";
export type * from "./generated/types.gen.js";
export type { Auth, Client } from "./generated/client/index.js";

/** Decoded SSE data must be validated against run-events.schema.json before use. */
export type StreamRunEventsResponse = unknown;
export type StreamRunEventsResponses = { 200: StreamRunEventsResponse };
export type StreamRunEventsOptions = Omit<
  Options<StreamRunEventsData, false, unknown>,
  "onRequest" | "sseDefaultRetryDelay" | "sseMaxRetryAttempts" | "sseMaxRetryDelay"
>;

/** One connection per call; failure rejects the iterator so callers can reauthenticate. */
export async function streamRunEvents(
  options: StreamRunEventsOptions,
): Promise<{ stream: AsyncGenerator<StreamRunEventsResponse, void, unknown> }> {
  const client = options.client ?? defaultClient;
  async function* readEvents(request: Parameters<Client["sse"]["get"]>[0]) {
    let reader: ReadableStreamDefaultReader<string> | undefined;
    const config = { ...client.getConfig(), ...request };
    const cancel = () => reader?.cancel().catch(() => {});
    try {
      if (config.signal?.aborted) return;
      const result = await client.get({
        ...request,
        parseAs: "stream",
        responseStyle: "fields",
        throwOnError: false,
      });
      if (!result.response) throw result.error;
      if (!result.response.ok) {
        throw new Error(`SSE failed: ${result.response.status} ${result.response.statusText}`);
      }
      if (result.error !== undefined) throw result.error;
      if (!result.response.body) throw new Error("No body in SSE response");
      reader = result.response.body.pipeThrough(new TextDecoderStream()).getReader();
      config.signal?.addEventListener("abort", cancel);
      if (config.signal?.aborted) return;
      let lastId: string | undefined;
      let retry = 3000;
      const frames: Array<{ message: EventSourceMessage; id: string | undefined; retry: number }> =
        [];
      const parser = createParser({
        onId: (id) => {
          lastId = id;
        },
        onRetry: (value) => {
          retry = value;
        },
        onEvent: (message) => frames.push({ message, id: lastId, retry }),
      });
      while (true) {
        const { done, value } = await reader.read();
        if (done || config.signal?.aborted) break;
        parser.feed(value);
        for (const frame of frames.splice(0)) {
          if (config.signal?.aborted) return;
          let data: unknown = frame.message.data;
          let parsedJson = false;
          try {
            data = JSON.parse(frame.message.data);
            parsedJson = true;
          } catch {
            /* Non-JSON event data remains untrusted text. */
          }
          if (parsedJson) {
            await config.responseValidator?.(data);
            if (config.signal?.aborted) return;
            if (config.responseTransformer) data = await config.responseTransformer(data);
          }
          if (config.signal?.aborted) return;
          options.onSseEvent?.({
            data,
            event: frame.message.event ?? "message",
            ...(frame.id === undefined ? {} : { id: frame.id }),
            retry: frame.retry,
          });
          if (config.signal?.aborted) return;
          yield data;
        }
      }
    } catch (error) {
      if (config.signal?.aborted) return;
      options.onSseError?.(error);
      throw error;
    } finally {
      config.signal?.removeEventListener("abort", cancel);
      await cancel();
      reader?.releaseLock();
    }
  }

  // The SDK still selects the generated URL and security requirements. Its private
  // SSE signature describes the wire string; this boundary publishes decoded data
  // only as unknown and replaces the parser without editing generated files.
  const get = (async (request) => ({ stream: readEvents(request) })) as Client["sse"]["get"];
  return generatedStreamRunEvents({
    ...options,
    client: { ...client, sse: { ...client.sse, get } },
  });
}

export interface BrainoidClientOptions {
  /** Absolute service root, for example https://api.example.test. */
  baseUrl: string;
  /** Called for each authenticated request; return the raw bearer token. */
  auth?: Config["auth"];
  /** Inject a platform transport or a test fake. */
  fetch?: typeof globalThis.fetch;
}

/** Create an isolated client. Credentials are never kept in a global client. */
export function createBrainoidClient({ baseUrl, auth, fetch }: BrainoidClientOptions) {
  const root = new URL(baseUrl);
  if (
    !/^https?:$/.test(root.protocol) ||
    root.username ||
    root.password ||
    root.search ||
    root.hash
  ) {
    throw new TypeError(
      "baseUrl must be an HTTP(S) service root without credentials, query or fragment",
    );
  }
  const serviceRoot = root.href.replace(/\/+$/, "");
  const client = createClient({
    baseUrl: `${serviceRoot}/v1`,
    ...(auth === undefined ? {} : { auth }),
    ...(fetch === undefined ? {} : { fetch }),
  });
  // The generator currently ignores path-level servers. These two public health
  // paths use the service root; business operations use its /v1 base path.
  client.interceptors.request.use((request, options) => {
    if (options.url === "/health/live" || options.url === "/health/ready") {
      return new Request(`${serviceRoot}${options.url}`, request);
    }
    return request;
  });
  return client;
}
