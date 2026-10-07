import assert from "node:assert/strict";
import test from "node:test";
import path from "node:path";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import ts from "typescript";
import { fallDatabase, moduleLoader, testUserId } from "./helpers/fall-db-harness.mjs";

const device = { deviceId: "robot-a", credentialId: "credential-a", displayName: "말벗", legacyChannelArn: "arn:test:a" };
const input = (operation, args = {}, requestId = operation) => ({ requestId, operation, arguments: args });
async function withDatabase(run) {
  const h = await fallDatabase(), load = moduleLoader();
  try {
    await h.db.exec(`INSERT INTO device_credentials(id,device_id,label,token_digest) VALUES
      ('credential-a','robot-a','test','digest-a'),('credential-b','robot-b','test','digest-b')`);
    await load("db/postgres.ts").withPostgresPoolForTest(h.pool, () => run(h, load("db/voice-agent.ts"), load));
  } finally { await h.db.close(); }
}

test("voice contract only accepts bounded device operations and no identity, URLs or consent changes", () => {
  const { parseVoiceRequest: parse } = moduleLoader()("app/voice-agent-contract.ts");
  assert.ok(parse(input("homecam_settings", { cameraEnabled: true, fallEnabled: false })));
  assert.ok(parse(input("homecam_events", { limit: 20, eventType: "cat" })));
  assert.ok(parse(input("result_publish", { kind: "mission", title: "순찰", summary: "끝났어요", state: "succeeded" })));
  assert.ok(parse(input("result_publish", { kind: "homecam", title: "홈캠", summary: "저장됐어요" })));
  for (const value of [input("shell", {}), input("homecam_status", { deviceId: "robot-b" }),
    { ...input("homecam_status"), deviceId: "robot-b" }, input("homecam_settings", { cloudConsent: true }),
    input("homecam_settings", { enabled: true }), input("homecam_settings", {}),
    input("homecam_settings", { cameraEnabled: 1 }), input("homecam_events", { limit: 21 }),
    input("homecam_events", { limit: -1 }), input("homecam_events", { eventType: "fall" }),
    input("homecam_events", { eventType: ["cat"] }),
    input("homecam_recordings", { url: "https://evil.test" }),
    input("result_publish", { kind: "event", title: "기록", summary: "기록" }),
    input("result_publish", { kind: "homecam", title: "기록", summary: "기록", referenceId: "x" }),
    input("result_publish", { kind: "status", title: "상태", summary: "x", href: "https://evil.test" }),
    input("result_publish", { kind: "status", title: "x".repeat(101), summary: "x" }),
    input("result_publish", { kind: ["mission"], title: "x", summary: "x" }),
    input("result_publish", { kind: "mission", state: ["succeeded"], title: "x", summary: "x" }),
    input("homecam_status", {}, "../other")]) assert.equal(parse(value), null, JSON.stringify(value));
});

test("delegation defaults off, is owner-only and ordinary robot results do not need it", async () => {
  await withDatabase(async (h, repo) => {
    assert.equal((await repo.readVoiceDelegation("robot-a")).enabled, false);
    assert.equal((await repo.operateVoiceDevice(device, input("homecam_status"))).code, "VOICE_DELEGATION_REQUIRED");
    await assert.rejects(repo.saveVoiceDelegation("robot-a", "u-family", true), /FORBIDDEN/);
    await assert.rejects(repo.saveVoiceDelegation("robot-b", "u-owner", true), /FORBIDDEN/);
    const published = await repo.operateVoiceDevice(device, input("result_publish", {
      kind: "mission", title: "순찰", summary: "완료", referenceId: "mission-1", state: "succeeded",
    }));
    assert.equal(published.success, true);
    assert.equal(published.result.href, "/?device=robot-a&view=robot#voice-result-result_publish");
    await repo.saveVoiceDelegation("robot-a", "u-owner", true);
    assert.equal((await repo.readVoiceDelegation("robot-a")).enabled, true);
    assert.equal((await h.db.query("SELECT granted_by FROM device_voice_delegations WHERE device_id='robot-a'")).rows[0].granted_by, "u-owner");
    assert.equal((await repo.operateVoiceDevice(device, input("homecam_status", {}, "status-2"))).success, true);
    assert.equal((await repo.listVoiceHistory("robot-a")).length, 3);
    assert.equal((await h.db.query("SELECT * FROM access_audit_log WHERE action='voice.operate'")).rows.length, 3);
  });
});

