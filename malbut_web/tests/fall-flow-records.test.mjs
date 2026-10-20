import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import test from "node:test";
import { fallDatabase, moduleLoader } from "./helpers/fall-db-harness.mjs";
import { buildFallNotification, isFallNotification } from "../infra/aws/push-broker/fall-notification.mjs";

// 하나로 합친 낙상 확인의 기록(SWM25-261, 목업 29~32번): 새 기록 종류, 회차, 회차마다 세는 알림.

const load = moduleLoader();
const { fallHistoryLines, fallCheckResult } = load("app/fall-event-label.ts");
const { parseFallEvent } = load("app/fall-contract.ts");
const { fallCheckView } = load("app/robot-fall-check-copy.ts");

const T0 = "2026-10-17T05:02:10.000Z";
const at = (ms) => new Date(Date.parse(T0) + ms).toISOString();
const event = (change = {}) => ({ schemaVersion: 1, eventId: randomUUID(), incidentId: randomUUID(),
  bootId: "boot-1", sequence: 1, evidenceRevision: 1, occurredAt: T0, eventKind: "incident_opened",
  state: "verifying", fallSeen: false, assessment: "suspected_fall", answer: null, reason: null,
  notificationLevel: null, checkRound: 1, ...change });
const ai = (purpose, explanation) => ({ requestId: randomUUID(), purpose, assessment: "suspected_fall", explanation });

/** One incident of the one check: [kind, reason, change]; every event carries its round. */
function journal(rows) {
  const incidentId = randomUUID();
  return rows.map(([eventKind, reason, change = {}], index) => event({
    incidentId, sequence: index + 1, occurredAt: at(index * 1000), eventKind, reason, ...change }));
}
const labels = (events) => fallHistoryLines(events).map((l) => l.label);

test("the robot sends the check round and the new kinds; a recheck must be a later round", () => {
  for (const [eventKind, reason] of [["arrival_compared", "different_person"], ["reverify_completed", "arrived"],
    ["departure_reviewed", "person_crawled"], ["fall_moment_reviewed", "self"]]) {
    assert.ok(parseFallEvent(event({ eventKind, reason })), eventKind);
  }
  assert.ok(parseFallEvent(event({ eventKind: "arrival_compared", reason: "gone",
    analysis: ai("arrival_check", "그 자리가 비어 있습니다.") })));
  assert.equal(parseFallEvent(event({ eventKind: "arrival_compared", reason: "gone",
    analysis: ai("posture_check", "x") })), null);
  assert.equal(parseFallEvent(event({ eventKind: "arrival_compared", reason: "maybe" })), null);
  assert.ok(parseFallEvent(event({ eventKind: "recheck_started", reason: "floor_recheck", checkRound: 2 })));
  assert.equal(parseFallEvent(event({ eventKind: "recheck_started", reason: "floor_recheck" })), null);
  assert.equal(parseFallEvent(event({ eventKind: "recheck_started", reason: "floor_recheck", checkRound: 2,
    state: "resolved" })), null);
  for (const checkRound of [0, 1.5, "2", null]) assert.equal(parseFallEvent(event({ checkRound })), null);
  const older = event();
  delete older.checkRound;
  assert.ok(parseFallEvent(older), "the older check sends no round");
  assert.ok(parseFallEvent(event({ eventKind: "incident_resolved", state: "resolved", reason: "left_the_spot" })));
  for (const [reason, level] of [["person_may_differ", "check"], ["left_unseen", "check"],
    ["could_not_compare", "check"], ["person_crawled_away", "check"], ["closed_after_alert", "info"]]) {
    assert.ok(parseFallEvent(event({ eventKind: "notification_requested", state: "recheck_required",
      notificationLevel: level, reason })), reason);
  }
});

