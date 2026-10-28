import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  cpSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { test } from "node:test";
import { parse } from "yaml";
import * as sdk from "../packages/api-client/src/index.ts";

const spec = parse(readFileSync("specs/openapi.yaml", "utf8"), { maxAliasCount: -1 });
const run = (command, args) =>
  spawnSync(command, args, {
    encoding: "utf8",
    timeout: 30_000,
    env: { ...process.env, REDOCLY_TELEMETRY: "off" },
  });
function withJson(value, callback) {
  const directory = mkdtempSync(join(tmpdir(), "brainoid-contract-test-"));
  try {
    const path = join(directory, "candidate.json");
    writeFileSync(path, JSON.stringify(value));
    return callback(path);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
}
function expectExit(result, code) {
  assert.equal(result.error, undefined);
  assert.equal(result.status, code, `${result.stdout}\n${result.stderr}`);
}

test("all 94 business operations and two health operations have generated methods", () => {
  const operations = Object.values(spec.paths)
    .flatMap((path) => Object.values(path))
    .filter((operation) => operation.operationId);
  assert.equal(operations.length, 96);
  assert.equal(
    spec.paths["/devices/{device_id}/snapshot:validate"].post.operationId,
    "validateDeviceSnapshot",
  );
  const target = spec.paths["/tenants/{tenant_id}/commands/{command_id}/target"].get;
  assert.equal(target.operationId, "getDeviceCommandTarget");
  assert.deepEqual(target.security, [{ userBearer: [] }, { deviceBearer: [] }]);
  const home = spec.paths["/tenants/{tenant_id}/integrations/{integration_id}/home-assistant"];
  assert.equal(home.get.operationId, "getHomeAssistantIntegration");
  assert.equal(home.delete.operationId, "revokeHomeAssistantIntegration");
  assert.deepEqual(home.get.security, [{ userBearer: [] }]);
  assert.deepEqual(home.delete.security, [{ userBearer: [] }]);
  assert.deepEqual(
    spec.paths["/tenants/{tenant_id}/integrations/{integration_id}/home-assistant/entities:sync"]
      .post.security,
    [{ deviceBearer: [] }],
  );
  for (const operation of operations) {
    // biome-ignore lint/performance/noDynamicNamespaceImportAccess: contract-wide export coverage.
    assert.equal(typeof sdk[operation.operationId], "function", operation.operationId);
  }
});

test("membership SDK Requests preserve dot segments and opaque UID query characters", () => {
  const tenant = "51cea16f-85c3-5b21-a3bf-5fe62e091124";
  const probe = run("node", ["tests/fixtures/memberships/sdk_requests.mjs", tenant]);
  expectExit(probe, 0);
  const requests = JSON.parse(probe.stdout);
  assert.deepEqual(
    requests.filter((request) => request.method === "PATCH").map((request) => request.user_id),
    [".", "..", "opaque / uid", "%2E", "u?/#+"],
  );
  assert.equal(requests.length, 10);
  for (const request of requests) {
    const url = new URL(request.url);
    assert.equal(url.pathname, `/v1/tenants/${tenant}/memberships`);
    assert.equal(url.searchParams.get("user_id"), request.user_id);
    assert.equal(request.headers.authorization, "Bearer synthetic-owner-token");
    assert.equal(request.headers["if-match"], request.method === "PATCH" ? '"1"' : '"2"');
    assert.equal(request.body, request.method === "PATCH" ? '{"role":"admin"}' : "");
  }
});

const invalidOpenApi = [
  [
    "unresolved reference",
    (s) => {
      s.components.schemas.User.properties.user_id = { $ref: "#/components/schemas/Missing" };
    },
    "no-unresolved-refs",
  ],
  [
    "duplicate operationId",
    (s) => {
      s.paths["/health/live"].get.operationId = "getReadiness";
    },
    "operation-operationId-unique",
  ],
  [
    "missing required info field",
    (s) => {
      delete s.info.version;
    },
    "struct",
  ],
  [
    "invalid schema type",
    (s) => {
      s.components.schemas.User.type = "not-a-type";
    },
    "struct",
  ],
  [
    "missing path parameter",
    (s) => {
      s.paths["/tenants/{tenant_id}"].get.parameters = [];
    },
    "path-parameters-defined",
  ],
  [
    "undefined authentication scheme",
    (s) => {
      s.paths["/users/me"].get.security = [{ unknownBearer: [] }];
    },
    "security-defined",
  ],
];
for (const [name, mutate, rule] of invalidOpenApi) {
  test(`OpenAPI validator rejects ${name}`, () => {
    const candidate = structuredClone(spec);
    mutate(candidate);
    withJson(candidate, (path) => {
      const result = run("pnpm", ["exec", "redocly", "lint", "--config", "redocly.yaml", path]);
      expectExit(result, 1);
      assert.match(result.stdout + result.stderr, new RegExp(rule));
    });
  });
}

for (const [name, mutate, diagnostic] of [
  [
    "zero multipleOf in component schema",
    (s) => {
      s.components.schemas.Version.multipleOf = 0;
    },
    "exclusiveMinimum",
  ],
  [
    "duplicate required in inline schema",
    (s) => {
      s.paths["/health/live"].get.responses["200"].content["application/json"].schema.required.push(
        "status",
      );
    },
    "uniqueItems",
  ],
]) {
  test(`OpenAPI Schema Object meta-schema rejects ${name}`, () => {
    const candidate = structuredClone(spec);
    mutate(candidate);
    withJson(candidate, (path) => {
      const result = run("scripts/validate_contracts", [path]);
      expectExit(result, 1);
      assert.match(result.stdout + result.stderr, new RegExp(diagnostic));
    });
  });
}

const ajv = ["exec", "ajv", "compile", "--spec=draft2020", "--strict=false", "-c", "ajv-formats"];
for (const [name, mutate, diagnostic] of [
  [
    "invalid meta-schema value",
    (schema) => {
      schema.$defs.started.properties.seq.type = "invalid";
    },
    "schema is invalid",
  ],
  [
    "unresolved local reference",
    (schema) => {
      schema.oneOf[0].$ref = "#/$defs/missing";
    },
    "can't resolve reference",
  ],
]) {
  test(`JSON Schema validator rejects ${name}`, () => {
    const schema = JSON.parse(readFileSync("specs/run-events.schema.json", "utf8"));
    mutate(schema);
    withJson(schema, (path) => {
      const result = run("pnpm", [...ajv, "-s", path]);
      expectExit(result, 1);
      assert.match(result.stderr, new RegExp(diagnostic));
    });
  });
}

test("required payload is validated for valid and incomplete SSE examples", () => {
  const event = {
    run_id: "5e36c181-fbc1-4720-8819-069f47b9c22c",
    seq: 1,
    type: "run.started",
    occurred_at: "2026-10-26T00:00:00Z",
    payload: {},
  };
  const args = [
    "exec",
    "ajv",
    "validate",
    "--spec=draft2020",
    "--strict=false",
    "-c",
    "ajv-formats",
    "-s",
    "specs/run-events.schema.json",
  ];
  withJson(event, (path) => expectExit(run("pnpm", [...args, "-d", path]), 0));
  delete event.payload;
  withJson(event, (path) => {
    const result = run("pnpm", [...args, "-d", path]);
    expectExit(result, 1);
    assert.match(result.stderr, /required/);
  });
});

for (const [name, mutate] of [
  [
    "deleted operation",
    (s) => {
      delete s.paths["/users/me"].get;
    },
  ],
  [
    "new required request field",
    (s) => {
      s.components.schemas.TenantCreate.required.push("timezone");
    },
  ],
  [
    "narrowed request enum",
    (s) => {
      s.components.schemas.MembershipPatch.properties.role.enum = ["member"];
    },
  ],
]) {
  test(`compatibility check rejects ${name}`, () => {
    const candidate = structuredClone(spec);
    mutate(candidate);
    withJson(candidate, (path) => {
      const result = run("scripts/check_contract_compatibility", ["specs/openapi.yaml", path]);
      expectExit(result, 1);
      assert.match(result.stdout + result.stderr, /error|warning/i);
    });
  });
}

test("additive operation and description-only changes remain compatible", () => {
  const candidate = structuredClone(spec);
  candidate.info.description = "Updated documentation";
  candidate.paths["/health/new"] = structuredClone(candidate.paths["/health/live"]);
  candidate.paths["/health/new"].get.operationId = "newHealth";
  withJson(candidate, (path) =>
    expectExit(run("scripts/check_contract_compatibility", ["specs/openapi.yaml", path]), 0),
  );
});

test("regeneration check rejects modified and extra artifacts without changing them", () => {
  const directory = mkdtempSync(join(tmpdir(), "brainoid-generation-test-"));
  try {
    for (const path of [
      "scripts/generate_api_client",
      "scripts/generate_api_client.mjs",
      "openapi-ts.config.mjs",
      "package.json",
      "tsconfig.base.json",
      "packages/api-client/tsconfig.json",
      "specs/openapi.yaml",
      "packages/api-client/src/generated",
    ]) {
      const destination = join(directory, path);
      mkdirSync(dirname(destination), { recursive: true });
      cpSync(path, destination, { recursive: true });
    }
    symlinkSync(resolve("node_modules"), join(directory, "node_modules"), "dir");
    const output = join(directory, "packages/api-client/src/generated");
    const path = join(output, "sdk.gen.js");
    const original = readFileSync(path, "utf8");
    for (const drift of ["modified", "extra"]) {
      const changed = drift === "modified" ? path : join(output, "unexpected.txt");
      const content = drift === "modified" ? `${original}\n// stale fixture\n` : "stale fixture";
      writeFileSync(changed, content);
      const result = spawnSync("scripts/generate_api_client", ["--check"], {
        cwd: directory,
        encoding: "utf8",
        timeout: 30_000,
      });
      expectExit(result, 1);
      assert.match(result.stderr, /Generated client differs/, result.stdout + result.stderr);
      assert.equal(readFileSync(changed, "utf8"), content);
      writeFileSync(path, original);
    }
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("client injects URL, rotating bearer and transport; public health has no auth", async () => {
  const requests = [];
  const schemes = [];
  let generation = 0;
  const client = sdk.createBrainoidClient({
    baseUrl: "https://api.example.test/",
    auth: async (scheme) => {
      schemes.push(scheme.key);
      return `fake-token-${++generation}`;
    },
    fetch: async (request) => {
      requests.push(request);
      return Response.json({ status: "ok" });
    },
  });
  await sdk.getCurrentUser({ client });
  await sdk.getCurrentUser({ client });
  await sdk.getLiveness({ client });
  await sdk.getReadiness({ client });
  assert.deepEqual(
    requests.map((request) => request.url),
    [
      "https://api.example.test/v1/users/me",
      "https://api.example.test/v1/users/me",
      "https://api.example.test/health/live",
      "https://api.example.test/health/ready",
    ],
  );
  assert.deepEqual(
    requests.map((request) => request.headers.get("Authorization")),
    ["Bearer fake-token-1", "Bearer fake-token-2", null, null],
  );
  assert.deepEqual(schemes, ["userBearer", "userBearer"]);
});

test("client serializes typed body, required headers, path and query for isolated clients", async () => {
  const requests = [];
  const transport = async (request) => {
    requests.push(request);
    return Response.json({});
  };
  const user = sdk.createBrainoidClient({
    baseUrl: "https://user.example.test",
    auth: "fake-user",
    fetch: transport,
  });
  const device = sdk.createBrainoidClient({
    baseUrl: "https://device.example.test",
    auth: (scheme) => {
      assert.equal(scheme.key, "deviceBearer");
      return "fake-device";
    },
    fetch: transport,
  });
  await sdk.createTenant({
    client: user,
    body: { name: "Fixture home" },
    headers: { "Idempotency-Key": "fixture-idempotency-key" },
  });
  await sdk.getDeviceSnapshot({
    client: device,
    path: { device_id: "fixture-device" },
    query: { cursor: "a+b/c" },
  });
  assert.equal(requests[0].method, "POST");
  assert.equal(requests[0].headers.get("Idempotency-Key"), "fixture-idempotency-key");
  assert.equal(requests[0].headers.get("Content-Type"), "application/json");
  assert.deepEqual(await requests[0].json(), { name: "Fixture home" });
  assert.equal(requests[0].headers.get("Authorization"), "Bearer fake-user");
  assert.equal(requests[1].headers.get("Authorization"), "Bearer fake-device");
  assert.equal(
    requests[1].url,
    "https://device.example.test/v1/devices/fixture-device/snapshot?cursor=a%2Bb%2Fc",
  );
});

test("client preserves structured API errors and exposes transport failures without retries", async () => {
  const error = {
    error: {
      code: "operation_forbidden",
      message: "Denied",
      request_id: "fixture",
      retryable: false,
    },
  };
  const client = sdk.createBrainoidClient({
    baseUrl: "https://api.example.test",
    fetch: async () => Response.json(error, { status: 403 }),
  });
  const response = await sdk.getCurrentUser({ client });
  assert.deepEqual(response.error, error);
  assert.equal(response.response.status, 403);
  let calls = 0;
  const offline = sdk.createBrainoidClient({
    baseUrl: "https://api.example.test",
    fetch: async () => {
      calls++;
      throw new Error("offline");
    },
  });
  await assert.rejects(sdk.getCurrentUser({ client: offline, throwOnError: true }), /offline/);
  assert.equal(calls, 1);
});

test("configuration rejects credential-bearing and unsupported URLs", () => {
  for (const baseUrl of [
    "https://user:secret@example.test",
    "ftp://example.test",
    "https://example.test?token=fake",
    "https://example.test#fake",
  ]) {
    assert.throws(() => sdk.createBrainoidClient({ baseUrl }), TypeError);
  }
});

const runStarted = {
  run_id: "5e36c181-fbc1-4720-8819-069f47b9c22c",
  seq: 1,
  type: "run.started",
  occurred_at: "2026-10-26T00:00:00Z",
  payload: {},
};
const streamPath = { tenant_id: "fixture-tenant", run_id: runStarted.run_id };
const eventResponse = () =>
  new Response(`id: 1\nevent: run.started\ndata: ${JSON.stringify(runStarted)}\n\n`, {
    headers: { "Content-Type": "text/event-stream" },
  });

test("public SSE adapter yields decoded event objects and exposes cursor metadata", async () => {
  const frames = [];
  const client = sdk.createBrainoidClient({
    baseUrl: "https://api.example.test",
    fetch: async (request) => {
      assert.equal(request.headers.get("Last-Event-ID"), "0");
      return eventResponse();
    },
  });
  const { stream } = await sdk.streamRunEvents({
    client,
    path: streamPath,
    headers: { "Last-Event-ID": "0" },
    onSseEvent: (frame) => frames.push(frame),
  });
  assert.deepEqual(await Array.fromAsync(stream), [runStarted]);
  assert.deepEqual(frames[0].data, runStarted);
  assert.equal(frames[0].id, "1");
  assert.equal(frames[0].event, "run.started");
});

test("SSE request customization uses the client request interceptor", async () => {
  let calls = 0;
  const client = sdk.createBrainoidClient({
    baseUrl: "https://api.example.test",
    fetch: async (request) => {
      assert.equal(request.headers.get("X-Fixture-Intercepted"), "yes");
      return eventResponse();
    },
  });
  client.interceptors.request.use((request) => {
    calls++;
    request.headers.set("X-Fixture-Intercepted", "yes");
    return request;
  });
  const { stream } = await sdk.streamRunEvents({ client, path: streamPath });
  assert.deepEqual(await Array.fromAsync(stream), [runStarted]);
  assert.equal(calls, 1);
});

test("SSE 401 rejects without retry and explicit reconnect acquires the updated token", async () => {
  const requests = [];
  let tokenCalls = 0;
  let currentToken = "fake-expired";
  const errors = [];
  const client = sdk.createBrainoidClient({
    baseUrl: "https://api.example.test",
    auth: (scheme) => {
      if (scheme.key !== "userBearer") return undefined;
      tokenCalls++;
      return currentToken;
    },
    fetch: async (request) => {
      requests.push(request.headers.get("Authorization"));
      return requests.length === 1 ? new Response(null, { status: 401 }) : eventResponse();
    },
  });
  const failed = await sdk.streamRunEvents({
    client,
    path: streamPath,
    onSseError: (error) => errors.push(error),
    // Even untyped JS callers cannot enable the upstream automatic retry.
    sseMaxRetryAttempts: 3,
    sseDefaultRetryDelay: 0,
  });
  await assert.rejects(Array.fromAsync(failed.stream), /SSE failed: 401/);
  assert.deepEqual(requests, ["Bearer fake-expired"]);
  assert.equal(tokenCalls, 1);
  assert.equal(errors.length, 1);
  currentToken = "fake-renewed";
  const reconnected = await sdk.streamRunEvents({ client, path: streamPath });
  assert.deepEqual(await Array.fromAsync(reconnected.stream), [runStarted]);
  assert.deepEqual(requests, ["Bearer fake-expired", "Bearer fake-renewed"]);
  assert.equal(tokenCalls, 2);
});

test("revoked SSE authorization is observable and revoked token provider stops reconnect", async () => {
  let requests = 0;
  let tokenCalls = 0;
  let revoked = false;
  const client = sdk.createBrainoidClient({
    baseUrl: "https://api.example.test",
    auth: (scheme) => {
      if (scheme.key !== "userBearer") return undefined;
      tokenCalls++;
      if (revoked) throw new Error("fixture session revoked");
      return "fake-revoked";
    },
    fetch: async () => {
      requests++;
      return new Response(null, { status: 403 });
    },
  });
  const { stream } = await sdk.streamRunEvents({ client, path: streamPath });
  await assert.rejects(Array.fromAsync(stream), /SSE failed: 403/);
  assert.equal(requests, 1);
  revoked = true;
  const reconnected = await sdk.streamRunEvents({ client, path: streamPath });
  await assert.rejects(Array.fromAsync(reconnected.stream), /fixture session revoked/);
  assert.equal(requests, 1);
  assert.equal(tokenCalls, 2);
});

async function receiveSseChunks(chunks) {
  const frames = [];
  const client = sdk.createBrainoidClient({
    baseUrl: "https://api.example.test",
    fetch: async () =>
      new Response(
        new ReadableStream({
          start(controller) {
            for (const chunk of chunks) controller.enqueue(chunk);
            controller.close();
          },
        }),
        { headers: { "Content-Type": "text/event-stream" } },
      ),
  });
  const { stream } = await sdk.streamRunEvents({
    client,
    path: streamPath,
    onSseEvent: (frame) => frames.push(frame),
  });
  return { events: await Array.fromAsync(stream), frames };
}

for (const multiline of [false, true]) {
  test(`SSE ${multiline ? "multiple" : "single"} data lines preserve events and metadata at every byte split`, async () => {
    const event = { ...runStarted, type: "text.delta", payload: { text: "こんにちは" } };
    const json = JSON.stringify(event, null, multiline ? 2 : undefined);
    const wire = `: heartbeat\r\nid: 1\r\nevent: text.delta\r\nretry: 1500\r\n${json
      .split("\n")
      .map((line) => `data: ${line}\r\n`)
      .join("")}\r\n`;
    const bytes = new TextEncoder().encode(wire);
    const expected = {
      events: [event],
      frames: [{ data: event, event: "text.delta", id: "1", retry: 1500 }],
    };
    assert.deepEqual(await receiveSseChunks([bytes]), expected);
    for (let split = 1; split < bytes.length; split++) {
      assert.deepEqual(
        await receiveSseChunks([bytes.slice(0, split), bytes.slice(split)]),
        expected,
        `byte split ${split} must preserve CRLF, UTF-8, data and metadata`,
      );
    }
    assert.deepEqual(
      await receiveSseChunks([...bytes].map((byte) => Uint8Array.of(byte))),
      expected,
    );
  });
}

test("SSE preserves id and retry state, text whitespace, and ignores incomplete final events", async () => {
  const wire =
    "id: first\r\n\r\nretry: 1000\r\ndata:   text\r\ndata: next\r\n\r\n" +
    "id:\nretry: 2000\nevent: custom\ndata: 2\n\n" +
    "data: incomplete";
  assert.deepEqual(await receiveSseChunks([new TextEncoder().encode(wire)]), {
    events: ["  text\nnext", 2],
    frames: [
      { data: "  text\nnext", event: "message", id: "first", retry: 1000 },
      { data: 2, event: "custom", id: "", retry: 2000 },
    ],
  });
});

test("stopping SSE iteration cancels the underlying response stream", async () => {
  let cancelled = false;
  const client = sdk.createBrainoidClient({
    baseUrl: "https://api.example.test",
    fetch: async () =>
      new Response(
        new ReadableStream({
          start(controller) {
            controller.enqueue(new TextEncoder().encode("data: {}\n\n"));
          },
          cancel() {
            cancelled = true;
          },
        }),
        { headers: { "Content-Type": "text/event-stream" } },
      ),
  });
  const { stream } = await sdk.streamRunEvents({ client, path: streamPath });
  await stream.next();
  await stream.return();
  assert.equal(cancelled, true);
});

async function abortableSse({
  wire = "data: 1\n\ndata: 2\n\n",
  controller = new AbortController(),
  ...options
} = {}) {
  const callbacks = [];
  const errors = [];
  let requests = 0;
  let cancellations = 0;
  const client = sdk.createBrainoidClient({
    baseUrl: "https://api.example.test",
    fetch: async () => {
      requests++;
      return new Response(
        new ReadableStream({
          start(stream) {
            stream.enqueue(new TextEncoder().encode(wire));
          },
          cancel() {
            cancellations++;
          },
        }),
        { headers: { "Content-Type": "text/event-stream" } },
      );
    },
  });
  const { stream } = await sdk.streamRunEvents({
    client,
    path: streamPath,
    ...options,
    signal: controller.signal,
    onSseEvent(frame) {
      callbacks.push(frame.data);
      options.onSseEvent?.(frame);
    },
    onSseError: (error) => errors.push(error),
  });
  return {
    stream,
    controller,
    callbacks,
    errors,
    get requests() {
      return requests;
    },
    get cancellations() {
      return cancellations;
    },
  };
}

test("SSE abort discards remaining frames from the same chunk", async () => {
  const fixture = await abortableSse();
  assert.deepEqual(await fixture.stream.next(), { value: 1, done: false });
  fixture.controller.abort();
  assert.deepEqual(await fixture.stream.next(), { value: undefined, done: true });
  assert.deepEqual(fixture.callbacks, [1]);
  assert.deepEqual(fixture.errors, []);
  assert.equal(fixture.cancellations, 1);
});

test("SSE abort cancels a pending read and completes the iterator", async () => {
  const fixture = await abortableSse({ wire: "data: 1\n\n" });
  assert.deepEqual(await fixture.stream.next(), { value: 1, done: false });
  const pending = fixture.stream.next();
  fixture.controller.abort();
  assert.deepEqual(await pending, { value: undefined, done: true });
  assert.deepEqual(fixture.callbacks, [1]);
  assert.deepEqual(fixture.errors, []);
  assert.equal(fixture.cancellations, 1);
});

for (const hook of ["responseValidator", "responseTransformer"]) {
  for (const outcome of ["resolve", "reject"]) {
    test(`SSE abort during ${hook} discards data when the hook later ${outcome}s`, async () => {
      const entered = Promise.withResolvers();
      const gate = Promise.withResolvers();
      const calls = [];
      const runHook = async (name, data) => {
        calls.push(name);
        if (name === hook) {
          entered.resolve();
          await gate.promise;
        }
        return data;
      };
      const fixture = await abortableSse({
        responseValidator: (data) => runHook("responseValidator", data),
        responseTransformer: (data) => runHook("responseTransformer", data),
      });
      const pending = fixture.stream.next();
      await entered.promise;
      fixture.controller.abort();
      if (outcome === "resolve") gate.resolve();
      else gate.reject(new Error("fixture hook ended after abort"));
      assert.deepEqual(await pending, { value: undefined, done: true });
      assert.deepEqual(fixture.callbacks, []);
      assert.deepEqual(fixture.errors, []);
      assert.deepEqual(
        calls,
        hook === "responseValidator"
          ? ["responseValidator"]
          : ["responseValidator", "responseTransformer"],
      );
      assert.equal(fixture.cancellations, 1);
    });
  }
}

test("SSE abort from its callback prevents yield and subsequent callbacks", async () => {
  const controller = new AbortController();
  const fixture = await abortableSse({
    controller,
    onSseEvent: () => controller.abort(),
  });
  assert.deepEqual(await fixture.stream.next(), { value: undefined, done: true });
  assert.deepEqual(fixture.callbacks, [1]);
  assert.deepEqual(fixture.errors, []);
  assert.equal(fixture.cancellations, 1);
});

test("SSE with an already aborted signal completes without starting transport", async () => {
  const controller = new AbortController();
  controller.abort();
  const fixture = await abortableSse({ controller });
  assert.deepEqual(await fixture.stream.next(), { value: undefined, done: true });
  assert.deepEqual(fixture.callbacks, []);
  assert.deepEqual(fixture.errors, []);
  assert.equal(fixture.requests, 0);
});

test("run cursor applies a seq once across explicit reconnect and preserves failed rendering", () => {
  const cursor = new sdk.RunEventCursor(runStarted.run_id);
  const rendered = [];
  assert.equal(
    cursor.apply(runStarted, (event) => {
      rendered.push(event);
    }),
    true,
  );
  assert.equal(
    cursor.apply(runStarted, () => assert.fail("duplicate")),
    false,
  );
  assert.equal(cursor.lastEventId, "1");
  assert.throws(() => cursor.apply({ ...runStarted, seq: 3 }, () => {}), /gap/);
  assert.throws(() => cursor.apply({ ...runStarted, run_id: "other" }, () => {}), /sequence/);
  assert.throws(
    () =>
      cursor.apply({ ...runStarted, seq: 2 }, () => {
        throw new Error("render failed");
      }),
    /render failed/,
  );
  assert.equal(cursor.lastEventId, "1");
  const reconnected = new sdk.RunEventCursor(runStarted.run_id, Number(cursor.lastEventId));
  assert.equal(
    reconnected.apply(runStarted, () => assert.fail("replayed")),
    false,
  );
  assert.equal(
    reconnected.apply({ ...runStarted, seq: 2 }, (event) => {
      rendered.push(event);
    }),
    true,
  );
  assert.deepEqual(
    rendered.map((event) => event.seq),
    [1, 2],
  );
  assert.equal(reconnected.lastEventId, "2");
});
