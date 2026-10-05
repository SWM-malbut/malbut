import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import path from "node:path";
import test from "node:test";
import { fallDatabase, moduleLoader, testUserId } from "./helpers/fall-db-harness.mjs";

// 사람 표시: robot person boxes of a clip segment. Positions only.
const recent = (offset) => new Date(Math.floor(Date.now() / 1000) * 1000 - 120_000 + offset).toISOString();

function event(change = {}) {
  return { schemaVersion: 1, eventId: randomUUID(), incidentId: randomUUID(), bootId: "boot-1",
    sequence: 1, evidenceRevision: 1, occurredAt: recent(0), eventKind: "incident_opened", state: "verifying",
    fallSeen: false, assessment: null, answer: null, reason: null, notificationLevel: null, ...change };
}
function clip(change = {}) {
  return { schemaVersion: 1, incidentId: randomUUID(), bootId: "boot-1", segmentIndex: 0, revision: 1,
    startAt: recent(-10_000), endAt: recent(20_000), anchorKinds: ["pose_motion"], foundDown: false,
    clockSource: "wall", clockStepped: false, ...change };
}
// Same shape as the robot journal (malbut_agent_server SqliteFallJournal.append_people).
function people(change = {}) {
  return { schemaVersion: 1, incidentId: randomUUID(), bootId: "boot-1", segmentIndex: 0, revision: 1,
    truncated: false, tracks: [{ key: "aaaaaaaaaaaa", target: true,
      samples: [[0, 100, 200, 300, 800], [200, 101, 200, 301, 800]] }],
    cloud: [[4000, 100, 400, 700, 900]], ...change };
}

async function withRepo(work, options) {
  const h = await fallDatabase(options), load = moduleLoader();
  const pg = load("db/postgres.ts");
  try {
    await pg.withPostgresPoolForTest(h.pool, () => work({ h, events: load("db/fall-incidents.ts"),
      review: load("db/fall-review.ts"), boxes: load("db/fall-people.ts") }));
  } finally { await h.db.close(); }
}

async function incidentWithClip(events, review, change = {}) {
  const opened = event();
  await events.storeFallEvent("robot-a", opened);
  await review.storeFallClip("robot-a", clip({ incidentId: opened.incidentId, ...change }));
  return opened.incidentId;
}

test("people contract is strict: boxes and times only, no raw IDs or media", () => {
  const { parseFallPeople } = moduleLoader()("app/fall-people-contract.ts");
  assert.ok(parseFallPeople(people()));
  assert.ok(parseFallPeople(people({ tracks: [] })));
  assert.ok(parseFallPeople(people({ cloud: [] })));
  const track = (change) => ({ tracks: [{ key: "aaaaaaaaaaaa", target: true, samples: [[0, 1, 2, 3, 4]], ...change }] });
  for (const change of [
    { image: "x" }, { schemaVersion: 2 }, { incidentId: "bad" }, { segmentIndex: 32 }, { revision: 0 },
    { truncated: "no" }, { tracks: [], cloud: [] },
    track({ key: "pose:0:track-1" }), track({ key: "AAAAAAAAAAAA" }), track({ samples: [] }),
    track({ samples: [[0, 300, 200, 100, 800]] }), track({ samples: [[0, 100, 200, 300, 1001]] }),
    track({ samples: [[200, 1, 2, 3, 4], [100, 1, 2, 3, 4]] }), track({ samples: [[0.5, 1, 2, 3, 4]] }),
    track({ samples: [[125_001, 1, 2, 3, 4]] }), track({ extra: 1 }),
    { tracks: Array.from({ length: 13 }, (_, i) => ({ key: i.toString(16).padStart(12, "0"), target: false,
      samples: [[0, 1, 2, 3, 4]] })) },
    { tracks: [people().tracks[0], people().tracks[0]] },
    { tracks: ["a", "b"].map((c) => ({ key: c.repeat(12), target: true, samples: [[0, 1, 2, 3, 4]] })) },
    { cloud: [[200, 1, 2, 3, 4], [100, 1, 2, 3, 4]] },
    { cloud: Array.from({ length: 33 }, () => [0, 1, 2, 3, 4]) },
  ]) assert.equal(parseFallPeople(people(change)), null, JSON.stringify(change).slice(0, 80));
  const { cloud, ...missing } = people();
  assert.ok(cloud);
  assert.equal(parseFallPeople(missing), null);
});

