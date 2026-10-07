import assert from "node:assert/strict";
import test from "node:test";
import path from "node:path";
import { readFileSync } from "node:fs";
import ts from "typescript";
import { fallDatabase, moduleLoader } from "./helpers/fall-db-harness.mjs";

const leaseId = "6f8edff4-d91c-40cd-8b20-74d766160e98";
const otherId = "70bc74fa-1eb2-4459-848d-07a9cd1b7654";

test("talk readiness is a fresh robot acknowledgment bound to the current device lease", async () => {
  const h = await fallDatabase();
  const load = moduleLoader({
    [path.join(h.root, "app/device-auth.ts")]: { getRequestDevice: async (r) =>
      ({ deviceId: r.headers.get("x-device") ?? "robot-a" }) },
  });
  try {
    await load("db/postgres.ts").withPostgresPoolForTest(h.pool, async () => {
      const repo = load("db/homecam.ts");
      const route = load("app/api/device/v1/heartbeat/route.ts");
      const beat = (body = {}, device = "robot-a") => route.POST(new Request("https://test/heartbeat", {
        method: "POST", headers: { "content-type": "application/json", "x-device": device },
        body: JSON.stringify(body),
      }));
      assert.equal((await (await beat()).json()).talkLease, null);
      const owner = { deviceId: "robot-a", userId: "u-owner", clientId: "viewer-a" };
      const lease = await repo.acquireTalkLease(owner);
      assert.equal(lease.ready, false);
      assert.equal(lease.readyForMs, 0);
      const renew = () => repo.acquireTalkLease({ ...owner, existingLeaseId: lease.leaseId });
      const talk = (await (await beat()).json()).talkLease;
      assert.equal(talk.leaseId, lease.leaseId);
      assert.ok(Number.isInteger(talk.remainingMs) && talk.remainingMs > 0 && talk.remainingMs <= 15000);
      const report = { leaseId: lease.leaseId, ready: true };
      for (const invalid of [null, [], {}, { ...report, ready: 1 }, { ...report, extra: true },
        { ...report, leaseId: "invalid" }, { ready: true }]) {
        assert.equal((await beat({ talkReport: invalid })).status, 400);
      }
      await beat({ talkReport: report }, "robot-b");
      await beat({ talkReport: { ...report, leaseId: otherId } });
      assert.equal((await renew()).ready, false);
      await beat({ talkReport: report });
      const prepared = await renew();
      assert.equal(prepared.ready, true);
      assert.ok(Number.isInteger(prepared.readyForMs) && prepared.readyForMs > 0 && prepared.readyForMs <= 2500);
      await beat({ talkReport: { ...report, ready: false } });
      assert.equal((await renew()).ready, false);
      await beat({ talkReport: report });
      await h.db.exec("UPDATE talk_leases SET ready_until=CURRENT_TIMESTAMP-INTERVAL '1 second'");
      assert.equal((await renew()).ready, false);
      await h.db.exec("UPDATE talk_leases SET expires_at=CURRENT_TIMESTAMP+INTERVAL '1 second'");
      await beat({ talkReport: report });
      const capped = (await h.db.query("SELECT ready_until=expires_at AS capped FROM talk_leases")).rows[0];
      assert.equal(capped.capped, true);
      await h.db.exec("UPDATE talk_leases SET expires_at=CURRENT_TIMESTAMP-INTERVAL '1 second'");
      assert.equal((await (await beat({ talkReport: report })).json()).talkLease, null);
      const replacement = await repo.acquireTalkLease(owner);
      assert.notEqual(replacement.leaseId, lease.leaseId);
      assert.equal(replacement.ready, false);
      await beat({ talkReport: report });
      assert.equal((await repo.acquireTalkLease({ ...owner, existingLeaseId: replacement.leaseId })).ready, false);
      assert.equal(await repo.releaseTalkLease({ ...owner, leaseId: replacement.leaseId }), true);
      assert.equal((await (await beat({ talkReport: report })).json()).talkLease, null);
    });
  } finally { await h.db.close(); }
});

