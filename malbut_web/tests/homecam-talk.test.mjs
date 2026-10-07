// 홈캠 › 현재 상태 › 마이크 (목업 20번): the guardian's voice to 말벗, one guardian at a time.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fallDatabase, moduleLoader } from "./helpers/fall-db-harness.mjs";

const ORIGIN = "https://homecam.example.com";

async function withTalk(work) {
  const h = await fallDatabase();
  const load = moduleLoader({
    [path.join(h.root, "app/server-auth.ts")]: {
      async getRequestUserId(request) { return request.headers.get("x-test-user"); },
    },
  });
  const pg = load("db/postgres.ts");
  const route = load("app/api/devices/[deviceId]/talk-lease/route.ts");
  const params = (deviceId) => ({ params: Promise.resolve({ deviceId }) });
  const headers = (userId) => (userId ? { "x-test-user": userId } : {});
  const post = (userId, body, deviceId = "robot-a") => route.POST(new Request(
    `${ORIGIN}/api/devices/${deviceId}/talk-lease`,
    { method: "POST", headers: { "content-type": "application/json", ...headers(userId) }, body: JSON.stringify(body) },
  ), params(deviceId));
  const get = async (userId, clientId, deviceId = "robot-a") => route.GET(new Request(
    `${ORIGIN}/api/devices/${deviceId}/talk-lease${clientId ? `?clientId=${clientId}` : ""}`,
    { headers: headers(userId) },
  ), params(deviceId));
  try {
    await pg.withPostgresPoolForTest(h.pool, () => work({ h, post, get }));
  } finally { await h.db.close(); }
}

test("other guardians see who is talking and wait instead of failing", async () => {
  await withTalk(async ({ h, post, get }) => {
    await h.db.exec("UPDATE users SET display_name='민지' WHERE id='u-family'");
    assert.deepEqual(await (await get("u-owner", "viewer-a")).json(), { holder: null });
    const talking = await post("u-family", { clientId: "viewer-f" });
    assert.equal(talking.status, 200);
    const { lease } = await talking.json();

    assert.deepEqual((await (await get("u-owner", "viewer-a")).json()).holder, { name: "민지", self: false });
    // The talking screen itself is not "someone else"; the same person's other device is.
    assert.equal((await (await get("u-family", "viewer-f")).json()).holder, null);
    assert.deepEqual((await (await get("u-family", "viewer-g")).json()).holder, { name: "민지", self: true });

    const busy = await post("u-owner", { clientId: "viewer-a" });
    assert.equal(busy.status, 409);
    assert.deepEqual(await busy.json(), {
      error: "다른 보호자가 말하는 중이에요.", code: "busy", holder: { name: "민지", self: false },
    });
    assert.equal((await post("u-family", { clientId: "viewer-f", leaseId: lease.leaseId })).status, 200);

    assert.equal((await get(null, "viewer-a")).status, 401);
    assert.equal((await get("u-owner", "viewer-a", "robot-b")).status, 403);
    assert.equal((await get("u-owner", "bad id")).status, 400);
  });
});

test("the server stops renewing a microphone 200 seconds after it started", async () => {
  await withTalk(async ({ h, post }) => {
    const first = (await (await post("u-family", { clientId: "viewer-f" })).json()).lease;
    await h.db.exec("UPDATE talk_leases SET created_at = CURRENT_TIMESTAMP - INTERVAL '199 seconds'");
    assert.equal((await post("u-family", { clientId: "viewer-f", leaseId: first.leaseId })).status, 200);
    await h.db.exec("UPDATE talk_leases SET created_at = CURRENT_TIMESTAMP - INTERVAL '201 seconds'");
    const late = await post("u-family", { clientId: "viewer-f", leaseId: first.leaseId });
    assert.equal(late.status, 409);
    assert.deepEqual(await late.json(), { error: "3분이 지나 마이크를 껐어요.", code: "time_limit" });

    // A new microphone after the old one expired starts its own 3 minutes.
    await h.db.exec("UPDATE talk_leases SET expires_at = CURRENT_TIMESTAMP - INTERVAL '1 second'");
    const next = (await (await post("u-owner", { clientId: "viewer-a" })).json()).lease;
    assert.notEqual(next.leaseId, first.leaseId);
    assert.equal((await post("u-owner", { clientId: "viewer-a", leaseId: next.leaseId })).status, 200);
    const fresh = await h.db.query(
      "SELECT created_at > CURRENT_TIMESTAMP - INTERVAL '10 seconds' AS fresh FROM talk_leases",
    );
    assert.equal(fresh.rows[0].fresh, true);
  });
});

