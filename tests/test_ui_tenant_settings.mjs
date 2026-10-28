import assert from "node:assert/strict";
import { test } from "node:test";
import { SessionController } from "../packages/ui/src/session.ts";
import { createSessionFixture, tenantA, tenantB } from "../packages/ui/src/session-fixture.ts";
import {
  TenantSettingsController,
  InvitationAcceptanceController,
} from "../packages/ui/src/tenant-settings/controller.ts";
import { createNativeAuthentication } from "../apps/mobile/src/firebase-native.ts";
const actor = "synthetic@example.test";
const tick = () => new Promise((resolve) => setImmediate(resolve));
const deferred = () => {
  let resolve;
  const promise = new Promise((r) => {
    resolve = r;
  });
  return { promise, resolve };
};
const writes = (fixture) => fixture.settings.controls.calls.filter((v) => v.method !== "GET");
async function using(work, role = "owner", alter = (f) => f.transport) {
  const fixture = createSessionFixture();
  fixture.devices.controls.role = role;
  const session = new SessionController({
    auth: fixture.auth,
    transport: alter(fixture),
    baseUrl: "https://session.invalid",
  });
  const stop = session.start();
  await tick();
  await session.signIn(actor, "synthetic");
  await session.selectTenant(tenantA);
  const settings = new TenantSettingsController(session),
    accept = new InvitationAcceptanceController(session);
  try {
    await settings.load();
    await work({ fixture, session, settings, accept });
  } finally {
    settings.dispose();
    accept.dispose();
    stop();
  }
}
test("member gets own aggregate without requesting membership list or allowing any management", () =>
  using(async ({ settings, fixture }) => {
    assert.equal(settings.snapshot().usage.turns, 2);
    assert.equal(settings.snapshot().members.length, 0);
    assert.equal(
      fixture.settings.controls.calls.some((v) => v.path.endsWith("memberships")),
      false,
    );
    await settings.prepare("invite", undefined, "admin");
    await settings.confirm();
    assert.equal(writes(fixture).length, 0);
  }, "member"));
test("admin cannot self promote, modify an owner or transfer owner, including direct controller calls", () =>
  using(async ({ settings, fixture }) => {
    fixture.settings.controls.members.set("owner", {
      user_id: "owner",
      role: "owner",
      status: "active",
      version: 2,
    });
    for (const [kind, uid, role] of [
      ["role", actor, "owner"],
      ["role", "owner", "member"],
      ["transfer", "u?/#+"],
      ["role", "u?/#+", "owner"],
    ]) {
      await settings.prepare(kind, uid, role);
      await settings.confirm();
    }
    assert.equal(writes(fixture).length, 0);
  }, "admin"));
test("last owner self demotion is not offered, while transfer remains available", () =>
  using(async ({ settings }) => {
    const own = settings.snapshot().members.find((v) => v.user_id === actor);
    assert.equal(settings.canChange(own, "member"), false);
    await settings.prepare("transfer", "u?/#+");
    assert.equal(settings.snapshot().review.kind, "transfer");
  }));
for (const uid of [".", "..", "opaque / uid", "%2E", "u?/#+"])
  test(`opaque UID ${uid} is unchanged in query and target If-Match, with explicit confirmation`, () =>
    using(async ({ settings, fixture }) => {
      fixture.settings.controls.members.set(uid, {
        user_id: uid,
        role: "member",
        status: "active",
        version: 13,
      });
      await settings.prepare("role", uid, "admin");
      assert.equal(writes(fixture).length, 0);
      assert.throws(() => {
        settings.snapshot().review.target.user_id = "other";
      });
      await Promise.all([settings.confirm(), settings.confirm()]);
      const [call] = writes(fixture);
      assert.equal(writes(fixture).length, 1);
      assert.equal(call.uid, uid);
      assert.equal(call.match, '"13"');
      assert.deepEqual(call.body, { role: "admin" });
      assert.equal(fixture.settings.controls.members.get(uid).role, "admin");
    }));
