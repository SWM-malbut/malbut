import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { execFileSync } from "node:child_process";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fallDatabase, moduleLoader } from "./helpers/fall-db-harness.mjs";

const SECRET = "s".repeat(64);
const KEY = "ollama-test-key-1234";

function event(change = {}) {
  return { schemaVersion: 1, eventId: randomUUID(), incidentId: randomUUID(), bootId: "boot-1",
    sequence: 1, evidenceRevision: 1, occurredAt: "2026-09-18T00:00:00.000Z", eventKind: "incident_opened",
    state: "verifying", fallSeen: false, assessment: null, answer: null, reason: null,
    notificationLevel: null, ...change };
}
const second = (ms) => new Date(Math.floor(ms / 1000) * 1000).toISOString();
// A small valid JPEG header with a SOF0 marker (640x400) is enough for jpegSize.
const JPEG = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0x00, 0x04, 0x00, 0x00, 0xff, 0xc0, 0x00, 0x11, 0x08,
  0x01, 0x90, 0x02, 0x80, 0x03, 0x01, 0x22, 0x00, 0x02, 0x11, 0x01, 0x03, 0x11, 0x01, 0xff, 0xd9]).toString("base64");
const reply = (content) => JSON.stringify({ model: "gemma4:31b", done: true, done_reason: "stop",
  message: { role: "assistant", content } });

test("review prompt, payload and reply rules are the robot's (Python cross-check)", async (t) => {
  const root = path.resolve(import.meta.dirname, "..");
  const prompt = moduleLoader()("app/fall-ai-prompt.ts");
  const offsets = Array.from({ length: 12 }, (_, i) => i * 454 / 1000);
  const bodies = {
    plain: reply('{"assessment":"suspected_fall","explanation":"바닥에 앉아 있음"}'),
    fenced: reply('```json\n{"assessment":"normal_activity","explanation":"걷는 중"}\n```'),
    extra: reply('{"assessment":"observed_fall","explanation":"x","confidence":0.9}'),
    label: reply('{"assessment":"fall","explanation":"x"}'),
    findings: reply('{"assessment":"observed_fall","explanation":"x","findings":[]}'),
    notDone: JSON.stringify({ done: false, message: { role: "assistant", content: "{}" } }),
    prose: reply("넘어진 것 같습니다"),
  };
  const python = JSON.parse(execFileSync("python3", [path.join(root, "tests/fixtures/fall_review_prompt.py"),
    JSON.stringify(offsets), JSON.stringify(bodies)], {
    env: { ...process.env, PYTHONPATH: path.resolve(root, "../malbut_agent_server") }, encoding: "utf8",
  }));
  assert.equal(prompt.REVIEW_SYSTEM_PROMPT, python.system);
  assert.equal(prompt.REVIEW_USER_PREFIX, python.userPrefix);
  const web = prompt.buildReviewPayload("gemma4:31b",
    offsets.map((o) => ({ jpegBase64: JPEG, offsetMs: o * 1000, width: 640, height: 400 })), 5000, false);
  for (const message of web.messages) delete message.images;
  assert.equal(web.messages[0].content, python.system);
  if (python.payload === null) {
    // The robot's payload builder needs Pillow, which the web CI image lacks.
    t.diagnostic("payload shape not compared: Pillow is not installed");
  } else {
    const meta = (p) => JSON.parse(p.messages[1].content.slice(prompt.REVIEW_USER_PREFIX.length));
    assert.deepEqual(meta(web), meta(python.payload));
    assert.deepEqual({ ...web, messages: null }, { ...python.payload, messages: null });
    assert.equal(web.messages[0].content, python.payload.messages[0].content);
  }
  for (const [name, body] of Object.entries(bodies)) {
    let result;
    try { result = prompt.parseReviewReply(body); } catch (error) { result = error.message; }
    assert.deepEqual(result, python.replies[name], name);
  }
});

