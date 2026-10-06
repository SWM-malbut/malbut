import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import path from "node:path";
import test from "node:test";
import { fallDatabase, moduleLoader, testUserId } from "./helpers/fall-db-harness.mjs";
import { buildFallNotification, isFallNotification } from "../infra/aws/push-broker/fall-notification.mjs";

const T0 = "2026-09-18T00:00:00.000Z";
const at = (ms) => new Date(Date.parse(T0) + ms).toISOString();

function event(change = {}) {
  return { schemaVersion: 1, eventId: randomUUID(), incidentId: randomUUID(), bootId: "boot-1",
    sequence: 1, evidenceRevision: 1, occurredAt: T0, eventKind: "incident_opened", state: "verifying",
    fallSeen: false, assessment: null, answer: null, reason: null, notificationLevel: null, ...change };
}
function notice(change = {}) {
  return event({ eventKind: "notification_requested", state: "help_required",
    answer: "help_request", notificationLevel: "urgent", reason: "help_requested", ...change });
}
// Same shape as the robot journal (malbut_agent_server SqliteFallJournal.append_clip).
function clip(change = {}) {
  return { schemaVersion: 1, incidentId: randomUUID(), bootId: "boot-1", segmentIndex: 0, revision: 1,
    startAt: at(-10_000), endAt: at(20_000), anchorKinds: ["pose_motion"], foundDown: false,
    clockSource: "wall", clockStepped: false, ...change };
}

async function withRepo(work, overrides = {}) {
  const h = await fallDatabase(), load = moduleLoader(overrides);
  const pg = load("db/postgres.ts");
  try {
    await pg.withPostgresPoolForTest(h.pool, () => work({
      h, load, events: load("db/fall-incidents.ts"), review: load("db/fall-review.ts"),
    }));
  } finally { await h.db.close(); }
}

async function recording(h, deviceId, start, end) {
  const id = randomUUID();
  // Only one active session per device: a finished recording is an ended session.
  // An open one belongs to a robot still reporting, whose lease slides to now + 1 h.
  const expiresAt = end === null ? new Date(Date.now() + 3600_000).toISOString() : at(86_400_000);
  await h.db.query(`INSERT INTO stream_sessions(id,room_code,device_id,started_by,started_at,expires_at,status)
    VALUES($1,$2,$3,'device',$4,$5,$6)`, [id, id, deviceId, start, expiresAt, end === null ? "active" : "ended"]);
  await h.db.query(`INSERT INTO recording_sessions(session_id,kvs_stream_arn,kvs_channel_arn,started_at,ended_at)
    VALUES($1,'arn:stream:a','arn:channel:a',$2,$3)`, [id, start, end]);
}

// Raw PGlite rows carry Date objects; keep milliseconds.
const ms = (value) => new Date(value).getTime();

async function outboxCreatedAt(h, incidentId) {
  return ms((await h.db.query(
    "SELECT created_at FROM fall_push_outbox WHERE incident_id=$1 ORDER BY created_at DESC LIMIT 1", [incidentId],
  )).rows[0].created_at);
}

test("merged source keeps history and clips, but no duplicate check or robot reminder", async () => {
  await withRepo(async ({ h, events, review }) => {
    const source = event({ assessment: "suspected_fall" }), target = event();
    await events.storeFallEvent("robot-a", source);
    await review.storeFallClip("robot-a", clip({ incidentId: source.incidentId }));
    await events.storeFallEvent("robot-a", notice({ incidentId: source.incidentId, sequence: 2,
      state: "recheck_required", answer: "no_response", reason: "person_no_response", notificationLevel: "check" }));
    const start = await outboxCreatedAt(h, source.incidentId);
    await review.scheduleFallReminders(start + 180_001);
    const merged = event({ incidentId: source.incidentId, sequence: 3, eventKind: "incident_merged",
      state: "resolved", assessment: "suspected_fall", answer: "no_response",
      reason: "findings_associated", mergedIntoIncidentIds: [target.incidentId] });
    // The source merge can arrive before the target's events (offline upload).
    await events.storeFallEvent("robot-a", merged);
    assert.equal((await events.storeFallEvent("robot-a", merged)).created, false);
    await events.storeFallEvent("robot-a", target);
    const detail = await review.getFallIncidentDetail("robot-a", source.incidentId);
    assert.equal(detail.category, "merged");
    assert.equal(detail.assessment, "suspected_fall");
    assert.equal(detail.needsCheck, false);
    assert.equal(detail.unacknowledged, false);
    assert.deepEqual(detail.mergedIntoIncidentIds, [target.incidentId]);
    assert.equal(detail.robotEvents.length, 3);
    assert.equal(detail.clips.length, 1);
    const ids = async (filter) => (await review.listFallIncidentSummaries("robot-a", filter)).map((i) => i.incidentId);
    assert.deepEqual(await ids("check"), [target.incidentId]);
    assert.deepEqual(await ids("closed"), [source.incidentId]);
    assert.deepEqual(await ids("normal"), []);
    assert.equal(await review.claimFallNotice("robot-a"), null);
    await review.scheduleFallReminders(start + 180_002);
    assert.equal((await h.db.query("SELECT status FROM fall_web_notices WHERE incident_id=$1",
      [source.incidentId])).rows[0].status, "canceled");
    // Earlier alerts remain as historical evidence, not erased by the merge.
    assert.equal((await h.db.query("SELECT count(*)::int AS n FROM fall_push_outbox WHERE incident_id=$1",
      [source.incidentId])).rows[0].n, 1);
  });
});

test("clip contract is strict: wall-clock ranges only, no media or session IDs", () => {
  const { parseFallClip } = moduleLoader()("app/fall-clip-contract.ts");
  assert.ok(parseFallClip(clip()));
  assert.ok(parseFallClip(clip({ anchorKinds: ["pose_found_down", "cloud_window"], foundDown: true })));
  for (const change of [
    { sessionIds: ["s1"] }, { image: "x" }, { schemaVersion: 2 }, { incidentId: "bad" }, { bootId: "" },
    { segmentIndex: 32 }, { segmentIndex: -1 }, { revision: 0 }, { startAt: "2026-09-18T00:00:00Z" },
    { endAt: at(-10_000) }, { endAt: at(116_000) }, { anchorKinds: [] }, { anchorKinds: ["guess"] },
    { anchorKinds: ["pose_motion", "pose_motion"] }, { clockSource: "monotonic" }, { foundDown: "no" },
  ]) assert.equal(parseFallClip(clip(change)), null, JSON.stringify(change));
  const { clockStepped, ...missing } = clip();
  assert.equal(clockStepped, false);
  assert.equal(parseFallClip(missing), null);
});

