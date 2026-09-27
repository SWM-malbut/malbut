import assert from "node:assert/strict";
import test from "node:test";
import path from "node:path";
import { readFileSync, readdirSync } from "node:fs";
import pg from "pg";
import { fallDatabase, moduleLoader } from "./helpers/fall-db-harness.mjs";

function report(change = {}) {
  return { bridgeRuntimeId: "bridge-a", managerRuntimeId: "manager-a", runtimeId: "vlm-a",
    sequence: "1", snapshotSequence: "1", requestedRevision: "1", appliedRevision: "1",
    applied: true, enabled: false, cameraEnabled: true, cloudConsent: false,
    reasonCode: "applied", reportAgeS: 0.2, ...change };
}
async function withDatabase(operation) {
  const h = await fallDatabase(), load = moduleLoader();
  try { await load("db/postgres.ts").withPostgresPoolForTest(h.pool,
    () => operation(h, load("db/fall-settings.ts"), load)); }
  finally { await h.db.close(); }
}

test("migration runner applies shared-prefix migrations once by full filename", async () => {
  const names = readdirSync(path.resolve(import.meta.dirname, "../db/migrations"))
    .filter((name) => /^\d+_[a-z0-9_-]+\.sql$/i.test(name)).sort();
  assert.ok(names.includes("0010_managed_robot_tools.sql"));
  assert.ok(names.includes("0011_fall_settings.sql"));
  assert.ok(names.includes("0011_manual_move_stream.sql"));
  const h = await fallDatabase({ through: "0010_managed_robot_tools" });
  const originalPool = pg.Pool, originalUrl = process.env.DATABASE_URL;
  const applied = [];
  // Run the production CLI against an isolated database, not AWS/PostgreSQL.
  const client = { release() {}, async query(sql, values) {
    // A single in-memory connection needs no cross-process advisory lock.
    if (sql.includes("pg_advisory_xact_lock")) return { rows: [], rowCount: 0 };
    const result = values ? await h.pool.query(sql, values) : (await h.db.exec(sql)).at(-1);
    if (sql.startsWith("INSERT INTO homecam_schema_migrations")) applied.push(values[0]);
    return result;
  } };
  pg.Pool = class { async connect() { return client; } async end() {} };
  process.env.DATABASE_URL = "postgresql://test:test@localhost/test";
  try {
    for (const attempt of [1, 2]) {
      const script = new URL("../scripts/migrate.mjs", import.meta.url);
      script.searchParams.set("shared-prefix-test", String(attempt));
      await import(script.href);
      // Existing deployed IDs stay unchanged; restarting must not run them again.
      assert.deepEqual(applied, ["0011_fall_settings", "0011_manual_move_stream"]);
      const versions = (await h.db.query(
        "SELECT version FROM homecam_schema_migrations ORDER BY version",
      )).rows.map((row) => row.version);
      assert.deepEqual(versions, names.map((name) => path.basename(name, ".sql")));
    }
  } finally {
    pg.Pool = originalPool;
    if (originalUrl === undefined) delete process.env.DATABASE_URL;
    else process.env.DATABASE_URL = originalUrl;
    await h.db.close();
  }
});

test("robot web copy includes the same fall settings integration", () => {
  const root = path.resolve(import.meta.dirname, "..");
  const copy = path.resolve(root, "../malbut_test/malbut_web");
  for (const file of [
    "app/api/device/v1/heartbeat/route.ts",
    "app/api/devices/[deviceId]/fall-settings/route.ts",
    "app/components/fall-settings-panel.tsx",
    "app/components/homecam-dashboard.tsx",
    "app/fall-settings-contract.ts", "db/fall-settings.ts", "db/schema.ts",
    "db/migrations/0011_fall_settings.sql", "docs/fall_settings.md",
  ]) {
    assert.equal(readFileSync(path.join(root, file), "utf8"),
      readFileSync(path.join(copy, file), "utf8"), file);
  }
});

