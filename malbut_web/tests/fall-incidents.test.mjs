import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { execFileSync } from "node:child_process";
import path from "node:path";
import test from "node:test";
import { fallDatabase, moduleLoader } from "./helpers/fall-db-harness.mjs";

function event(change = {}) {
  return { schemaVersion: 1, eventId: randomUUID(), incidentId: randomUUID(), bootId: "boot-1",
    sequence: 1, evidenceRevision: 1, occurredAt: "2026-09-18T00:00:00.000Z",
    eventKind: "incident_opened", state: "verifying", fallSeen: false,
    assessment: null, answer: null, reason: null, notificationLevel: null, ...change };
}
function notice(change = {}) {
  return event({ eventKind: "notification_requested", state: "help_required",
    answer: "help_request", notificationLevel: "urgent", reason: "help_requested", ...change });
}

test("fall request contract is strict, bounded, and excludes recipients/media/device spoofing", async () => {
  const { parseFallEvent, readFallEvent } = moduleLoader()("app/fall-contract.ts");
  assert.ok(parseFallEvent(event()));
  assert.ok(parseFallEvent(notice()));
  for (const change of [
    { deviceId: "robot-b" }, { image: "secret" }, { recipient: "other@example.com" },
    { sequence: 0 }, { evidenceRevision: true }, { eventKind: "unknown" },
    { notificationLevel: "urgent" }, { occurredAt: "bad" }, { bootId: "" },
  ]) assert.equal(parseFallEvent(event(change)), null);
  assert.equal(parseFallEvent(notice({ answer: "okay" })), null);
  const req = (body) => new Request("https://test/api", {
    method: "POST", headers: { "content-type": "application/json" }, body,
  });
  assert.ok(await readFallEvent(req(JSON.stringify(event()))));
  assert.equal(await readFallEvent(req(" ".repeat(8193))), null);
  assert.equal(await readFallEvent(req("{")), null);
});

test("Agent confirmation accepts independent situation/help judgments and validates new intents", () => {
  const { parseFallEvent } = moduleLoader()("app/fall-contract.ts");
  assert.ok(parseFallEvent(event({ eventKind: "incident_updated", evidenceRevision: 2 })));
  for (const reason of ["confirmed_incident", "resolved", "unknown"]) {
    for (const help of [false, true]) {
      assert.ok(parseFallEvent(event({ eventKind: "confirmation_completed", reason,
        state: help ? "help_required" : "resolved", answer: help ? "help_request" : "okay" })));
    }
  }
  const confirmation = event({ eventKind: "confirmation_completed", reason: "unknown",
    state: "resolved", answer: "okay" });
  for (const change of [{ reason: "invalid" }, { answer: null }, { state: "verifying" },
    { answer: "help_request" }, { notificationLevel: "urgent" }]) {
    assert.equal(parseFallEvent({ ...confirmation, ...change }), null);
  }
  assert.ok(parseFallEvent(notice({ reason: "confirmation_help_required", fallSeen: false })));
  assert.equal(parseFallEvent(notice({ reason: "confirmation_help_required", answer: "okay" })), null);
  assert.equal(parseFallEvent(notice({ reason: "confirmation_help_required", state: "verifying" })), null);
  assert.equal(parseFallEvent(event({ state: "resolved" })), null);
});

test("incident and notification persist atomically, retries and conflicting IDs are handled", async () => {
  const h = await fallDatabase(), load = moduleLoader();
  const pg = load("db/postgres.ts"), repo = load("db/fall-incidents.ts");
  try { await pg.withPostgresPoolForTest(h.pool, async () => {
    const input = notice();
    assert.equal((await repo.storeFallEvent("robot-a", input)).created, true);
    assert.equal((await repo.storeFallEvent("robot-a", input)).created, false);
    const [a, b] = await Promise.all([repo.storeFallEvent("robot-a", input), repo.storeFallEvent("robot-a", input)]);
    assert.equal(a.created || b.created, false);
    await assert.rejects(repo.storeFallEvent("robot-a", { ...input, fallSeen: true }), /IDEMPOTENCY/);
    await assert.rejects(repo.storeFallEvent("robot-a", { ...input, eventId: randomUUID() }), /SEQUENCE/);
    assert.equal((await h.db.query("SELECT * FROM fall_push_outbox")).rows.length, 1);
    assert.equal((await h.db.query("SELECT * FROM fall_incident_events")).rows.length, 1);
    assert.equal((await h.db.query("SELECT * FROM fall_incidents")).rows.length, 1);
    // Same remote IDs on another authenticated device remain fully isolated.
    await repo.storeFallEvent("robot-b", input);
    assert.equal((await repo.listFallIncidents("robot-b")).length, 1);
    const bad = event();
    await assert.rejects(repo.storeFallEvent("missing-device", bad));
    assert.equal((await h.db.query("SELECT * FROM fall_incident_events WHERE event_id=$1", [bad.eventId])).rows.length, 0);
  }); } finally { await h.db.close(); }
});