test("key encryption is bound to the robot and version; only printable keys", async () => {
  const c = moduleLoader()("app/fall-cloud-key-crypto.ts");
  const sealed = await c.encryptFallCloudKey(KEY, "robot-a", 2, SECRET);
  assert.doesNotMatch(sealed, /ollama-test/);
  assert.equal(await c.decryptFallCloudKey(sealed, "robot-a", 2, SECRET), KEY);
  await assert.rejects(c.decryptFallCloudKey(sealed, "robot-b", 2, SECRET));
  await assert.rejects(c.decryptFallCloudKey(sealed, "robot-a", 3, SECRET));
  await assert.rejects(c.decryptFallCloudKey(sealed, "robot-a", 2, "t".repeat(64)));
  await assert.rejects(c.encryptFallCloudKey(KEY, "robot-a", 1, "short"), /SECRET_MISSING/);
  for (const bad of ["short", "has space key", "키".repeat(10), 1234567890]) assert.equal(c.isValidFallCloudKey(bad), false);
});

async function withAi(work, overrides = {}) {
  const h = await fallDatabase(), load = moduleLoader(overrides);
  const pg = load("db/postgres.ts");
  try {
    await pg.withPostgresPoolForTest(h.pool, () => work({
      h, load, events: load("db/fall-incidents.ts"), review: load("db/fall-review.ts"), ai: load("db/fall-ai-review.ts"),
    }));
  } finally { await h.db.close(); }
}

test("only the owner sets or deletes the key; members see the last 4; the robot fetches by version", async () => {
  await withAi(async ({ h, ai }) => {
    assert.deepEqual(await ai.readFallCloudKeyView("robot-a"), { configured: false, last4: null, keyVersion: 0,
      updatedAt: null, robotModel: null, robotHasCurrent: false });
    await assert.rejects(ai.setFallCloudKey("robot-a", "family@example.com", KEY, SECRET), /FORBIDDEN/);
    await assert.rejects(ai.setFallCloudKey("robot-a", "owner@example.com", "bad key", SECRET), /INVALID/);
    await ai.setFallCloudKey("robot-a", "owner@example.com", KEY, SECRET);
    const view = await ai.readFallCloudKeyView("robot-a");
    assert.equal(view.last4, "1234");
    assert.equal(view.keyVersion, 1);
    assert.equal(view.robotHasCurrent, false);
    assert.equal(JSON.stringify(view).includes("ollama-test"), false);
    const stored = (await h.db.query("SELECT ciphertext FROM fall_cloud_keys")).rows[0].ciphertext;
    assert.doesNotMatch(stored, /ollama-test/);
    const sync = (known, model = "gemma4:31b", device = "robot-a") =>
      ai.syncFallCloudKeyForDevice(device, known, model, SECRET);
    assert.deepEqual(await sync(0), { keyVersion: 1, changed: true, apiKey: KEY });
    // Sent is not stored: only the robot's next report confirms it.
    assert.equal((await ai.readFallCloudKeyView("robot-a")).robotHasCurrent, false);
    // Up to date: the key is not sent again.
    assert.deepEqual(await sync(1), { keyVersion: 1, changed: false, apiKey: null });
    assert.equal((await ai.readFallCloudKeyView("robot-a")).robotHasCurrent, true);
    await ai.setFallCloudKey("robot-a", "owner@example.com", null, SECRET);
    assert.deepEqual(await sync(1), { keyVersion: 2, changed: true, apiKey: null });
    assert.equal((await ai.readFallCloudKeyView("robot-a")).configured, false);
    // Never set on the server: the robot keeps its own key file.
    assert.deepEqual(await sync(0, "gemma4:31b", "robot-b"), { keyVersion: 0, changed: false, apiKey: null });
    assert.equal((await ai.readFallCloudKeyView("robot-b")).robotModel, "gemma4:31b");
    const audit = (await h.db.query("SELECT action,metadata_json FROM access_audit_log ORDER BY created_at")).rows;
    assert.deepEqual(audit.map((a) => a.action), ["fall_cloud_key_set", "fall_cloud_key_deleted"]);
    assert.ok(audit.every((a) => !a.metadata_json.includes("ollama-test")));
    assert.equal((await ai.readFallCloudKeyView("robot-a")).robotModel, "gemma4:31b");
    await assert.rejects(sync(2, "gemma4:31b-cloud"), /MODEL_INVALID/);
    await assert.rejects(sync(2, "gemma4:cloud"), /MODEL_INVALID/);
  });
});