test("[재확인 · N회차] marks a later round's notification and survives the broker check", () => {
  const base = { deviceId: "robot-a", notificationId: randomUUID(), incidentId: randomUUID(), level: "urgent",
    reason: "robot_could_not_confirm", occurredAt: T0 };
  const again = buildFallNotification({ ...base, checkRound: 2 });
  assert.equal(again.body, "[재확인 · 2회차] 말벗이 넘어진 것 같은 분을 직접 확인하지 못했어요.\n지금 바로 확인해 주세요.");
  assert.equal(again.data.checkRound, "2");
  assert.ok(isFallNotification(again));
  assert.ok(buildFallNotification({ ...base, resend: true, checkRound: 3 }).body.startsWith("[재발신] [재확인 · 3회차] "));
  assert.equal(buildFallNotification({ ...base, checkRound: "yes" }), null);
  assert.equal(buildFallNotification({ ...base, checkRound: 0 }), null);
  // The first round has no mark and no round in the data.
  const first = buildFallNotification({ ...base, checkRound: 1 });
  assert.ok(!first.body.includes("재확인"));
  assert.equal(first.data.checkRound, undefined);
  assert.ok(isFallNotification(first));
});

async function withRepo(work) {
  const h = await fallDatabase(), repoLoad = moduleLoader();
  try {
    await repoLoad("db/postgres.ts").withPostgresPoolForTest(h.pool, () => work({
      h, events: repoLoad("db/fall-incidents.ts"), review: repoLoad("db/fall-review.ts"),
    }));
  } finally { await h.db.close(); }
}

test("a recheck alerts again for a reason already sent in an earlier round (decision B)", async () => {
  await withRepo(async ({ h, events }) => {
    const alert = (round, sequence, reason = "robot_could_not_confirm", change = {}) => event({
      incidentId, sequence, occurredAt: at(sequence * 1000), eventKind: "notification_requested",
      state: "recheck_required", notificationLevel: "urgent", reason, checkRound: round, ...change });
    const incidentId = randomUUID();
    await events.storeFallEvent("robot-a", event({ incidentId }));
    await events.storeFallEvent("robot-a", alert(1, 2));
    await events.storeFallEvent("robot-a", alert(1, 3));  // Same round: once.
    await events.storeFallEvent("robot-a", event({ incidentId, sequence: 4, eventKind: "recheck_started",
      reason: "floor_recheck", checkRound: 2 }));
    await events.storeFallEvent("robot-a", alert(2, 5));
    const help = { state: "help_required", answer: "help_request" };
    await events.storeFallEvent("robot-a", alert(2, 6, "confirmation_help_required", help));
    const rows = (await h.db.query(
      "SELECT reason,check_round,status FROM fall_push_outbox ORDER BY check_round,reason")).rows;
    assert.deepEqual(rows.map((r) => [r.reason, r.check_round, r.status]), [
      ["robot_could_not_confirm", 1, "pending"],
      ["confirmation_help_required", 2, "pending"], ["robot_could_not_confirm", 2, "pending"]]);
    const claimed = [];
    for (let claim; (claim = await events.claimFallPush("robot-a"));) {
      claimed.push([claim.reason, claim.checkRound]);
      await events.finishFallPush(claim, true, null);
    }
    assert.deepEqual(claimed.sort(), [["confirmation_help_required", 2], ["robot_could_not_confirm", 1],
      ["robot_could_not_confirm", 2]]);
  });
});

test("an older robot without rounds keeps one alert per reason", async () => {
  await withRepo(async ({ h, events }) => {
    const incidentId = randomUUID();
    const older = (sequence) => {
      const e = event({ incidentId, sequence, occurredAt: at(sequence * 1000), eventKind: "notification_requested",
        state: "recheck_required", notificationLevel: "urgent", reason: "robot_could_not_confirm" });
      delete e.checkRound;
      return e;
    };
    await events.storeFallEvent("robot-a", older(1));
    await events.storeFallEvent("robot-a", older(2));
    assert.equal((await h.db.query("SELECT count(*)::int AS n FROM fall_push_outbox")).rows[0].n, 1);
  });
});