test("late normal results cannot erase recorded falls or downgrade notification rank", async () => {
  const h = await fallDatabase(), load = moduleLoader();
  const pg = load("db/postgres.ts"), repo = load("db/fall-incidents.ts");
  try { await pg.withPostgresPoolForTest(h.pool, async () => {
    const urgent = notice({ sequence: 3, fallSeen: true, assessment: "observed_fall" });
    await repo.storeFallEvent("robot-a", urgent);
    const older = event({ incidentId: urgent.incidentId, sequence: 1, assessment: "normal_activity", answer: "okay" });
    await repo.storeFallEvent("robot-a", older);
    let [saved] = await repo.listFallIncidents("robot-a");
    assert.equal(saved.fallSeen, true);
    assert.equal(saved.state, "help_required");
    assert.equal(saved.notificationRank, 3);
    await repo.storeFallEvent("robot-a", event({ incidentId: urgent.incidentId, sequence: 4, assessment: "normal_activity" }));
    [saved] = await repo.listFallIncidents("robot-a");
    assert.equal(saved.fallSeen, true);
    assert.equal(saved.notificationRank, 3);
    assert.equal(saved.state, "help_required");
    await assert.rejects(repo.storeFallEvent("robot-a", event({
      incidentId: urgent.incidentId, sequence: 5, eventKind: "incident_resolved", state: "resolved",
      reason: "normal_verified", assessment: "normal_activity", answer: "okay",
    })), /STATE_CONFLICT/);
    await assert.rejects(repo.storeFallEvent("robot-a", event({ incidentId: urgent.incidentId, sequence: 5, bootId: "boot-2" })), /BOOT/);
  }); } finally { await h.db.close(); }
});

test("push claims are leased, partial receipts survive restart, and old lease cannot finish", async () => {
  const h = await fallDatabase(), load = moduleLoader();
  const pg = load("db/postgres.ts"), repo = load("db/fall-incidents.ts");
  try { await pg.withPostgresPoolForTest(h.pool, async () => {
    const input = notice();
    await repo.storeFallEvent("robot-a", input);
    const claims = await Promise.all([repo.claimFallPush(), repo.claimFallPush()]);
    assert.equal(claims.filter(Boolean).length, 1);
    const claim = claims.find(Boolean), subscriptionId = randomUUID();
    await repo.recordFallPushResults(claim, [{ subscriptionId, status: 201 }]);
    await h.db.exec("UPDATE fall_push_outbox SET lease_until=CURRENT_TIMESTAMP-INTERVAL '1 second'");
    const next = await repo.claimFallPush();
    assert.notEqual(next.leaseId, claim.leaseId);
    assert.equal(next.subscriptionResults[subscriptionId], 201);
    assert.equal(await repo.finishFallPush(claim, true, null), false);
    await assert.rejects(repo.recordFallPushResults(claim, []), /LEASE_LOST/);
    assert.equal(await repo.finishFallPush(next, false, "push_failed"), true);
    assert.equal(await repo.claimFallPush(), null); // backoff
    await h.db.exec("UPDATE fall_push_outbox SET next_attempt_at=CURRENT_TIMESTAMP-INTERVAL '1 second'");
    const retried = await repo.claimFallPush();
    assert.equal(await repo.finishFallPush(retried, true, null), true);
    assert.equal(await repo.claimFallPush(), null);
  }); } finally { await h.db.close(); }
});

test("higher grades suppress pending lower notices; repeated same grade queues only once", async () => {
  const h = await fallDatabase(), load = moduleLoader();
  const pg = load("db/postgres.ts"), repo = load("db/fall-incidents.ts");
  try { await pg.withPostgresPoolForTest(h.pool, async () => {
    const info = notice();
    Object.assign(info, { notificationLevel: "info", reason: "fall_observed_person_okay", fallSeen: true, answer: "okay", state: "recheck_required" });
    await repo.storeFallEvent("robot-a", info);
    const firstClaim = await repo.claimFallPush();
    await repo.storeFallEvent("robot-a", notice({ incidentId: info.incidentId, sequence: 2 }));
    assert.equal(await repo.finishFallPush(firstClaim, true, null), false);
    await repo.storeFallEvent("robot-a", notice({ incidentId: info.incidentId, sequence: 3 }));
    const rows = (await h.db.query("SELECT level,status FROM fall_push_outbox ORDER BY level")).rows;
    assert.deepEqual(rows, [{ level: "info", status: "superseded" }, { level: "urgent", status: "pending" }]);
  }); } finally { await h.db.close(); }
});

