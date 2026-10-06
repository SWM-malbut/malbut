// 지도 탭 › 지도 관리 (SWM25-237): names for the robot's saved maps, kept on the web.
import assert from "node:assert/strict";
import path from "node:path";
import test from "node:test";
import { fallDatabase, moduleLoader } from "./helpers/fall-db-harness.mjs";

const ORIGIN = "https://homecam.example.com";
const ENV = { AUTH_PUBLIC_ORIGIN: ORIGIN };

async function withLabels(work) {
  const h = await fallDatabase();
  const load = moduleLoader({
    [path.join(h.root, "app/runtime-env.ts")]: { getRuntimeEnvironment: () => ENV, getRuntimeValue: (name) => ENV[name] },
    [path.join(h.root, "app/server-auth.ts")]: { async getRequestUserId(request) { return request.headers.get("x-test-user"); } },
  });
  const pg = load("db/postgres.ts");
  const route = load("app/api/devices/[deviceId]/robot/map-labels/route.ts");
  const params = () => ({ params: Promise.resolve({ deviceId: "robot-a" }) });
  const read = async (userId = "u-owner") => route.GET(new Request(`${ORIGIN}/api/devices/robot-a/robot/map-labels`, {
    headers: userId ? { "x-test-user": userId } : {},
  }), params());
  const put = (userId, body, origin = ORIGIN) => route.PUT(new Request(`${ORIGIN}/api/devices/robot-a/robot/map-labels`, {
    method: "PUT",
    headers: { "content-type": "application/json", ...(origin ? { origin } : {}), ...(userId ? { "x-test-user": userId } : {}) },
    body: JSON.stringify(body),
  }), params());
  try {
    await pg.withPostgresPoolForTest(h.pool, () => work({ h, read, put }));
  } finally { await h.db.close(); }
}

test("the owner names a map when making it and renames it later; guardians cannot", async () => {
  await withLabels(async ({ h, read, put }) => {
    assert.deepEqual((await (await read()).json()).labels, {});
    const made = await put("u-owner", { map: "map-20261007-1430.yaml", name: "  지도 10월 7일 14:30 " });
    assert.equal(made.status, 200);
    assert.deepEqual(await made.json(), { map: "map-20261007-1430.yaml", name: "지도 10월 7일 14:30" });
    assert.equal((await put("u-owner", { map: "map-20261007-1430.yaml", name: "1층" })).status, 200);
    assert.deepEqual((await (await read()).json()).labels, { "map-20261007-1430.yaml": "1층" });
    const audit = (await h.db.query("SELECT count(*)::int AS n FROM access_audit_log WHERE action='robot.map_named'")).rows[0].n;
    assert.equal(audit, 2);

    assert.equal((await read("u-family")).status, 403);
    assert.equal((await put("u-family", { map: "home.yaml", name: "거실층" })).status, 403);
    assert.equal((await read(null)).status, 401);
    assert.equal((await put("u-owner", { map: "home.yaml", name: "집" }, "https://evil.example")).status, 403);
  });
});

test("names are 1 to 40 characters for a robot map file", async () => {
  await withLabels(async ({ put }) => {
    for (const [body, status] of [
      [{ map: "home.yaml", name: "" }, 422],
      [{ map: "home.yaml", name: "   " }, 422],
      [{ map: "home.yaml", name: "가".repeat(41) }, 422],
      [{ map: "home.yaml", name: "줄\n바꿈" }, 422],
      [{ map: "../home.yaml", name: "집" }, 400],
      [{ map: "home.png", name: "집" }, 400],
      [{ map: "home.yaml", name: "집", extra: 1 }, 400],
      [{ map: "home.yaml", name: "가".repeat(40) }, 200],
    ]) {
      assert.equal((await put("u-owner", body)).status, status, JSON.stringify(body));
    }
  });
});

test("new maps get a dated name and a free ASCII file name", () => {
  const load = moduleLoader();
  const { newMapNames } = load("app/robot-map-names.ts");
  const at = new Date(2026, 9, 7, 14, 5);
  assert.deepEqual({ ...newMapNames(at, new Set()) }, { label: "지도 10월 7일 14:05", stem: "map-20261007-1405" });
  assert.equal(newMapNames(at, new Set(["map-20261007-1405.yaml"])).stem, "map-20261007-1405-2");
  assert.equal(newMapNames(at, new Set(["map-20261007-1405.yaml", "map-20261007-1405-2.yaml"])).stem,
    "map-20261007-1405-3");
  assert.match(newMapNames(at, new Set()).stem, /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/);
});
