import assert from "node:assert/strict";
import { test } from "node:test";
import { CalendarController } from "../packages/ui/src/calendar/controller.ts";
import { publishCalendarAuthority } from "../packages/ui/src/calendar/authority.ts";
import {
  calendarIds,
  validChange,
  event,
  timeLabel,
  proposal,
} from "../packages/ui/src/calendar/contracts.ts";
import { calendarId, integrationId } from "../packages/ui/src/calendar/fixture.ts";
import { createSessionFixture, tenantA, tenantB } from "../packages/ui/src/session-fixture.ts";
import { SessionController } from "../packages/ui/src/session.ts";
import { ChatController } from "../packages/ui/src/chat/controller.ts";
import { fixturePrivate } from "../packages/ui/src/chat/fixture.ts";
import {
  nativeCalendarOAuth,
  fixedReturnURL,
  authorizationURL,
} from "../apps/mobile/platform/oauth.ts";
import { syntheticBrowser } from "../apps/mobile/platform/oauth-fixture.ts";

const uid = "synthetic@example.test",
  tick = () => new Promise((resolve) => setTimeout(resolve, 5));
const range = { time_min: "2099-11-01T00:00:00Z", time_max: "2099-11-08T00:00:00Z" };
const writes = (fixture) => fixture.calendar.controls.calls.filter((v) => v.method !== "GET");
async function using(
  work,
  {
    alter = (f) => f.transport,
    surface = "mobile",
    bridge = syntheticBrowser(),
    initialize = true,
  } = {},
) {
  const fixture = createSessionFixture();
  const session = new SessionController({
    auth: fixture.auth,
    baseUrl: "https://session.invalid",
    transport: alter(fixture),
  });
  const stop = session.start();
  await tick();
  await session.signIn(uid, "synthetic");
  await session.selectTenant(tenantA);
  const context = session.snapshot().context,
    calendar = new CalendarController(
      context,
      surface,
      nativeCalendarOAuth(bridge.browser, bridge.app, bridge.target),
    );
  try {
    if (initialize) await calendar.load();
    if (initialize && surface === "mobile") {
      await calendar.select(integrationId);
      await calendar.events(calendarId, range);
    }
    await work({ fixture, session, context, calendar, bridge });
  } finally {
    stop();
  }
}
async function make(calendar, title = "新しい架空の予定") {
  calendar.edit("create");
  calendar.draft({ ...calendar.snapshot().draft, summary: title });
  await calendar.propose();
  assert.ok(calendar.snapshot().proposal);
}
test("selected opaque calendar events preserve all-day exclusive end, UTC, zone, ETag and fetched_at", () =>
  using(async ({ calendar, fixture }) => {
    const state = calendar.snapshot();
    assert.equal(state.events.length, 2);
    assert.equal(state.events[0].end.date, "2099-11-03");
    assert.match(timeLabel(state.events[1].start), /America\/New_York/);
    assert.equal(state.events[0].id, "opaque/event+one");
    assert.ok(state.events[0].fetched_at);
    assert.equal(
      new URLSearchParams(fixture.calendar.controls.calls.at(-1).query).get("calendar_id"),
      calendarId,
    );
  }));
test("T015 immutable preview changes create another proposal with new key and hash; draft never changes prior preview", () =>
  using(async ({ calendar, fixture }) => {
    await make(calendar, "最初");
    const first = calendar.snapshot().proposal;
    assert.throws(() => {
      first.normalized_request.summary = "改変";
    }, TypeError);
    calendar.edit("create");
    calendar.draft({ ...calendar.snapshot().draft, summary: "変更後" });
    await calendar.decide("confirm");
    assert.equal(writes(fixture).length, 1);
    await calendar.propose();
    assert.notEqual(calendar.snapshot().proposal.proposal_id, first.proposal_id);
    assert.notEqual(calendar.snapshot().proposal.args_hash, first.args_hash);
    assert.notEqual(writes(fixture)[0].key, writes(fixture)[1].key);
    assert.equal(first.normalized_request.summary, "最初");
  }));
test("T016 duplicate taps and response loss retry exact proposal/hash/key; 202 is pending operation", () =>
  using(async ({ calendar, fixture }) => {
    await make(calendar);
    fixture.calendar.controls.scenario = "lost";
    await Promise.all([calendar.decide("confirm"), calendar.decide("confirm")]);
    assert.equal(writes(fixture).length, 2);
    assert.equal(calendar.snapshot().pending, true);
    await calendar.retry();
    assert.deepEqual(writes(fixture)[1], writes(fixture)[2]);
    assert.deepEqual(Object.keys(writes(fixture)[1].body).sort(), ["args_hash", "confirmed"]);
    assert.equal(calendar.snapshot().operation.status, "pending");
    await calendar.decide("confirm");
    assert.equal(writes(fixture).length, 3);
    await calendar.refreshProposal();
    assert.equal(calendar.snapshot().proposal.status, "confirmed");
    assert.equal(fixture.calendar.operations.size, 1);
  }));