test("authenticated ingestion acknowledges durable storage even when push is offline", async () => {
  const h = await fallDatabase();
  const overrides = {
    [path.join(h.root, "app/device-auth.ts")]: { async getRequestDevice(req) {
      return req.headers.get("authorization") === "Bearer device-a" ? { deviceId: "robot-a" } : null;
    } },
    [path.join(h.root, "app/fall-event-push.ts")]: { async deliverPendingFallPush() { throw new Error("offline"); } },
    [path.join(h.root, "app/server-auth.ts")]: { async getRequestUserEmail(req) { return req.headers.get("x-test-email"); } },
  };
  const load = moduleLoader(overrides), pg = load("db/postgres.ts");
  const api = load("app/api/device/v1/fall-events/route.ts");
  const read = load("app/api/devices/[deviceId]/fall-incidents/route.ts");
  const request = (payload, token = "device-a") => new Request("https://test/api", {
    method: "POST", headers: { authorization: `Bearer ${token}`, "content-type": "application/json", "x-malbut-device-id": "robot-a" }, body: JSON.stringify(payload),
  });
  try { await pg.withPostgresPoolForTest(h.pool, async () => {
    const input = notice();
    assert.equal((await api.POST(request(input, "wrong"))).status, 401);
    const mismatched = request(input);
    mismatched.headers.set("x-malbut-device-id", "robot-b");
    assert.equal((await api.POST(mismatched)).status, 403);
    assert.equal((await api.POST(request({ ...input, deviceId: "robot-b" }))).status, 400);
    const response = await api.POST(request(input));
    assert.equal(response.status, 201);
    const body = await response.json();
    assert.equal(body.stored, true);
    assert.equal(body.push.accepted, false);
    assert.equal((await api.POST(request(input))).status, 200);
    assert.equal((await api.POST(request({ ...input, fallSeen: true }))).status, 409);
    const get = (email) => new Request("https://test/api", { headers: { "x-test-email": email } });
    assert.equal((await read.GET(get("stranger@example.com"), { params: Promise.resolve({ deviceId: "robot-a" }) })).status, 404);
    assert.equal((await read.GET(get("family@example.com"), { params: Promise.resolve({ deviceId: "robot-a" }) })).status, 200);
    assert.equal((await read.GET(get("owner@example.com"), { params: Promise.resolve({ deviceId: "robot-b" }) })).status, 404);
    await h.db.exec("DELETE FROM homecam_schema_migrations WHERE version='0009_fall_incidents'");
    assert.equal((await api.POST(request(event()))).status, 503);
  }); } finally { await h.db.close(); }
});

test("fall delivery retries failures only and never equates no subscribers with acceptance", async () => {
  // Test the worker with persisted claim receipts and a fake dispatch boundary.
  const loadRoot = (await import("node:url")).fileURLToPath(new URL("../", import.meta.url));
  const finishes = [], batches = [], before = [];
  let outcome = { dispatched: true, delivered: 0, failed: 0, pruned: 0 };
  let prior = { a: 201, b: 503, c: 410 };
  const load = moduleLoader({
    [path.join(loadRoot, "db/fall-incidents.ts")]: {
      async claimFallPush() { return { subscriptionResults: prior }; },
      async recordFallPushResults(c, r) { before.push(r); },
      async finishFallPush(c, complete, error) { finishes.push({ complete, error }); return true; },
    },
    [path.join(loadRoot, "app/push-broker.ts")]: { async dispatchFallPush(c, hooks) {
      batches.push(hooks.excludeSubscriptionIds); await hooks.beforeBatch();
      await hooks.onResults([{ subscriptionId: "b", status: 201 }]); return outcome;
    } },
  });
  const worker = load("app/fall-event-push.ts");
  assert.equal((await worker.deliverPendingFallPush()).accepted, true);
  assert.deepEqual(batches[0], ["a", "c"]);
  assert.equal(before.length, 2);
  prior = {};
  outcome = { dispatched: false, delivered: 0, pruned: 0, reason: "no_subscribers" };
  assert.equal((await worker.deliverPendingFallPush()).accepted, false);
  assert.equal(finishes.at(-1).complete, false);
  outcome = { dispatched: true, delivered: 0, failed: 1, pruned: 0 };
  assert.equal((await worker.deliverPendingFallPush()).accepted, false);
});

