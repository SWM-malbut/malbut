import assert from "node:assert/strict";
import path from "node:path";
import test from "node:test";
import { fallDatabase, moduleLoader, testUserId } from "./helpers/fall-db-harness.mjs";

// 2026-10-13: the fall check comes before the guardian's talk, from the suspicion
// until its question ends; the way back does not hold the speaker.

async function withTalkRoute(work) {
  const h = await fallDatabase();
  const load = moduleLoader({
    [path.join(h.root, "app/server-auth.ts")]: {
      getRequestUserId: async (r) => testUserId(r.headers.get("x-test-email")),
    },
  });
  try {
    await load("db/postgres.ts").withPostgresPoolForTest(h.pool, async () => {
      const route = load("app/api/devices/[deviceId]/talk-lease/route.ts");
      const talk = () => route.POST(new Request("https://test/api/devices/robot-a/talk-lease", {
        method: "POST",
        headers: { "content-type": "application/json", "x-test-email": "owner@example.com" },
        body: JSON.stringify({ clientId: "viewer-a" }),
      }), { params: Promise.resolve({ deviceId: "robot-a" }) });
      const robot = (detail, ageMs = 0) => h.pool.query(
        `INSERT INTO robot_runtime_state(device_id, state, message, localization_state, nav2_json,
           drive_mode_json, map_revision_counter, observed_at, updated_at)
         VALUES ('robot-a', 'idle', '', 'ok', '{}', $1, 0, $2, $2)
         ON CONFLICT (device_id) DO UPDATE SET drive_mode_json = excluded.drive_mode_json,
           observed_at = excluded.observed_at`,
        [JSON.stringify({ mode: "idle", detail }), new Date(Date.now() - ageMs).toISOString()]);
      await work({ talk, robot });
    });
  } finally { await h.db.close(); }
}

test("talk waits while a fall check holds the speaker, and says why", async () => {
  await withTalkRoute(async ({ talk, robot }) => {
    for (const phase of ["approach", "look", "ask", "wait"]) {
      await robot({ fall_check_active: true, fall_check: { phase } });
      const refused = await talk();
      assert.equal(refused.status, 409, phase);
      assert.deepEqual(await refused.json(),
        { error: "낙상 확인 중이라 잠시 후에 말할 수 있어요.", code: "fall_check" });
    }
  });
});

test("the way back, no check, or a robot not reporting lets the guardian talk", async () => {
  for (const [detail, ageMs] of [
    [{ fall_check_active: true, fall_check: { phase: "return" } }, 0],
    [{ fall_check_active: false }, 0],
    [{ fall_check_active: true, fall_check: { phase: "approach" } }, 60_000],
  ]) {
    await withTalkRoute(async ({ talk, robot }) => {
      await robot(detail, ageMs);
      const response = await talk();
      assert.equal(response.status, 200, JSON.stringify(detail));
      assert.ok((await response.json()).lease.leaseId);
    });
  }
});