async function ready(h, ai) {
  await ai.setFallCloudKey("robot-a", "owner@example.com", KEY, SECRET);
  await ai.syncFallCloudKeyForDevice("robot-a", 0, "gemma4:31b", SECRET);
  await h.db.query("INSERT INTO device_state(device_id,fall_cloud_consent) VALUES('robot-a',true) ON CONFLICT(device_id) DO UPDATE SET fall_cloud_consent=true");
}

test("review requests: inside the incident only, consent and key required, one at a time", async () => {
  await withAi(async ({ h, events, review, ai }) => {
    const opened = event();
    await events.storeFallEvent("robot-a", opened);
    const id = opened.incidentId;
    await review.storeFallClip("robot-a", { schemaVersion: 1, incidentId: id, bootId: "boot-1", segmentIndex: 0,
      revision: 1, startAt: "2026-09-17T23:59:50.000Z", endAt: "2026-09-18T00:00:20.000Z",
      anchorKinds: ["pose_motion"], foundDown: false, clockSource: "wall", clockStepped: false });
    const inside = "2026-09-18T00:00:05.000Z";
    await assert.rejects(ai.requestFallAiReview("robot-a", id, "family@example.com", "2026-09-18T00:00:25.000Z"),
      /OUTSIDE_INCIDENT/);
    await assert.rejects(ai.requestFallAiReview("robot-a", id, "family@example.com", inside), /CONSENT_OFF/);
    await h.db.query("INSERT INTO device_state(device_id,fall_cloud_consent) VALUES('robot-a',true) ON CONFLICT(device_id) DO UPDATE SET fall_cloud_consent=true");
    await assert.rejects(ai.requestFallAiReview("robot-a", id, "family@example.com", inside), /KEY_MISSING/);
    await ai.setFallCloudKey("robot-a", "owner@example.com", KEY, SECRET);
    await assert.rejects(ai.requestFallAiReview("robot-a", id, "family@example.com", inside), /MODEL_UNKNOWN/);
    await ai.syncFallCloudKeyForDevice("robot-a", 0, "gemma4:31b", SECRET);
    const { reviewId } = await ai.requestFallAiReview("robot-a", id, "family@example.com", inside);
    await assert.rejects(ai.requestFallAiReview("robot-a", id, "owner@example.com", inside), /IN_PROGRESS/);
    const listed = await ai.listFallAiReviews("robot-a", id);
    assert.deepEqual(listed.map((r) => [r.reviewId, r.status, r.momentAt]), [[reviewId, "queued", inside]]);
    // A missed-fall report is reviewed at its own moment only.
    const report = await review.reportMissedFall("robot-a", "family@example.com", inside, Date.parse(inside) + 60_000);
    await assert.rejects(ai.requestFallAiReview("robot-a", report.incidentId, "owner@example.com",
      "2026-09-18T00:00:06.000Z"), /OUTSIDE_INCIDENT/);
    assert.ok(await ai.requestFallAiReview("robot-a", report.incidentId, "owner@example.com", inside));
  });
});

function workerOverrides(root, state) {
  return {
    [path.join(root, "app/runtime-env.ts")]: { getRuntimeEnvironment() {
      return { FALL_KEY_ENCRYPTION_SECRET: SECRET, KVS_STREAM_ARN: "arn:stream:a" };
    } },
    [path.join(root, "app/kvs-device-config.ts")]: { resolveDeviceKvsResources() { return { streamArn: "arn:stream:a" }; } },
    [path.join(root, "app/kvs-broker.ts")]: { async requestBrokerImages(input) {
      state.broker.push(input);
      if (state.brokerError) throw new Error(state.brokerError);
      const start = Date.parse(input.startAt);
      return Array.from({ length: state.frames ?? 12 }, (_, i) => ({
        at: new Date(start + i * 454).toISOString(), jpegBase64: JPEG, error: null }));
    } },
  };
}

