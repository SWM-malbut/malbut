import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import { fallDatabase, moduleLoader } from "./helpers/fall-db-harness.mjs";
import { buildFallNotification } from "../infra/aws/push-broker/fall-notification.mjs";

// 낙상 확인이 직접 움직이기를 끊지 않고, 직접 확인하지 못하면 알리는 기능(목업 27·28번).

const T0 = "2026-10-12T05:02:10.000Z";
const at = (ms) => new Date(Date.parse(T0) + ms).toISOString();

function event(change = {}) {
  return { schemaVersion: 1, eventId: randomUUID(), incidentId: randomUUID(), bootId: "boot-1",
    sequence: 1, evidenceRevision: 1, occurredAt: T0, eventKind: "incident_opened", state: "verifying",
    fallSeen: false, assessment: "suspected_fall", answer: null, reason: null, notificationLevel: null, ...change };
}
const notCheckedAlert = (change = {}) => event({ eventKind: "notification_requested", state: "recheck_required",
  notificationLevel: "urgent", reason: "robot_could_not_confirm", ...change });
const helpAlert = (change = {}) => event({ eventKind: "notification_requested", state: "help_required",
  answer: "help_request", notificationLevel: "urgent", reason: "help_requested", ...change });

async function withRepo(work, overrides = {}) {
  const h = await fallDatabase(), load = moduleLoader(overrides);
  const pg = load("db/postgres.ts");
  try {
    await pg.withPostgresPoolForTest(h.pool, () => work({
      h, load, events: load("db/fall-incidents.ts"), review: load("db/fall-review.ts"),
    }));
  } finally { await h.db.close(); }
}

/** One incident's robot events, numbered in order. */
function journal(kinds) {
  const incidentId = randomUUID();
  return kinds.map(([eventKind, reason, change = {}], index) => event({
    incidentId, sequence: index + 1, occurredAt: at(index * 1000), eventKind, reason, ...change }));
}

test("contract: the new records and their reasons, nothing else", () => {
  const { parseFallEvent } = moduleLoader()("app/fall-contract.ts");
  for (const [eventKind, reasons] of [
    ["check_waiting", ["manual_drive", "busy"]],
    ["check_not_confirmed", ["waited_manual_drive", "waited_busy"]],
    ["revisit_started", ["manual_drive", "busy"]],
    ["same_person_compared", ["same", "different", "unclear"]],
  ]) {
    for (const reason of reasons) assert.ok(parseFallEvent(event({ eventKind, reason })), `${eventKind}:${reason}`);
    assert.equal(parseFallEvent(event({ eventKind, reason: "other" })), null);
    assert.equal(parseFallEvent(event({ eventKind, reason: null })), null);
  }
  assert.ok(parseFallEvent(event({ eventKind: "person_check_completed", reason: "nobody_there" })));
  // 직접 확인하지 못한 이유는 사건을 닫지 않는다.
  for (const reason of ["speech_unavailable", "question_error", "time_limit"]) {
    assert.ok(parseFallEvent(event({ eventKind: "agent_check_failed", reason, state: "recheck_required" })));
  }
  assert.ok(parseFallEvent(event({ eventKind: "incident_resolved", reason: "guardian_closed", state: "resolved" })));
  // The comparison may carry its Cloud explanation, and only there.
  const analysis = { requestId: "same-1", purpose: "same_person_check", assessment: "unobservable",
    explanation: "회색 상의와 검은 바지가 같아 보입니다." };
  assert.ok(parseFallEvent(event({ eventKind: "same_person_compared", reason: "same", analysis })));
  assert.equal(parseFallEvent(event({ eventKind: "same_person_compared", reason: "same",
    analysis: { ...analysis, purpose: "person_check" } })), null);
  assert.equal(parseFallEvent(event({ eventKind: "posture_check_completed", reason: "standing", analysis })), null);
});