test("fall settings HTTP types preserve uint64, reject unknown keys and contradictory replies", () => {
  const { parseFallSettingsPatch: patch, parseFallSettingsReport: parse, isFallRevision } = moduleLoader()("app/fall-settings-contract.ts");
  assert.ok(patch({ expectedRevision: "1", enabled: true }));
  assert.ok(patch({ expectedRevision: "18446744073709551615", cloudConsent: false }));
  for (const v of ["0", "01", "1.0", "-1", "18446744073709551616", 1, null, true]) assert.equal(isFallRevision(v), false);
  for (const v of [{ enabled: true }, { expectedRevision: "1" }, { expectedRevision: "1", enabled: "true" },
    { expectedRevision: "1", cameraEnabled: true }, { expectedRevision: "1", cloudConsent: null }]) assert.equal(patch(v), null);
  assert.ok(parse(report({ sequence: "18446744073709551615" })));
  for (const change of [{ sequence: 1 }, { snapshotSequence: "0" }, { reportAgeS: Infinity },
    { reportAgeS: -1 }, { reportAgeS: NaN }, { runtimeId: "" }, { runtimeId: "x".repeat(129) },
    { applied: false }, { reasonCode: "internal_error" }, { appliedRevision: "0" },
    { deviceId: "robot-b" }, { enabled: 1 }, { appliedRevision: "2" }]) assert.equal(parse(report(change)), null);
  assert.ok(parse(report({ applied: false, reasonCode: "internal_error", appliedRevision: "0", cameraEnabled: false })));
});

test("owner settings use compare-and-swap; camera shares revision; media/heartbeat do not", async () => {
  await withDatabase(async (h, repo) => {
    let snap = await repo.readFallSettingsSnapshot("robot-a");
    assert.deepEqual(snap.settings, { settingsRevision: "1", enabled: false, cameraEnabled: true, cloudConsent: false });
    await assert.rejects(repo.saveFallSettings("robot-a", "family@example.com", { expectedRevision: "1", enabled: true }), /FORBIDDEN/);
    await assert.rejects(repo.saveFallSettings("robot-a", "outsider@example.com", { expectedRevision: "1", enabled: true }), /FORBIDDEN/);
    assert.deepEqual(await repo.saveFallSettings("robot-a", "owner@example.com", { expectedRevision: "1", enabled: true }), { settingsRevision: "2" });
    await assert.rejects(repo.saveFallSettings("robot-a", "owner@example.com", { expectedRevision: "1", cloudConsent: true }), /REVISION_CONFLICT/);
    const before = await repo.readFallSettingsSnapshot("robot-a");
    await repo.saveFallSettings("robot-a", "owner@example.com", { expectedRevision: "2", enabled: true });
    assert.equal((await repo.readFallSettingsSnapshot("robot-a")).savedAt, before.savedAt);
    // This is also the existing camera endpoint's database update path.
    await h.db.query("UPDATE device_state SET camera_enabled=0 WHERE device_id=$1", ["robot-a"]);
    snap = await repo.readFallSettingsSnapshot("robot-a");
    assert.equal(snap.settings.settingsRevision, "3");
    assert.equal(snap.settings.cameraEnabled, snap.desiredState.cameraEnabled);
    assert.equal(snap.settings.enabled, true);
    assert.equal(snap.settings.cloudConsent, false);
    await h.db.query("UPDATE device_state SET monitoring_enabled=1,microphone_enabled=0,last_seen_at=CURRENT_TIMESTAMP WHERE device_id=$1", ["robot-a"]);
    assert.equal((await repo.readFallSettingsSnapshot("robot-a")).settings.settingsRevision, "3");
    assert.equal((await h.db.query("SELECT * FROM fall_settings_versions WHERE device_id='robot-a'")).rows.length, 3);
    const results = await Promise.allSettled([
      repo.saveFallSettings("robot-a", "owner@example.com", { expectedRevision: "3", cloudConsent: true }),
      repo.saveFallSettings("robot-a", "owner@example.com", { expectedRevision: "3", enabled: false }),
    ]);
    assert.equal(results.filter((r) => r.status === "fulfilled").length, 1);
    assert.equal((await repo.readFallSettingsSnapshot("robot-a")).settings.settingsRevision, "4");
  });
});

