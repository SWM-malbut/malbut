import assert from "node:assert/strict";
import test from "node:test";
import path from "node:path";
import { fallDatabase, moduleLoader } from "./helpers/fall-db-harness.mjs";

const session = "patrol-chat-" + "a".repeat(32);
const device = { deviceId: "robot-a", credentialId: "credential-a" };
const report = { sessionId: session, appliedRevision: 0 };
const consent = { action: "consent", expectedRevision: 1, categories: ["daily"], externalProcessing: true };
async function database(run) {
  const h = await fallDatabase(), load = moduleLoader();
  try {
    await h.db.exec("INSERT INTO device_credentials(id,device_id,label,token_digest) VALUES ('credential-a','robot-a','test','digest')");
    await load("db/postgres.ts").withPostgresPoolForTest(h.pool, () => run(h, load("db/companion.ts"), load));
  } finally { await h.db.close(); }
}

test("companion inputs reject fake identity, implicit consent and malformed encounters", () => {
  const { parseCompanionChange: parse, parseCompanionReport: parseReport } = moduleLoader()("app/companion-contract.ts");
  assert.ok(parse(consent));
  assert.ok(parse({ action: "confirm", expectedRevision: 2, sessionId: session, self: true, alone: true }));
  for (const value of [{ ...consent, userId: "other" }, { ...consent, categories: ["health"] },
    { ...consent, categories: ["daily", "daily"] }, { ...consent, externalProcessing: false },
    { action: "confirm", expectedRevision: 2, sessionId: session, self: true, alone: false },
    { action: "confirm", expectedRevision: 2, sessionId: "fake", self: true, alone: true },
    { action: "erase", expectedRevision: -1 }, { action: "confirm", expectedRevision: 1 }]) assert.equal(parse(value), null);
  assert.ok(parseReport(report));
  assert.equal(parseReport({ ...report, confirmed: true }), null);
  assert.equal(parseReport({ ...report, appliedRevision: "1" }), null);
});

test("only owner can grant consent and confirm a fresh matching encounter", async () => {
  await database(async (h, repo) => {
    await assert.rejects(repo.companionView("robot-a", "u-family"), /FORBIDDEN/);
    await assert.rejects(repo.changeCompanion("robot-a", "u-family", consent), /FORBIDDEN/);
    assert.deepEqual((await repo.companionView("robot-a", "u-owner")).categories, []);
    await repo.changeCompanion("robot-a", "u-owner", consent);
    const confirm = { action: "confirm", expectedRevision: 2, sessionId: session, self: true, alone: true };
    await assert.rejects(repo.changeCompanion("robot-a", "u-owner", confirm), /NO_ENCOUNTER/);
    const initial = await repo.syncCompanion(device, report);
    assert.equal(initial.confirmedSessionId, null);
    assert.deepEqual(initial.categories, ["daily"]);
    await assert.rejects(repo.changeCompanion("robot-a", "u-owner", { ...confirm, sessionId: "patrol-chat-" + "b".repeat(32) }), /NO_ENCOUNTER/);
    const confirmed = await repo.changeCompanion("robot-a", "u-owner", confirm);
    assert.equal(confirmed.confirmed, true);
    assert.equal(confirmed.revision, 3);
    await assert.rejects(repo.changeCompanion("robot-a", "u-owner", confirm), /CHANGED/);
    const lease = await repo.syncCompanion(device, report);
    assert.equal(lease.confirmedSessionId, session);
    assert.ok(lease.remainingSeconds > 290 && lease.remainingSeconds <= 300);
    // Polling cannot extend a five-minute identity confirmation.
    await h.db.exec("UPDATE device_companion_policy SET confirmed_until=clock_timestamp()+INTERVAL '30 seconds'");
    assert.ok((await repo.syncCompanion(device, report)).remainingSeconds <= 30);
    await h.db.exec("UPDATE device_companion_policy SET confirmed_until=clock_timestamp()-INTERVAL '1 second'");
    assert.equal((await repo.syncCompanion(device, report)).confirmedSessionId, null);
  });
});