test("contract: four new alerts with fixed levels and copy", () => {
  const { parseFallEvent } = moduleLoader()("app/fall-contract.ts");
  const copy = {
    robot_could_not_confirm: ["urgent", "말벗이 낙상 의심 상황을 직접 확인하지 못했습니다. 즉시 확인해 주세요."],
    revisit_same_person_off_floor: ["info",
      "말벗이 다시 확인했을 때 같은 분으로 보이는 분이 바닥이 아닌 곳에 있었습니다. 괜찮아 보이지만 확인해 주세요."],
    revisit_person_unclear_off_floor: ["check",
      "말벗이 다시 확인했을 때 바닥이 아닌 곳에 있는 분이 있었지만 같은 분인지 알 수 없습니다. 상태를 확인해 주세요."],
    revisit_nobody_there: ["check", "말벗이 다시 확인했을 때 그 자리에 사람이 없었습니다. 상태를 확인해 주세요."],
  };
  for (const [reason, [level, body]] of Object.entries(copy)) {
    const alert = notCheckedAlert({ reason, notificationLevel: level });
    assert.ok(parseFallEvent(alert), reason);
    assert.equal(parseFallEvent({ ...alert, notificationLevel: level === "urgent" ? "check" : "urgent" }), null);
    assert.equal(buildFallNotification({ deviceId: "robot-a", notificationId: alert.eventId,
      incidentId: alert.incidentId, level, reason, occurredAt: alert.occurredAt }).body, body);
  }
});

test("contract: a fine answer the robot cannot tie to the person seen alerts once (check)", async () => {
  const { parseFallEvent } = moduleLoader()("app/fall-contract.ts");
  const alert = notCheckedAlert({ reason: "scene_answer_unconfirmed", notificationLevel: "check",
    answer: "unclear" });
  assert.ok(parseFallEvent(alert));
  assert.equal(parseFallEvent({ ...alert, notificationLevel: "urgent" }), null);
  assert.equal(buildFallNotification({ deviceId: "robot-a", notificationId: alert.eventId,
    incidentId: alert.incidentId, level: "check", reason: alert.reason, occurredAt: alert.occurredAt }).body,
  "누군가 괜찮다고 답했지만, 쓰러진 분인지 확인하지 못했습니다. 상태를 확인해 주세요.");
  await withRepo(async ({ h, events }) => {
    const [opened, unconfirmed, help] = journal([
      ["incident_opened", "target_unidentified"],
      ["notification_requested", "scene_answer_unconfirmed",
        { state: "recheck_required", answer: "unclear", notificationLevel: "check" }],
      ["notification_requested", "help_requested",
        { state: "help_required", answer: "help_request", notificationLevel: "urgent" }],
    ]);
    for (const e of [opened, unconfirmed, help, unconfirmed]) await events.storeFallEvent("robot-a", e);
    // Its own row, kept pending after a later 도움 요청 (per reason, not per level).
    assert.deepEqual((await h.db.query("SELECT reason,status FROM fall_push_outbox ORDER BY reason")).rows
      .map((r) => [r.reason, r.status]), [["help_requested", "pending"], ["scene_answer_unconfirmed", "pending"]]);
  });
});

test("alerts: 직접 확인하지 못함 never swallows a later 도움 요청 or revisit result", async () => {
  await withRepo(async ({ h, events }) => {
    const [opened, notChecked, revisit, help] = journal([
      ["incident_opened", "cloud_crosscheck"],
      ["notification_requested", "robot_could_not_confirm",
        { state: "recheck_required", notificationLevel: "urgent" }],
      ["notification_requested", "revisit_person_unclear_off_floor",
        { state: "recheck_required", notificationLevel: "check" }],
      ["notification_requested", "help_requested",
        { state: "help_required", answer: "help_request", notificationLevel: "urgent" }],
    ]);
    for (const e of [opened, notChecked, revisit, help]) await events.storeFallEvent("robot-a", e);
    // Sent again with the same IDs (robot retry): still one row each.
    await events.storeFallEvent("robot-a", notChecked);
    assert.deepEqual((await h.db.query(
      "SELECT reason,level,status FROM fall_push_outbox ORDER BY created_at,reason")).rows.map((r) => [r.reason, r.status]).sort(),
    [["help_requested", "pending"], ["revisit_person_unclear_off_floor", "pending"],
      ["robot_could_not_confirm", "pending"]]);
    // Once per reason: a second 직접 확인하지 못함 for the same incident is not queued.
    await events.storeFallEvent("robot-a", notCheckedAlert({ incidentId: opened.incidentId, sequence: 9 }));
    assert.equal((await h.db.query(
      "SELECT count(*)::int AS n FROM fall_push_outbox WHERE reason='robot_could_not_confirm'")).rows[0].n, 1);
  });
});