test("lost proposal creation retains exact body and key and cannot create a new draft until reconciled", () =>
  using(async ({ calendar, fixture }) => {
    calendar.edit("create");
    calendar.draft({ ...calendar.snapshot().draft, summary: "保存前" });
    fixture.calendar.controls.scenario = "lost";
    await calendar.propose();
    calendar.draft({ ...calendar.snapshot().draft, summary: "上書き不可" });
    assert.equal(calendar.snapshot().draft.summary, "保存前");
    await calendar.retry();
    assert.deepEqual(writes(fixture)[0], writes(fixture)[1]);
    assert.equal(fixture.calendar.proposals.size, 1);
  }));
test("T037 reject has no body and pending expiry server convergence does not delete fixed history", () =>
  using(async ({ calendar, fixture }) => {
    await make(calendar);
    await calendar.decide("reject");
    assert.equal(calendar.snapshot().proposal.status, "declined");
    assert.equal(writes(fixture).at(-1).body, undefined);
    await make(calendar);
    const value = calendar.snapshot().proposal;
    fixture.calendar.proposals.get(value.proposal_id).status = "expired";
    await calendar.refreshProposal();
    assert.equal(calendar.snapshot().proposal.status, "expired");
    assert.equal(
      calendar.snapshot().proposal.normalized_request.summary,
      value.normalized_request.summary,
    );
    const before = writes(fixture).length;
    await calendar.decide("confirm");
    assert.equal(writes(fixture).length, before);
  }));
test("T015 exact local expiry blocks first confirmation while accepted operation remains refreshable", () =>
  using(async ({ calendar, fixture }) => {
    await make(calendar);
    const original = Date.now;
    Date.now = () => Date.parse(calendar.snapshot().proposal.expires_at);
    try {
      await calendar.decide("confirm");
    } finally {
      Date.now = original;
    }
    assert.equal(writes(fixture).length, 1);
    await make(calendar);
    await calendar.decide("confirm");
    const op = calendar.snapshot().operation.operation_id;
    fixture.calendar.operations.get(op).status = "succeeded";
    await calendar.refreshProposal();
    assert.equal(calendar.snapshot().operation.status, "succeeded");
  }));
test("T016 result unknown and provider conflict are visible and unknown cannot silently re-create", () =>
  using(async ({ calendar, fixture }) => {
    await make(calendar);
    fixture.calendar.controls.scenario = "unknown";
    await calendar.decide("confirm");
    await calendar.refreshProposal();
    assert.equal(calendar.snapshot().operation.status, "outcome_unknown");
    calendar.edit("create");
    assert.equal(calendar.snapshot().draft, undefined);
    await calendar.decide("confirm");
    assert.equal(writes(fixture).length, 2);
  }));
test("update and delete use exact opaque target/provider ETag and update full content", () =>
  using(async ({ calendar, fixture }) => {
    calendar.edit("update", "opaque/event+one");
    calendar.draft({ ...calendar.snapshot().draft, summary: "変更した題名" });
    await calendar.propose();
    assert.equal(writes(fixture)[0].body.event_id, "opaque/event+one");
    assert.equal(writes(fixture)[0].body.provider_etag, '"provider-v1"');
    assert.deepEqual(writes(fixture)[0].body.end, { date: "2099-11-03" });
    await calendar.decide("reject");
    calendar.edit("delete", "opaque:event:two");
    await calendar.propose();
    assert.deepEqual(writes(fixture).at(-1).body, {
      action: "delete",
      calendar_id: calendarId,
      event_id: "opaque:event:two",
      provider_etag: '"provider-v2"',
    });
  }));
test("selection 412 preserves draft, shows current, requires explicit reconciliation and new If-Match", () =>
  using(async ({ calendar, fixture }) => {
    calendar.selectionDraft(["opaque+new@example.test"]);
    fixture.calendar.controls.scenario = "conflict";
    await calendar.saveSelection();
    assert.deepEqual(calendar.snapshot().selectionDraft, ["opaque+new@example.test"]);
    assert.deepEqual(calendar.snapshot().conflict.current.allowed_calendar_ids, [
      "other@example.test",
    ]);
    await calendar.saveSelection();
    assert.equal(writes(fixture).length, 1);
    calendar.acceptCurrentSelection();
    await calendar.saveSelection();
    assert.equal(writes(fixture).at(-1).match, '"2"');
  }));
test("selection empty is meaningful and disconnect lost response exact replay returns revoked cleanup pending", () =>
  using(async ({ calendar, fixture }) => {
    calendar.selectionDraft([]);
    await calendar.saveSelection();
    assert.deepEqual(writes(fixture)[0].body, { allowed_calendar_ids: [] });
    fixture.calendar.controls.scenario = "lost";
    await calendar.disconnect();
    await calendar.retry();
    assert.deepEqual(writes(fixture)[1], writes(fixture)[2]);
    assert.equal(calendar.snapshot().selected.status, "revoked");
    assert.equal(calendar.snapshot().selected.cleanup_status, "pending");
    assert.equal(calendar.snapshot().events.length, 0);
    fixture.calendar.controls.scenario = "cleanup";
    await calendar.refreshIntegration();
    assert.equal(calendar.snapshot().selected.cleanup_status, "complete");
  }));
