import assert from "node:assert/strict";
import test from "node:test";
import { fallDatabase, moduleLoader } from "./helpers/fall-db-harness.mjs";

const HOUR = 3600_000;
const STORAGE = { deviceId: "robot-a", mode: "storage", channelArn: "arn:channel:a", streamArn: "arn:stream:a" };

async function withMedia(work) {
  const h = await fallDatabase(), load = moduleLoader();
  const pg = load("db/postgres.ts");
  try {
    await pg.withPostgresPoolForTest(h.pool, () => work({
      h, homecam: load("db/homecam.ts"), review: load("db/fall-review.ts"),
    }));
  } finally { await h.db.close(); }
}

/** A storage session that started recording two hours ago; its robot last reported `quietMs` ago. */
async function recordingRobot(h, homecam, quietMs) {
  const session = await homecam.prepareDeviceMediaSession(STORAGE);
  const now = Date.now();
  await h.db.query("UPDATE recording_sessions SET started_at=$2 WHERE session_id=$1",
    [session.id, new Date(now - 2 * HOUR).toISOString()]);
  // Every healthy heartbeat slides the lease to (report time + 1 h).
  await h.db.query("UPDATE stream_sessions SET expires_at=$2 WHERE id=$1",
    [session.id, new Date(now - quietMs + HOUR).toISOString()]);
  return { id: session.id, lastReport: now - quietMs };
}

const endedAt = async (h, id) => Date.parse((await h.db.query(
  "SELECT ended_at FROM recording_sessions WHERE session_id=$1", [id])).rows[0].ended_at);
const near = (actual, expected, label) =>
  assert.ok(Math.abs(actual - expected) < 5_000, `${label}: ${new Date(actual).toISOString()} vs ${new Date(expected).toISOString()}`);

test("a robot that lost power stopped recording at its last report, not when it came back", async () => {
  await withMedia(async ({ h, homecam, review }) => {
    const robot = await recordingRobot(h, homecam, 20 * 60_000);
    const now = Date.now();
    // Still open on the server, but the timeline already ends the span at the last report.
    const timeline = await review.getFallTimeline("robot-a",
      new Date(now - 3 * HOUR).toISOString(), new Date(now + 60_000).toISOString(), now);
    assert.equal(timeline.recordings.length, 1);
    near(Date.parse(timeline.recordings[0].endAt), robot.lastReport, "open span");
    // The robot comes back and opens a new session: the old recording closes at the last report.
    await homecam.prepareDeviceMediaSession(STORAGE);
    near(await endedAt(h, robot.id), robot.lastReport, "closed on restart");
  });
});

test("a robot still reporting keeps recording until now, so a session refresh leaves no gap", async () => {
  await withMedia(async ({ h, homecam, review }) => {
    const robot = await recordingRobot(h, homecam, 1_000);
    const now = Date.now();
    const timeline = await review.getFallTimeline("robot-a",
      new Date(now - 3 * HOUR).toISOString(), new Date(now + 60_000).toISOString(), now);
    assert.equal(timeline.recordings[0].endAt, new Date(now).toISOString());
    await homecam.prepareDeviceMediaSession(STORAGE);
    near(await endedAt(h, robot.id), Date.now(), "refresh");
  });
});

test("a robot quiet for over an hour is closed at its last report when the lease runs out", async () => {
  await withMedia(async ({ h, homecam }) => {
    const robot = await recordingRobot(h, homecam, 65 * 60_000);
    // Any device list or session request expires leases first.
    await homecam.listHomecamDevices("u-owner");
    near(await endedAt(h, robot.id), robot.lastReport, "expired");
  });
});

test("turning recording off ends at once; after a power loss it ends at the last report", async () => {
  await withMedia(async ({ h, homecam }) => {
    const live = await recordingRobot(h, homecam, 1_000);
    await homecam.stopDeviceMediaSession("robot-a", "storage_disabled", live.id);
    near(await endedAt(h, live.id), Date.now(), "owner turned it off");

    const quiet = await recordingRobot(h, homecam, 10 * 60_000);
    await homecam.stopDeviceMediaSession("robot-a", "storage_disabled", quiet.id);
    near(await endedAt(h, quiet.id), quiet.lastReport, "turned off after power loss");
  });
});