test("alerts: the older reasons keep one per level and only a higher level", async () => {
  await withRepo(async ({ h, events }) => {
    const incidentId = randomUUID();
    const store = (sequence, reason, level, change = {}) => events.storeFallEvent("robot-a", event({
      incidentId, sequence, eventKind: "notification_requested", state: "help_required", answer: "help_request",
      notificationLevel: level, reason, ...change }));
    await store(1, "person_no_response", "check", { state: "recheck_required", answer: "no_response" });
    await store(2, "help_requested", "urgent");
    // Same level, another older reason: dropped as before.
    await store(3, "confirmation_help_required", "urgent");
    // A lower older level after a higher one: kept as superseded.
    await store(4, "fall_observed_person_okay", "info", { state: "recheck_required", answer: "okay", fallSeen: true });
    const rows = Object.fromEntries((await h.db.query("SELECT reason,status FROM fall_push_outbox")).rows
      .map((r) => [r.reason, r.status]));
    assert.deepEqual(rows, { person_no_response: "superseded", help_requested: "pending",
      fall_observed_person_okay: "superseded" });
    // A new alert is never superseded by, nor supersedes, the older ones.
    await events.storeFallEvent("robot-a", event({ incidentId, sequence: 5, eventKind: "notification_requested",
      state: "recheck_required", notificationLevel: "check", reason: "revisit_nobody_there" }));
    assert.equal((await h.db.query(
      "SELECT status FROM fall_push_outbox WHERE reason='revisit_nobody_there'")).rows[0].status, "pending");
    assert.equal((await h.db.query(
      "SELECT status FROM fall_push_outbox WHERE reason='help_requested'")).rows[0].status, "pending");
  });
});

test("alerts: a pending older check is still sent after 직접 확인하지 못함", async () => {
  await withRepo(async ({ h, events }) => {
    const incidentId = randomUUID();
    await events.storeFallEvent("robot-a", notCheckedAlert({ incidentId, sequence: 1 }));
    // Went back, asked, no answer.
    await events.storeFallEvent("robot-a", event({ incidentId, sequence: 2, eventKind: "notification_requested",
      state: "recheck_required", answer: "no_response", notificationLevel: "check", reason: "person_no_response" }));
    assert.deepEqual((await h.db.query("SELECT reason,status FROM fall_push_outbox ORDER BY reason")).rows, [
      { reason: "person_no_response", status: "pending" }, { reason: "robot_could_not_confirm", status: "pending" }]);
  });
});

test("migration 0025 keeps existing alerts and moves the key to the reason", async () => {
  const h = await fallDatabase({ through: "0024_voice_agent" });
  try {
    const incidentId = randomUUID(), eventId = randomUUID();
    await h.db.query(`INSERT INTO fall_incidents(device_id,incident_id,boot_id,evidence_revision,state,occurred_at)
      VALUES('robot-a',$1,'boot-1',1,'help_required',$2)`, [incidentId, T0]);
    await h.db.query(`INSERT INTO fall_incident_events(device_id,event_id,incident_id,sequence,payload_json)
      VALUES('robot-a',$1,$2,1,'{}')`, [eventId, incidentId]);
    await h.db.query(`INSERT INTO fall_push_outbox(device_id,notification_id,incident_id,level,reason,occurred_at)
      VALUES('robot-a',$1,$2,'urgent','help_requested',$3)`, [eventId, incidentId, T0]);
    await h.db.exec(readFileSync(path.join(h.root, "db/migrations/0025_fall_push_per_reason.sql"), "utf8"));
    const keys = (await h.db.query(`SELECT conname FROM pg_constraint
      WHERE conrelid='fall_push_outbox'::regclass AND contype='u' ORDER BY conname`)).rows.map((r) => r.conname);
    assert.deepEqual(keys, ["fall_push_outbox_incident_reason_key"]);
    assert.equal((await h.db.query("SELECT count(*)::int AS n FROM fall_push_outbox")).rows[0].n, 1);
  } finally { await h.db.close(); }
});