test("people storage waits for the clip, keeps the newest revision and flags the clip", async () => {
  await withRepo(async ({ events, review, boxes }) => {
    const incidentId = await incidentWithClip(events, review);
    await assert.rejects(boxes.storeFallPeople("robot-a", people({ incidentId, segmentIndex: 1 })),
      /FALL_PEOPLE_CLIP_MISSING/);
    await assert.rejects(boxes.storeFallPeople("robot-b", people({ incidentId })), /CLIP_MISSING/);
    await assert.rejects(boxes.storeFallPeople("robot-a", people({ incidentId, bootId: "boot-2" })),
      /FALL_PEOPLE_CONFLICT/);
    assert.equal((await review.getFallIncidentDetail("robot-a", incidentId)).clips[0].hasPeople, false);
    const first = people({ incidentId });
    assert.equal((await boxes.storeFallPeople("robot-a", first)).created, true);
    assert.equal((await boxes.storeFallPeople("robot-a", first)).created, false);
    await assert.rejects(boxes.storeFallPeople("robot-a", { ...first, cloud: [] }), /FALL_PEOPLE_CONFLICT/);
    await boxes.storeFallPeople("robot-a", { ...first, revision: 2, cloud: [] });
    assert.deepEqual(await boxes.storeFallPeople("robot-a", first),
      { stored: true, incidentId, segmentIndex: 0, revision: 1, created: false });
    assert.equal((await review.getFallIncidentDetail("robot-a", incidentId)).clips[0].hasPeople, true);
    const scene = await boxes.getFallClipPeople("robot-a", incidentId, 0);
    assert.deepEqual(scene, { segmentIndex: 0, revision: 2, truncated: false, cloud: [],
      people: [{ label: "사람 1", target: true, samples: first.tracks[0].samples }] });
    assert.equal(await boxes.getFallClipPeople("robot-a", incidentId, 1), null);
  });
});

test("linked incidents share 사람 N, numbered by first appearance", async () => {
  await withRepo(async ({ events, review, boxes }) => {
    // A starts first; B's person appears 5 s into A, A's own person 8 s in.
    const a = await incidentWithClip(events, review, { startAt: recent(-10_000), endAt: recent(20_000) });
    const b = await incidentWithClip(events, review, { startAt: recent(0), endAt: recent(30_000) });
    const alone = await incidentWithClip(events, review, { startAt: recent(60_000), endAt: recent(90_000) });
    const pa = "aaaaaaaaaaaa", pb = "bbbbbbbbbbbb", pc = "cccccccccccc";
    await boxes.storeFallPeople("robot-a", people({ incidentId: a, cloud: [], tracks: [
      { key: pa, target: true, samples: [[8000, 1, 2, 3, 4]] },
      { key: pb, target: false, samples: [[5000, 1, 2, 3, 4]] }] }));
    await boxes.storeFallPeople("robot-a", people({ incidentId: b, cloud: [], tracks: [
      { key: pb, target: true, samples: [[0, 1, 2, 3, 4]] },
      { key: pa, target: false, samples: [[0, 1, 2, 3, 4]] }] }));
    await boxes.storeFallPeople("robot-a", people({ incidentId: alone, cloud: [], tracks: [
      { key: pc, target: true, samples: [[0, 1, 2, 3, 4]] }] }));
    const labels = async (id) => Object.fromEntries((await boxes.getFallClipPeople("robot-a", id, 0)).people
      .map((p) => [p.target ? "target" : "other", p.label]));
    assert.deepEqual(await labels(a), { target: "사람 2", other: "사람 1" });
    assert.deepEqual(await labels(b), { target: "사람 1", other: "사람 2" });
    assert.deepEqual(await labels(alone), { target: "사람 1" });
  });
});

test("boxes are deleted with the video after retention; storage waits for migration 0015", async () => {
  await withRepo(async ({ events, review, boxes }) => {
    const old = await incidentWithClip(events, review, {
      startAt: new Date(Date.now() - 8 * 86_400_000).toISOString(),
      endAt: new Date(Date.now() - 8 * 86_400_000 + 30_000).toISOString() });
    const fresh = await incidentWithClip(events, review);
    await boxes.storeFallPeople("robot-a", people({ incidentId: old }));
    await boxes.storeFallPeople("robot-a", people({ incidentId: fresh }));
    assert.equal(await boxes.purgeExpiredFallPeople(), 1);
    assert.equal(await boxes.getFallClipPeople("robot-a", old, 0), null);
    assert.ok(await boxes.getFallClipPeople("robot-a", fresh, 0));
  });
  await withRepo(async ({ events, review, boxes }) => {
    // The incident detail itself needs the users of 0016, which always follows 0015.
    const incidentId = await incidentWithClip(events, review);
    assert.equal(await boxes.purgeExpiredFallPeople(), 0);
    await assert.rejects(boxes.storeFallPeople("robot-a", people({ incidentId })), /MIGRATION_REQUIRED/);
  }, { through: "0014_fall_report_memo" });
});