test("목업 29번: the one check's history and result (바닥 → 물어봄, 넘어진 뒤 일어섬, 스스로 앉음)", () => {
  const floor = journal([
    ["incident_opened", "pose_sustained_horizontal_posture"],
    ["approach_started", "patrol_stopped"],
    ["approach_completed", "arrived"],
    ["arrival_compared", "same_person", { analysis: ai("arrival_check", "회색 상의가 같아 보입니다.") }],
    ["view_completed", "arrived"],
    ["posture_check_completed", "on_floor"],
    ["question_requested", null],
    ["confirmation_completed", "resolved", { state: "resolved", answer: "okay" }],
    ["incident_resolved", "response_completed", { state: "resolved", answer: "okay" }],
  ]);
  // 줄마다 "누가: 무엇", AI 판정과 말벗 결정은 줄을 나눔(웹 용어 F).
  assert.deepEqual(labels(floor).slice(1), [
    "말벗: 순찰을 멈추고 확인하러 감",
    "말벗: 넘어진 것 같은 자리 1 m 앞에 도착 (처음 본 방향)",
    "클라우드 AI: 처음 본 분이 그대로 있음 (회색 상의가 같아 보임)",
    "말벗: 몸 전체가 보이는 자리로 옮김",
    "클라우드 AI: 몸 전체를 보니 바닥에 있음",
    "말벗: 물어봄",
    "대답: 괜찮다고 함",
    "말벗: 확인을 마침 · 30분 뒤 다시 확인",
  ]);
  assert.equal(fallCheckResult(floor).title, "바닥에 있음 · 괜찮다고 답함");
  assert.equal(fallCheckResult(floor).looking, null);

  const up = journal([
    ["incident_opened", "pose_rapid_posture_change", { fallSeen: true }],
    ["approach_started", null],
    ["approach_completed", "arrived"],
    ["arrival_compared", "person_unsure"],
    ["view_completed", "arrived"],
    ["arrival_compared", "same_person"],
    ["posture_check_completed", "standing"],
    ["fall_moment_reviewed", "fall"],
    ["question_requested", null],
    ["confirmation_completed", "resolved", { state: "resolved", answer: "okay" }],
    ["incident_resolved", "response_completed", { state: "resolved", answer: "okay" }],
  ]);
  assert.deepEqual(labels(up).slice(3, 10), [
    "클라우드 AI: 사람은 있는데 같은 분인지 모름",
    "말벗: 몸 전체가 보이는 자리에서 다시 비교",
    "말벗: 몸 전체가 보이는 자리로 옮김",
    "클라우드 AI: 처음 본 분이 그대로 있음",
    "클라우드 AI: 몸 전체를 보니 서 있음",
    "클라우드 AI: 넘어지는 장면을 다시 보니 넘어진 것으로 보임",
    "말벗: 물어봄 (넘어지는 장면을 봐서 일어났어도)",
  ]);
  assert.equal(labels(up).at(-1), "말벗: 확인을 마침");
  assert.equal(fallCheckResult(up).title, "서 있음 · 넘어진 것으로 보여 물어봄 · 괜찮다고 답함");

  const self = journal([
    ["incident_opened", "pose_rapid_posture_change", { fallSeen: true }],
    ["approach_completed", "arrived"],
    ["arrival_compared", "same_person"],
    ["posture_check_completed", "sitting_off_floor"],
    ["fall_moment_reviewed", "self"],
    ["posture_check_completed", "sitting_off_floor"],
    ["incident_resolved", "off_floor_verified", { state: "resolved" }],
  ]);
  assert.deepEqual(labels(self).slice(4, 6), [
    "클라우드 AI: 넘어지는 장면을 다시 보니 스스로 앉거나 누운 동작으로 보임",
    "클라우드 AI: 몸 전체를 다시 보니 침대나 의자에 앉아 있음 (두 번 모두 같음)",
  ]);
  assert.equal(fallCheckResult(self).title, "침대나 의자에 앉아 있음 · 스스로 앉은 동작으로 보임 → 묻지 않음");
});