test("reports are historical only, device scoped, validated, and retries never renew receipt age", async () => {
  await withDatabase(async (h, repo) => {
    await repo.readFallSettingsSnapshot("robot-a");
    const input = report();
    assert.equal((await repo.storeFallSettingsReport("robot-a", input)).created, true);
    const first = (await h.db.query("SELECT * FROM fall_settings_reports")).rows[0];
    const reordered = Object.fromEntries(Object.entries({ ...input, reportAgeS: 999 }).reverse());
    assert.equal((await repo.storeFallSettingsReport("robot-a", reordered)).created, false);
    const duplicate = (await h.db.query("SELECT * FROM fall_settings_reports")).rows[0];
    assert.deepEqual(duplicate, first);
    await assert.rejects(repo.storeFallSettingsReport("robot-a", report({ runtimeId: "other" })), /REPORT_CONFLICT/);
    await assert.rejects(repo.storeFallSettingsReport("robot-a", report({ sequence: "2", enabled: true })), /VALUES_MISMATCH/);
    await assert.rejects(repo.storeFallSettingsReport("robot-a", report({ sequence: "2", requestedRevision: "9", appliedRevision: "9" })), /UNKNOWN_REVISION/);
    await assert.rejects(repo.storeFallSettingsReport("robot-b", report()), /UNKNOWN_REVISION/);
    const view = await repo.readFallSettingsView("robot-a");
    assert.equal(view.receiptState, "history_only");
    assert.equal(view.runtimeVerified, false);
    assert.equal(view.reports[0].applied, true);
    await repo.saveFallSettings("robot-a", "owner@example.com", { expectedRevision: "1", enabled: true });
    // Late old-version and old-run responses are stored, not applied to latest settings.
    await repo.storeFallSettingsReport("robot-a", report({ sequence: "2", runtimeId: "old-run" }));
    const next = await repo.readFallSettingsView("robot-a");
    assert.equal(next.settings.settingsRevision, "2");
    assert.equal(next.settings.enabled, true);
    assert.deepEqual(next.reports, []);
    await repo.storeFallSettingsReport("robot-a", report({ sequence: "3", requestedRevision: "2", applied: false, reasonCode: "internal_error" }));
    assert.equal((await repo.readFallSettingsView("robot-a")).reports[0].applied, false);
    // Missing live runtime binding: never promote even a matching latest reply to live status.
    await repo.storeFallSettingsReport("robot-a", report({ sequence: "4", requestedRevision: "2", appliedRevision: "2", enabled: true }));
    assert.equal((await repo.readFallSettingsView("robot-a")).runtimeVerified, false);
  });
});

test("six-second no-reply display does not change saved settings; migration is explicit", async () => {
  await withDatabase(async (h, repo) => {
    await repo.readFallSettingsSnapshot("robot-a");
    // Advance only the view clock, without sleeping or changing saved settings.
    const clockPool = { ...h.pool, async query(sql, args) {
      const result = await h.pool.query(sql, args);
      if (sql.includes('AS "checkedAt"')) result.rows[0].checkedAt = new Date(Date.parse(result.rows[0].savedAt) + 6100).toISOString();
      return result;
    } };
    // Use one loader/context for the replacement pool and repository.
    const load = moduleLoader();
    await load("db/postgres.ts").withPostgresPoolForTest(clockPool, async () => {
      const view = await load("db/fall-settings.ts").readFallSettingsView("robot-a");
      assert.equal(view.receiptState, "no_response");
      assert.equal(view.settings.settingsRevision, "1");
    });
    await h.db.query("DELETE FROM homecam_schema_migrations WHERE version='0011_fall_settings'");
    assert.equal(await repo.hasFallSettingsSchema(), false);
    await assert.rejects(repo.readFallSettingsSnapshot("robot-a"), /MIGRATION_REQUIRED/);
  });
});