test("clip storage keeps the newest revision and waits for the incident instead of blocking", async () => {
  await withRepo(async ({ h, events, review }) => {
    const opened = event();
    const first = clip({ incidentId: opened.incidentId });
    await assert.rejects(review.storeFallClip("robot-a", first), /FALL_CLIP_INCIDENT_MISSING/);
    await events.storeFallEvent("robot-a", opened);
    assert.equal((await review.storeFallClip("robot-a", first)).created, true);
    assert.equal((await review.storeFallClip("robot-a", first)).created, false);
    await assert.rejects(review.storeFallClip("robot-a", { ...first, endAt: at(25_000) }), /FALL_CLIP_CONFLICT/);
    await assert.rejects(review.storeFallClip("robot-a", { ...first, bootId: "boot-2", revision: 2 }), /CONFLICT/);
    const wider = { ...first, revision: 2, endAt: at(40_000), anchorKinds: ["pose_motion", "cloud_window"] };
    await review.storeFallClip("robot-a", wider);
    // A late older revision is acknowledged without overwriting.
    assert.deepEqual(await review.storeFallClip("robot-a", first),
      { stored: true, incidentId: first.incidentId, segmentIndex: 0, revision: 1, created: false });
    const rows = (await h.db.query("SELECT revision,end_at,anchor_kinds FROM fall_incident_clips")).rows
      .map((r) => ({ ...r, end_at: new Date(r.end_at).toISOString() }));
    assert.deepEqual(rows, [{ revision: 2, end_at: at(40_000), anchor_kinds: ["pose_motion", "cloud_window"] }]);
    // Same IDs on another device are another incident.
    await assert.rejects(review.storeFallClip("robot-b", wider), /INCIDENT_MISSING/);
  });
});

test("playback state: preparing, available, partial, unavailable, expired", () => {
  const { clipPlaybackState: state } = moduleLoader()("db/fall-review.ts");
  const start = at(0), end = at(30_000), now = Date.parse(T0) + 60_000;
  const span = (a, b) => ({ start: Date.parse(T0) + a, end: b === null ? null : Date.parse(T0) + b, streamArn: "x" });
  assert.equal(state(start, end, [span(-5_000, null)], Date.parse(T0) + 40_000), "preparing");
  assert.equal(state(start, end, [span(-5_000, null)], now), "available");
  assert.equal(state(start, end, [span(-5_000, 10_000), span(10_500, 50_000)], now), "available");
  assert.equal(state(start, end, [span(5_000, 50_000)], now), "partial");
  assert.equal(state(start, end, [span(-5_000, 10_000), span(20_000, 50_000)], now), "partial");
  assert.equal(state(start, end, [], now), "unavailable");
  assert.equal(state(start, end, [span(-5_000, null)], Date.parse(T0) + 8 * 86_400_000), "expired");
});

test("incident list filters: needs check, AI failure, normal awaiting review, reports, closed", async () => {
  await withRepo(async ({ events, review }) => {
    const urgent = notice();
    await events.storeFallEvent("robot-a", urgent);
    const failed = event();
    await events.storeFallEvent("robot-a", failed);
    await events.storeFallEvent("robot-a", event({ incidentId: failed.incidentId, sequence: 2,
      eventKind: "analysis_unavailable", reason: "cloud_timeout" }));
    const normal = event();
    await events.storeFallEvent("robot-a", normal);
    await events.storeFallEvent("robot-a", event({ incidentId: normal.incidentId, sequence: 2,
      eventKind: "incident_resolved", state: "resolved", reason: "normal_verified",
      answer: "okay", assessment: "normal_activity" }));
    const report = await review.reportMissedFall("robot-a", "u-family", T0, Date.parse(T0) + 60_000);
    const ids = async (filter) => (await review.listFallIncidentSummaries("robot-a", filter)).map((i) => i.incidentId).sort();
    assert.deepEqual(await ids("check"), [urgent.incidentId, failed.incidentId].sort());
    assert.deepEqual(await ids("normal"), [normal.incidentId]);
    assert.deepEqual(await ids("report"), [report.incidentId]);
    assert.deepEqual(await ids("closed"), []);
    assert.equal((await ids("all")).length, 4);
    const byId = Object.fromEntries((await review.listFallIncidentSummaries("robot-a")).map((i) => [i.incidentId, i]));
    assert.equal(byId[failed.incidentId].aiFailed, true);
    assert.equal(byId[failed.incidentId].needsCheck, true);
    // AI "normal" still awaits a human: not closed, not in 확인 필요.
    assert.equal(byId[normal.incidentId].reviewPending, true);
    assert.equal(byId[normal.incidentId].reviewState, "open");
    assert.equal(byId[report.incidentId].origin, "user_report");
    // List cards: alert counts, scene state and same-scene incidents.
    assert.deepEqual(byId[urgent.incidentId].notification, { level: "urgent", sent: 1, total: 3 });
    assert.equal(byId[failed.incidentId].notification, null);
    assert.equal(byId[urgent.incidentId].sceneState, null);
    assert.equal(byId[report.incidentId].sceneState, "expired");
    assert.equal(byId[report.incidentId].linkedCount, 0);
    // Closing needs at least one opinion (anyone's).
    await assert.rejects(review.closeFallIncident("robot-a", normal.incidentId, "u-owner"), /NEEDS_OPINION/);
    await review.setFallOpinion("robot-a", normal.incidentId, "u-family", "normal", null);
    await review.closeFallIncident("robot-a", normal.incidentId, "u-owner");
    assert.deepEqual(await ids("closed"), [normal.incidentId]);
    assert.equal((await review.listFallIncidentSummaries("robot-a", "normal"))[0].reviewPending, false);
    assert.deepEqual(await review.listFallIncidentSummaries("robot-b"), []);
  });
});