test("목업 29·30번: a different person keeps it open; an empty spot is looked at again first", () => {
  const other = journal([
    ["incident_opened", "cloud_crosscheck"],
    ["approach_completed", "arrived"],
    ["arrival_compared", "different_person"],
    ["notification_requested", "person_may_differ", { state: "recheck_required", notificationLevel: "check" }],
    ["view_completed", "arrived"],
    ["posture_check_completed", "standing"],
    ["posture_check_completed", "standing"],
  ]);
  assert.deepEqual(labels(other).slice(2), [
    "클라우드 AI: 처음 본 분과 다른 분이 있음",
    "알림: 확인 필요 · 처음 본 분과 다를 수 있음",
    "말벗: 몸 전체가 보이는 자리로 옮김",
    "클라우드 AI: 몸 전체를 보니 서 있음",
    "클라우드 AI: 몸 전체를 다시 보니 서 있음 (두 번 모두 같음)",
    "말벗: 다른 분이라 묻지 않음 · 사건은 열어 둠",
  ]);
  assert.equal(fallCheckResult(other).title, "다른 분이 서 있음 · 처음 본 분은 아직 확인하지 못함");

  const walked = journal([
    ["incident_opened", "cloud_crosscheck"],
    ["approach_completed", "arrived"],
    ["arrival_compared", "gone"],
    ["reverify_completed", "arrived"],
    ["arrival_compared", "gone"],
    ["departure_reviewed", "person_walked", { analysis: ai("departure_check", "일어나 오른쪽으로 걸어 나갔습니다.") }],
    ["incident_resolved", "left_the_spot", { state: "resolved" }],
  ]);
  assert.deepEqual(labels(walked).slice(2), [
    "클라우드 AI: 처음 본 자리가 비어 있음",
    "말벗: 처음 본 자리로 가서 다시 봄",
    "말벗: 처음 본 자리로 가서 같은 방향으로 다시 봄",
    "클라우드 AI: 처음 본 자리가 비어 있음",
    "말벗: 떠났는지 확인",
    "클라우드 AI: 사람이 걸어서 떠남 (일어나 오른쪽으로 걸어 나갔음)",
    "말벗: 사건 종료 · 그 자리를 떠남 (묻지 않음)",
  ]);
  assert.equal(fallCheckResult(walked).summary, "그 자리를 걸어서 떠남");

  const crawled = journal([
    ["incident_opened", "cloud_crosscheck"],
    ["arrival_compared", "gone"], ["reverify_completed", "arrived"], ["arrival_compared", "gone"],
    ["departure_reviewed", "person_crawled"],
    ["notification_requested", "person_crawled_away", { state: "recheck_required", notificationLevel: "check" }],
  ]);
  assert.equal(labels(crawled).at(-1), "알림: 확인 필요 · 바닥을 기어서 옮김");
  assert.equal(fallCheckResult(crawled).title, "그 자리에 없음 · 바닥을 기어서 옮긴 것으로 보임");

  const animal = journal([
    ["incident_opened", "cloud_crosscheck"],
    ["arrival_compared", "gone"], ["reverify_completed", "arrived"], ["arrival_compared", "gone"],
    ["departure_reviewed", "animal_left"],
    ["incident_resolved", "not_a_person", { state: "resolved" }],
  ]);
  assert.equal(fallCheckResult(animal).title, "동물이 떠남 · 사람 아님 → 묻지 않음");
});

test("목업 31번: a recheck is a new round, marked in the history and the result", () => {
  const blocked = journal([
    ["incident_opened", "cloud_crosscheck"],
    ["check_waiting", "manual_drive", { state: "verifying" }],
    ["check_not_confirmed", "waited_manual_drive", { state: "recheck_required" }],
    ["notification_requested", "robot_could_not_confirm", { state: "recheck_required", notificationLevel: "urgent" }],
    ["confirmation_completed", "resolved", { state: "resolved", answer: "okay" }],
    ["incident_resolved", "response_completed", { state: "resolved", answer: "okay" }],
    ["notification_requested", "closed_after_alert", { state: "resolved", notificationLevel: "info" }],
    ["recheck_started", "floor_recheck", { checkRound: 2 }],
    ["check_waiting", "manual_drive", { checkRound: 2 }],
    ["check_not_confirmed", "waited_manual_drive", { state: "recheck_required", checkRound: 2 }],
    ["notification_requested", "robot_could_not_confirm",
      { state: "recheck_required", notificationLevel: "urgent", checkRound: 2 }],
  ]);
  const lines = fallHistoryLines(blocked);
  assert.deepEqual(lines.filter((l) => l.divider).map((l) => [l.divider, l.label]), [
    ["1회차", "클라우드 AI: 낙상 의심 발견"], ["2회차", "말벗: 다시 확인 시작 (바닥에서 확인을 마친 지 30분)"]]);
  assert.ok(lines.some((l) => l.label === "알림: 일반 · 앞서 알린 상황의 확인을 마침"));
  assert.equal(lines.at(-1).label, "말벗: 1분째 확인하지 못함 → 긴급 알림");
  const result = fallCheckResult(blocked);
  assert.equal(result.round, 2);
  assert.equal(result.title, "2회차: 수동 조작 중이라 1분 안에 묻지 못함");

  const checking = blocked.slice(0, 8);
  assert.equal(fallCheckResult(checking).looking, "다시 확인하러 가는 중");
  // One round: no round marks.
  assert.ok(fallHistoryLines(blocked.slice(0, 7)).every((l) => !l.divider));
});