test("Pi controller never loads private calendars, creates proposals, confirms or rejects", () =>
  using(
    async ({ calendar, fixture }) => {
      await calendar.authorize("read_write");
      await calendar.openProposal(crypto.randomUUID());
      await calendar.decide("confirm");
      await calendar.decide("reject");
      await calendar.disconnect();
      assert.equal(fixture.calendar.controls.calls.length, 0);
    },
    { surface: "display" },
  ));
test("OAuth sends exact read/body, matched return only refetches integrations, never confirms", () =>
  using(async ({ calendar, fixture, bridge }) => {
    await calendar.authorize("read");
    assert.deepEqual(writes(fixture)[0].body, { scope_profile: "read", return_target: "mobile" });
    const count = fixture.calendar.controls.calls.length;
    bridge.returnURL(`${bridge.target}?code=synthetic`);
    await tick();
    assert.equal(fixture.calendar.controls.calls.length, count);
    bridge.returnURL(bridge.target);
    await tick();
    assert.equal(calendar.snapshot().oauthWaiting, false);
    assert.equal(
      fixture.calendar.controls.calls.at(-1).path,
      `/v1/tenants/${tenantA}/integrations`,
    );
    assert.equal(writes(fixture).length, 1);
  }));
test("OAuth request loss keeps same key/body; browser cancel and launch/init failures preserve authenticated app", () =>
  using(async ({ calendar, fixture, session, bridge }) => {
    fixture.calendar.controls.scenario = "lost";
    await calendar.authorize("read_write");
    assert.equal(calendar.snapshot().pending, true);
    await calendar.retry();
    assert.deepEqual(writes(fixture)[0], writes(fixture)[1]);
    bridge.finish();
    await tick();
    assert.equal(calendar.snapshot().oauthWaiting, false);
    assert.equal(session.snapshot().context.uid, uid);
  }));
test("late native callback and old 403 cannot affect new tenant, privacy dispose empties body synchronously", () =>
  using(async ({ calendar, session, context, bridge, fixture }) => {
    await make(calendar);
    await calendar.authorize("read");
    await session.selectTenant(tenantB);
    const fresh = session.snapshot().context;
    assert.equal(context.signal.aborted, true);
    assert.equal(calendar.snapshot().proposal, undefined);
    assert.deepEqual(calendar.snapshot().events, []);
    const count = fixture.calendar.controls.calls.length;
    bridge.returnURL(bridge.target);
    await tick();
    assert.equal(fixture.calendar.controls.calls.length, count);
    assert.equal(session.snapshot().context, fresh);
    assert.equal(bridge.listenerCount(), 0);
  }));