test("HTTP: device box upload and member-only box reading", async () => {
  const h = await fallDatabase();
  const load = moduleLoader({
    [path.join(h.root, "app/device-auth.ts")]: { async getRequestDevice(req) {
      return req.headers.get("authorization") === "Bearer device-a" ? { deviceId: "robot-a" } : null;
    } },
    [path.join(h.root, "app/server-auth.ts")]: { async getRequestUserId(req) { return testUserId(req.headers.get("x-test-email")); } },
  });
  const pg = load("db/postgres.ts"), events = load("db/fall-incidents.ts"), review = load("db/fall-review.ts");
  const upload = load("app/api/device/v1/fall-incident-people/route.ts");
  const read = load("app/api/devices/[deviceId]/fall-incidents/[incidentId]/clips/[segmentIndex]/people/route.ts");
  const post = (payload, token = "device-a") => new Request("https://web.test/api", {
    method: "POST", body: typeof payload === "string" ? payload : JSON.stringify(payload),
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json", "x-malbut-device-id": "robot-a" },
  });
  const get = (email) => new Request("https://web.test/api", { headers: email ? { "x-test-email": email } : {} });
  const params = (p) => ({ params: Promise.resolve(p) });
  try { await pg.withPostgresPoolForTest(h.pool, async () => {
    const opened = event();
    const boxes = people({ incidentId: opened.incidentId });
    assert.equal((await upload.POST(post(boxes, "wrong"))).status, 401);
    assert.equal((await upload.POST(post({ ...boxes, image: "x" }))).status, 400);
    assert.equal((await upload.POST(post(JSON.stringify({ ...boxes, pad: "x".repeat(262_144) })))).status, 400);
    assert.equal((await upload.POST(post(boxes))).status, 503); // clip not stored yet
    await events.storeFallEvent("robot-a", opened);
    await review.storeFallClip("robot-a", clip({ incidentId: opened.incidentId }));
    const stored = await upload.POST(post(boxes));
    assert.equal(stored.status, 201);
    assert.deepEqual(await stored.json(), { stored: true, incidentId: opened.incidentId, segmentIndex: 0, revision: 1 });
    assert.equal((await upload.POST(post(boxes))).status, 200);
    assert.equal((await upload.POST(post({ ...boxes, cloud: [] }))).status, 409);

    const p = { deviceId: "robot-a", incidentId: opened.incidentId, segmentIndex: "0" };
    assert.equal((await read.GET(get(null), params(p))).status, 401);
    assert.equal((await read.GET(get("stranger@example.com"), params(p))).status, 404);
    assert.equal((await read.GET(get("family@example.com"), params({ ...p, segmentIndex: "32" }))).status, 404);
    assert.equal((await read.GET(get("family@example.com"), params({ ...p, segmentIndex: "1" }))).status, 404);
    const scene = await (await read.GET(get("family@example.com"), params(p))).json();
    assert.equal(scene.people[0].label, "사람 1");
    assert.deepEqual(scene.cloud, boxes.cloud);
  }); } finally { await h.db.close(); }
});

test("사람 표시 screen: off by choice is remembered, boxes load only on playback", async () => {
  const { readFile } = await import("node:fs/promises");
  const panel = await readFile(new URL("../app/components/fall-incidents-panel.tsx", import.meta.url), "utf8");
  for (const text of ["사람 표시", "이 사건의 사람", "다른 사람", "클라우드 AI 추정 위치", "AI 추정",
    "로봇 시계가 바뀌어 시각과 사람 표시가 조금 어긋날 수 있어요."]) assert.ok(panel.includes(text), text);
  assert.match(panel, /localStorage\.getItem\(OVERLAY_KEY\) !== "off"/);
  assert.match(panel, /if \(!wanted \|\| !showPeople \|\| !clip\.hasPeople \|\| loaded\) return;/);
});