test("missed-fall report is −10/+20 s, recorded only, and limited to the retention window", async () => {
  await withRepo(async ({ h, review }) => {
    const now = Date.parse(T0) + 60_000;
    const { incidentId } = await review.reportMissedFall("robot-a", "u-family", T0, now);
    const detail = await review.getFallIncidentDetail("robot-a", incidentId);
    assert.deepEqual(detail.clips.map((c) => [c.startAt, c.endAt]), [[at(-10_000), at(20_000)]]);
    assert.equal(detail.reportedBy, "u-family");
    // Without a chosen name, people are shown by their login email.
    assert.equal(detail.reportedByName, "family@example.com");
    assert.deepEqual(detail.notifications, []);
    assert.equal((await h.db.query("SELECT * FROM fall_web_notices")).rows.length, 0);
    for (const moment of [at(120_000), at(-8 * 86_400_000), "2026-09-18T00:00:00Z", "bad"]) {
      await assert.rejects(review.reportMissedFall("robot-a", "u-owner", moment, now), /FALL_REPORT/);
    }
  });
});

test("detail shows clips, automatic judgments, notifications, all opinions and linked incidents", async () => {
  await withRepo(async ({ h, events, review }) => {
    // Playback state is computed against the real clock: use recent times.
    const recent = (offset) => new Date(Math.floor(Date.now() / 1000) * 1000 - 120_000 + offset).toISOString();
    const a = notice(), b = event();
    await events.storeFallEvent("robot-a", a);
    await events.storeFallEvent("robot-a", b);
    await review.storeFallClip("robot-a", clip({ incidentId: a.incidentId, startAt: recent(-10_000), endAt: recent(20_000) }));
    await review.storeFallClip("robot-a", clip({ incidentId: b.incidentId, startAt: recent(0), endAt: recent(30_000),
      anchorKinds: ["pose_found_down"], foundDown: true }));
    await recording(h, "robot-a", recent(-60_000), null);
    await review.setFallOpinion("robot-a", a.incidentId, "u-owner", "fall", "바닥에 누워 있음");
    await review.setFallOpinion("robot-a", a.incidentId, "u-family", "suspected_fall", null);
    const detail = await review.getFallIncidentDetail("robot-a", a.incidentId);
    assert.equal(detail.clips[0].playbackState, "available");
    assert.deepEqual(detail.robotEvents.map((e) => e.eventKind), ["notification_requested"]);
    assert.deepEqual(detail.notifications.map((n) => [n.kind, n.level]), [["first", "urgent"]]);
    assert.deepEqual(detail.opinions.map((o) => [o.userId, o.userName, o.label]),
      [["u-owner", "owner@example.com", "fall"], ["u-family", "family@example.com", "suspected_fall"]]);
    assert.deepEqual(detail.opinionCounts, { fall: 1, suspected_fall: 1 });
    assert.deepEqual(detail.linkedIncidentIds, [b.incidentId]);
    assert.equal((await review.getFallIncidentDetail("robot-a", b.incidentId)).foundDown, true);
    assert.equal(await review.getFallIncidentDetail("robot-b", a.incidentId), null);
  });
});

test("closing records the labels; only a new label reopens and notifies everyone", async () => {
  await withRepo(async ({ h, events, review }) => {
    const opened = event();
    await events.storeFallEvent("robot-a", opened);
    const id = opened.incidentId;
    await review.setFallOpinion("robot-a", id, "u-owner", "normal", null);
    assert.deepEqual(await review.closeFallIncident("robot-a", id, "u-family"), { closed: true, changed: true });
    assert.deepEqual(await review.closeFallIncident("robot-a", id, "u-owner"), { closed: true, changed: false });
    // Same label: recorded only.
    assert.equal((await review.setFallOpinion("robot-a", id, "u-family", "normal", "괜찮아 보임")).reopened, false);
    assert.equal((await review.getFallIncidentDetail("robot-a", id)).reviewState, "closed");
    // Clearing an opinion never reopens.
    assert.equal((await review.setFallOpinion("robot-a", id, "u-family", null, null)).reopened, false);
    const result = await review.setFallOpinion("robot-a", id, "u-family", "fall", null);
    assert.equal(result.reopened, true);
    const detail = await review.getFallIncidentDetail("robot-a", id);
    assert.equal(detail.reviewState, "open");
    assert.ok(detail.reopenedAt);
    assert.deepEqual(detail.activity.map((a) => a.action),
      ["opinion_set", "closed", "opinion_set", "opinion_cleared", "opinion_set", "reopened"]);
    const notices = (await h.db.query("SELECT notice_id,kind,level,reason,status FROM fall_web_notices")).rows;
    assert.deepEqual(notices.map(({ notice_id, ...n }) => (assert.equal(notice_id, result.noticeId), n)),
      [{ kind: "reopen", level: "check", reason: "reopened_by_opinion", status: "pending" }]);
    // Closing again cancels the pending notice.
    await review.closeFallIncident("robot-a", id, "u-owner");
    assert.equal((await h.db.query("SELECT status FROM fall_web_notices")).rows[0].status, "canceled");
    await assert.rejects(review.setFallOpinion("robot-a", randomUUID(), "u-owner", "fall", null), /NOT_FOUND/);
  });
});

test("urgent: [재발신] at 2 and 4 minutes, then flagged as nobody checked", async () => {
  await withRepo(async ({ h, events, review }) => {
    const urgent = notice();
    await events.storeFallEvent("robot-a", urgent);
    const t = await outboxCreatedAt(h, urgent.incidentId);
    const rounds = async () => (await h.db.query(
      "SELECT round,kind,level,reason FROM fall_web_notices ORDER BY round")).rows;
    assert.equal((await review.scheduleFallReminders(t + 119_000)).created, 0);
    assert.equal((await review.scheduleFallReminders(t + 120_000)).created, 1);
    assert.equal((await review.scheduleFallReminders(t + 130_000)).created, 0);
    assert.equal((await review.scheduleFallReminders(t + 240_000)).created, 1);
    assert.deepEqual(await rounds(), [
      { round: 2, kind: "resend", level: "urgent", reason: "help_requested" },
      { round: 3, kind: "resend", level: "urgent", reason: "help_requested" }]);
    await review.scheduleFallReminders(t + 359_000);
    assert.equal((await review.listFallIncidentSummaries("robot-a"))[0].unacknowledged, false);
    await review.scheduleFallReminders(t + 360_000);
    assert.equal((await review.scheduleFallReminders(t + 600_000)).created, 0);
    // "아무도 확인하지 않음" comes first in every list.
    await events.storeFallEvent("robot-a", event());
    const list = await review.listFallIncidentSummaries("robot-a");
    assert.equal(list[0].incidentId, urgent.incidentId);
    assert.equal(list[0].unacknowledged, true);
    // An opinion acknowledges and clears the flag.
    await review.setFallOpinion("robot-a", urgent.incidentId, "u-family", "fall", null);
    assert.equal((await review.listFallIncidentSummaries("robot-a", "check"))
      .find((i) => i.incidentId === urgent.incidentId).unacknowledged, false);
  });
});