test("bounded pagination continues empty page and publishes only after all pages, rejects duplicate/loop/overlimit", async () => {
  for (const mode of ["empty", "duplicate", "loop", "limit", "malformed"]) {
    let pages = 0;
    await using(
      async ({ calendar }) => {
        assert.equal(calendar.snapshot().events.length, mode === "empty" ? 2 : 0);
        if (mode !== "empty") assert.ok(calendar.snapshot().notice);
      },
      {
        alter: (fixture) => async (request) => {
          if (!request.url.includes("/calendar-events")) return fixture.transport(request);
          const value = await (await fixture.transport(request)).json();
          pages++;
          if (mode === "empty")
            return Response.json(pages === 1 ? { items: [], next_cursor: "next" } : value);
          if (mode === "malformed") value.items[1].calendar_id = "different";
          if (mode === "limit") return Response.json({ items: [], next_cursor: `page-${pages}` });
          if (mode === "loop") return Response.json({ items: [], next_cursor: "same" });
          return Response.json({
            ...value,
            next_cursor: mode === "duplicate" && pages === 1 ? "next" : null,
          });
        },
      },
    );
    if (mode === "limit") assert.equal(pages, 10);
  }
});
test("reject malformed integration scope/owner/version and weak ETag without exposing calendar events", async () => {
  for (const kind of ["owner", "tenant", "version", "weak"])
    await using(
      async ({ calendar }) => {
        assert.equal(calendar.snapshot().selected, undefined);
        assert.equal(calendar.snapshot().events.length, 0);
      },
      {
        alter: (fixture) => async (request) => {
          const response = await fixture.transport(request);
          if (!request.url.endsWith(integrationId)) return response;
          const value = await response.json();
          if (kind === "owner") value.owner_user_id = "other";
          if (kind === "tenant") value.tenant_id = tenantB;
          if (kind === "version") value.version = 0;
          return Response.json(value, { headers: { ETag: kind === "weak" ? 'W/"1"' : '"1"' } });
        },
      },
    );
});
test("strict dates, time kind, positive window, opaque ids, fixed hash reject malformed data", () => {
  const value = {
    action: "create",
    calendar_id: calendarId,
    summary: "架空",
    start: { date: "2026-02-28" },
    end: { date: "2026-03-01" },
  };
  assert.equal(validChange(value), true);
  assert.equal(validChange({ ...value, start: { date: "2026-02-30" } }), false);
  assert.equal(validChange({ ...value, end: value.start }), false);
  assert.equal(
    validChange({
      ...value,
      start: { date_time: "2026-11-01T01:30:00", timezone: "America/New_York" },
    }),
    false,
  );
  assert.equal(validChange({ ...value, recurrence: "RRULE" }), false);
  assert.equal(calendarIds([calendarId, calendarId]), false);
  assert.equal(calendarIds(Array(11).fill("x")), false);
  assert.throws(() =>
    event(
      {
        id: "x",
        calendar_id: calendarId,
        summary: "x",
        start: value.start,
        end: value.end,
        etag: "e",
        status: "cancelled",
      },
      calendarId,
    ),
  );
  assert.throws(() =>
    proposal({
      proposal_id: crypto.randomUUID(),
      integration_id: integrationId,
      action: "create",
      normalized_request: value,
      args_hash: "modified",
      expires_at: new Date().toISOString(),
      status: "pending",
    }),
  );
});
test("fixed HTTPS return rejects query, fragment, credentials, arbitrary origins and provider API callback cannot choose owner", () => {
  const target = "https://calendar.example.test/mobile";
  assert.equal(fixedReturnURL(target), target);
  for (const v of [
    "http://calendar.example.test/mobile",
    `${target}?code=x`,
    `${target}#state=x`,
    "https://user@calendar.example.test/mobile",
    "javascript:alert(1)",
    "https://localhost/mobile",
  ])
    assert.equal(fixedReturnURL(v), undefined);
  assert.equal(
    authorizationURL("https://accounts.google.com/o/oauth2/v2/auth?state=synthetic"),
    true,
  );
  assert.equal(authorizationURL("https://accounts.google.com.evil.test/o/oauth2/v2/auth"), false);
});
test("native lease disposes late listeners, ignores cold launch, serializes stale close before newer launch", async () => {
  const bridge = syntheticBrowser();
  let returns = 0,
    release;
  bridge.app.getLaunchUrl = () =>
    new Promise((resolve) => {
      release = resolve;
    });
  const oauth = nativeCalendarOAuth(bridge.browser, bridge.app, bridge.target);
  const first = oauth.open(
    () => returns++,
    () => {},
  );
  await first.launch("https://accounts.google.com/o/oauth2/v2/auth");
  first.dispose();
  const second = oauth.open(
    () => returns++,
    () => {},
  );
  await second.launch("https://accounts.google.com/o/oauth2/v2/auth");
  release({ url: bridge.target });
  await tick();
  assert.equal(returns, 0);
  bridge.returnURL(bridge.target);
  bridge.returnURL(bridge.target);
  assert.equal(returns, 1);
  assert.deepEqual(bridge.calls, ["open", "close", "open"]);
  second.dispose();
  await tick();
  assert.equal(bridge.listenerCount(), 0);
});
test("native initialization rejection and browser failure remain local, without unhandled listener promises", async () => {
  for (const mode of ["init", "launch"]) {
    const bridge = syntheticBrowser();
    if (mode === "init")
      bridge.app.addListener = async () => {
        throw Error("synthetic");
      };
    else
      bridge.browser.open = async () => {
        throw Error("synthetic");
      };
    await using(
      async ({ calendar, session }) => {
        await calendar.authorize("read");
        assert.equal(calendar.snapshot().pending, true);
        assert.equal(calendar.snapshot().oauthWaiting, false);
        assert.equal(session.snapshot().context.uid, uid);
      },
      { bridge },
    );
    await tick();
    assert.equal(bridge.listenerCount(), 0);
  }
});
test("late request after tenant switch and bfcache suspension cannot republish old private proposal", () =>
  using(async ({ calendar, fixture, session }) => {
    await make(calendar);
    session.suspend();
    assert.equal(calendar.snapshot().closed, true);
    assert.equal(calendar.snapshot().proposal, undefined);
    await session.resume();
    assert.equal(session.snapshot().phase, "signed-out");
    assert.ok(fixture.calendar.controls.calls.length);
  }));

test("T037 conversation waiting ID opens current canonical proposal; run cancellation prevents stale confirmation", () =>
  using(async ({ calendar, fixture, context }) => {
    const chat = new ChatController(context, "mobile");
    fixture.chat.controls.scenario = "waiting";
    fixture.chat.controls.delay = 1;
    await chat.load();
    await chat.select(fixturePrivate);
    chat.draft("架空のカレンダー依頼");
    await chat.send();
    await tick();
    await chat.refresh();
    const id = chat.snapshot().run.waiting_proposal_id;
    assert.ok(id);
    await calendar.openProposal(id);
    assert.equal(calendar.snapshot().proposal.normalized_request.summary, "会話からの架空の予定");
    fixture.calendar.proposals.get(id).status = "cancelled";
    await calendar.decide("confirm");
    assert.equal(calendar.snapshot().proposal.status, "cancelled");
    assert.equal(fixture.calendar.operations.size, 0);
    chat.dispose();
  }));