test("transfer uses actor If-Match plus target_version; loss preserves exact recovery after actor becomes member", () =>
  using(async ({ settings, fixture, session }) => {
    await settings.prepare("transfer", "u?/#+");
    fixture.settings.controls.scenario = "lost";
    const old = session.snapshot().context;
    old.keep("private", "must erase");
    await settings.confirm();
    assert.equal(old.signal.aborted, true);
    assert.equal(session.snapshot().tenant.my_role, "member");
    assert.equal(settings.snapshot().members.length, 0);
    assert.equal(settings.snapshot().pending, true);
    await settings.load();
    await settings.retry();
    const calls = writes(fixture);
    assert.equal(calls.length, 2);
    assert.deepEqual(calls[1], calls[0]);
    assert.equal(calls[0].match, '"7"');
    assert.deepEqual(calls[0].body, { target_user_id: "u?/#+", target_version: 3 });
    assert.equal(settings.snapshot().pending, false);
    assert.equal(settings.snapshot().usage.turns, 2);
  }));
test("self demotion response loss keeps receipt through refresh and erases manager cache", () =>
  using(async ({ settings, fixture }) => {
    fixture.settings.controls.members.set("another-owner", {
      user_id: "another-owner",
      role: "owner",
      status: "active",
      version: 2,
    });
    await settings.prepare("role", actor, "member");
    fixture.settings.controls.scenario = "lost";
    await settings.confirm();
    assert.equal(settings.snapshot().pending, true);
    assert.equal(settings.snapshot().members.length, 0);
    await settings.retry();
    assert.deepEqual(writes(fixture)[0], writes(fixture)[1]);
    assert.equal(settings.snapshot().pending, false);
  }));
test("recent-auth refusal retains exact revoke request; real reauth completes before retry", () =>
  using(async ({ settings, fixture, session }) => {
    await settings.prepare("revoke", "u?/#+");
    fixture.settings.controls.scenario = "recent";
    await settings.confirm();
    assert.equal(settings.snapshot().reauthentication, true);
    assert.equal(settings.snapshot().pending, true);
    const before = session.snapshot().context;
    await settings.reauthenticate(actor, "synthetic-password");
    assert.equal(before.signal.aborted, true);
    assert.equal(settings.snapshot().reauthentication, false);
    await settings.retry();
    assert.deepEqual(writes(fixture)[0], writes(fixture)[1]);
    assert.equal(fixture.settings.controls.members.get("u?/#+").status, "revoked");
  }));
test("an edited version between review and confirmation produces comparison and no write", () =>
  using(async ({ settings, fixture }) => {
    await settings.prepare("role", "u?/#+", "admin");
    const target = fixture.settings.controls.members.get("u?/#+");
    fixture.settings.controls.members.set(target.user_id, { ...target, version: 12 });
    await settings.confirm();
    assert.equal(writes(fixture).length, 0);
    assert.equal(settings.snapshot().conflict.version, 12);
  }));
test("server 412 refreshes the list and requires a new explicit review", () =>
  using(async ({ settings, fixture }) => {
    await settings.prepare("role", "u?/#+", "admin");
    fixture.settings.controls.scenario = "conflict";
    await settings.confirm();
    assert.equal(settings.snapshot().pending, false);
    assert.equal(settings.snapshot().review, undefined);
    assert.match(settings.snapshot().notice, /競合/);
  }));
test("409 reservation conflict never reports success or invents a replacement key", () =>
  using(
    async ({ settings, fixture }) => {
      await settings.prepare("role", "u?/#+", "admin");
      await settings.confirm();
      assert.equal(settings.snapshot().pending, true);
      await settings.retry();
      assert.deepEqual(writes(fixture)[0], writes(fixture)[1]);
      assert.match(settings.snapshot().notice, /競合/);
    },
    "owner",
    (f) => async (req) => {
      if (req.method === "PATCH") {
        await f.transport(req);
        return Response.json({}, { status: 409 });
      }
      return f.transport(req);
    },
  ));