test("one opinion stops reminders for everyone; info has none; check is 3 min × 2", async () => {
  await withRepo(async ({ h, events, review }) => {
    const urgent = notice();
    await events.storeFallEvent("robot-a", urgent);
    const t = await outboxCreatedAt(h, urgent.incidentId);
    await review.scheduleFallReminders(t + 120_000);
    await review.setFallOpinion("robot-a", urgent.incidentId, "u-owner", "suspected_fall", null);
    assert.equal((await h.db.query("SELECT status FROM fall_web_notices")).rows[0].status, "canceled");
    assert.equal((await review.scheduleFallReminders(t + 240_000)).created, 0);
    assert.equal((await review.scheduleFallReminders(t + 600_000)).created, 0);
    assert.equal((await review.listFallIncidentSummaries("robot-a"))[0].unacknowledged, false);

    const info = notice({ notificationLevel: "info", reason: "fall_observed_person_okay", fallSeen: true,
      answer: "okay", state: "verifying" });
    await events.storeFallEvent("robot-a", info);
    assert.equal((await review.scheduleFallReminders(Date.now() + 600_000)).created, 0);

    const check = notice({ notificationLevel: "check", reason: "person_no_response", answer: "no_response",
      state: "recheck_required" });
    await events.storeFallEvent("robot-a", check);
    const c = await outboxCreatedAt(h, check.incidentId);
    assert.equal((await review.scheduleFallReminders(c + 120_000)).created, 0);
    assert.equal((await review.scheduleFallReminders(c + 180_000)).created, 1);
    assert.equal((await review.scheduleFallReminders(c + 360_000)).created, 0);
    const flagged = (await review.listFallIncidentSummaries("robot-a", "check"))
      .find((i) => i.incidentId === check.incidentId);
    assert.equal(flagged.unacknowledged, true);
  });
});

test("escalation to urgent starts a new cycle and cancels the lower one; closing stops all", async () => {
  await withRepo(async ({ h, events, review }) => {
    const check = notice({ notificationLevel: "check", reason: "person_no_response", answer: "no_response",
      state: "recheck_required" });
    await events.storeFallEvent("robot-a", check);
    const c = await outboxCreatedAt(h, check.incidentId);
    await review.scheduleFallReminders(c + 180_000);
    // Leave the check resend pending (undelivered), then the robot escalates.
    await events.storeFallEvent("robot-a", notice({ incidentId: check.incidentId, sequence: 2 }));
    const u = await outboxCreatedAt(h, check.incidentId);
    assert.equal((await review.scheduleFallReminders(u + 1_000)).created, 0);
    assert.deepEqual((await h.db.query("SELECT level,status FROM fall_web_notices ORDER BY created_at")).rows,
      [{ level: "check", status: "canceled" }]);
    assert.equal((await review.scheduleFallReminders(u + 120_000)).created, 1);
    assert.equal((await h.db.query("SELECT level,round FROM fall_web_notices WHERE status='pending'")).rows[0].level,
      "urgent");
    await review.setFallOpinion("robot-a", check.incidentId, "u-owner", "fall", null);
    await review.closeFallIncident("robot-a", check.incidentId, "u-owner");
    assert.equal((await h.db.query("SELECT 1 FROM fall_web_notices WHERE status='pending'")).rows.length, 0);
    assert.equal((await review.scheduleFallReminders(u + 240_000)).created, 0);
  });
});

test("reopen notice gets one [재발신] after 3 min unless someone answers", async () => {
  await withRepo(async ({ h, events, review }) => {
    const opened = event();
    await events.storeFallEvent("robot-a", opened);
    const id = opened.incidentId;
    await review.setFallOpinion("robot-a", id, "u-family", "normal", null);
    await review.closeFallIncident("robot-a", id, "u-owner");
    await review.setFallOpinion("robot-a", id, "u-owner", "fall", null);
    const r = ms((await h.db.query("SELECT created_at FROM fall_web_notices")).rows[0].created_at);
    // The reopening opinion itself does not acknowledge the new cycle.
    assert.equal((await review.scheduleFallReminders(r + 180_000)).created, 1);
    const resend = (await h.db.query("SELECT kind,round,level,reason FROM fall_web_notices WHERE kind='resend'")).rows;
    assert.deepEqual(resend, [{ kind: "resend", round: 2, level: "check", reason: "reopened_by_opinion" }]);
    await review.setFallOpinion("robot-a", id, "u-family", "fall", null);
    assert.equal((await h.db.query("SELECT status FROM fall_web_notices WHERE kind='resend'")).rows[0].status, "canceled");
  });
});

test("notification payload: [재발신] keeps level and message; robots cannot send web-only reasons", () => {
  const base = { deviceId: "robot-a", notificationId: randomUUID(), incidentId: randomUUID(),
    level: "urgent", reason: "help_requested", occurredAt: T0 };
  const first = buildFallNotification(base), again = buildFallNotification({ ...base, resend: true });
  assert.equal(again.body, `[재발신] ${first.body}`);
  assert.equal(again.data.resend, "true");
  assert.equal(again.data.level, "urgent");
  assert.ok(isFallNotification(again));
  assert.equal(isFallNotification({ ...again, body: first.body }), false);
  assert.equal(buildFallNotification({ ...base, resend: "yes" }), null);
  const reopen = buildFallNotification({ ...base, level: "check", reason: "reopened_by_opinion" });
  assert.match(reopen.body, /다른 의견/);
  assert.equal(buildFallNotification({ ...base, reason: "reopened_by_opinion" }), null);
  const { parseFallEvent } = moduleLoader()("app/fall-contract.ts");
  assert.equal(parseFallEvent(notice({ notificationLevel: "check", reason: "reopened_by_opinion" })), null);
});

test("reminder delivery uses the shared push boundary with the [재발신] flag", async () => {
  const root = (await import("node:url")).fileURLToPath(new URL("../", import.meta.url));
  const sent = [], finishes = [];
  const load = moduleLoader({
    [path.join(root, "db/fall-review.ts")]: {
      async claimFallNotice() {
        return { deviceId: "robot-a", noticeId: randomUUID(), incidentId: randomUUID(), kind: "resend",
          level: "urgent", reason: "help_requested", occurredAt: T0, leaseId: "l", subscriptionResults: {} };
      },
      async recordFallNoticeResults() {},
      async finishFallNotice(c, complete, error) { finishes.push({ complete, error }); return true; },
    },
    [path.join(root, "app/push-broker.ts")]: { async dispatchFallPush(input, hooks) {
      sent.push(input); await hooks.beforeBatch();
      return { dispatched: true, delivered: 1, failed: 0, pruned: 0 };
    } },
  });
  const worker = load("app/fall-event-push.ts");
  assert.equal((await worker.deliverPendingFallNotice()).accepted, true);
  assert.equal(sent[0].resend, true);
  assert.ok(buildFallNotification(sent[0]));
  assert.deepEqual(finishes, [{ complete: true, error: null }]);
});