test("late old-tenant 403 response is never parsed and cannot clear newer tenant or expose its old proposal", async () => {
  let resolve,
    captured = false,
    armed = false,
    reads = 0;
  await using(
    async ({ calendar, session, fixture }) => {
      const value = fixture.calendar.seedProposal(tenantA, uid, crypto.randomUUID());
      armed = true;
      const pending = calendar.openProposal(value.proposal_id);
      while (!captured) await tick();
      await session.selectTenant(tenantB);
      const fresh = session.snapshot().context;
      const response = Response.json({}, { status: 403 });
      response.json = async () => {
        reads++;
        return {};
      };
      resolve(response);
      await pending;
      assert.equal(reads, 0);
      assert.equal(session.snapshot().context, fresh);
      assert.equal(calendar.snapshot().proposal, undefined);
    },
    {
      alter: (fixture) => (request) =>
        armed && request.url.includes("/calendar-proposals/")
          ? new Promise((done) => {
              resolve = done;
              captured = true;
            })
          : fixture.transport(request),
    },
  );
});

test("refetch refuses changed fixed proposal content/hash and provider conflict is terminal without silent retries", () =>
  using(async ({ calendar, fixture }) => {
    await make(calendar);
    const original = calendar.snapshot().proposal;
    fixture.calendar.proposals.get(original.proposal_id).normalized_request.summary = "tampered";
    await calendar.refreshProposal();
    assert.equal(
      calendar.snapshot().proposal.normalized_request.summary,
      original.normalized_request.summary,
    );
    assert.ok(calendar.snapshot().notice);
    fixture.calendar.proposals.get(original.proposal_id).normalized_request.summary =
      original.normalized_request.summary;
    fixture.calendar.controls.scenario = "provider_conflict";
    await calendar.decide("confirm");
    await calendar.refreshProposal();
    assert.equal(calendar.snapshot().operation.status, "failed");
    assert.equal(calendar.snapshot().operation.error_code, "conflict");
    const count = writes(fixture).length;
    await calendar.decide("confirm");
    assert.equal(writes(fixture).length, count);
  }));

test("late listener handles are individually removed after early dispose without affecting a newer flow", async () => {
  const bridge = syntheticBrowser();
  const original = bridge.app.addListener;
  let resolve;
  bridge.app.addListener = async (...args) => {
    const handle = await original(...args);
    return new Promise((done) => {
      resolve = () => done(handle);
    });
  };
  const oauth = nativeCalendarOAuth(bridge.browser, bridge.app, bridge.target);
  const lease = oauth.open(
    () => assert.fail("stale return"),
    () => {},
  );
  await tick();
  lease.dispose();
  resolve();
  await tick();
  assert.equal(bridge.listenerCount(), 0);
});

test("R1 authority change clears fixed preview, accepted operation, drafts and every mounted conversation card", async () => {
  for (const reason of ["disconnect", "empty", "version", "reauth", "missing"])
    await using(async ({ calendar, fixture, context }) => {
      await make(calendar);
      const id = calendar.snapshot().proposal.proposal_id,
        chatCard = new CalendarController(context, "mobile");
      await chatCard.openProposal(id);
      if (reason === "disconnect") {
        await calendar.decide("confirm");
        await chatCard.refreshProposal();
        assert.ok(chatCard.snapshot().operation);
      } else {
        calendar.edit("update", "opaque/event+one");
        assert.ok(calendar.snapshot().draft);
      }
      if (reason === "disconnect") await calendar.disconnect();
      if (reason === "empty") {
        calendar.selectionDraft([]);
        await calendar.saveSelection();
      }
      if (reason === "version") {
        fixture.calendar.record(tenantA, uid).version++;
        await calendar.refreshIntegration();
      }
      if (reason === "reauth") {
        fixture.calendar.controls.scenario = "reauth";
        await calendar.refreshIntegration();
      }
      if (reason === "missing") {
        fixture.calendar.controls.scenario = "empty";
        await calendar.load();
      }
      for (const c of [calendar, chatCard]) {
        assert.equal(c.snapshot().proposal, undefined, reason);
        assert.equal(c.snapshot().operation, undefined, reason);
        assert.equal(c.snapshot().draft, undefined, reason);
        assert.deepEqual(c.snapshot().events, [], reason);
        const before = writes(fixture).length;
        await c.decide("confirm");
        await c.decide("reject");
        await c.retry();
        assert.equal(writes(fixture).length, before);
      }
      chatCard.dispose();
    });
});