test("settings save once, preserve cloud consent, advance fall revision and flow through existing heartbeat", async () => {
  await withDatabase(async (h, repo, originalLoad) => {
    await repo.saveVoiceDelegation("robot-a", "u-owner", true);
    const request = input("homecam_settings", { cameraEnabled: true, fallEnabled: true, microphoneEnabled: false });
    const first = await repo.operateVoiceDevice(device, request);
    assert.equal(first.code, "SETTINGS_SAVED");
    assert.equal(first.result.savedRevision, "2");
    assert.equal(first.result.runtimeVerified, false);
    assert.deepEqual(await repo.operateVoiceDevice(device, request), first);
    await assert.rejects(repo.operateVoiceDevice(device, { ...request, arguments: { cameraEnabled: false } }), /CONFLICT/);
    const state = (await h.db.query("SELECT * FROM device_state WHERE device_id='robot-a'")).rows[0];
    assert.equal(state.fall_cloud_consent, false);
    assert.equal(state.microphone_enabled, 0);
    assert.equal(state.fall_settings_revision, "2");
    const load = moduleLoader({
      [path.join(h.root, "db/postgres.ts")]: originalLoad("db/postgres.ts"),
      [path.join(h.root, "app/device-auth.ts")]: { getRequestDevice: async () => device },
    });
    const heartbeat = await load("app/api/device/v1/heartbeat/route.ts").POST(new Request("https://test/api", {
      method: "POST", headers: { "content-type": "application/json" }, body: "{}",
    }));
    assert.equal(heartbeat.status, 200);
    const payload = await heartbeat.json();
    assert.equal(payload.mediaSettingsRevision, "2");
    assert.deepEqual(payload.desiredState, { cameraEnabled: true, monitoringEnabled: false, microphoneEnabled: false });
    assert.deepEqual(payload.fallSettings, { settingsRevision: "2", enabled: true, cameraEnabled: true, cloudConsent: false });
  });
});