test("HTTP: device clip upload, member-only review routes and same-origin mutations", async () => {
  const h = await fallDatabase();
  const root = h.root;
  const delivered = [];
  const load = moduleLoader({
    [path.join(root, "app/device-auth.ts")]: { async getRequestDevice(req) {
      return req.headers.get("authorization") === "Bearer device-a" ? { deviceId: "robot-a" } : null;
    } },
    [path.join(root, "app/server-auth.ts")]: { async getRequestUserId(req) { return testUserId(req.headers.get("x-test-email")); } },
    [path.join(root, "app/runtime-env.ts")]: { getRuntimeEnvironment() { return {}; } },
    [path.join(root, "app/fall-event-push.ts")]: {
      async deliverPendingFallNotice(input) { delivered.push(input); return { processed: true }; },
      async deliverPendingFallPush() { return { processed: false }; },
    },
  });
  const pg = load("db/postgres.ts"), events = load("db/fall-incidents.ts");
  const clips = load("app/api/device/v1/fall-incident-clips/route.ts");
  const list = load("app/api/devices/[deviceId]/fall-incidents/route.ts");
  const detail = load("app/api/devices/[deviceId]/fall-incidents/[incidentId]/route.ts");
  const opinion = load("app/api/devices/[deviceId]/fall-incidents/[incidentId]/opinion/route.ts");
  const close = load("app/api/devices/[deviceId]/fall-incidents/[incidentId]/close/route.ts");
  const reports = load("app/api/devices/[deviceId]/fall-reports/route.ts");
  const upload = (payload, token = "device-a") => new Request("https://web.test/api", {
    method: "POST", body: JSON.stringify(payload), headers: { authorization: `Bearer ${token}`,
      "content-type": "application/json", "x-malbut-device-id": "robot-a" },
  });
  const user = (email, method = "GET", body, origin = "https://web.test") => new Request("https://web.test/api", {
    method, ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    headers: { "x-test-email": email, "content-type": "application/json", origin },
  });
  const params = (p) => ({ params: Promise.resolve(p) });
  try { await pg.withPostgresPoolForTest(h.pool, async () => {
    const opened = event();
    const range = clip({ incidentId: opened.incidentId });
    assert.equal((await clips.POST(upload(range, "wrong"))).status, 401);
    assert.equal((await clips.POST(upload({ ...range, sessionIds: [] }))).status, 400);
    assert.equal((await clips.POST(upload(range))).status, 503); // incident not stored yet
    await events.storeFallEvent("robot-a", opened);
    const stored = await clips.POST(upload(range));
    assert.equal(stored.status, 201);
    assert.deepEqual(await stored.json(), { stored: true, incidentId: range.incidentId, segmentIndex: 0, revision: 1 });
    assert.equal((await clips.POST(upload(range))).status, 200);
    assert.equal((await clips.POST(upload({ ...range, endAt: at(21_000) }))).status, 409);

    const p = { deviceId: "robot-a", incidentId: opened.incidentId };
    assert.equal((await list.GET(user("stranger@example.com"), params({ deviceId: "robot-a" }))).status, 404);
    assert.equal((await list.GET(new Request("https://web.test/api?filter=bogus",
      { headers: { "x-test-email": "family@example.com" } }), params({ deviceId: "robot-a" }))).status, 400);
    const listed = await (await list.GET(new Request("https://web.test/api?filter=check",
      { headers: { "x-test-email": "family@example.com" } }), params({ deviceId: "robot-a" }))).json();
    assert.deepEqual(listed.incidents.map((i) => i.incidentId), [opened.incidentId]);
    assert.equal((await detail.GET(user("stranger@example.com"), params(p))).status, 404);
    assert.equal((await detail.GET(user("family@example.com"), params({ ...p, incidentId: "x" }))).status, 404);
    const body = await (await detail.GET(user("family@example.com"), params(p))).json();
    assert.equal(body.incident.clips.length, 1);

    assert.equal((await opinion.PUT(user("family@example.com", "PUT", { label: "fall" }, "https://evil.test"),
      params(p))).status, 403);
    assert.equal((await opinion.PUT(user("family@example.com", "PUT", { label: "maybe" }), params(p))).status, 400);
    assert.equal((await opinion.PUT(user("family@example.com", "PUT", { label: "fall", memo: "x".repeat(501) }),
      params(p))).status, 400);
    const early = await close.POST(user("family@example.com", "POST", {}), params(p));
    assert.equal(early.status, 409);
    assert.equal((await early.json()).reason, "needs_opinion");
    assert.equal((await opinion.PUT(user("family@example.com", "PUT", { label: "normal", memo: " 괜찮음 " }),
      params(p))).status, 200);
    assert.equal((await close.POST(user("family@example.com", "POST", {}), params(p))).status, 200);
    const reopened = await (await opinion.PUT(user("owner@example.com", "PUT", { label: "fall" }), params(p))).json();
    assert.deepEqual(reopened, { saved: true, reopened: true });
    assert.equal(delivered.length, 1);
    const after = await (await detail.GET(user("owner@example.com"), params(p))).json();
    assert.deepEqual(after.incident.opinions.map((o) => [o.label, o.memo]), [["normal", "괜찮음"], ["fall", null]]);

    assert.equal((await reports.POST(user("family@example.com", "POST", { momentAt: "bad" }),
      params({ deviceId: "robot-a" }))).status, 400);
    const moment = new Date(Math.floor(Date.now() / 1000) * 1000 - 60_000).toISOString();
    const created = await reports.POST(user("family@example.com", "POST", { momentAt: moment }),
      params({ deviceId: "robot-a" }));
    assert.equal(created.status, 201);
    assert.equal((await reports.POST(user("stranger@example.com", "POST", { momentAt: moment }),
      params({ deviceId: "robot-a" }))).status, 404);
  }); } finally { await h.db.close(); }
});