test("worker: 12 photos from the recording, robot prompt and model, verdict recorded", async () => {
  const state = { broker: [], posts: [] };
  const root = path.resolve(import.meta.dirname, "..");
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    state.posts.push({ url: String(url), body: JSON.parse(init.body), auth: init.headers.authorization });
    if (state.status) return new Response("{}", { status: state.status });
    return new Response(reply(state.content ?? '{"assessment":"observed_fall","explanation":"넘어지는 장면"}'),
      { status: 200, headers: { "content-type": "application/json" } });
  };
  try {
    await withAi(async ({ h, events, review, ai, load }) => {
      const worker = load("app/fall-ai-review-worker.ts");
      await ready(h, ai);
      const moment = second(Date.now() - 120_000);
      const report = await review.reportMissedFall("robot-a", "family@example.com", moment);
      const { reviewId } = await ai.requestFallAiReview("robot-a", report.incidentId, "family@example.com", moment);
      // The robot's own incident check goes first on the shared key.
      const busy = event();
      await events.storeFallEvent("robot-a", busy);
      assert.equal((await worker.processFallAiJob()).reason, "not_due_or_robot_busy");
      await h.db.query("UPDATE fall_incidents SET updated_at=CURRENT_TIMESTAMP-INTERVAL '2 minutes' WHERE incident_id=$1",
        [busy.incidentId]);
      const done = await worker.processFallAiJob();
      assert.deepEqual(done, { processed: true, ok: true, kind: "review", reason: "completed" });
      assert.deepEqual(state.broker[0], { deviceId: "robot-a", streamArn: "arn:stream:a", startAt: moment,
        endAt: new Date(Date.parse(moment) + 454 * 11).toISOString(), count: 12 });
      const sent = state.posts[0];
      assert.equal(sent.url, "https://ollama.com/api/chat");
      assert.equal(sent.auth, `Bearer ${KEY}`);
      assert.equal(sent.body.model, "gemma4:31b");
      assert.equal(sent.body.messages[1].images.length, 12);
      // Photo-only: no memo, opinion or earlier verdict in the judgment request.
      assert.doesNotMatch(JSON.stringify(sent.body.messages), /memo|verdict|family@/);
      const [saved] = await ai.listFallAiReviews("robot-a", report.incidentId);
      assert.deepEqual([saved.reviewId, saved.status, saved.assessment, saved.frameCount, saved.historyIncomplete],
        [reviewId, "completed", "observed_fall", 12, false]);
      // The incident itself is untouched.
      const detail = await review.getFallIncidentDetail("robot-a", report.incidentId);
      assert.equal(detail.reviewState, "open");
      assert.deepEqual(detail.opinions, []);

      // Follow-up: memo and earlier verdicts go along; the answer is reference only.
      await review.setFallOpinion("robot-a", report.incidentId, "owner@example.com", "fall", "오른쪽으로 쓰러짐");
      await ai.askFallAiQuestion("robot-a", report.incidentId, reviewId, "owner@example.com", "손으로 짚었나요?");
      state.content = "손으로 바닥을 짚는 모습이 보입니다.";
      assert.equal((await worker.processFallAiJob()).kind, "question");
      const followup = state.posts.at(-1).body.messages[1].content;
      assert.match(followup, /오른쪽으로 쓰러짐/);
      assert.match(followup, /observed_fall/);
      const [after] = await ai.listFallAiReviews("robot-a", report.incidentId);
      assert.equal(after.assessment, "observed_fall");
      assert.deepEqual(after.questions.map((q) => [q.status, q.answer]), [["completed", state.content]]);
      // Switch off: only the question and the photos.
      await ai.askFallAiQuestion("robot-a", report.incidentId, reviewId, "owner@example.com", "앉았나요?", false);
      await worker.processFallAiJob();
      assert.doesNotMatch(state.posts.at(-1).body.messages[1].content, /오른쪽으로 쓰러짐|observed_fall/);
    }, workerOverrides(root, state));
  } finally { globalThis.fetch = originalFetch; }
});