function viewer() {
  const source = readFileSync(new URL("../app/components/homecam-app.tsx", import.meta.url), "utf8");
  const tree = ts.createSourceFile("homecam.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const declarations = {};
  function visit(node) {
    if (ts.isVariableDeclaration(node) && ["releaseTalkLease", "startTalking"].includes(node.name.getText(tree))) {
      declarations[node.name.getText(tree)] = node.getText(tree);
    }
    ts.forEachChild(node, visit);
  }
  visit(tree);
  const script = ts.transpileModule(
    `const ${declarations.releaseTalkLease}; const ${declarations.startTalking};`,
    { compilerOptions: { target: ts.ScriptTarget.ES2022 } },
  ).outputText;
  const ref = (current) => ({ current });
  const track = { enabled: false }, requests = [], released = [], timers = new Map();
  let timerId = 0, now = 0;
  const scope = {
    microphoneRef: ref({ getAudioTracks: () => [track] }),
    viewerStateRef: ref("live"), viewerGenerationRef: ref(1), viewerClientIdRef: ref("viewer-a"),
    viewerMountedRef: ref(true), talkIntentRef: ref(false), talkAttemptRef: ref(0),
    talkLeaseRef: ref(null), talkLeaseTimerRef: ref(null),
    talkLeasePending: false, talking: false, deviceId: "robot-a",
    setTalkLeasePending() {}, setTalking() {}, setMicrophoneNotice() {},
    setTalkHolder() {}, setTalkEnded() {}, talkStopReasonRef: ref(null),
    notifyTalkLeaseRelease: (lease) => released.push(lease.leaseId),
    useCallback: (callback) => callback,
    Date: { now: () => now },
    performance: { now: () => now },
    window: {
      setTimeout: (callback, delay) => { timers.set(++timerId, { callback, delay }); return timerId; },
      clearTimeout: (id) => timers.delete(id),
    },
    fetch: (_url, options) => new Promise((resolve, reject) => requests.push({
      body: JSON.parse(options.body), reject,
      reply: (id, ready, readyForMs = ready ? 2500 : 0) => resolve({
        ok: true, json: async () => ({ lease: { leaseId: id, ready, readyForMs } }),
      }),
    })),
  };
  const functions = new Function(...Object.keys(scope), `${script}\nreturn { startTalking, releaseTalkLease };`)(...Object.values(scope));
  return { ...functions, track, requests, released, scope,
    async tick(delay) {
      const timer = [...timers.entries()].find(([, item]) => item.delay === delay);
      assert.ok(timer, `missing ${delay}ms timer`);
      timers.delete(timer[0]); now += delay; timer[1].callback(); await flush();
    },
    advance: (ms) => { now += ms; },
  };
}

const flush = () => new Promise((resolve) => setImmediate(resolve));

test("viewer stays muted until same-lease readiness, and stops on readiness loss", async () => {
  const v = viewer(), started = v.startTalking();
  v.requests[0].reply(leaseId, false); await flush();
  assert.equal(v.track.enabled, false);
  await v.tick(250);
  assert.equal(v.requests[1].body.leaseId, leaseId);
  v.requests[1].reply(leaseId, true); await started;
  assert.equal(v.track.enabled, true);
  await v.tick(8000);
  v.requests[2].reply(leaseId, false); await flush();
  assert.equal(v.track.enabled, false);
  assert.deepEqual(v.released, [leaseId]);
});

test("a delayed ready response cannot enable the mic after its readiness window expired", async () => {
  const v = viewer(), started = v.startTalking();
  // The server's ready=true snapshot has at most 2500 ms left. Simulate a
  // delayed response while robot heartbeats stop and its local gate expires.
  v.advance(3000);
  v.requests[0].reply(leaseId, true);
  await flush();
  assert.equal(v.track.enabled, false);
  await v.tick(250);
  assert.equal(v.requests[1].body.leaseId, leaseId);
  v.requests[1].reply(leaseId, true);
  await started;
  assert.equal(v.track.enabled, true);
  await v.tick(8000);
  v.advance(1000);
  v.requests[2].reply(leaseId, true, 500);
  await flush();
  assert.equal(v.track.enabled, false);
  assert.deepEqual(v.released, [leaseId]);
});

test("release while waiting, a superseded start, and readiness timeout cannot enable the mic", async () => {
  const v = viewer(), first = v.startTalking();
  v.releaseTalkLease();
  const second = v.startTalking();
  v.requests[0].reply(leaseId, true); await first;
  assert.equal(v.track.enabled, false);
  assert.deepEqual(v.released, [leaseId]);
  v.requests[1].reply(otherId, false); await flush();
  v.advance(10000); await v.tick(250); await second;
  assert.equal(v.track.enabled, false);
  assert.deepEqual(v.released, [leaseId, otherId]);
  const third = v.startTalking();
  v.requests[2].reply(leaseId, false); await flush();
  await v.tick(250);
  v.releaseTalkLease();
  v.requests[3].reply(leaseId, true); await third;
  assert.equal(v.track.enabled, false);
});

test("a start reports talking, a failure, or being dropped by a connection change", async () => {
  const ok = viewer(), started = ok.startTalking();
  ok.requests[0].reply(leaseId, true);
  assert.equal(await started, "talking");

  const slow = viewer(), timedOut = slow.startTalking();
  slow.requests[0].reply(leaseId, false); await flush();
  slow.advance(10000); await slow.tick(250);
  assert.equal(await timedOut, "failed");

  // A reconnect replaces the viewer generation while waiting for readiness.
  const moved = viewer(), dropped = moved.startTalking();
  moved.requests[0].reply(leaseId, false); await flush();
  moved.scope.viewerGenerationRef.current = 2;
  await moved.tick(250);
  assert.equal(await dropped, "abandoned");
  assert.equal(moved.track.enabled, false);
});