test("before migration 0025 an alert is refused (robot retries) instead of half-stored", async () => {
  const h = await fallDatabase({ through: "0024_voice_agent" }), load = moduleLoader();
  try {
    await load("db/postgres.ts").withPostgresPoolForTest(h.pool, async () => {
      const repo = load("db/fall-incidents.ts");
      await assert.rejects(repo.storeFallEvent("robot-a", notCheckedAlert()), /FALL_MIGRATION_REQUIRED/);
      assert.equal((await h.db.query("SELECT count(*)::int AS n FROM fall_incident_events")).rows[0].n, 0);
      assert.equal((await repo.storeFallEvent("robot-a", event())).stored, true);
    });
  } finally { await h.db.close(); }
});

test("reminders: 직접 확인하지 못함 runs the urgent cycle; a later 도움 요청 starts a new one", async () => {
  await withRepo(async ({ h, events, review }) => {
    const incidentId = randomUUID();
    await events.storeFallEvent("robot-a", notCheckedAlert({ incidentId, sequence: 1 }));
    const first = new Date((await h.db.query(
      "SELECT created_at FROM fall_push_outbox WHERE incident_id=$1", [incidentId])).rows[0].created_at).getTime();
    assert.equal((await review.scheduleFallReminders(first + 120_000)).created, 1);
    // A revisit check alert does not replace the urgent cycle.
    await events.storeFallEvent("robot-a", event({ incidentId, sequence: 2, eventKind: "notification_requested",
      state: "recheck_required", notificationLevel: "check", reason: "revisit_nobody_there" }));
    assert.equal((await review.scheduleFallReminders(first + 240_000)).created, 1);
    assert.deepEqual((await h.db.query("SELECT round,level,reason FROM fall_web_notices ORDER BY round")).rows, [
      { round: 2, level: "urgent", reason: "robot_could_not_confirm" },
      { round: 3, level: "urgent", reason: "robot_could_not_confirm" }]);
    const [{ notification }] = await review.listFallIncidentSummaries("robot-a");
    assert.deepEqual(notification, { level: "urgent", sent: 1, total: 3 });
    // [재발신] keeps the reason's copy.
    assert.equal(buildFallNotification({ deviceId: "robot-a", notificationId: randomUUID(), incidentId,
      level: "urgent", reason: "robot_could_not_confirm", occurredAt: T0, resend: true }).body,
    "[재발신] 말벗이 낙상 의심 상황을 직접 확인하지 못했습니다. 즉시 확인해 주세요.");
    await events.storeFallEvent("robot-a", helpAlert({ incidentId, sequence: 3 }));
    const help = new Date((await h.db.query(
      "SELECT created_at FROM fall_push_outbox WHERE reason='help_requested'")).rows[0].created_at).getTime();
    assert.equal((await review.scheduleFallReminders(help + 120_000)).created, 1);
    assert.equal((await h.db.query(
      "SELECT reason FROM fall_web_notices ORDER BY created_at DESC LIMIT 1")).rows[0].reason, "help_requested");
  });
});