test("worker failures: quota retries later, missing frames and withdrawn consent fail without guessing", async () => {
  const state = { broker: [], posts: [] };
  const root = path.resolve(import.meta.dirname, "..");
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response("{}", { status: state.status ?? 429 });
  try {
    await withAi(async ({ h, review, ai, load }) => {
      const worker = load("app/fall-ai-review-worker.ts");
      await ready(h, ai);
      const moment = second(Date.now() - 120_000);
      const report = await review.reportMissedFall("robot-a", "family@example.com", moment);
      await ai.requestFallAiReview("robot-a", report.incidentId, "family@example.com", moment);
      assert.equal((await worker.processFallAiJob()).reason, "cloud_quota_exhausted");
      let [r] = await ai.listFallAiReviews("robot-a", report.incidentId);
      assert.deepEqual([r.status, r.errorCode], ["queued", "cloud_quota_exhausted"]);
      await h.db.query("UPDATE fall_ai_reviews SET next_attempt_at=CURRENT_TIMESTAMP");
      state.frames = 3;
      assert.equal((await worker.processFallAiJob()).reason, "frames_unavailable");
      [r] = await ai.listFallAiReviews("robot-a", report.incidentId);
      assert.equal(r.status, "failed");
      // A recent moment whose stills are not archived yet is retried, not failed.
      const fresh = await review.reportMissedFall("robot-a", "owner@example.com", second(Date.now() - 40_000));
      await ai.requestFallAiReview("robot-a", fresh.incidentId, "owner@example.com", second(Date.now() - 40_000));
      assert.equal((await worker.processFallAiJob()).reason, "frames_unavailable_yet");
      assert.equal((await ai.listFallAiReviews("robot-a", fresh.incidentId))[0].status, "queued");
      // A worker that keeps dying gives up after the attempt limit.
      await h.db.query(`UPDATE fall_ai_reviews SET status='running',attempt_count=6,
        lease_until=CURRENT_TIMESTAMP-INTERVAL '1 second' WHERE incident_id=$1`, [fresh.incidentId]);
      await ai.recoverFallAiJobs();
      const lost = (await ai.listFallAiReviews("robot-a", fresh.incidentId))[0];
      assert.deepEqual([lost.status, lost.errorCode], ["failed", "worker_lost"]);
      // A new request is allowed after the previous one finished.
      await ai.requestFallAiReview("robot-a", report.incidentId, "owner@example.com", moment);
      await h.db.query("UPDATE device_state SET fall_cloud_consent=false");
      assert.equal((await worker.processFallAiJob()).reason, "cloud_consent_off");
      // Recording not settled yet: wait, do not fail.
      await h.db.query("UPDATE device_state SET fall_cloud_consent=true");
      const recent = await review.reportMissedFall("robot-a", "owner@example.com", second(Date.now() - 2_000));
      await ai.requestFallAiReview("robot-a", recent.incidentId, "owner@example.com", second(Date.now() - 2_000));
      assert.equal((await worker.processFallAiJob()).reason, "recording_not_ready");
      assert.equal((await ai.listFallAiReviews("robot-a", recent.incidentId))[0].status, "queued");
    }, workerOverrides(root, state));
  } finally { globalThis.fetch = originalFetch; }
});

test("jpegSize reads SOF dimensions and rejects non-JPEG data", () => {
  const { jpegSize } = moduleLoader({
    [path.resolve(import.meta.dirname, "../app/runtime-env.ts")]: { getRuntimeEnvironment() { return {}; } },
  })("app/fall-ai-review-worker.ts");
  assert.deepEqual(jpegSize(Buffer.from(JPEG, "base64")), { width: 640, height: 400 });
  // Fill bytes before a marker are allowed; a zero segment length is not.
  const filled = Buffer.from(JPEG, "base64");
  const withFill = Buffer.concat([filled.subarray(0, 8), Buffer.from([0xff, 0xff]), filled.subarray(8)]);
  assert.deepEqual(jpegSize(withFill), { width: 640, height: 400 });
  assert.equal(jpegSize(Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0x00, 0x00, 0xff, 0xd9])), null);
  assert.equal(jpegSize(Buffer.from("not a jpeg")), null);
});

