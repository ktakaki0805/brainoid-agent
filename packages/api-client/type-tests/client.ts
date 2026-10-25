import {
  createBrainoidClient,
  createTenant,
  getCurrentUser,
  getTenant,
  streamRunEvents,
  type StreamRunEventsResponse,
  updateTenant,
} from "../src/index";

const client = createBrainoidClient({
  baseUrl: "https://api.example.test",
  auth: async () => "fake",
});
createTenant({ client, body: { name: "Fixture" }, headers: { "Idempotency-Key": "fake-key" } });
getCurrentUser({ client }).then((result) => {
  if (result.data) {
    const uid: string = result.data.user_id;
    void uid;
    // @ts-expect-error Firebase UID is opaque text, not a number.
    const invalid: number = result.data.user_id;
    void invalid;
  }
});
// @ts-expect-error A required path parameter cannot be omitted.
getTenant({ client });
// @ts-expect-error Creation requires the name field in its body.
createTenant({ client, body: {}, headers: { "Idempotency-Key": "fake-key" } });
// @ts-expect-error Mutation requires both If-Match and Idempotency-Key headers.
updateTenant({ client, path: { tenant_id: "fixture" }, body: { name: "Updated" } });

streamRunEvents({
  client,
  path: { tenant_id: "fixture", run_id: "fixture" },
  onSseEvent(frame) {
    const data: unknown = frame.data;
    void data;
    // @ts-expect-error Decoded SSE data is untrusted; it is not a wire string.
    const text: string = frame.data;
    void text;
  },
}).then(async ({ stream }) => {
  for await (const event of stream) {
    const data: StreamRunEventsResponse = event;
    void data;
    // @ts-expect-error The public adapter requires validation before using an event.
    const text: string = event;
    void text;
  }
});
streamRunEvents({
  client,
  path: { tenant_id: "fixture", run_id: "fixture" },
  // @ts-expect-error Reconnection requires a new call and fresh authentication.
  sseMaxRetryAttempts: 2,
});
streamRunEvents({
  client,
  path: { tenant_id: "fixture", run_id: "fixture" },
  // @ts-expect-error Request customization uses client.interceptors.request.
  onRequest: async () => new Request("https://api.example.test"),
});