test("처리 완료 reaches the robot until it stops; then the incident is not called normal", async () => {
  await withRepo(async ({ h, events, review }) => {
    const [opened, alert] = journal([
      ["incident_opened", "cloud_crosscheck", { assessment: "normal_activity" }],
      ["notification_requested", "robot_could_not_confirm", { state: "recheck_required", notificationLevel: "urgent",
        assessment: "normal_activity" }],
    ]);
    const id = opened.incidentId;
    for (const e of [opened, alert]) await events.storeFallEvent("robot-a", e);
    assert.deepEqual(await review.listClosedFallIncidentsForRobot("robot-a"), []);
    await review.setFallOpinion("robot-a", id, "u-owner", "normal", null);
    await review.closeFallIncident("robot-a", id, "u-owner");
    assert.deepEqual(await review.listClosedFallIncidentsForRobot("robot-a"), [id]);
    assert.deepEqual(await review.listClosedFallIncidentsForRobot("robot-b"), []);
    // The robot stopped and reported it.
    await events.storeFallEvent("robot-a", event({ incidentId: id, sequence: 3, eventKind: "incident_resolved",
      state: "resolved", reason: "guardian_closed", assessment: "normal_activity" }));
    assert.deepEqual(await review.listClosedFallIncidentsForRobot("robot-a"), []);
    // A member closed it; the robot did not judge it normal.
    const [summary] = await review.listFallIncidentSummaries("robot-a", "closed");
    assert.equal(summary.incidentId, id);
    assert.notEqual(summary.category, "normal");
    assert.deepEqual(await review.listFallIncidentSummaries("robot-a", "normal"), []);
    const detail = await review.getFallIncidentDetail("robot-a", id);
    const { fallHistoryLines } = moduleLoader()("app/fall-event-label.ts");
    assert.equal(fallHistoryLines(detail.robotEvents).at(-1).label, "처리 완료 → 말벗이 확인을 멈춤");
    // Robot already resolved, a reopened one, an old one, and a missed-fall report are not sent.
    const closed = async (minutesAgo, state = "recheck_required") => {
      const e = event({ state });
      await events.storeFallEvent("robot-a", e);
      await h.db.query(`UPDATE fall_incidents SET review_state='closed',closed_by='u-owner',
        closed_at=CURRENT_TIMESTAMP-($2::int * INTERVAL '1 minute') WHERE incident_id=$1`, [e.incidentId, minutesAgo]);
      return e.incidentId;
    };
    const recent = await closed(14);
    await closed(16);
    await h.db.query(`INSERT INTO fall_incidents(device_id,incident_id,origin,occurred_at,reported_by,
      reported_moment_at,review_state,closed_at,closed_by) VALUES('robot-a',$1,'user_report',$2,'u-owner',$2,
      'closed',CURRENT_TIMESTAMP,'u-owner')`, [randomUUID(), T0]);
    assert.deepEqual(await review.listClosedFallIncidentsForRobot("robot-a"), [recent]);
    const many = [];
    for (let i = 0; i < 20; i++) many.push(await closed(1));
    assert.equal((await review.listClosedFallIncidentsForRobot("robot-a")).length, 16);
  });
});

test("state upload answers with closed incidents and never fails because of them", async () => {
  const root = path.resolve(import.meta.dirname, "..");
  const state = { state: "idle" };
  const stored = [];
  const route = (closed) => moduleLoader({
    [path.join(root, "app/device-auth.ts")]: { getRequestDevice: async () => ({ deviceId: "robot-a" }) },
    [path.join(root, "app/robot-contract.ts")]: {
      parseRobotState: (value) => value, readRobotJson: async (request) => request.json() },
    [path.join(root, "db/robot-map.ts")]: { storeRobotState: async (deviceId, value) => { stored.push(value); } },
    [path.join(root, "db/fall-review.ts")]: { listClosedFallIncidentsForRobot: closed },
  })("app/api/device/v1/robot/state/route.ts");
  const post = (closed) => route(closed).POST(new Request("https://test/api", {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(state) }));
  const id = randomUUID();
  let response = await post(async () => [id]);
  assert.equal(response.status, 202);
  assert.deepEqual(await response.json(), { accepted: true, closedFallIncidents: [id] });
  response = await post(async () => { throw new Error("FALL_MIGRATION_REQUIRED"); });
  assert.equal(response.status, 202);
  assert.deepEqual(await response.json(), { accepted: true, closedFallIncidents: [] });
  assert.equal(stored.length, 2);
});

// 목업 28번의 상태마다 사건 화면에 보이는 이유·다시 본 결과·기록·부제목.
const waitedStart = [
  ["incident_opened", "cloud_crosscheck"],
  ["check_waiting", "manual_drive"],
  ["check_not_confirmed", "waited_manual_drive", { state: "recheck_required" }],
  ["notification_requested", "robot_could_not_confirm", { state: "recheck_required", notificationLevel: "urgent" }],
  ["revisit_started", "manual_drive", { state: "recheck_required" }],
  ["approach_completed", "arrived", { state: "recheck_required" }],
];
const comparison = (reason, explanation) => ["same_person_compared", reason, { state: "recheck_required",
  analysis: { requestId: `same-${reason}`, purpose: "same_person_check", assessment: "unobservable", explanation } }];