test("clip playback answers expired / no recording / preparing without calling KVS", async () => {
  const h = await fallDatabase();
  const broker = [];
  const load = moduleLoader({
    [path.join(h.root, "app/server-auth.ts")]: { async getRequestUserId(req) { return testUserId(req.headers.get("x-test-email")); } },
    [path.join(h.root, "app/kvs-broker.ts")]: { async requestBrokerEventPlayback(input) { broker.push(input); throw new Error("no"); } },
  });
  const pg = load("db/postgres.ts"), events = load("db/fall-incidents.ts"), review = load("db/fall-review.ts");
  const playback = load("app/api/devices/[deviceId]/fall-incidents/[incidentId]/clips/[segmentIndex]/playback/route.ts");
  const call = (incidentId, segmentIndex = "0", email = "family@example.com") => playback.POST(
    new Request("https://web.test/api", { method: "POST", headers: { "x-test-email": email } }),
    { params: Promise.resolve({ deviceId: "robot-a", incidentId, segmentIndex }) });
  const now = Math.floor(Date.now() / 1000) * 1000;
  const iso = (offset) => new Date(now + offset).toISOString();
  try { await pg.withPostgresPoolForTest(h.pool, async () => {
    const old = event(), missing = event(), fresh = event();
    for (const e of [old, missing, fresh]) await events.storeFallEvent("robot-a", e);
    await review.storeFallClip("robot-a", clip({ incidentId: old.incidentId, startAt: iso(-8 * 86_400_000),
      endAt: iso(-8 * 86_400_000 + 30_000) }));
    await review.storeFallClip("robot-a", clip({ incidentId: missing.incidentId, startAt: iso(-120_000), endAt: iso(-90_000) }));
    await review.storeFallClip("robot-a", clip({ incidentId: fresh.incidentId, startAt: iso(-10_000), endAt: iso(20_000) }));
    assert.equal((await call(old.incidentId)).status, 410);
    assert.equal((await call(missing.incidentId)).status, 404);
    assert.equal((await call(fresh.incidentId)).status, 425);
    assert.equal((await call(fresh.incidentId, "32")).status, 404);
    assert.equal((await call(fresh.incidentId, "0", "stranger@example.com")).status, 404);
    assert.equal(broker.length, 0);
  }); } finally { await h.db.close(); }
});


test("scene still (정지 사진): member-only JPEG of the suspected moment, never logged as viewing", async () => {
  const h = await fallDatabase();
  const broker = [];
  let answer = (input) => [{ at: input.startAt, jpegBase64: Buffer.from("jpeg-bytes").toString("base64"), error: null }];
  const load = moduleLoader({
    [path.join(h.root, "app/server-auth.ts")]: { async getRequestUserId(req) { return testUserId(req.headers.get("x-test-email")); } },
    [path.join(h.root, "app/runtime-env.ts")]: { getRuntimeEnvironment() { return {}; } },
    [path.join(h.root, "app/kvs-device-config.ts")]: { resolveDeviceKvsResources() { return { streamArn: "arn:stream:a" }; } },
    [path.join(h.root, "app/kvs-broker.ts")]: { async requestBrokerImages(input) { broker.push(input); return answer(input); } },
  });
  const pg = load("db/postgres.ts"), events = load("db/fall-incidents.ts"), review = load("db/fall-review.ts");
  const still = load("app/api/devices/[deviceId]/fall-incidents/[incidentId]/clips/[segmentIndex]/still/route.ts");
  const call = (incidentId, segmentIndex = "0", email = "family@example.com") => still.GET(
    new Request("https://web.test/api", { headers: { "x-test-email": email } }),
    { params: Promise.resolve({ deviceId: "robot-a", incidentId, segmentIndex }) });
  const now = Math.floor(Date.now() / 1000) * 1000;
  const iso = (offset) => new Date(now + offset).toISOString();
  try { await pg.withPostgresPoolForTest(h.pool, async () => {
    await recording(h, "robot-a", iso(-3600_000), null);
    const seen = event({ occurredAt: iso(-115_000) }), other = event(), unrecorded = event();
    for (const e of [seen, other, unrecorded]) await events.storeFallEvent("robot-a", e);
    await review.storeFallClip("robot-a", clip({ incidentId: seen.incidentId, startAt: iso(-120_000), endAt: iso(-90_000) }));
    await review.storeFallClip("robot-a", clip({ incidentId: other.incidentId, startAt: iso(-300_000), endAt: iso(-270_000) }));
    await review.storeFallClip("robot-a", clip({ incidentId: unrecorded.incidentId, startAt: iso(-8 * 3600_000),
      endAt: iso(-8 * 3600_000 + 30_000) }));

    const ok = await call(seen.incidentId);
    assert.equal(ok.status, 200);
    assert.equal(ok.headers.get("content-type"), "image/jpeg");
    assert.match(ok.headers.get("cache-control"), /^private/);
    assert.equal(Buffer.from(await ok.arrayBuffer()).toString(), "jpeg-bytes");
    // The suspected moment itself, two samples over half a second.
    assert.deepEqual(broker[0], { deviceId: "robot-a", streamArn: "arn:stream:a", startAt: iso(-115_000),
      endAt: iso(-114_500), count: 2 });
    // A clip that does not hold the incident's moment uses its own anchor, 10 s after its start.
    assert.equal((await call(other.incidentId)).status, 200);
    assert.equal(broker[1].startAt, iso(-290_000));

    assert.equal((await call(unrecorded.incidentId)).status, 404);
    assert.equal((await call(seen.incidentId, "32")).status, 404);
    assert.equal((await call(seen.incidentId, "0", "stranger@example.com")).status, 404);
    assert.equal(broker.length, 2);

    // No frame archived for that moment yet: no photo (the video can still play).
    answer = (input) => [{ at: input.startAt, jpegBase64: null, error: "NO_MEDIA" }];
    assert.equal((await call(seen.incidentId)).status, 404);
    answer = () => { throw new Error("KVS_BROKER_404"); };
    assert.equal((await call(seen.incidentId)).status, 404);
    answer = () => { throw new Error("KVS_BROKER_503"); };
    assert.equal((await call(seen.incidentId)).status, 503);

    // Looking at the still is not a viewing record; only playing the video is.
    assert.equal((await h.db.query("SELECT count(*)::int AS n FROM access_audit_log")).rows[0].n, 0);
  }); } finally { await h.db.close(); }
});