test("role refresh clears invitation codes, privileged lists, and unrelated feature cache", () =>
  using(async ({ settings, fixture, session }) => {
    await settings.prepare("invite", undefined, "member");
    await settings.confirm();
    assert.equal(settings.snapshot().invitation.code.length, 64);
    const old = session.snapshot().context;
    old.keep("private", "discard");
    fixture.devices.controls.role = "member";
    await settings.load();
    assert.equal(old.signal.aborted, true);
    assert.equal(settings.snapshot().invitation, undefined);
    assert.equal(settings.snapshot().members.length, 0);
    assert.equal(settings.snapshot().usage.turns, 2);
  }));
for (const action of ["tenant", "user", "logout", "pagehide"])
  test(`${action} change destroys bound recovery and rejects late response`, () =>
    using(async ({ settings, fixture, session }) => {
      await settings.prepare("transfer", "u?/#+");
      fixture.settings.controls.scenario = "lost";
      await settings.confirm();
      assert.equal(settings.snapshot().pending, true);
      if (action === "tenant") await session.selectTenant(tenantB);
      if (action === "user") fixture.emit({ uid: "another@example.test" });
      if (action === "logout") await session.signOut();
      if (action === "pagehide") session.suspend();
      await tick();
      await settings.retry();
      assert.equal(writes(fixture).length, 1);
      assert.equal(settings.snapshot().pending, false);
    }));
test("member/role check immediately before confirmation prevents stale privilege write", () =>
  using(async ({ settings, fixture }) => {
    await settings.prepare("invite", undefined, "admin");
    fixture.devices.controls.role = "member";
    await settings.confirm();
    assert.equal(writes(fixture).length, 0);
    assert.equal(settings.snapshot().members.length, 0);
  }));
test("acceptance without selected tenant sends only code and fixed key; 429 after loss retains exact attempt", () =>
  using(async ({ accept, fixture, session }) => {
    await session.selectTenant(undefined);
    fixture.settings.controls.scenario = "lost";
    await accept.accept("a".repeat(64));
    fixture.settings.controls.scenario = "rate";
    await accept.accept();
    assert.equal(accept.snapshot().pending, true);
    assert.match(accept.snapshot().notice, /失敗したとは限りません/);
    fixture.settings.controls.scenario = "normal";
    await accept.accept();
    const calls = writes(fixture);
    assert.equal(calls.length, 3);
    assert.deepEqual(calls[1], calls[0]);
    assert.deepEqual(calls[2], calls[0]);
    assert.deepEqual(Object.keys(calls[0].body), ["code"]);
    assert.equal(accept.snapshot().pending, false);
    assert.equal(session.selectedTenantId(), undefined);
  }));
test("acceptance discards code and pending request on user loss", () =>
  using(async ({ accept, fixture, session }) => {
    fixture.settings.controls.scenario = "lost";
    await accept.accept("a".repeat(64));
    await session.signOut();
    await accept.accept();
    assert.equal(writes(fixture).length, 1);
    assert.equal(accept.snapshot().pending, false);
  }));
for (const quota of ["available", "limited", "exhausted"])
  test(`usage ${quota} is the server aggregate with no selector`, () =>
    using(async ({ settings, fixture }) => {
      fixture.settings.controls.quota = quota;
      await settings.load();
      assert.equal(settings.snapshot().usage.quota_state, quota);
      assert.ok(
        fixture.settings.controls.calls
          .filter((v) => v.path.endsWith("usage"))
          .every((v) => v.uid === null),
      );
    }));
test("logout queues SDK signout after in-flight same-user reauth and cannot restore login", () =>
  using(async ({ settings, fixture, session }) => {
    const gate = deferred(),
      started = deferred(),
      events = [];
    fixture.auth.reauthenticate = async () => {
      events.push("reauth-start");
      started.resolve();
      await gate.promise;
      fixture.emit({ uid: actor });
      events.push("reauth-end");
    };
    const oldSignOut = fixture.auth.signOut;
    fixture.auth.signOut = async () => {
      events.push("signout");
      await oldSignOut();
    };
    const operation = session.reauthenticate(actor, "synthetic");
    await started.promise;
    const closing = session.signOut();
    gate.resolve();
    assert.equal(await operation, false);
    await closing;
    assert.deepEqual(events, ["reauth-start", "reauth-end", "signout"]);
    assert.equal(await fixture.auth.current(), null);
    assert.equal(session.globalContext(), undefined);
    assert.equal(settings.snapshot().members.length, 0);
  }));