test("incident screen: each mockup 28 state", async () => {
  const label = moduleLoader()("app/fall-event-label.ts");
  const view = (rows) => {
    const events = journal(rows);
    return { check: label.fallRobotCheck(events), lines: label.fallHistoryLines(events).map((l) => l.label) };
  };
  let { check, lines } = view([...waitedStart,
    ["person_check_completed", "partial_body", { state: "recheck_required" }],
    ["view_completed", "arrived", { state: "recheck_required" }],
    ["posture_check_completed", "standing", { state: "recheck_required" }],
    comparison("same", "회색 상의와 검은 바지가 같아 보입니다."),
    ["notification_requested", "revisit_same_person_off_floor", { state: "recheck_required", notificationLevel: "info" }],
    ["approach_returned", "returned", { state: "recheck_required" }],
  ]);
  assert.deepEqual(check, { reason: "직접 움직이기 중이라 1분 안에 묻지 못함", waited: true, stopped: false, failed: null,
    revisit: { title: "서 있음 · 같은 분으로 보임", subtitle: "다시 가 보니 같은 분으로 보이는 분이 서 있음",
      note: "AI가 처음 장면과 옷차림을 비교했어요(참고): 회색 상의와 검은 바지가 같아 보임. 같은 분인지는 직접 확인해 주세요." } });
  assert.deepEqual(lines.slice(1), [
    "확인하러 가려 했지만 직접 움직이기 중 → 끝나면 확인",
    "1분째 확인하지 못함 → 긴급 알림",
    "직접 움직이기 끝남 → 묻기 전에 의심 장소로 다시 감",
    "의심 장소 1 m 앞에 다시 도착",
    "가까이에서 다시 확인: 사람 · 몸 일부만 보임",
    "몸 전체가 보이는 자리로 옮김",
    "몸 전체 확인: 서 있음",
    "처음 장면과 비교: 같은 분으로 보임 (AI: 회색 상의와 검은 바지가 같아 보임)",
    "묻지 않음 · 일반 알림 · 사건은 열어 둠",
    "원래 자리로 돌아옴",
  ]);

  ({ check } = view([...waitedStart, ["posture_check_completed", "sitting_off_floor"],
    comparison("unclear", "처음 장면은 다리만 보입니다.")]));
  assert.deepEqual(check.revisit, { title: "침대나 의자에 앉아 있음 · 같은 분인지 알 수 없음",
    subtitle: "다시 가 보니 침대나 의자에 앉아 있는 분이 있음 · 같은 분인지 모름",
    note: "AI가 처음 장면과 옷차림을 비교했어요(참고): 처음 장면은 다리만 보임." });
  ({ check } = view([...waitedStart, ["posture_check_completed", "standing"], comparison("different", "옷 색이 다릅니다.")]));
  assert.equal(check.revisit.subtitle, "다시 가 보니 서 있는 분이 있음 · 다른 분으로 보임");

  ({ check, lines } = view([...waitedStart, ["person_check_completed", "nobody_there"],
    ["notification_requested", "revisit_nobody_there", { state: "recheck_required", notificationLevel: "check" }]]));
  assert.equal(check.revisit.title, "그 자리에 사람이 없음");
  assert.match(check.revisit.note, /그래서 "사람 아님"으로 닫지 않아요\.$/);
  assert.deepEqual(lines.slice(-2), ["가까이에서 다시 확인: 그 자리에 사람이 없음", "묻지 않음 · 확인 필요 알림 · 사건은 열어 둠"]);

  // 바닥: 묻는 일이 비교를 기다리지 않고, 비교는 뒤이어 기록된다.
  ({ check } = view([...waitedStart, ["posture_check_completed", "on_floor"], ["question_requested", null],
    comparison("same", "같은 옷입니다.")]));
  assert.deepEqual([check.revisit.title, check.revisit.subtitle], ["바닥에 있음 · 같은 분으로 보임 → 물어봄", "다시 가 보니 바닥에 있음"]);
  assert.equal(check.revisit.note, "바닥에 있으면 지금처럼 물어봐요. 대답에 따라 알림은 지금 규칙과 같아요.");

  ({ check, lines } = view([...waitedStart.slice(0, 4),
    ["agent_check_failed", "time_limit", { state: "recheck_required" }]]));
  assert.deepEqual([check.waited, check.stopped, check.revisit], [true, true, null]);
  assert.equal(lines.at(-1), "막혀서 10분 동안 확인하지 못함 → 확인을 멈춤");

  ({ check, lines } = view([["incident_opened", "cloud_crosscheck"], ["posture_check_completed", "on_floor"],
    ["question_requested", null],
    ["agent_check_failed", "speech_unavailable", { state: "recheck_required", answer: "failed" }],
    ["notification_requested", "robot_could_not_confirm", { state: "recheck_required", notificationLevel: "urgent" }]]));
  assert.deepEqual(check, { reason: "말벗 음성이 꺼져 있어 물을 수 없음", waited: false, stopped: false,
    failed: "말벗 음성이 꺼져 있어 묻지 못함", revisit: null });
  assert.equal(lines.at(-1), "말벗 음성이 꺼져 있어 물을 수 없음 → 바로 긴급 알림");

  // 예전 질문 실패와 평소 사건은 이 상자를 띄우지 않는다.
  assert.equal(view([["incident_opened", "cloud_crosscheck"], ["agent_check_failed", "agent_failed"]]).check, null);
  assert.equal(view([["incident_opened", "cloud_crosscheck"], ["approach_started", null]]).check, null);
});