test("R1 loss of authority fences a late private proposal response and cannot re-open from stale transport", async () => {
  let armed = false,
    release,
    captured = false;
  await using(
    async ({ calendar, context }) => {
      await make(calendar);
      const id = calendar.snapshot().proposal.proposal_id;
      const chat = new CalendarController(context, "mobile");
      armed = true;
      const loading = chat.openProposal(id);
      while (!captured) await tick();
      await calendar.disconnect();
      release();
      await loading;
      assert.equal(chat.snapshot().proposal, undefined);
      assert.equal(chat.snapshot().busy, false);
      chat.dispose();
    },
    {
      alter: (fixture) => async (request) => {
        const result = await fixture.transport(request);
        if (armed && request.url.includes("/calendar-proposals/") && request.method === "GET") {
          captured = true;
          await new Promise((done) => {
            release = done;
          });
        }
        return result;
      },
    },
  );
});

test("R1 late accepted confirm cannot restore private operation after another card disconnects", async () => {
  let armed = false,
    release,
    captured = false;
  await using(
    async ({ calendar, fixture, context }) => {
      await make(calendar);
      const other = new CalendarController(context, "mobile");
      await other.load();
      await other.select(integrationId);
      armed = true;
      const confirming = calendar.decide("confirm");
      while (!captured) await tick();
      await other.disconnect();
      assert.equal(calendar.snapshot().proposal, undefined);
      assert.equal(calendar.snapshot().pending, false);
      release();
      await confirming;
      assert.equal(calendar.snapshot().operation, undefined);
      assert.equal(calendar.snapshot().busy, false);
      assert.equal(fixture.calendar.operations.size, 1);
      other.dispose();
    },
    {
      alter: (fixture) => async (request) => {
        const response = await fixture.transport(request);
        if (armed && request.url.endsWith(":confirm")) {
          captured = true;
          await new Promise((done) => {
            release = done;
          });
        }
        return response;
      },
    },
  );
});

test("R1 late event page after selection change never republishes events or changes a later request", async () => {
  let armed = false,
    release,
    captured = false;
  await using(
    async ({ calendar, context }) => {
      const other = new CalendarController(context, "mobile");
      await other.load();
      await other.select(integrationId);
      armed = true;
      const loading = calendar.events(calendarId, range);
      while (!captured) await tick();
      other.selectionDraft([]);
      await other.saveSelection();
      assert.deepEqual(calendar.snapshot().events, []);
      await calendar.refreshIntegration();
      release();
      await loading;
      assert.deepEqual(calendar.snapshot().events, []);
      assert.deepEqual(calendar.snapshot().selected.allowed_calendar_ids, []);
      assert.equal(calendar.snapshot().busy, false);
      other.dispose();
    },
    {
      alter: (fixture) => async (request) => {
        const response = await fixture.transport(request);
        if (armed && request.url.includes("/calendar-events")) {
          captured = true;
          await new Promise((done) => {
            release = done;
          });
        }
        return response;
      },
    },
  );
});

test("R1 unknown mutation keeps exact retry, unrelated integration/current identity stay available", () =>
  using(async ({ calendar, context, fixture, session }) => {
    await make(calendar);
    const fixed = calendar.snapshot().proposal;
    fixture.calendar.controls.scenario = "lost";
    await calendar.decide("confirm");
    assert.equal(calendar.snapshot().pending, true);
    const unrelated = {
      ...fixture.calendar.record(tenantA, uid),
      id: crypto.randomUUID(),
      status: "revoked",
      allowed_calendar_ids: [],
    };
    publishCalendarAuthority(context, unrelated.id, unrelated);
    assert.equal(calendar.snapshot().proposal.proposal_id, fixed.proposal_id);
    assert.equal(calendar.snapshot().pending, true);
    assert.equal(session.snapshot().context, context);
    await calendar.retry();
    assert.deepEqual(writes(fixture).at(-1), writes(fixture).at(-2));
    assert.ok(calendar.snapshot().operation);
  }));

test("R1 old complete list cannot erase a newer canonical observation through an absent row", async () => {
  let armed = false,
    captured = false,
    release;
  await using(
    async ({ calendar, context, fixture }) => {
      await make(calendar);
      const other = new CalendarController(context, "mobile");
      armed = true;
      const listing = other.load();
      while (!captured) await tick();
      fixture.calendar.record(tenantA, uid).version++;
      await calendar.refreshIntegration();
      await calendar.events(calendarId, range);
      await make(calendar, "新しい観測の予定");
      release();
      await listing;
      assert.equal(calendar.snapshot().selected.status, "active");
      assert.equal(calendar.snapshot().proposal.normalized_request.summary, "新しい観測の予定");
      assert.equal(other.snapshot().busy, false);
      assert.deepEqual(other.snapshot().integrations, []);
      other.dispose();
    },
    {
      alter: (fixture) => async (request) => {
        if (
          armed &&
          request.method === "GET" &&
          new URL(request.url).pathname.endsWith("/integrations")
        ) {
          captured = true;
          await new Promise((done) => {
            release = done;
          });
          return Response.json({ items: [], next_cursor: null });
        }
        return fixture.transport(request);
      },
    },
  );
});