test("review fixes: microsecond timestamps, robot-normal reminders, reopened normal needs check, null recording start", async () => {
  await withRepo(async ({ h, events, review }) => {
    // Real Postgres stores microseconds; the reopening opinion must still not acknowledge.
    const opened = event();
    await events.storeFallEvent("robot-a", opened);
    await review.setFallOpinion("robot-a", opened.incidentId, "u-family", "normal", null);
    await review.closeFallIncident("robot-a", opened.incidentId, "u-owner");
    await review.setFallOpinion("robot-a", opened.incidentId, "u-owner", "fall", null);
    await h.db.query("UPDATE fall_web_notices SET created_at='2026-09-18T00:00:00.123Z'");
    await h.db.query(`UPDATE fall_incident_activity SET created_at='2026-09-18T00:00:00.123456Z'
      WHERE action='opinion_set' AND label='fall'`);
    await h.db.query(`UPDATE fall_incident_activity SET created_at='2026-09-17T23:59:00.000Z'
      WHERE action IN ('opinion_set','closed') AND label IS DISTINCT FROM 'fall'`);
    assert.equal((await review.scheduleFallReminders(Date.parse("2026-09-18T00:03:00.123Z"))).created, 1);

    // Check alert, then the robot verifies normal: no reminders, no "nobody checked".
    const check = notice({ notificationLevel: "check", reason: "person_no_response", answer: "no_response",
      state: "recheck_required" });
    await events.storeFallEvent("robot-a", check);
    const c = await outboxCreatedAt(h, check.incidentId);
    await review.scheduleFallReminders(c + 180_000);
    await events.storeFallEvent("robot-a", event({ incidentId: check.incidentId, sequence: 2,
      eventKind: "incident_resolved", state: "resolved", reason: "normal_verified", answer: "okay",
      assessment: "normal_activity" }));
    assert.equal((await review.scheduleFallReminders(c + 360_000)).created, 0);
    assert.deepEqual((await h.db.query("SELECT status FROM fall_web_notices WHERE incident_id=$1",
      [check.incidentId])).rows, [{ status: "canceled" }]);
    const normal = (await review.listFallIncidentSummaries("robot-a", "normal"))
      .find((i) => i.incidentId === check.incidentId);
    assert.equal(normal.unacknowledged, false);
    assert.equal(normal.reviewPending, true);

    // Reopening a robot-normal incident puts it back in 확인 필요.
    await review.setFallOpinion("robot-a", check.incidentId, "u-owner", "normal", null);
    await review.closeFallIncident("robot-a", check.incidentId, "u-owner");
    await review.setFallOpinion("robot-a", check.incidentId, "u-family", "fall", null);
    const reopened = (await review.listFallIncidentSummaries("robot-a", "check"))
      .find((i) => i.incidentId === check.incidentId);
    assert.equal(reopened.needsCheck, true);
    assert.equal(reopened.category, "check");

    // A recording without its own start time uses the stream session start, never 1970.
    const { clipPlaybackState } = moduleLoader()("db/fall-review.ts");
    const id = randomUUID();
    await h.db.query(`INSERT INTO stream_sessions(id,room_code,device_id,started_by,started_at,expires_at)
      VALUES($1,$1,'robot-a','device','2030-01-01T00:00:00.000Z','2030-01-02T00:00:00.000Z')`, [id]);
    await h.db.query(`INSERT INTO recording_sessions(session_id,kvs_stream_arn,kvs_channel_arn,started_at,ended_at)
      VALUES($1,'arn:stream:a','arn:channel:a',NULL,NULL)`, [id]);
    const recent = new Date(Math.floor(Date.now() / 1000) * 1000 - 120_000).toISOString();
    const report = await review.reportMissedFall("robot-a", "u-owner", recent);
    const detail = await review.getFallIncidentDetail("robot-a", report.incidentId);
    assert.equal(detail.clips[0].playbackState, "unavailable");
    assert.equal(typeof clipPlaybackState, "function");
  });
});

test("사건 screen: incidents replace general events; demo API only for the local demo device", async () => {
  const { readFile } = await import("node:fs/promises");
  const [panel, dashboard, header] = await Promise.all([
    readFile(new URL("../app/components/fall-incidents-panel.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/components/homecam-dashboard.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/components/homecam-header.tsx", import.meta.url), "utf8"),
  ]);
  for (const text of ["가장 먼저 확인할 사건", "아무도 확인하지 않음", "AI 판정 실패", "검수 전",
    "넘어진 순간은 녹화되지 않았을 수 있음", "의견을 남기면 모든 사용자에게 가는 [재발신]이 멈춰요",
    "참고 답변 · 판정 결과에는 반영되지 않아요", "자동 판정 기록", "알림 이력", "처리 완료"]) {
    assert.ok(panel.includes(text), text);
  }
  // 처리 완료 needs at least one opinion (anyone's).
  assert.match(panel, /disabled=\{busy === "close" \|\| detail\.opinions\.length === 0\}/);
  // Toggle buttons: pressing the selected label again clears it.
  assert.match(panel, /setDraftLabel\(draftLabel === key \? null : key\)/);
  assert.match(panel, /demo \? demoIncidentFetch\(url, init\) : fetch\(url, init\)/);
  assert.match(dashboard, /demo=\{LOCAL_HOME_CAM_DEMO && selectedDevice\.id === LOCAL_DEMO_DEVICE_ID\}/);
  assert.match(header, /<span>사건<\/span>/);
  assert.doesNotMatch(dashboard, /\/events\?\$\{params\}|EventPlayback|removeEventFromList/);
  // 정지 사진 (SWM25-236): the scene itself is the play button over the still; no second play button.
  assert.match(panel, /className=\{`fall-scene-start \$\{still === "shown" \? "has-photo" : ""\}`\}/);
  assert.match(panel, /aria-label="장면 영상 보기" disabled=\{!playable\} onClick=\{onStart\}/);
  assert.match(panel, /\/clips\/\$\{clip\.segmentIndex\}\/still/);
  assert.match(panel, /onLoad=\{\(\) => setStill\("shown"\)\} onError=\{\(\) => setStill\("none"\)\}/);
  assert.ok(panel.includes("누르면 이 구간을 재생해요"));
  assert.doesNotMatch(panel, />장면 영상 보기<\/button>/);
});