test("incident list carries the robot check summary from storage", async () => {
  await withRepo(async ({ events, review }) => {
    const rows = journal([...waitedStart, ["person_check_completed", "nobody_there", { state: "recheck_required" }]]);
    for (const e of rows) await events.storeFallEvent("robot-a", e);
    const [summary] = await review.listFallIncidentSummaries("robot-a");
    assert.equal(summary.robotCheck.reason, "직접 움직이기 중이라 1분 안에 묻지 못함");
    assert.equal(summary.robotCheck.revisit.subtitle, "다시 가 보니 그 자리에 사람이 없음");
    assert.equal(summary.needsCheck, true);
    const detail = await review.getFallIncidentDetail("robot-a", rows[0].incidentId);
    assert.deepEqual(detail.robotCheck, summary.robotCheck);
  });
});

test("map screen: fall check stages read as mockup 27", () => {
  const { fallCheckView } = moduleLoader()("app/robot-fall-check-copy.ts");
  const detail = (fall_check, extra = {}) => ({ fall_check_active: true, fall_check, ...extra });
  const target = { x: 1.2, y: 3.4 };
  assert.equal(fallCheckView({ fall_check_active: false }), null);
  assert.equal(fallCheckView(undefined), null);
  assert.deepEqual(fallCheckView(detail({ phase: "approach", waiting_for: null, alerted: false, target },
    { last_patrol: { outcome: "fall_check", fall_result: "pending" } })),
  { title: "낙상 의심 장소를 확인하러 가고 있어요", detail: "순찰은 멈췄어요. 확인이 끝나면 원래 자리로 돌아와요.", target });
  assert.equal(fallCheckView(detail({ phase: "approach", target })).detail, "확인이 끝나면 원래 자리로 돌아와요.");
  assert.equal(fallCheckView(detail({ phase: "look", target })).title, "낙상 의심 장소 앞에서 확인하고 있어요");
  assert.equal(fallCheckView(detail({ phase: "ask", target })).title, "낙상 의심 상황을 말로 묻고 있어요");
  assert.equal(fallCheckView(detail({ phase: "return", target })).title, "확인을 마치고 원래 자리로 돌아가고 있어요");
  assert.deepEqual(fallCheckView(detail({ phase: "wait", waiting_for: "manual_drive", alerted: false, target })), {
    title: "직접 움직이기가 끝나면 낙상 확인을 이어 가요",
    detail: "직접 움직이기는 끊지 않아요. 1분 안에 확인하지 못하면 보호자에게 긴급 알림을 보내요.", target });
  assert.deepEqual(fallCheckView(detail({ phase: "wait", waiting_for: "manual_drive", alerted: true, target })), {
    title: "직접 움직이기가 끝나면 의심 장소로 다시 가서 확인해요",
    detail: "1분 안에 확인하지 못해 보호자에게 긴급 알림을 보냈어요. 그새 움직였을 수 있어 묻기 전에 다시 봐요.", target });
  assert.deepEqual(fallCheckView(detail({ phase: "wait", waiting_for: "busy", alerted: false, target: null })), {
    title: "다른 일이 끝나면 낙상 확인을 이어 가요",
    detail: "말벗이 켜지는 중이거나 위치를 다시 잡는 중이에요. 1분 안에 확인하지 못하면 보호자에게 긴급 알림을 보내요.",
    target: null });
  // No place to go back to: it asks as soon as it can.
  assert.match(fallCheckView(detail({ phase: "wait", waiting_for: "busy", alerted: true, target: null })).detail,
    /끝나면 바로 물어봐요\.$/);
  // An older robot only says a fall mission holds the wheels.
  assert.deepEqual(fallCheckView({ fall_check_active: true }),
    { title: "낙상 의심을 확인하고 있어요", detail: "확인이 끝나면 다시 이동할 수 있어요.", target: null });
  assert.equal(fallCheckView(detail({ phase: "wait", target: { x: "1", y: 2 } })).target, null);
});