test("web owner save, camera change, heartbeat delivery and apply receipt round-trip through real DB", async () => {
  await withDatabase(async (h, repo, originalLoad) => {
    const load = moduleLoader({
      [path.join(h.root, "db/postgres.ts")]: originalLoad("db/postgres.ts"),
      [path.join(h.root, "app/server-auth.ts")]: { getRequestUserEmail: async (r) => r.headers.get("x-test-email") },
      [path.join(h.root, "app/device-auth.ts")]: { getRequestDevice: async (r) =>
        r.headers.get("authorization") === "Bearer robot-a" ? { deviceId: "robot-a" } : null },
      [path.join(h.root, "app/runtime-env.ts")]: { getRuntimeEnvironment: () => ({}) },
    });
    const web = load("app/api/devices/[deviceId]/fall-settings/route.ts");
    const heartbeat = load("app/api/device/v1/heartbeat/route.ts");
    const camera = load("app/api/devices/[deviceId]/settings/route.ts");
    const context = { params: Promise.resolve({ deviceId: "robot-a" }) };
    const request = (body, email = "owner@example.com", headers = {}) => new Request("https://test/api", {
      method: body ? "PATCH" : "GET", headers: { "content-type": "application/json", origin: "https://test",
        ...(email ? { "x-test-email": email } : {}), ...headers }, ...(body ? { body: JSON.stringify(body) } : {}),
    });
    const beat = (body = {}, token = "robot-a") => new Request("https://test/api", { method: "POST",
      headers: { authorization: `Bearer ${token}`, "content-type": "application/json" }, body: JSON.stringify(body) });
    const patch = { expectedRevision: "1", enabled: true, cloudConsent: true };
    assert.equal((await web.GET(request(null, ""), context)).status, 401);
    assert.equal((await web.GET(request(null, "stranger@example.com"), context)).status, 403);
    assert.equal((await web.GET(request(null, "family@example.com"), context)).status, 200);
    assert.equal((await web.PATCH(request(patch, "family@example.com"), context)).status, 403);
    assert.equal((await web.PATCH(request(patch, "owner@example.com", { origin: "https://evil.test" }), context)).status, 403);
    assert.equal((await web.PATCH(request(patch, "owner@example.com", { "sec-fetch-site": "cross-site" }), context)).status, 403);
    assert.equal((await web.PATCH(request({ ...patch, cameraEnabled: true }), context)).status, 400);
    const saved = await web.PATCH(request(patch), context);
    assert.equal(saved.status, 200);
    assert.deepEqual(await saved.json(), { saved: true, savedRevision: "2" });
    assert.equal((await web.PATCH(request(patch), context)).status, 409);
    assert.equal((await heartbeat.POST(beat({}, "wrong"))).status, 401);
    for (const value of [true, [], { deviceId: "robot-b" }, { fallSettingsReport: null }]) {
      assert.equal((await heartbeat.POST(beat(value))).status, 400);
    }
    const response = await heartbeat.POST(beat());
    assert.equal(response.status, 200);
    assert.equal(response.headers.get("cache-control"), "no-store");
    const data = await response.json();
    assert.deepEqual(Object.keys(data.desiredState).sort(), ["cameraEnabled", "microphoneEnabled", "monitoringEnabled"]);
    assert.deepEqual(data.fallSettings, { settingsRevision: "2", enabled: true, cameraEnabled: true, cloudConsent: true });
    const applied = report({ requestedRevision: "2", appliedRevision: "2", enabled: true, cloudConsent: true });
    assert.equal((await heartbeat.POST(beat({ fallSettingsReport: applied }))).status, 200);
    assert.equal((await heartbeat.POST(beat({ fallSettingsReport: { ...applied, reportAgeS: 20 } }))).status, 200);
    assert.equal((await heartbeat.POST(beat({ fallSettingsReport: { ...applied, runtimeId: "other" } }))).status, 409);
    assert.equal((await h.db.query("SELECT * FROM fall_settings_reports")).rows.length, 1);
    const view = await (await web.GET(request(null), context)).json();
    assert.equal(view.runtimeVerified, false);
    assert.equal(view.receiptState, "history_only");
    assert.equal((await camera.PATCH(request({ cameraEnabled: false }), context)).status, 200);
    const changed = await (await heartbeat.POST(beat())).json();
    assert.equal(changed.fallSettings.settingsRevision, "3");
    assert.equal(changed.fallSettings.cameraEnabled, false);
    assert.equal(changed.desiredState.cameraEnabled, false);
    assert.equal((await repo.readFallSettingsView("robot-a")).reports.length, 0);
    // Old installations continue serving media, but do not advertise fall support.
    await h.db.query("DELETE FROM homecam_schema_migrations WHERE version='0011_fall_settings'");
    assert.equal((await web.GET(request(null), context)).status, 503);
    const old = await heartbeat.POST(beat());
    assert.equal(old.status, 200);
    assert.equal((await old.json()).fallSettings, undefined);
    assert.equal((await heartbeat.POST(beat({ fallSettingsReport: applied }))).status, 503);
  });
});