test("HTTP: key routes, robot key sync, review route errors", async () => {
  const h = await fallDatabase();
  const load = moduleLoader({
    [path.join(h.root, "app/server-auth.ts")]: { async getRequestUserEmail(req) { return req.headers.get("x-test-email"); } },
    [path.join(h.root, "app/device-auth.ts")]: { async getRequestDevice(req) {
      return req.headers.get("authorization") === "Bearer device-a" ? { deviceId: "robot-a" } : null;
    } },
    [path.join(h.root, "app/runtime-env.ts")]: { getRuntimeEnvironment() { return { FALL_KEY_ENCRYPTION_SECRET: SECRET }; } },
    [path.join(h.root, "app/fall-ai-review-worker.ts")]: { startFallAiJob() { return false; } },
  });
  const pg = load("db/postgres.ts"), events = load("db/fall-incidents.ts");
  const keyRoute = load("app/api/devices/[deviceId]/fall-cloud-key/route.ts");
  const deviceKey = load("app/api/device/v1/fall-cloud-key/route.ts");
  const reviews = load("app/api/devices/[deviceId]/fall-incidents/[incidentId]/ai-reviews/route.ts");
  const user = (email, method = "GET", body) => new Request("https://web.test/api", { method,
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    headers: { "x-test-email": email, "content-type": "application/json", origin: "https://web.test" } });
  const robot = (method = "GET", body) => new Request("https://web.test/api", { method,
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    headers: { authorization: "Bearer device-a", "x-malbut-device-id": "robot-a", "content-type": "application/json" } });
  const params = (p) => ({ params: Promise.resolve(p) });
  try { await pg.withPostgresPoolForTest(h.pool, async () => {
    const d = params({ deviceId: "robot-a" });
    assert.equal((await keyRoute.PUT(user("family@example.com", "PUT", { apiKey: KEY }), d)).status, 403);
    assert.equal((await keyRoute.PUT(user("stranger@example.com", "PUT", { apiKey: KEY }), d)).status, 404);
    assert.equal((await keyRoute.PUT(user("owner@example.com", "PUT", { apiKey: KEY, extra: 1 }), d)).status, 400);
    const saved = await keyRoute.PUT(user("owner@example.com", "PUT", { apiKey: KEY }), d);
    assert.equal(saved.status, 200);
    const text = await saved.text();
    assert.doesNotMatch(text, /ollama-test/);
    assert.equal(JSON.parse(text).last4, "1234");
    assert.equal((await (await keyRoute.GET(user("family@example.com"), d)).json()).configured, true);

    const fetched = await deviceKey.POST(robot("POST", { knownVersion: 0, model: "gemma4:31b" }));
    assert.deepEqual(await fetched.json(), { keyVersion: 1, changed: true, apiKey: KEY });
    assert.equal((await deviceKey.POST(robot("POST", { knownVersion: 0, model: "bad model" }))).status, 400);
    assert.equal((await deviceKey.POST(robot("POST", { knownVersion: -1, model: null }))).status, 400);
    const wrong = robot("POST", { knownVersion: 0, model: null });
    wrong.headers.set("x-malbut-device-id", "robot-b");
    assert.equal((await deviceKey.POST(wrong)).status, 403);
    assert.equal((await (await keyRoute.GET(user("owner@example.com"), d)).json()).robotModel, "gemma4:31b");

    const opened = event();
    await events.storeFallEvent("robot-a", opened);
    const p = params({ deviceId: "robot-a", incidentId: opened.incidentId });
    const outside = await reviews.POST(user("family@example.com", "POST", { momentAt: "2026-09-19T00:00:00.000Z" }), p);
    assert.equal(outside.status, 409);
    assert.equal((await outside.json()).reason, "outside_incident");
    const consent = await reviews.POST(user("family@example.com", "POST", { momentAt: "2026-09-18T00:00:05.000Z" }), p);
    assert.equal((await consent.json()).reason, "consent_off");
  }); } finally { await h.db.close(); }
});

test("KVS broker exposes bounded stills only and IAM allows GetImages", async () => {
  const [broker, stack] = await Promise.all([
    readFile(new URL("../infra/aws/kvs-broker/index.mjs", import.meta.url), "utf8"),
    readFile(new URL("../infra/cdk/lib/homecam-dev-stack.ts", import.meta.url), "utf8"),
  ]);
  assert.match(broker, /"GET_IMAGES"/);
  assert.match(broker, /maxImageRangeMs = 10_000/);
  assert.match(broker, /maxImageCount = 12/);
  assert.match(broker, /input\.streamArn !== resources\.streamArn/);
  assert.match(stack, /"kinesisvideo:GetImages"/);
  assert.match(stack, /FALL_KEY_ENCRYPTION_SECRET: ecs\.Secret\.fromSecretsManager/);
});