test("tenant switch during reauth never reselects original tenant", () =>
  using(async ({ fixture, session }) => {
    const gate = deferred(),
      start = deferred();
    fixture.auth.reauthenticate = async () => {
      start.resolve();
      await gate.promise;
    };
    const request = session.reauthenticate(actor, "synthetic");
    await start.promise;
    await session.selectTenant(tenantB);
    gate.resolve();
    assert.equal(await request, false);
    assert.equal(session.snapshot().tenant.id, tenantB);
  }));
function nativePlugin() {
  let user = { uid: "native-owner", email: "owner@example.test" },
    listener;
  const calls = [];
  const plugin = {
    getCurrentUser: async () => ({ user }),
    getTenantId: async () => ({ tenantId: null }),
    addListener: async (_, cb) => {
      listener = cb;
      return { remove: async () => {} };
    },
    signInWithEmailAndPassword: async () => {
      calls.push("signin");
      listener?.({ user });
      return { user };
    },
    signOut: async () => {
      calls.push("signout");
      user = null;
      listener?.({ user });
    },
    getIdToken: async () => ({ token: "synthetic" }),
  };
  return {
    plugin,
    calls,
    emit(value) {
      user = value;
      listener?.({ user });
    },
  };
}
test("native reauthentication checks email before credential sign-in and verifies same UID", async () => {
  const f = nativePlugin(),
    auth = createNativeAuthentication(f.plugin),
    stop = auth.subscribe(() => {});
  await tick();
  await assert.rejects(auth.reauthenticate("native-owner", "other@example.test", "synthetic"));
  assert.deepEqual(f.calls, []);
  await auth.reauthenticate("native-owner", "owner@example.test", "synthetic");
  assert.deepEqual(f.calls, ["signin"]);
  stop();
});
test("native external signout and same UID return invalidate in-flight reauthentication", async () => {
  const f = nativePlugin(),
    auth = createNativeAuthentication(f.plugin),
    stop = auth.subscribe(() => {});
  await tick();
  const gate = deferred(),
    entered = deferred();
  f.plugin.signInWithEmailAndPassword = async () => {
    entered.resolve();
    await gate.promise;
    return { user: { uid: "native-owner", email: "owner@example.test" } };
  };
  const pending = auth.reauthenticate("native-owner", "owner@example.test", "synthetic");
  await entered.promise;
  f.emit(null);
  f.emit({ uid: "native-owner", email: "owner@example.test" });
  gate.resolve();
  await assert.rejects(pending);
  assert.equal(await auth.current(), null);
  assert.deepEqual(f.calls, ["signout"]);
  stop();
});

test("late invitation completion after A to B to A cannot restore code or old confirmation", () => {
  const late = deferred(),
    entered = deferred();
  return using(
    async ({ settings, session }) => {
      await settings.prepare("invite", undefined, "member");
      const pending = settings.confirm();
      await entered.promise;
      await session.selectTenant(tenantB);
      await session.selectTenant(tenantA);
      late.resolve();
      await pending;
      assert.equal(settings.snapshot().invitation, undefined);
      assert.equal(settings.snapshot().review, undefined);
      assert.equal(settings.snapshot().pending, false);
    },
    "owner",
    (fixture) => async (request) => {
      const response = await fixture.transport(request);
      if (request.method === "POST" && request.url.endsWith("/invitations")) {
        entered.resolve();
        await late.promise;
      }
      return response;
    },
  );
});
test("switching A to B to A during reauth invalidates its original selection generation", () =>
  using(async ({ fixture, session }) => {
    const late = deferred(),
      entered = deferred();
    fixture.auth.reauthenticate = async () => {
      entered.resolve();
      await late.promise;
    };
    const pending = session.reauthenticate(actor, "synthetic");
    await entered.promise;
    await session.selectTenant(tenantB);
    await session.selectTenant(tenantA);
    const current = session.snapshot().context;
    late.resolve();
    assert.equal(await pending, false);
    assert.equal(session.snapshot().context, current);
  }));