test("uint64 report IDs and saved revision survive beyond JavaScript safe integer", async () => {
  await withDatabase(async (h, repo) => {
    // Insert a restored device state at a high revision; updates still use the DB trigger.
    await h.db.query("INSERT INTO device_state(device_id,fall_settings_revision) VALUES('robot-a',9007199254740993)");
    assert.equal((await repo.readFallSettingsSnapshot("robot-a")).settings.settingsRevision, "9007199254740993");
    await repo.saveFallSettings("robot-a", "owner@example.com", { expectedRevision: "9007199254740993", enabled: true });
    assert.equal((await repo.readFallSettingsSnapshot("robot-a")).settings.settingsRevision, "9007199254740994");
    await repo.storeFallSettingsReport("robot-a", report({ sequence: "18446744073709551615", snapshotSequence: "9007199254740995",
      requestedRevision: "9007199254740994", appliedRevision: "9007199254740994", enabled: true }));
    assert.equal((await repo.readFallSettingsView("robot-a")).reports[0].sequence, "18446744073709551615");
    await h.db.query("INSERT INTO device_state(device_id,fall_settings_revision) VALUES('robot-b',18446744073709551615)");
    await assert.rejects(h.db.query("UPDATE device_state SET camera_enabled=0 WHERE device_id='robot-b'"), /check constraint/);
    assert.equal((await repo.readFallSettingsSnapshot("robot-b")).settings.cameraEnabled, true);
  });
});

test("upgrading an existing database preserves camera/recording and starts fall consent OFF", async () => {
  const h = await fallDatabase({ through: "0009_fall_incidents" });
  try {
    await h.db.query("INSERT INTO device_state(device_id,camera_enabled,monitoring_enabled) VALUES('robot-a',0,0),('robot-b',1,1)");
    await h.db.exec(readFileSync(path.join(h.root, "db/migrations/0011_fall_settings.sql"), "utf8"));
    const versions = (await h.db.query("SELECT device_id,revision::text,enabled,camera_enabled,cloud_consent FROM fall_settings_versions ORDER BY device_id")).rows;
    assert.deepEqual(versions, [
      { device_id: "robot-a", revision: "1", enabled: false, camera_enabled: false, cloud_consent: false },
      { device_id: "robot-b", revision: "1", enabled: false, camera_enabled: true, cloud_consent: false },
    ]);
    assert.deepEqual((await h.db.query("SELECT monitoring_enabled FROM device_state ORDER BY device_id")).rows,
      [{ monitoring_enabled: 0 }, { monitoring_enabled: 1 }]);
    await h.db.query("UPDATE device_state SET camera_enabled=1 WHERE device_id='robot-a'");
    assert.equal((await h.db.query("SELECT revision::text FROM fall_settings_versions WHERE device_id='robot-a' ORDER BY revision DESC LIMIT 1")).rows[0].revision, "2");
  } finally { await h.db.close(); }
});

test("report storage failure is not acknowledged as successful heartbeat", async () => {
  const root = path.resolve(import.meta.dirname, "..");
  let calledMedia = false;
  const load = moduleLoader({
    [path.join(root, "app/device-auth.ts")]: { getRequestDevice: async () => ({ deviceId: "robot-a" }) },
    [path.join(root, "db/fall-settings.ts")]: {
      hasFallSettingsSchema: async () => true,
      storeFallSettingsReport: async () => { throw new Error("database connection lost"); },
    },
    [path.join(root, "db/homecam.ts")]: { updateDeviceHeartbeat: async () => { calledMedia = true; } },
  });
  const response = await load("app/api/device/v1/heartbeat/route.ts").POST(new Request("https://test/api", {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ fallSettingsReport: report() }),
  }));
  assert.equal(response.status, 500);
  assert.equal(calledMedia, false);
  assert.doesNotMatch(JSON.stringify(await response.json()), /database connection lost/);
});