test("map screen: fall status does not gate mission requests outside the Manager", () => {
  const root = path.resolve(import.meta.dirname, "..");
  const panel = readFileSync(path.join(root, "app/components/robot-map-panel.tsx"), "utf8");
  const manager = readFileSync(path.join(root, "app/components/real-robot-map-manager.tsx"), "utf8");
  const disabledLine = (label) => {
    const index = panel.lastIndexOf(`>${label}</button>`);
    assert.ok(index > 0, label);
    return panel.slice(panel.lastIndexOf("<button", index), index);
  };
  for (const label of ["이 위치로 이동", "사람 따라가기", "자율 배회 시작"]) {
    assert.doesNotMatch(disabledLine(label), /fallCheck/);
  }
  assert.match(panel, /"순찰 다시 시작" : "방 순찰 시작"/);
  assert.doesNotMatch(panel, /if \(fallCheck && mapMode === "navigate"\) return;/);
  assert.doesNotMatch(panel, /fallCheckActive\(/);
  assert.doesNotMatch(manager, /fallCheckActive/);
  // 직접 움직이기 is not gated by the fall check.
  const pad = panel.slice(panel.indexOf("<MapDrivePad"), panel.indexOf("/>", panel.indexOf("<MapDrivePad")));
  assert.doesNotMatch(pad, /fallCheck/);
  // 순찰 멈춤 카드: 다시 가서 본 뒤 알림만 보냄.
  assert.match(panel, /"확인 결과: 보호자에게 알렸어요 · 원래 자리로 돌아왔어요"/);
});

test("contract: '그만' during the check stops it without a question and alerts once (check)", async () => {
  // 2026-10-13: "그만/멈춰" ranks above the fall check like 직접 움직이기.
  const load = moduleLoader();
  const { parseFallEvent } = load("app/fall-contract.ts");
  const { fallEventLabel } = load("app/fall-event-label.ts");
  const stopped = event({ eventKind: "check_stopped", reason: "user_stop", state: "recheck_required",
    answer: "failed" });
  assert.ok(parseFallEvent(stopped));
  assert.equal(parseFallEvent({ ...stopped, reason: "other" }), null);
  assert.equal(fallEventLabel(stopped), "'그만'이라는 말을 듣고 확인을 멈춤 (묻지 않음)");
  const alert = notCheckedAlert({ reason: "check_stopped_by_user", notificationLevel: "check", answer: "failed" });
  assert.ok(parseFallEvent(alert));
  assert.equal(parseFallEvent({ ...alert, notificationLevel: "urgent" }), null);
  assert.equal(buildFallNotification({ deviceId: "robot-a", notificationId: alert.eventId,
    incidentId: alert.incidentId, level: "check", reason: alert.reason, occurredAt: alert.occurredAt }).body,
  "말벗이 확인하러 가던 중 '그만'이라는 말을 듣고 멈췄습니다. 상태를 확인해 주세요.");
  await withRepo(async ({ h, events }) => {
    const [opened, stop, notice, help] = journal([
      ["incident_opened", "cloud_crosscheck"],
      ["check_stopped", "user_stop", { state: "recheck_required", answer: "failed" }],
      ["notification_requested", "check_stopped_by_user",
        { state: "recheck_required", answer: "failed", notificationLevel: "check" }],
      ["notification_requested", "help_requested",
        { state: "help_required", answer: "help_request", notificationLevel: "urgent" }],
    ]);
    for (const e of [opened, stop, notice, help, notice]) await events.storeFallEvent("robot-a", e);
    assert.deepEqual((await h.db.query("SELECT reason,status FROM fall_push_outbox ORDER BY reason")).rows
      .map((r) => [r.reason, r.status]), [["check_stopped_by_user", "pending"], ["help_requested", "pending"]]);
  });
});