test("the microphone notice under the switch follows mockup 20", async () => {
  const load = moduleLoader();
  const { talkNote, formatTalkRemaining, TALK_LIMIT_MS } = load("app/homecam-talk.ts");
  assert.equal(TALK_LIMIT_MS, 180_000);
  assert.equal(formatTalkRemaining(161_000), "2:41");
  assert.equal(formatTalkRemaining(180_000), "3:00");
  assert.equal(formatTalkRemaining(400), "0:01");
  assert.equal(formatTalkRemaining(-5), "0:00");
  const base = { phase: "off", remainingMs: 180_000, holder: null, timedOut: false, error: "" };
  assert.equal(talkNote(base), null);
  assert.deepEqual(talkNote({ ...base, phase: "talking", remainingMs: 161_000 }), {
    tone: "info", title: "말하는 중 · 2:41 뒤 자동으로 꺼져요",
    text: "내 목소리가 말벗 스피커로 나가요. 그동안 말벗은 듣지도 말하지도 않아요. 하던 말도 멈춰요.",
  });
  assert.equal(talkNote({ ...base, phase: "starting" }).title, "말벗이 말하기를 준비하고 있어요");
  assert.equal(talkNote({ ...base, holder: { name: "민지", self: false } }).title, "민지 님이 말하는 중이에요");
  assert.equal(talkNote({ ...base, holder: { name: "민지", self: true } }).title, "다른 기기에서 말하는 중이에요");
  assert.equal(talkNote({ ...base, timedOut: true }).title, "3분이 지나 마이크를 껐어요");
  // Someone else talking outranks this screen's older timeout or error.
  assert.equal(talkNote({ ...base, timedOut: true, error: "x", holder: { name: "민지", self: false } }).title,
    "민지 님이 말하는 중이에요");
  assert.deepEqual(talkNote({ ...base, error: "말벗이 말하기를 준비하지 못했어요. 잠시 뒤 다시 켜 주세요." }), {
    tone: "danger", title: "마이크를 켜지 못했어요",
    text: "말벗이 말하기를 준비하지 못했어요. 잠시 뒤 다시 켜 주세요.",
  });
});

test("the microphone is a switch that turns itself off, not push-to-talk", async () => {
  const page = await readFile(new URL("../app/components/homecam-app.tsx", import.meta.url), "utf8");
  // A tap elsewhere or window focus no longer ends talking; leaving the page still does.
  assert.doesNotMatch(page, /window\.addEventListener\("(blur|pointerup)"/);
  assert.match(page, /window\.addEventListener\("pagehide", handleRelease\)/);
  assert.match(page, /document\.visibilityState === "hidden"[\s\S]*?releaseTalkLease\(\)/);
  assert.match(page, /if \(!talking\) return;[\s\S]*?window\.setTimeout\(\(\) => \{[\s\S]*?releaseTalkLease\(\);[\s\S]*?setTalkTimedOut\(true\);[\s\S]*?\}, TALK_LIMIT_MS\)/);
  assert.match(page, /talk-lease\?clientId=/);
  assert.match(page, /onTalkChange=\{setLiveTalk\}/);
  assert.match(page, /microphoneNotice && !embedded/);
  // Turning the microphone on is a call: this device's sound comes on in the same tap.
  assert.match(page, /const turnSpeakerOn = async \(\) => \{\s*if \(speakerMuted \|\| soundBlocked\) await toggleSpeaker\(\);/);
  assert.match(page, /const toggleTalk = async \(\) => \{[\s\S]*?void turnSpeakerOn\(\);[\s\S]*?await /);
  // The first use reconnects for the microphone, which can mute the video again.
  assert.match(page, /await turnSpeakerOnRef\.current\(\);\s*await startTalkingRef\.current\(\);/);
});

test("the home summary and the 현재 상태 caption say which microphone is which", async () => {
  const dashboard = await readFile(new URL("../app/components/homecam-dashboard.tsx", import.meta.url), "utf8");
  assert.match(dashboard, /microphoneEnabled \? "말벗 마이크 켜짐" : "말벗 마이크 꺼짐"/);
  assert.doesNotMatch(dashboard, /"마이크 켜짐"|"마이크 꺼짐"/);
  assert.match(dashboard, /마이크를 켜면 스피커도 같이 켜져 통화처럼 서로 말할 수 있어요/);
  assert.match(dashboard, /설정 › 홈캠 설정 › 말벗 마이크에서 정해요\(소유자\)/);
});