test("media and fall status distinguish saved, applied receipts, failed receipts and stale evidence", async () => {
  await withDatabase(async (h, repo, originalLoad) => {
    await repo.saveVoiceDelegation("robot-a", "u-owner", true);
    const saved = await repo.operateVoiceDevice(device, input("homecam_settings", { microphoneEnabled: false, fallEnabled: true }));
    const load = moduleLoader({
      [path.join(h.root, "db/postgres.ts")]: originalLoad("db/postgres.ts"),
      [path.join(h.root, "app/device-auth.ts")]: { getRequestDevice: async () => device },
    });
    const heartbeat = (body) => load("app/api/device/v1/heartbeat/route.ts").POST(new Request("https://test/api", {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
    }));
    const status = async (id) => (await repo.operateVoiceDevice(device, input("homecam_status", {}, id))).result;
    assert.equal((await status("waiting")).mediaApplyReceipt.state, "waiting");
    const media = { runtimeId: "media-a", sequence: "1", requestedRevision: saved.result.mediaSettingsRevision,
      cameraEnabled: true, microphoneEnabled: false, monitoringEnabled: false, applied: true, reasonCode: "applied", reportAgeS: 0 };
    assert.equal((await heartbeat({ mediaSettingsReport: media })).status, 200);
    let result = await status("applied");
    assert.equal(result.mediaApplyReceipt.state, "reported_applied");
    assert.equal(result.mediaApplyReceipt.runtimeId, "media-a");
    assert.equal(result.mediaApplyReceipt.microphoneEnabled, false);
    assert.equal(result.mediaApplyReceipt.runtimeVerified, false);
    assert.equal(result.mediaApplyReceipt.fresh, true);
    await h.db.query("UPDATE device_media_settings_reports SET received_at=clock_timestamp()-INTERVAL '1 minute'");
    assert.equal((await heartbeat({ mediaSettingsReport: media })).status, 200);
    assert.equal((await status("old")).mediaApplyReceipt.fresh, false, "duplicate report must not refresh age");
    assert.equal((await heartbeat({ mediaSettingsReport: { ...media, sequence: "2", applied: false, reasonCode: "local_media_unavailable" } })).status, 200);
    assert.equal((await status("failed")).mediaApplyReceipt.state, "reported_failed");
    assert.equal((await heartbeat({ mediaSettingsReport: { ...media, sequence: "3", microphoneEnabled: true } })).status, 409);
    assert.equal((await heartbeat({ mediaSettingsReport: { ...media, sequence: "0" } })).status, 400);
    await repo.operateVoiceDevice(device, input("homecam_settings", { microphoneEnabled: true }, "new-settings"));
    assert.equal((await heartbeat({ mediaSettingsReport: { ...media, sequence: "4" } })).status, 200, "old receipt must not block new desired state");
    result = await status("new-waiting");
    assert.equal(result.mediaSettingsRevision, "3");
    assert.equal(result.mediaApplyReceipt.state, "waiting");
    assert.equal(result.settingsRevision, saved.result.savedRevision, "microphone must not advance fall revision");
    const fall = { bridgeRuntimeId: "bridge-a", managerRuntimeId: "manager-a", runtimeId: "fall-a",
      sequence: "1", snapshotSequence: "1", requestedRevision: result.settingsRevision, appliedRevision: result.settingsRevision,
      applied: true, enabled: true, cameraEnabled: true, cloudConsent: false, reasonCode: "applied", reportAgeS: 0 };
    assert.equal((await heartbeat({ fallSettingsReport: fall })).status, 200);
    assert.equal((await status("fall-applied")).fallApplyReceipt.state, "reported_applied");
    await repo.saveVoiceDelegation("robot-a", "u-owner", false);
    const refused = await repo.operateVoiceDevice(device, input("result_publish", { kind: "homecam", title: "홈캠", summary: "상태" }));
    assert.equal(refused.code, "VOICE_DELEGATION_REQUIRED");
  });
});

test("camera OFF ends affected media sessions and refuses monitoring without camera", async () => {
  await withDatabase(async (h, repo) => {
    await repo.saveVoiceDelegation("robot-a", "u-owner", true);
    await h.db.exec(`INSERT INTO stream_sessions(id,room_code,device_id,started_by,started_at,expires_at,mode) VALUES
      ('p2p','ROOMA1','robot-a','device',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP+INTERVAL '1 hour','p2p'),
      ('storage','ROOMA2','robot-a','device',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP+INTERVAL '1 hour','storage');
      INSERT INTO recording_sessions(session_id,kvs_stream_arn,kvs_channel_arn,started_at) VALUES('storage','arn:stream','arn:channel',CURRENT_TIMESTAMP);
      INSERT INTO device_state(device_id,monitoring_enabled,p2p_session_id,storage_session_id,active_session_id,active_stream_mode)
      VALUES('robot-a',1,'p2p','storage','storage','storage');`);
    assert.equal((await repo.operateVoiceDevice(device, input("homecam_settings", { cameraEnabled: false }))).success, true);
    const state = (await h.db.query("SELECT * FROM device_state WHERE device_id='robot-a'")).rows[0];
    assert.equal(state.monitoring_enabled, 0);
    assert.equal(state.p2p_session_id, null);
    assert.equal(state.storage_session_id, null);
    assert.equal(state.active_stream_mode, "idle");
    assert.deepEqual((await h.db.query("SELECT status FROM stream_sessions")).rows, [{ status: "ended" }, { status: "ended" }]);
    assert.ok((await h.db.query("SELECT ended_at FROM recording_sessions")).rows[0].ended_at);
    const denied = await repo.operateVoiceDevice(device, input("homecam_settings", { monitoringEnabled: true }, "monitoring"));
    assert.equal(denied.code, "CAMERA_DISABLED");
    assert.equal((await h.db.query("SELECT state FROM device_voice_requests WHERE request_id='monitoring'")).rows[0].state, "failed");
  });
});