test("R1 a fresh authorized list can recover after absence without restoring the previous private card", () =>
  using(async ({ calendar, fixture }) => {
    await make(calendar);
    fixture.calendar.controls.scenario = "empty";
    await calendar.load();
    assert.equal(calendar.snapshot().proposal, undefined);
    fixture.calendar.controls.scenario = "normal";
    await calendar.load();
    assert.equal(calendar.snapshot().integrations.length, 1);
    assert.equal(calendar.snapshot().proposal, undefined);
    await calendar.select(integrationId);
    await calendar.events(calendarId, range);
    assert.equal(calendar.snapshot().events.length, 2);
  }));

test("R1 revoked in-flight response is cancelled before a late 403 can clear the current authenticated session", async () => {
  let armed = false,
    captured = false,
    release,
    reads = 0;
  await using(
    async ({ calendar, context, session }) => {
      await make(calendar);
      const other = new CalendarController(context, "mobile");
      await other.load();
      await other.select(integrationId);
      armed = true;
      const loading = calendar.refreshProposal();
      while (!captured) await tick();
      await other.disconnect();
      await loading;
      release();
      await tick();
      assert.equal(session.snapshot().context, context);
      assert.equal(reads, 0);
      assert.equal(calendar.snapshot().proposal, undefined);
      other.dispose();
    },
    {
      alter: (fixture) => async (request) => {
        if (armed && request.method === "GET" && request.url.includes("/calendar-proposals/")) {
          captured = true;
          await new Promise((done) => {
            release = done;
          });
          const response = Response.json({}, { status: 403 });
          response.json = async () => {
            reads++;
            return {};
          };
          return response;
        }
        return fixture.transport(request);
      },
    },
  );
});

test("R1 first canonical metadata arriving after a conversation proposal clears denied scope and in-flight confirmation", async () => {
  let captured = false,
    release;
  await using(
    async ({ calendar, fixture, context, session }) => {
      const value = fixture.calendar.seedProposal(tenantA, uid, crypto.randomUUID());
      const chat = new CalendarController(context, "mobile");
      await chat.openProposal(value.proposal_id);
      const confirming = chat.decide("confirm");
      while (!captured) await tick();
      fixture.calendar.record(tenantA, uid).allowed_calendar_ids = [];
      await calendar.load();
      await confirming;
      assert.equal(chat.snapshot().proposal, undefined);
      assert.equal(chat.snapshot().pending, false);
      assert.equal(chat.snapshot().busy, false);
      release();
      await tick();
      assert.equal(chat.snapshot().operation, undefined);
      assert.equal(session.snapshot().context, context);
      chat.dispose();
    },
    {
      initialize: false,
      alter: (fixture) => async (request) => {
        const response = await fixture.transport(request);
        if (request.url.endsWith(":confirm")) {
          captured = true;
          await new Promise((done) => {
            release = done;
          });
        }
        return response;
      },
    },
  );
});

test("R1 absent rows do not forget an observed revoked version and permit stale active resurrection", () =>
  using(async ({ calendar, fixture }) => {
    await calendar.disconnect();
    const revoked = fixture.calendar.record(tenantA, uid).version;
    fixture.calendar.controls.scenario = "empty";
    await calendar.load();
    fixture.calendar.controls.scenario = "normal";
    fixture.calendar.record(tenantA, uid).status = "active";
    await calendar.load();
    assert.deepEqual(calendar.snapshot().integrations, []);
    assert.equal(fixture.calendar.record(tenantA, uid).version, revoked);
  }));

test("R2 complete absence erases proposal-only cards, accepted operations and uncertain retries without metadata", async () => {
  for (const phase of ["displayed", "accepted", "uncertain"])
    await using(
      async ({ calendar, fixture, context, session }) => {
        const fixed = fixture.calendar.seedProposal(tenantA, uid, crypto.randomUUID()),
          chat = new CalendarController(context, "mobile");
        await chat.openProposal(fixed.proposal_id);
        if (phase === "uncertain") fixture.calendar.controls.scenario = "lost";
        if (phase !== "displayed") await chat.decide("confirm");
        if (phase === "accepted") assert.ok(chat.snapshot().operation);
        if (phase === "uncertain") assert.equal(chat.snapshot().pending, true);
        fixture.calendar.controls.scenario = "empty";
        await calendar.load();
        assert.equal(chat.snapshot().proposal, undefined);
        assert.equal(chat.snapshot().operation, undefined);
        assert.equal(chat.snapshot().pending, false);
        assert.equal(chat.snapshot().busy, false);
        const count = writes(fixture).length;
        await chat.retry();
        await chat.decide("confirm");
        assert.equal(writes(fixture).length, count);
        assert.equal(session.snapshot().context, context);
        chat.dispose();
      },
      { initialize: false },
    );
});