test("Python runtime journal -> authenticated API -> database -> push broker payload", async () => {
  const h = await fallDatabase();
  const records = JSON.parse(execFileSync("python3", [path.join(h.root, "tests/fixtures/fall_runtime_payloads.py")], {
    env: { ...process.env, PYTHONPATH: path.resolve(h.root, "../malbut_agent_server") }, encoding: "utf8",
  }));
  const pushes = [], originalFetch = globalThis.fetch;
  const load = moduleLoader({
    [path.join(h.root, "app/device-auth.ts")]: { async getRequestDevice() { return { deviceId: "robot-a" }; } },
    [path.join(h.root, "app/runtime-env.ts")]: { getRuntimeEnvironment() { return {
      PUSH_BROKER_URL: "https://broker.example.com/", PUSH_BROKER_SECRET: "test-secret",
    }; } },
  });
  const pg = load("db/postgres.ts"), api = load("app/api/device/v1/fall-events/route.ts");
  globalThis.fetch = async (url, init) => {
    assert.equal(String(url), "https://broker.example.com/");
    const body = JSON.parse(init.body); pushes.push(body.notification);
    return Response.json({ results: body.subscriptions.map((s) => ({ subscriptionId: s.subscriptionId, status: 201 })) });
  };
  try { await pg.withPostgresPoolForTest(h.pool, async () => {
    await h.db.query(`INSERT INTO push_subscriptions(id,user_email,device_id,endpoint,p256dh,auth)
      VALUES($1,'owner@example.com','robot-a','https://fcm.googleapis.com/test',$2,$3)`,
    [randomUUID(), "a".repeat(60), "b".repeat(24)]);
    for (const record of records) {
      const req = new Request("https://web.example.com/api/device/v1/fall-events", {
        method: "POST", headers: { "content-type": "application/json", "x-malbut-device-id": "robot-a" }, body: JSON.stringify(record),
      });
      assert.equal((await api.POST(req)).status, 201);
    }
    // Recovered offline backlog uploads urgent first; old info is superseded.
    assert.deepEqual(pushes.map((p) => p.data.level), ["urgent"]);
    assert.equal((await h.db.query("SELECT * FROM fall_incident_events")).rows.length, records.length);
    const saved = (await h.db.query("SELECT * FROM fall_incidents")).rows[0];
    assert.equal(saved.fall_seen, true);
    assert.equal(saved.answer, "help_request");
    assert.equal(saved.notification_rank, 3);
    assert.deepEqual((await h.db.query("SELECT level,status FROM fall_push_outbox ORDER BY level")).rows,
      [{ level: "info", status: "superseded" }, { level: "urgent", status: "accepted" }]);
  }); } finally { globalThis.fetch = originalFetch; await h.db.close(); }
});

test("Manager confirmation journal preserves all six outcomes through storage and notification intent", async () => {
  const h = await fallDatabase(), load = moduleLoader();
  const records = JSON.parse(execFileSync("python3", [path.join(h.root, "tests/fixtures/fall_confirmation_payloads.py")], {
    env: { ...process.env, PYTHONPATH: path.resolve(h.root, "../malbut_agent_server") }, encoding: "utf8",
  }));
  const pg = load("db/postgres.ts"), repo = load("db/fall-incidents.ts");
  const { buildFallNotification } = load("infra/aws/push-broker/fall-notification.mjs");
  try { await pg.withPostgresPoolForTest(h.pool, async () => {
    for (const record of records) assert.equal((await repo.storeFallEvent("robot-a", record)).stored, true);
    const confirmations = records.filter((r) => r.eventKind === "confirmation_completed");
    assert.equal(confirmations.length, 6);
    assert.ok(records.some((r) => r.eventKind === "incident_updated"));
    assert.equal(new Set(confirmations.map((r) => `${r.reason}:${r.state}`)).size, 6);
    const incidents = await repo.listFallIncidents("robot-a");
    assert.equal(incidents.filter((i) => i.state === "help_required").length, 3);
    assert.equal(incidents.filter((i) => i.state === "resolved").length, 3);
    const notices = (await h.db.query("SELECT level,reason FROM fall_push_outbox")).rows;
    assert.equal(notices.length, 3);
    assert.ok(notices.every((n) => n.level === "urgent" && n.reason === "confirmation_help_required"));
    const claim = await repo.claimFallPush();
    const notification = buildFallNotification(claim);
    assert.ok(notification);
    assert.match(notification.body, /도움이 필요한 것으로 판단/);
    assert.doesNotMatch(notification.body, /대상자가 도움을 요청|답변이 없습니다|낙상 확정/);
  }); } finally { await h.db.close(); }
});