test("revocation cancels pending intents, prevents replay disclosure and rechecks grantor/credential", async () => {
  await withDatabase(async (h, repo) => {
    await repo.saveVoiceDelegation("robot-a", "u-owner", true);
    await repo.operateVoiceDevice(device, input("homecam_status", {}, "completed"));
    await h.db.query(`INSERT INTO device_voice_requests(device_id,request_id,credential_id,operation,arguments_json,requires_delegation)
      VALUES('robot-a','pending','credential-a','homecam_settings','{"cameraEnabled":false}',true)`);
    await repo.saveVoiceDelegation("robot-a", "u-owner", false);
    const canceled = await repo.operateVoiceDevice(device, input("homecam_settings", { cameraEnabled: false }, "pending"));
    assert.equal(canceled.code, "VOICE_DELEGATION_REQUIRED");
    assert.equal((await h.db.query("SELECT camera_enabled FROM device_state WHERE device_id='robot-a'")).rows[0].camera_enabled, 1);
    assert.equal((await repo.operateVoiceDevice(device, input("homecam_status", {}, "completed"))).code, "VOICE_DELEGATION_REQUIRED");
    assert.equal((await h.db.query("SELECT state FROM device_voice_requests WHERE request_id='completed'")).rows[0].state, "completed");
    await repo.saveVoiceDelegation("robot-a", "u-owner", true);
    assert.equal((await repo.operateVoiceDevice(device, input("homecam_settings", { cameraEnabled: false }, "pending"))).success, false);
    await h.db.query("UPDATE device_memberships SET role='family' WHERE user_id='u-owner'");
    assert.equal((await repo.operateVoiceDevice(device, input("homecam_status", {}, "owner-removed"))).code, "VOICE_DELEGATION_REQUIRED");
    await h.db.query("UPDATE device_credentials SET revoked_at=CURRENT_TIMESTAMP WHERE id='credential-a'");
    assert.equal((await repo.operateVoiceDevice(device, input("result_publish", { kind: "status", title: "상태", summary: "대기" }))).code, "VOICE_CREDENTIAL_REVOKED");
  });
});

test("record reads and published references stay inside authenticated device, without playback secrets", async () => {
  await withDatabase(async (h, repo) => {
    await repo.saveVoiceDelegation("robot-a", "u-owner", true);
    await h.db.exec(`INSERT INTO homecam_events(id,device_id,event_type,occurred_at,idempotency_key,request_fingerprint) VALUES
      ('event-a','robot-a','cat',CURRENT_TIMESTAMP,'event-a','a'),('event-b','robot-b','person',CURRENT_TIMESTAMP,'event-b','b');
      INSERT INTO fall_incidents(device_id,incident_id,boot_id,evidence_revision,state,occurred_at) VALUES
      ('robot-a','fall-a','boot-a',1,'resolved',CURRENT_TIMESTAMP),('robot-b','fall-b','boot-b',1,'help_required',CURRENT_TIMESTAMP);`);
    const events = await repo.operateVoiceDevice(device, input("homecam_events"));
    assert.deepEqual(events.result.events.map((e) => e.id), ["event-a"]);
    assert.equal(events.result.events[0].href, "/voice-results/robot-a/event/event-a");
    assert.deepEqual((await repo.operateVoiceDevice(device, input("homecam_falls"))).result.incidents.map((i) => i.id), ["fall-a"]);
    assert.deepEqual((await repo.operateVoiceDevice(device, input("homecam_recordings"))).result.recordings, []);
    const publish = (referenceId) => input("result_publish", { kind: "event", title: "감지", summary: "기록", referenceId }, referenceId);
    assert.equal((await repo.operateVoiceDevice(device, publish("event-b"))).code, "VOICE_REFERENCE_NOT_FOUND");
    const eventResult = await repo.operateVoiceDevice(device, publish("event-a"));
    assert.equal(eventResult.success, true);
    assert.equal(eventResult.result.referenceHref, "/voice-results/robot-a/event/event-a");
    const fall = await repo.operateVoiceDevice(device, input("result_publish", {
      kind: "fall", title: "낙상", summary: "확인 완료", referenceId: "fall-a",
    }, "publish-fall"));
    assert.equal(fall.result.referenceHref, "/voice-results/robot-a/fall/fall-a");
    assert.doesNotMatch(JSON.stringify(await repo.listVoiceHistory("robot-a")), /arn:|presign|playbackUrl|token_digest/);
  });
});