test("membership pagination finds later targets and uses only opaque cursor on same tenant", () =>
  using(
    async ({ settings, fixture }) => {
      assert.equal(settings.snapshot().members.length, 1);
      assert.equal(settings.snapshot().cursor, "later-page");
      await settings.prepare("role", "u?/#+", "admin");
      assert.equal(settings.snapshot().review.target.user_id, "u?/#+");
      await settings.confirm();
      assert.equal(writes(fixture).length, 1);
    },
    "owner",
    (fixture) => async (request) => {
      const response = await fixture.transport(request);
      if (request.method === "GET" && new URL(request.url).pathname.endsWith("/memberships")) {
        const page = await response.json();
        const more = new URL(request.url).searchParams.get("cursor") === "later-page";
        return Response.json({
          items: more ? page.items.slice(1) : page.items.slice(0, 1),
          next_cursor: more ? null : "later-page",
        });
      }
      return response;
    },
  ));
test("server denial during management discards all tenant feature resources", () =>
  using(async ({ settings, fixture, session }) => {
    const old = session.snapshot().context;
    let disposed = false;
    old.own(() => {
      disposed = true;
    });
    fixture.settings.controls.scenario = "denied";
    await settings.load();
    assert.equal(disposed, true);
    assert.equal(session.snapshot().context, undefined);
    assert.equal(settings.snapshot().members.length, 0);
    assert.equal(settings.snapshot().usage, undefined);
  }));
test("acknowledged mutation followed by failed read remains confirmed and clears old display", () => {
  let fail = false;
  return using(
    async ({ settings }) => {
      await settings.prepare("role", "u?/#+", "admin");
      await settings.confirm();
      assert.equal(settings.snapshot().pending, false);
      assert.equal(settings.snapshot().members.length, 0);
      assert.match(settings.snapshot().notice, /変更を確認しました/);
    },
    "owner",
    (fixture) => async (request) => {
      if (fail && request.method === "GET") throw Error("Synthetic read failure");
      const response = await fixture.transport(request);
      if (request.method === "PATCH") fail = true;
      return response;
    },
  );
});
test("native credential sign-in returning a different UID fails closed and signs out", async () => {
  const f = nativePlugin(),
    auth = createNativeAuthentication(f.plugin),
    stop = auth.subscribe(() => {});
  await tick();
  f.plugin.signInWithEmailAndPassword = async () => {
    f.emit({ uid: "wrong", email: "wrong@example.test" });
    return { user: { uid: "wrong" } };
  };
  await assert.rejects(auth.reauthenticate("native-owner", "owner@example.test", "synthetic"));
  assert.equal(await auth.current(), null);
  stop();
});

test("last-owner demotion across pages is rejected before write and owner transfer remains usable", () =>
  using(
    async ({ settings, fixture }) => {
      await settings.prepare("role", actor, "member");
      await settings.confirm();
      assert.equal(settings.snapshot().pending, false);
      assert.equal(writes(fixture).length, 0);
      await settings.prepare("transfer", "u?/#+");
      assert.equal(settings.snapshot().review.kind, "transfer");
      await settings.confirm();
      assert.equal(writes(fixture).length, 1);
      assert.equal(settings.snapshot().pending, false);
      assert.equal(fixture.devices.controls.role, "member");
    },
    "owner",
    (fixture) => async (request) => {
      const response = await fixture.transport(request);
      if (request.method === "GET" && new URL(request.url).pathname.endsWith("/memberships")) {
        const page = await response.json();
        const more = new URL(request.url).searchParams.get("cursor") === "later-page";
        return Response.json({
          items: more ? page.items.slice(1) : page.items.slice(0, 1),
          next_cursor: more ? null : "later-page",
        });
      }
      return response;
    },
  ));
