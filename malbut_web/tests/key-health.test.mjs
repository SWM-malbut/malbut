import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fallDatabase, moduleLoader } from "./helpers/fall-db-harness.mjs";

const SECRET = "k".repeat(40);
const ORIGIN = "https://homecam.example.com";
const ENV = { FALL_KEY_ENCRYPTION_SECRET: SECRET, AUTH_PUBLIC_ORIGIN: ORIGIN };
const MINUTE = 60_000;

async function withHealth(work) {
  const h = await fallDatabase();
  const load = moduleLoader({
    [path.join(h.root, "app/runtime-env.ts")]: { getRuntimeEnvironment: () => ENV, getRuntimeValue: (name) => ENV[name] },
    [path.join(h.root, "app/server-auth.ts")]: { async getRequestUserId(request) { return request.headers.get("x-test-user"); } },
  });
  const pg = load("db/postgres.ts");
  // Every key check says yes.
  load("app/service-key-check.ts").setKeyCheckFetchForTest(async (url) =>
    new URL(String(url)).hostname === "apis.data.go.kr" ? Response.json({ response: { header: { resultCode: "00" } } }) : Response.json({}));
  const keys = load("db/service-keys.ts");
  const health = load("app/api/devices/[deviceId]/key-health/route.ts");
  const list = load("app/api/devices/[deviceId]/service-keys/route.ts");
  const one = load("app/api/devices/[deviceId]/service-keys/[service]/route.ts");
  const params = (extra = {}) => ({ params: Promise.resolve({ deviceId: "robot-a", ...extra }) });
  const as = (userId, method = "GET", body) => new Request(`${ORIGIN}/api/devices/robot-a/x`, {
    method, headers: { ...(userId ? { "x-test-user": userId } : {}), ...(method === "GET" ? {} : { "content-type": "application/json", origin: ORIGIN }) },
    ...(method === "GET" ? {} : { body: JSON.stringify(body ?? {}) }),
  });
  const problems = async (userId = "u-family") => (await (await health.GET(as(userId), params())).json()).problems;
  const setKey = (service, apiKey) => one[apiKey ? "PUT" : "DELETE"](as("u-owner", apiKey ? "PUT" : "DELETE", apiKey ? { apiKey } : {}), params({ service }));
  // The robot's sync, `minutesAgo` in the past.
  const report = (healthReport, { known = { openai: 0, kma: 0 }, minutesAgo = 0 } = {}) => keys.syncServiceKeysForDevice({
    deviceId: "robot-a", known, models: { openai: null }, health: healthReport, secret: SECRET,
    now: new Date(Date.now() - minutesAgo * MINUTE),
  });
  try {
    await pg.withPostgresPoolForTest(h.pool, () => work({ h, keys, health, list, as, params, problems, setKey, report }));
  } finally { await h.db.close(); }
}

test("the home screen hears about a key only from a fresh report about the key the web holds", async () => {
  await withHealth(async ({ problems, setKey, report }) => {
    assert.deepEqual(await problems(), []);
    // The team key on the robot (nothing set on the web) stopped working.
    await report({ openai: { state: "invalid", code: "authentication_failed" } });
    assert.deepEqual(await problems(), [{ service: "openai", problem: "invalid" }]);
    // A robot that stopped reporting says nothing about now.
    await report({ openai: { state: "invalid", code: null } }, { minutesAgo: 11 });
    assert.deepEqual(await problems(), []);

    // A new key: the old report no longer counts, nor one sent before the robot fetched it.
    await report({ openai: { state: "invalid", code: null } });
    await setKey("openai", "sk-proj-new-key-1234");
    assert.deepEqual(await problems(), []);
    await report({ openai: { state: "invalid", code: null } }, { known: { openai: 0, kma: 0 } });
    assert.deepEqual(await problems(), [], "the robot still holds the old key");
    await report({ openai: { state: "quota", code: "insufficient_quota" } }, { known: { openai: 1, kma: 0 } });
    assert.deepEqual(await problems(), [{ service: "openai", problem: "quota" }]);
    await report({ openai: { state: "ok", code: null } }, { known: { openai: 1, kma: 0 } });
    assert.deepEqual(await problems(), []);

    // Deleted on the web: missing, whatever the robot says.
    await setKey("openai", null);
    assert.deepEqual(await problems(), [{ service: "openai", problem: "missing" }]);

    // Several at once: conversation first, then the fall check, then weather.
    await report({ kma: { state: "missing", code: "kma_key_required" }, fall: { state: "quota", code: "cloud_quota_exhausted" } },
      { known: { openai: 2, kma: 0 } });
    assert.deepEqual(await problems(), [
      { service: "openai", problem: "missing" }, { service: "fall", problem: "quota" }, { service: "kma", problem: "missing" },
    ]);
  });
});

test("owners and guardians see which keys need checking; the key screen shows the same", async () => {
  await withHealth(async ({ h, health, list, as, params, report }) => {
    await report({ kma: { state: "invalid", code: null } });
    assert.equal((await health.GET(as("u-family"), params())).status, 200);
    assert.equal((await health.GET(as(null), params())).status, 401);
    await h.db.exec("INSERT INTO users(id) VALUES ('u-stranger')");
    assert.equal((await health.GET(as("u-stranger"), params())).status, 404);
    const views = await (await list.GET(as("u-owner"), params())).json();
    assert.deepEqual([views.openai.problem, views.kma.problem, views.fall.problem], [null, "invalid", null]);
    // Only whether something is wrong, never a key.
    assert.deepEqual(Object.keys((await (await health.GET(as("u-family"), params())).json()).problems[0]).sort(), ["problem", "service"]);
  });
});

test("the home notice and the key cards follow mockups 13 and 15", async () => {
  const [notice, screen, dashboard] = await Promise.all([
    readFile(new URL("../app/components/key-health-notice.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/components/service-keys-settings.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/components/homecam-dashboard.tsx", import.meta.url), "utf8"),
  ]);
  for (const text of ["말벗이 지금 대화를 할 수 없어요", "낙상 AI 확인을 할 수 없어요", "말벗이 날씨를 확인할 수 없어요",
    "AI 재확인이 빠져 정확도가 떨어질 수 있어요", "키를 확인해 주세요.", "소유자에게 키를 확인해 달라고 알려 주세요.",
    "도 확인이 필요해요.", "키 확인하기"]) assert.ok(notice.includes(text), text);
  // Conversation and fall AI are red, weather yellow.
  assert.match(notice, /openai: \{[^}]*urgent: true/);
  assert.match(notice, /fall: \{[^}]*urgent: true/);
  assert.match(notice, /kma: \{[^}]*urgent: false/);
  assert.match(notice, /\{isOwner && <button/);
  for (const text of ["쓸 수 없음", "말벗에 미리 들어 있는 팀 키를 쓸 수 없어요", "요금 한도가 찼어요.", "키가 틀렸거나 만료됐어요."]) {
    assert.ok(screen.includes(text), text);
  }
  assert.match(dashboard, /<KeyHealthNotice/);
});