test("map references require the device's actual map and use revision-bound authenticated previews", async () => {
  await withDatabase(async (h, repo) => {
    await h.db.exec(`INSERT INTO robot_maps(device_id,revision,map_id,map_revision,width,height,resolution,origin_x,origin_y,origin_yaw,preview_base64)
      VALUES('robot-a','image-revision-a','real-map-a','geometry-a',1,1,0.05,0,0,0,'png'),
            ('robot-b','image-revision-b','real-map-b','geometry-b',1,1,0.05,0,0,0,'png');`);
    const publish = (referenceId) => input("result_publish", { kind: "map", title: "지도", summary: "지도 결과", referenceId }, referenceId);
    assert.equal((await repo.operateVoiceDevice(device, publish("real-map-b"))).code, "VOICE_REFERENCE_NOT_FOUND");
    const own = await repo.operateVoiceDevice(device, publish("real-map-a"));
    assert.equal(own.success, true, "ordinary map result does not require Homecam delegation");
    assert.equal(own.result.referenceHref, "/api/devices/robot-a/robot/map?revision=image-revision-a");
  });
});

test("HTTP boundaries require device auth, owner and same-origin delegation, and reject oversized input", async () => {
  await withDatabase(async (h, repo, originalLoad) => {
    const load = moduleLoader({
      [path.join(h.root, "db/postgres.ts")]: originalLoad("db/postgres.ts"),
      [path.join(h.root, "app/server-auth.ts")]: { getRequestUserId: async (r) => r.headers.get("x-test-user-id") || testUserId(r.headers.get("x-test-email")) },
      [path.join(h.root, "app/device-auth.ts")]: { getRequestDevice: async (r) => r.headers.get("authorization") === "Bearer robot-a" ? device : null },
      [path.join(h.root, "app/runtime-env.ts")]: { getRuntimeEnvironment: () => ({}) },
    });
    const web = load("app/api/devices/[deviceId]/voice-agent/route.ts");
    const api = load("app/api/device/v1/agent/operate/route.ts");
    const context = { params: Promise.resolve({ deviceId: "robot-a" }) };
    const patch = (email, origin = "https://test") => new Request("https://test/api", { method: "PATCH",
      headers: { "content-type": "application/json", "x-test-email": email, origin }, body: '{"enabled":true}' });
    assert.equal((await web.PATCH(patch("family@example.com"), context)).status, 403);
    assert.equal((await web.PATCH(patch("owner@example.com", "https://evil.test"), context)).status, 403);
    assert.equal((await web.PATCH(patch("owner@example.com"), context)).status, 200);
    const socialOwner = new Request("https://test/api", { method: "PATCH", headers: {
      "content-type": "application/json", "x-test-user-id": "u-owner", origin: "https://test",
    }, body: '{"enabled":true}' });
    assert.equal((await web.PATCH(socialOwner, context)).status, 200, "user-ID sessions need no email");
    const call = (body, auth = true) => api.POST(new Request("https://test/api", { method: "POST",
      headers: { "content-type": "application/json", ...(auth ? { authorization: "Bearer robot-a" } : {}) }, body: JSON.stringify(body) }));
    assert.equal((await call(input("homecam_status"), false)).status, 401);
    assert.equal((await call({ ...input("homecam_status"), deviceId: "robot-b" })).status, 400);
    assert.equal((await call(input("homecam_settings", { cloudConsent: true }))).status, 400);
    assert.equal((await call(input("result_publish", { kind: "status", title: "x", summary: "x".repeat(9000) }))).status, 400);
    assert.equal((await call(input("homecam_status"))).status, 200);
    assert.equal((await web.GET(new Request("https://test/api", { headers: { "x-test-email": "family@example.com" } }), context)).status, 200);
    await h.db.query("DELETE FROM homecam_schema_migrations WHERE version='0024_voice_agent'");
    assert.equal((await call(input("homecam_status"))).status, 503);
  });
});