test("the older check keeps its labels (no round on its events)", () => {
  const older = journal([["approach_started", "patrol_stopped"], ["approach_completed", "arrived"]])
    .map((e) => { const rest = { ...e }; delete rest.checkRound; return rest; });
  assert.deepEqual(labels(older), ["말벗: 순찰을 멈추고 확인하러 감 (사람인지 확실하지 않음)", "말벗: 넘어진 것 같은 자리 1 m 앞에 도착"]);
  assert.equal(fallCheckResult(older), null);
});

test("a person who walked away is closed by the robot like off the floor (처리 완료)", async () => {
  await withRepo(async ({ events, review }) => {
    const rows = journal([
      ["incident_opened", "cloud_crosscheck"],
      ["arrival_compared", "gone"], ["reverify_completed", "arrived"], ["arrival_compared", "gone"],
      ["departure_reviewed", "person_walked"],
      ["incident_resolved", "left_the_spot", { state: "resolved" }],
    ]);
    for (const row of rows) await events.storeFallEvent("robot-a", row);
    const detail = await review.getFallIncidentDetail("robot-a", rows[0].incidentId);
    assert.equal(detail.category, "off_floor");
    assert.equal(detail.closeReason, "left_the_spot");
    assert.equal(detail.checkResult.summary, "그 자리를 걸어서 떠남");
    assert.ok(detail.robotEvents.every((e) => e.checkRound === 1));
  });
});

test("SWM25-269: an asked incident stays in 전체 when a later round finds them gone or up", async () => {
  await withRepo(async ({ events, review }) => {
    const asked = [
      ["incident_opened", "pose_sustained_horizontal_posture"],
      ["arrival_compared", "same_person"], ["posture_check_completed", "on_floor"], ["question_requested", null],
      ["confirmation_completed", "resolved", { state: "resolved", answer: "okay" }],
      ["incident_resolved", "response_completed", { state: "resolved", answer: "okay" }],
    ];
    for (const close of ["left_the_spot", "off_floor_verified"]) {
      const rows = journal([...asked,
        ["recheck_started", "stood_up", { checkRound: 2 }],
        ["arrival_compared", "gone", { checkRound: 2 }],
        ["incident_resolved", close, { state: "resolved", checkRound: 2 }]]);
      for (const row of rows.slice(0, asked.length)) await events.storeFallEvent("robot-a", row);
      const before = await review.getFallIncidentDetail("robot-a", rows[0].incidentId);
      for (const row of rows.slice(asked.length)) await events.storeFallEvent("robot-a", row);
      const after = await review.getFallIncidentDetail("robot-a", rows[0].incidentId);
      assert.equal(before.category, "check");
      assert.equal(after.category, before.category, close);  // Not "묻지도, 알리지도 않았어요".
      const all = await review.listFallIncidentSummaries("robot-a", "all");
      assert.ok(all.some((i) => i.incidentId === rows[0].incidentId), close);
    }
  });
});

test("목업 32번: the map card reads the one check's step and why it goes", () => {
  const card = (fall_check) => fallCheckView({ fall_check_active: true, fall_check });
  assert.equal(card({ phase: "look", step: "compare", purpose: "approach" }).title, "처음 봤을 때와 지금을 비교하고 있어요");
  assert.equal(card({ phase: "approach", step: "reverify", purpose: "approach" }).title, "처음 본 자리로 가서 다시 보고 있어요");
  assert.equal(card({ phase: "approach", step: "approach", purpose: "recheck" }).title,
    "30분 전에 바닥에서 확인한 분을 다시 보러 가고 있어요");
  assert.equal(card({ phase: "approach", step: "approach", purpose: "approach" }).detail, "처음 본 쪽에서 1 m 앞까지 가요.");
  // Waiting keeps 목업 27번; the older robot sends no step.
  assert.equal(card({ phase: "wait", step: "approach", waiting_for: "manual_drive" }).title,
    "수동 조작이 끝나면 낙상 확인을 이어 가요");
  assert.equal(card({ phase: "approach" }).title, "낙상 의심 장소를 확인하러 가고 있어요");
});