test("departure, stale camera encounter, owner transfer and revoked credentials clear authority", async () => {
  await database(async (h, repo) => {
    await repo.changeCompanion("robot-a", "u-owner", consent);
    await repo.syncCompanion(device, report);
    await h.db.exec("UPDATE device_companion_policy SET session_seen_at=clock_timestamp()-INTERVAL '11 seconds'");
    const confirmation = { action: "confirm", expectedRevision: 2, sessionId: session, self: true, alone: true };
    await assert.rejects(repo.changeCompanion("robot-a", "u-owner", confirmation), /NO_ENCOUNTER/);
    await repo.syncCompanion(device, report);
    await repo.changeCompanion("robot-a", "u-owner", confirmation);
    assert.equal((await repo.syncCompanion(device, { ...report, sessionId: null })).confirmedSessionId, null);
    assert.equal((await repo.syncCompanion(device, report)).confirmedSessionId, null);
    await h.db.exec("UPDATE device_memberships SET role='family' WHERE user_id='u-owner'; UPDATE device_memberships SET role='owner' WHERE user_id='u-family'");
    const transferred = await repo.syncCompanion(device, report);
    assert.equal(transferred.ownerId, "u-family");
    assert.deepEqual(transferred.categories, []);
    assert.equal(transferred.confirmedSessionId, null);
    await h.db.exec("UPDATE device_credentials SET revoked_at=clock_timestamp()");
    await assert.rejects(repo.syncCompanion(device, report), /FORBIDDEN/);
  });
});

test("erasure and consent changes have separate saved and robot-applied revisions", async () => {
  await database(async (_h, repo) => {
    await repo.changeCompanion("robot-a", "u-owner", consent);
    const erased = await repo.changeCompanion("robot-a", "u-owner", { action: "erase", expectedRevision: 2 });
    assert.equal(erased.eraseRevision, 1);
    assert.equal(erased.appliedRevision, 0);
    await repo.syncCompanion(device, { ...report, appliedRevision: erased.revision });
    const applied = await repo.companionView("robot-a", "u-owner");
    assert.equal(applied.appliedRevision, applied.revision);
    const disabled = await repo.changeCompanion("robot-a", "u-owner", {
      action: "consent", expectedRevision: applied.revision, categories: [], externalProcessing: false });
    assert.deepEqual(disabled.categories, []);
    assert.equal(disabled.confirmed, false);
    assert.equal(disabled.eraseRevision, 1);
  });
});

test("HTTP binds users/devices to authenticated identities and rejects cross-site approval", async () => {
  await database(async (h, _repo, original) => {
    let user = null, authenticatedDevice = null;
    const load = moduleLoader({
      [path.join(h.root, "db/postgres.ts")]: original("db/postgres.ts"),
      [path.join(h.root, "app/server-auth.ts")]: { getRequestUserId: async () => user },
      [path.join(h.root, "app/device-auth.ts")]: { getRequestDevice: async () => authenticatedDevice },
      [path.join(h.root, "app/runtime-env.ts")]: { getRuntimeEnvironment: () => ({ AUTH_PUBLIC_ORIGIN: "https://app.test" }) },
    });
    const ownerRoute = load("app/api/devices/[deviceId]/companion/route.ts");
    const deviceRoute = load("app/api/device/v1/companion/route.ts");
    const context = { params: Promise.resolve({ deviceId: "robot-a" }) };
    const request = (body, origin = "https://app.test") => new Request("https://app.test/api", {
      method: "PATCH", headers: { origin, "content-type": "application/json" }, body: JSON.stringify(body) });
    assert.equal((await ownerRoute.PATCH(request(consent), context)).status, 401);
    user = "u-family";
    assert.equal((await ownerRoute.PATCH(request(consent), context)).status, 403);
    user = "u-owner";
    assert.equal((await ownerRoute.PATCH(request(consent, "https://evil.test"), context)).status, 403);
    assert.equal((await ownerRoute.PATCH(request({ ...consent, ownerId: "forged" }), context)).status, 400);
    assert.equal((await ownerRoute.PATCH(request(consent), context)).status, 200);
    assert.equal((await deviceRoute.POST(request(report))).status, 401);
    authenticatedDevice = device;
    assert.equal((await deviceRoute.POST(request(report))).status, 200);
    assert.equal((await deviceRoute.POST(request({ ...report, ownerId: "forged" }))).status, 400);
  });
});