test("연속 녹화: day timeline marks, recorded spans and report memo", async () => {
  await withRepo(async ({ h, events, review }) => {
    const now = Math.floor(Date.now() / 1000) * 1000;
    const at = (offset) => new Date(now + offset).toISOString();
    await recording(h, "robot-a", at(-3 * 3600_000), at(-2 * 3600_000));
    await recording(h, "robot-a", at(-3600_000), null);
    const fall = notice({ occurredAt: at(-30 * 60_000), fallSeen: true });
    await events.storeFallEvent("robot-a", fall);
    const report = await review.reportMissedFall("robot-a", "u-family", at(-10 * 60_000), now, "  거실에서 넘어지심 ");
    const timeline = await review.getFallTimeline("robot-a", at(-4 * 3600_000), at(60_000), now);
    assert.equal(timeline.recordings.length, 2);
    assert.equal(timeline.recordings[1].endAt, at(0)); // an open recording ends now
    assert.deepEqual(timeline.incidents.map((i) => i.kind), ["fall", "report"]);
    assert.equal((await review.getFallIncidentDetail("robot-a", report.incidentId)).reportMemo, "거실에서 넘어지심");
    await assert.rejects(review.getFallTimeline("robot-a", at(0), at(-1000), now), /RANGE_INVALID/);
    await assert.rejects(review.getFallTimeline("robot-a", at(-30 * 3600_000), at(0), now), /RANGE_INVALID/);
    await assert.rejects(review.reportMissedFall("robot-a", "u-owner", at(-60_000), now, "x".repeat(501)), /INVALID/);
    assert.deepEqual(await review.recordingStreamFor("robot-a", at(-90 * 60_000), at(-80 * 60_000)), []);
    assert.deepEqual(await review.recordingStreamFor("robot-a", at(-10 * 60_000), at(-9 * 60_000)), ["arn:stream:a"]);
  });
});

test("recording playback: member-only, ≤ 10 min, only where this device recorded", async () => {
  const h = await fallDatabase();
  const broker = [];
  const load = moduleLoader({
    [path.join(h.root, "app/server-auth.ts")]: { async getRequestUserId(req) { return testUserId(req.headers.get("x-test-email")); } },
    [path.join(h.root, "app/runtime-env.ts")]: { getRuntimeEnvironment() { return { KVS_BROKER_SECRET: "s".repeat(64) }; } },
    [path.join(h.root, "app/kvs-device-config.ts")]: { resolveDeviceKvsResources() { return { streamArn: "arn:stream:a" }; } },
    [path.join(h.root, "app/kvs-broker.ts")]: { async requestBrokerEventPlayback(input) {
      broker.push(input);
      return { playbackUrl: "https://kvs.example.com/hls/v1/getHLSMasterPlaylist.m3u8?SessionToken=t",
        expiresAt: new Date(Date.now() + 300_000).toISOString(), alignedStartAt: input.startAt, streamArn: input.streamArn };
    } },
    [path.join(h.root, "app/recording-playback-proxy.ts")]: { async createFallClipPlaybackProxy() {
      return { playbackUrl: "/api/devices/robot-a/fall-hls/p/getHLSMasterPlaylist.m3u8", setCookie: "c=1" };
    } },
  });
  const pg = load("db/postgres.ts");
  const route = load("app/api/devices/[deviceId]/recording-playback/route.ts");
  const now = Math.floor(Date.now() / 1000) * 1000;
  const at = (offset) => new Date(now + offset).toISOString();
  const call = (body, email = "family@example.com") => route.POST(new Request("https://web.test/api", {
    method: "POST", body: JSON.stringify(body),
    headers: { "x-test-email": email, "content-type": "application/json", origin: "https://web.test" },
  }), { params: Promise.resolve({ deviceId: "robot-a" }) });
  try { await pg.withPostgresPoolForTest(h.pool, async () => {
    await recording(h, "robot-a", at(-3600_000), null);
    assert.equal((await call({ startAt: at(-600_000), endAt: at(-300_000) }, "stranger@example.com")).status, 404);
    assert.equal((await call({ startAt: at(-1200_000), endAt: at(0) })).status, 400); // > 10 min
    assert.equal((await call({ startAt: at(-2 * 3600_000), endAt: at(-2 * 3600_000 + 60_000) })).status, 404);
    const ok = await call({ startAt: at(-600_000), endAt: at(-300_000) });
    assert.equal(ok.status, 200);
    assert.equal((await ok.json()).alignedStartAt, at(-600_000));
    assert.equal(broker.length, 1);
  }); } finally { await h.db.close(); }
});

test("연속 녹화·설정 screens follow the mockup and only the owner edits settings", async () => {
  const { readFile } = await import("node:fs/promises");
  const [timeline, settings, dashboard] = await Promise.all([
    readFile(new URL("../app/components/fall-timeline-panel.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/components/fall-homecam-settings.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/components/homecam-dashboard.tsx", import.meta.url), "utf8"),
  ]);
  for (const text of ["최근 7일까지 볼 수 있어요", "24시간 타임라인 · 표시는 사건 위치", "사건 다시 검토",
    "이 시간은 원래 사건 밖이에요. 놓친 낙상으로 새로 신고할까요?", "놓친 낙상 신고", "신고만 남기기",
    "신고하고 AI에게 검토 받기", "검토 진행 중 · 결과는 원래 사건에 기록돼요", "고른 순간 주변 2분", "AI에게 보내는 5초"]) {
    assert.ok(timeline.includes(text), text);
  }
  for (const text of ["카메라 사용", "연속 녹화", "낙상 감지", "클라우드 AI 확인 동의",
    "설정은 소유자만 바꿀 수 있어요. 지금 상태만 보여요.", "저장된 영상"]) {
    assert.ok(settings.includes(text), text);
  }
  // The Cloud AI key moved to 설정 › AI·서비스 키 (service-keys.test.mjs); only the consent stays here.
  assert.doesNotMatch(settings, /fall-cloud-key|클라우드 AI 키/);
  assert.doesNotMatch(settings, /음성 녹음/); // not built yet; left out on purpose
  assert.match(settings, /const disabled = !isOwner \|\| blocked \|\| row\.busy/);
  assert.match(dashboard, /<FallTimelinePanel/);
  // Today keeps recording: refetch every 30 s, and at once for a moment after the last fetch.
  assert.match(timeline, /window\.setInterval\(\(\) => setReload\(\(count\) => count \+ 1\), 30_000\)/);
  assert.match(timeline, /if \(showsToday && loadedAt && moment > loadedAt\)/);
  assert.match(timeline, /\}, \[base, day0, request, reload\]\);/);
  assert.match(dashboard, /<FallHomecamSettings/);
  assert.doesNotMatch(dashboard, /FallSettingsPanel/);
});