test("voice detail pages use current user IDs and bind recording/event rows to the selected device", async () => {
  await withDatabase(async (h, _repo, load) => {
    await h.db.exec(`INSERT INTO stream_sessions(id,device_id,mode,room_code,started_by,status,started_at,expires_at) VALUES
      ('record-a','robot-a','storage','AAAA','device:robot-a','active',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP+INTERVAL '1 hour'),
      ('record-b','robot-b','storage','BBBB','device:robot-b','active',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP+INTERVAL '1 hour');
      INSERT INTO recording_sessions(session_id,kvs_stream_arn,kvs_channel_arn,started_at) VALUES
      ('record-a','arn:a','arn:channel-a',CURRENT_TIMESTAMP-INTERVAL '2 minutes'),
      ('record-b','arn:b','arn:channel-b',CURRENT_TIMESTAMP-INTERVAL '2 minutes');
      INSERT INTO homecam_events(id,device_id,event_type,occurred_at,idempotency_key,request_fingerprint) VALUES
      ('event-a','robot-a','cat',CURRENT_TIMESTAMP,'event-a','a'),
      ('event-b','robot-b','cat',CURRENT_TIMESTAMP,'event-b','b');`);
    let userId = "u-owner";
    const require = createRequire(import.meta.url);
    const source = readFileSync(path.join(h.root, "app/voice-results/[deviceId]/[kind]/[referenceId]/page.tsx"), "utf8");
    const code = ts.transpileModule(source, { fileName: "page.tsx", compilerOptions: {
      module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
    } }).outputText;
    const mod = { exports: {} };
    new Function("require", "module", "exports", code)((name) => {
      if (name === "next/navigation") return { notFound: () => { throw new Error("NOT_FOUND"); } };
      if (name.endsWith("/chatgpt-auth")) return { requireChatGPTUser: async () => ({ userId, email: null }) };
      if (name.endsWith("/db/homecam")) return load("db/homecam.ts");
      if (name.endsWith("/db/postgres")) return load("db/postgres.ts");
      if (name.endsWith("/voice-recording-result")) return { VoiceRecordingResult: () => null };
      return require(name);
    }, mod, mod.exports);
    const page = (kind, referenceId, deviceId = "robot-a") => mod.exports.default({
      params: Promise.resolve({ deviceId, kind, referenceId }),
    });
    assert.match(JSON.stringify(await page("recording", "record-a")), /momentAt/);
    assert.match(JSON.stringify(await page("event", "event-a")), /고양이/);
    await assert.rejects(page("recording", "record-b"), /NOT_FOUND/);
    await assert.rejects(page("event", "event-b"), /NOT_FOUND/);
    await assert.rejects(page("recording", "record-b", "robot-b"), /NOT_FOUND/);
    userId = "u-family";
    assert.ok(await page("recording", "record-a"));
    userId = "u-stranger";
    await assert.rejects(page("recording", "record-a"), /NOT_FOUND/);
  });
});

test("robot web mirror contains identical voice backend and UI", () => {
  const root = path.resolve(import.meta.dirname, ".."), copy = path.resolve(root, "../malbut_test/malbut_web");
  for (const file of ["app/voice-agent-contract.ts", "app/voice-agent-http.ts", "db/voice-agent.ts",
    "app/media-settings-contract.ts", "db/media-settings.ts", "db/fall-settings.ts", "app/api/device/v1/heartbeat/route.ts",
    "db/migrations/0024_voice_agent.sql", "db/schema.ts", "app/components/voice-agent-panel.tsx",
    "app/components/managed-robot-controls.tsx", "app/components/robot-map-panel.tsx",
    "app/components/homecam-dashboard.tsx", "app/api/device/v1/agent/operate/route.ts",
    "app/components/homecam-app.tsx", "app/components/voice-recording-result.tsx",
    "app/voice-results/[deviceId]/[kind]/[referenceId]/page.tsx",
    "app/api/devices/[deviceId]/voice-agent/route.ts", "docs/voice_agent.md"]) {
    assert.equal(readFileSync(path.join(root, file), "utf8"), readFileSync(path.join(copy, file), "utf8"), file);
  }
});