test("R2 first empty collection aborts unknown late proposal success and denial before any private body is parsed", async () => {
  for (const status of [200, 403]) {
    let captured = false,
      release,
      reads = 0;
    await using(
      async ({ calendar, fixture, context, session }) => {
        const fixed = fixture.calendar.seedProposal(tenantA, uid, crypto.randomUUID()),
          chat = new CalendarController(context, "mobile");
        const opening = chat.openProposal(fixed.proposal_id);
        while (!captured) await tick();
        fixture.calendar.controls.scenario = "empty";
        await calendar.load();
        await opening;
        assert.equal(chat.snapshot().busy, false);
        release();
        await tick();
        assert.equal(chat.snapshot().proposal, undefined);
        assert.equal(reads, 0);
        assert.equal(session.snapshot().context, context);
        chat.dispose();
      },
      {
        initialize: false,
        alter: (fixture) => async (request) => {
          const response = await fixture.transport(request);
          if (request.method === "GET" && request.url.includes("/calendar-proposals/")) {
            const result = status === 200 ? response : Response.json({}, { status });
            const json = result.json.bind(result);
            result.json = async () => {
              reads++;
              return json();
            };
            captured = true;
            await new Promise((done) => {
              release = done;
            });
            return result;
          }
          return response;
        },
      },
    );
  }
});

test("R2 complete absence cancels an in-flight confirmation even without metadata and cannot restore accepted state", async () => {
  let captured = false,
    release;
  await using(
    async ({ calendar, fixture, context }) => {
      const fixed = fixture.calendar.seedProposal(tenantA, uid, crypto.randomUUID()),
        chat = new CalendarController(context, "mobile");
      await chat.openProposal(fixed.proposal_id);
      const confirming = chat.decide("confirm");
      while (!captured) await tick();
      fixture.calendar.controls.scenario = "empty";
      await calendar.load();
      await confirming;
      release();
      await tick();
      assert.equal(chat.snapshot().proposal, undefined);
      assert.equal(chat.snapshot().operation, undefined);
      assert.equal(chat.snapshot().pending, false);
      assert.equal(fixture.calendar.operations.size, 1);
      chat.dispose();
    },
    {
      initialize: false,
      alter: (fixture) => async (request) => {
        const response = await fixture.transport(request);
        if (request.url.endsWith(":confirm")) {
          captured = true;
          await new Promise((done) => {
            release = done;
          });
        }
        return response;
      },
    },
  );
});

test("R2 older list absence cannot erase a later authenticated proposal or acceptance observation", async () => {
  for (const action of ["open", "confirm"]) {
    let armed = false,
      captured = false,
      release;
    await using(
      async ({ calendar, fixture, context }) => {
        const fixed = fixture.calendar.seedProposal(tenantA, uid, crypto.randomUUID()),
          chat = new CalendarController(context, "mobile");
        if (action === "confirm") await chat.openProposal(fixed.proposal_id);
        armed = true;
        const listing = calendar.load();
        while (!captured) await tick();
        if (action === "open") await chat.openProposal(fixed.proposal_id);
        else await chat.decide("confirm");
        release();
        await listing;
        assert.equal(chat.snapshot().proposal.proposal_id, fixed.proposal_id);
        if (action === "confirm") assert.ok(chat.snapshot().operation);
        assert.equal(calendar.snapshot().busy, false);
        chat.dispose();
      },
      {
        initialize: false,
        alter: (fixture) => async (request) => {
          if (
            armed &&
            request.method === "GET" &&
            new URL(request.url).pathname.endsWith("/integrations")
          ) {
            captured = true;
            await new Promise((done) => {
              release = done;
            });
            return Response.json({ items: [], next_cursor: null });
          }
          return fixture.transport(request);
        },
      },
    );
  }
});

test("R2 complete list only clears absent proposal dependencies and preserves an included integration's exact unknown retry", () =>
  using(
    async ({ calendar, fixture, context, session }) => {
      const present = fixture.calendar.seedProposal(tenantA, uid, crypto.randomUUID()),
        missing = fixture.calendar.seedProposal(tenantA, uid, crypto.randomUUID());
      missing.integration_id = crypto.randomUUID();
      const keep = new CalendarController(context, "mobile"),
        clear = new CalendarController(context, "mobile");
      await keep.openProposal(present.proposal_id);
      await clear.openProposal(missing.proposal_id);
      fixture.calendar.controls.scenario = "lost";
      await keep.decide("confirm");
      assert.equal(keep.snapshot().pending, true);
      await calendar.load();
      assert.equal(clear.snapshot().proposal, undefined);
      assert.equal(keep.snapshot().proposal.proposal_id, present.proposal_id);
      assert.equal(keep.snapshot().pending, true);
      assert.equal(session.snapshot().context, context);
      await keep.retry();
      assert.deepEqual(writes(fixture).at(-1), writes(fixture).at(-2));
      assert.ok(keep.snapshot().operation);
      keep.dispose();
      clear.dispose();
    },
    { initialize: false },
  ));
