import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { moduleLoader } from "./helpers/fall-db-harness.mjs";

const load = moduleLoader();
const { fallEventLabel, nounEnding, plainLabels } = load("app/fall-event-label.ts");
const { parseFallEvent, readFallEvent } = load("app/fall-contract.ts");
const analysis = (changes = {}) => ({ requestId: randomUUID(), purpose: "crosscheck",
  assessment: "suspected_fall", explanation: "바닥 가까이 누운 것으로 보이는 형상이 있습니다.", ...changes });
const event = (changes = {}) => ({ schemaVersion: 1, eventId: randomUUID(), incidentId: randomUUID(),
  bootId: "test-boot", sequence: 1, evidenceRevision: 1, occurredAt: "2026-10-06T05:54:00.641Z",
  eventKind: "analysis_completed", state: "verifying", fallSeen: false,
  assessment: "suspected_fall", answer: null, reason: null, notificationLevel: null, ...changes });

for (const [reason, expected] of [
  ["pose_rapid_posture_change", "급격한 자세 변화"],
  ["pose_sustained_horizontal_posture", "누운 자세 추정"],
  ["pose_sustained_low_posture", "낮은 자세 지속"],
  ["pose_candidate", "감지 근거 기록 없음"],
]) {
  test(`Pose displays measured reason once: ${reason}`, () => {
    for (const eventKind of ["incident_opened", "incident_updated"]) {
      for (const assessment of [null, "suspected_fall", "normal_activity", "observed_fall"]) {
        assert.equal(fallEventLabel(event({ eventKind, reason, assessment, answer: "okay" })),
          `말벗: 낙상 의심 발견 (자세 분석: ${expected})`);
      }
    }
  });
}

test("the 14:54 cloud scene is not attributed to YOLO; unknown history stays unknown", () => {
  for (const reason of ["target_unidentified", "cloud_crosscheck"]) {
    assert.equal(fallEventLabel(event({ eventKind: "incident_opened", reason })), "클라우드 AI: 낙상 의심 발견");
  }
  for (const reason of [null, "unknown_future_reason", "__proto__", "constructor"]) {
    assert.equal(fallEventLabel(event({ eventKind: "incident_opened", reason })), "말벗: 사건 생성 · 낙상 의심");
  }
});

test("decision_required: answer is parenthesized", () => {
  const eventKind = "decision_required", label = "말벗: 추가 판단 필요";
  assert.equal(fallEventLabel(event({ eventKind, answer: "unclear" })), `${label} · 낙상 의심 (잘 알아듣지 못함)`);
  assert.equal(fallEventLabel(event({ eventKind, answer: "no_response" })), `${label} · 낙상 의심 (대답 없음)`);
  assert.equal(fallEventLabel(event({ eventKind, assessment: null, answer: "unclear" })), `${label} (잘 알아듣지 못함)`);
  assert.equal(fallEventLabel(event({ eventKind, answer: null })), `${label} · 낙상 의심`);
});

// 웹 용어 F: 줄마다 "누가: 무엇". 답 줄은 "대답: 무엇", 알림 줄은 "알림: 단계 · 무엇".
test("answers read 대답: 무엇 without the video judgment", () => {
  for (const eventKind of ["voice_result", "confirmation_completed"]) {
    assert.equal(fallEventLabel(event({ eventKind, answer: "help_request" })), "대답: 도움 요청");
    assert.equal(fallEventLabel(event({ eventKind, answer: "okay", assessment: "observed_fall" })), "대답: 괜찮다고 함");
    assert.equal(fallEventLabel(event({ eventKind, answer: null })), "대답: 기록 없음");
  }
  assert.equal(fallEventLabel(event({ eventKind: "voice_result", answer: "no_response" })), "대답: 없음");
  // 영상만 본 장면: 괜찮다는 답을 "잘 알아듣지 못함"으로 보이지 않는다(2026-10-12 16:31).
  assert.equal(fallEventLabel(event({ eventKind: "voice_result", answer: "unclear",
    reason: "scene_answer_unassociated" })), "대답: 괜찮다고 함 · 쓰러진 분인지 모름");
});

test("every alert reads 알림: level · what", () => {
  const notice = (reason, notificationLevel) => fallEventLabel(event({
    eventKind: "notification_requested", reason, notificationLevel, answer: "help_request" }));
  assert.equal(notice("help_requested", "urgent"), "알림: 긴급 · 도움 요청");
  assert.equal(notice("confirmation_help_required", "urgent"), "알림: 긴급 · 도움이 필요해 보임");
  assert.equal(notice("person_no_response", "check"), "알림: 확인 필요 · 물어봤지만 대답이 없음");
  assert.equal(notice("check_required_not_confirmed_fall", "check"), "알림: 확인 필요 · 넘어졌는지 확실하지 않음");
  assert.equal(notice("fall_observed_person_okay", "info"), "알림: 일반 · 넘어졌지만 괜찮다고 함");
  assert.equal(notice("robot_could_not_confirm", "urgent"), "알림: 긴급 · 말벗이 직접 확인하지 못함");
  assert.equal(notice("scene_answer_unconfirmed", "check"), "알림: 확인 필요 · 괜찮다는 대답, 쓰러진 분인지 모름");
  assert.equal(notice("help_requested", undefined), "알림: 긴급 · 도움 요청");
  assert.equal(notice("unknown_future_reason", "check"), "알림: 확인 필요");
  // A failed question, older reasons included, reads like the reason box.
  for (const reason of ["confirmation_transport_failed", "agent_failed", "question_error"]) {
    assert.equal(fallEventLabel(event({ eventKind: "agent_check_failed", reason })), "말벗: 질문 중 오류가 나 물을 수 없음");
  }
});

test("silence stays no response in stored events and labels, distinct from help and faults", () => {
  const silent = event({ eventKind: "voice_result", state: "recheck_required",
    answer: "no_response", reason: "no_response" });
  assert.deepEqual(parseFallEvent(silent), silent);
  assert.equal(fallEventLabel(silent), "대답: 없음");
  assert.equal(fallEventLabel(event({ eventKind: "confirmation_completed", state: "help_required",
    answer: "help_request", reason: "unknown" })), "대답: 도움 요청");
  assert.equal(fallEventLabel(event({ eventKind: "agent_check_failed", state: "recheck_required",
    answer: "failed", reason: "question_error" })), "말벗: 질문 중 오류가 나 물을 수 없음");
});

test("a question request never invents spoken text, delivery or an old assessment", () => {
  assert.equal(fallEventLabel(event({ eventKind: "question_requested", answer: "unclear" })), "말벗: 물어봄");
});

test("Cloud shows the actual explanation and matching request assessment, not retained stronger state", () => {
  const record = analysis();
  assert.equal(fallEventLabel(event({ assessment: "observed_fall", answer: "help_request", analysis: record })),
    `클라우드 AI: 낙상 의심 (${nounEnding(record.explanation)})`);
  assert.equal(fallEventLabel(event()), "클라우드 AI: 낙상 의심 (AI 이유 없음)");
  // Person-association errors are not explanations of why the VLM suspected a fall.
  assert.equal(fallEventLabel(event({ reason: "target_unidentified" })), "클라우드 AI: 낙상 의심 (AI 이유 없음)");
});

test("bounded optional explanation accepts old records and canonicalizes nested ordering", async () => {
  const legacy = event();
  assert.deepEqual(parseFallEvent(legacy), legacy);
  const record = analysis();
  const valid = event({ analysis: record });
  assert.deepEqual(parseFallEvent(valid), valid);
  assert.equal(JSON.stringify(parseFallEvent({ ...valid, analysis: {
    explanation: record.explanation, assessment: record.assessment, purpose: record.purpose, requestId: record.requestId,
  } })), JSON.stringify(parseFallEvent(valid)));
  for (const text of ["한".repeat(1000), "😀".repeat(1000)]) {
    const body = JSON.stringify(event({ analysis: analysis({ explanation: text }) }));
    assert.ok(Buffer.byteLength(body) <= 8192);
    assert.ok(await readFallEvent(new Request("https://test/api", {
      method: "POST", headers: { "content-type": "application/json" }, body,
    })));
  }
  for (const change of [
    { analysis: null }, { analysis: [] }, { analysis: {} },
    { eventKind: "question_requested", analysis: record },
    { analysis: analysis({ explanation: "" }) }, { analysis: analysis({ explanation: " " }) },
    { analysis: analysis({ explanation: "x".repeat(1001) }) },
    { analysis: analysis({ image: "base64 pixels" }) },
    { analysis: analysis({ transcript: "speech" }) },
    { analysis: analysis({ purpose: "invented" }) }, { analysis: analysis({ assessment: "safe" }) },
    { analysis: analysis({ requestId: "bad request id" }) },
  ]) assert.equal(parseFallEvent(event(change)), null, JSON.stringify(change));
});

test("model text is rendered as text, never HTML or instructions", () => {
  const explanation = '<script>alert("x")</script><img src=x onerror=alert(1)>';
  const text = fallEventLabel(event({ analysis: analysis({ explanation }) }));
  const html = renderToStaticMarkup(createElement("span", null, text));
  assert.ok(html.includes("&lt;script&gt;"));
  assert.ok(!html.includes("<script>") && !html.includes("<img"));
  const panel = readFileSync(new URL("../app/components/fall-incidents-panel.tsx", import.meta.url), "utf8");
  // Labels come from fallHistoryLines (fallEventLabel per event) and render as text.
  assert.ok(panel.includes("fallHistoryLines(detail.robotEvents)") && panel.includes("{label}</span>"));
  assert.ok(!panel.includes("dangerouslySetInnerHTML"));
  assert.ok(!panel.includes('incident_opened: "자세 분석: 낙상 의심"'));
});

test("history shows AI explanations with short noun endings, keeping the stored text", () => {
  for (const [input, expected] of [
    ["가구와 옷가지 사이에 사람이 바닥에 쓰러져 있는 모습이 관찰됩니다.", "가구와 옷가지 사이에 사람이 바닥에 쓰러져 있는 모습이 관찰됨"],
    ["하강 과정은 보이지 않으나, 확인이 필요합니다.", "하강 과정은 보이지 않으나, 확인이 필요함"],
    ["형상이 있습니다. 움직임이 없습니다.", "형상이 있음. 움직임이 없음"],
    ["일상적인 실내 환경입니다", "일상적인 실내 환경임"],
    ["정상으로 보입니다!", "정상으로 보임!"],
    ["사람이 관찰되지 않음", "사람이 관찰되지 않음"],
    ["다니다 넘어짐", "다니다 넘어짐"],
  ]) assert.equal(nounEnding(input), expected);
  const label = fallEventLabel(event({ analysis: analysis() }));
  assert.equal(label, "클라우드 AI: 낙상 의심 (바닥 가까이 누운 것으로 보이는 형상이 있음)");
});

// 웹 용어 10번: 실기 10-17 22:19 "…활동 중이므로 floor_active로 판단됩니다."
test("English posture labels in AI explanations read as Korean with matching particles", () => {
  for (const [input, expected] of [
    ["활동 중이므로 floor_active로 판단됩니다.", "활동 중이므로 바닥에서 활동으로 판단됩니다."],
    ["엉덩이가 닿아 있어 floor로 판단됩니다.", "엉덩이가 닿아 있어 바닥으로 판단됩니다."],
    ["'furniture'로 분류합니다.", "침대·의자로 분류합니다."],
    ["standing이 아니라 floor입니다.", "서 있음이 아니라 바닥입니다."],
    ["어두워 unclear를 고릅니다.", "어두워 알 수 없음을 고릅니다."],
    ["\"floor_active\"와 다릅니다.", "바닥에서 활동과 다릅니다."],
    // 이며·이다 is not the subject particle: the label changes, the ending stays.
    ["floor_active이며 손이 움직입니다.", "바닥에서 활동이며 손이 움직입니다."],
    ["Floor 상태", "바닥 상태"],
    // Not a label: left as written.
    ["floorboard 위, outstanding 없음", "floorboard 위, outstanding 없음"],
  ]) assert.equal(plainLabels(input), expected);
  const label = fallEventLabel(event({ eventKind: "posture_check_completed", reason: "active_on_floor",
    analysis: analysis({ purpose: "posture_check", assessment: "normal_activity",
      explanation: "상체가 세워져 있어 floor_active로 판단됩니다." }) }));
  assert.equal(label, "클라우드 AI: 몸 전체를 보니 바닥에 앉아 활동 중 (상체가 세워져 있어 바닥에서 활동으로 판단됨)");
  const panel = readFileSync(new URL("../app/components/fall-incidents-panel.tsx", import.meta.url), "utf8");
  assert.ok(panel.includes("<p>{plainLabels(latest.explanation)}</p>"));
});


test("approach events read as short history lines (mockup 22)", () => {
  const label = (eventKind, reason, change = {}) => fallEventLabel(event({ eventKind, reason, ...change }));
  assert.equal(label("approach_started", "patrol_stopped"), "말벗: 순찰을 멈추고 확인하러 감 (사람인지 확실하지 않음)");
  assert.equal(label("approach_started", null), "말벗: 확인하러 감 (사람인지 확실하지 않음)");
  assert.equal(label("approach_completed", "arrived"), "말벗: 넘어진 것 같은 자리 1 m 앞에 도착");
  assert.equal(label("approach_completed", "no_path"), "말벗: 가까이 가지 못함 (갈 길 없음) → 이 자리에서 물어봄");
  assert.equal(label("approach_completed", "no_map"), "말벗: 가까이 가지 못함 (지도 없음) → 이 자리에서 물어봄");
  assert.equal(label("person_check_completed", "person"), "클라우드 AI: 가까이에서 보니 사람");
  assert.equal(label("person_check_completed", "not_a_person", { analysis: analysis({
    purpose: "person_check", assessment: "normal_activity", explanation: "가방과 옷가지만 보입니다." }) }),
  "클라우드 AI: 가까이에서 보니 사람 아님 (가방과 옷가지만 보임)");
  assert.equal(label("person_check_completed", "not_a_person"), "클라우드 AI: 가까이에서 보니 사람 아님");
  assert.equal(label("incident_resolved", "not_a_person", { state: "resolved" }), "말벗: 사건 종료 · 사람 아님");
  assert.equal(label("approach_returned", "returned", { state: "resolved" }), "말벗: 원래 자리로 돌아옴");
  assert.equal(label("approach_returned", "return_failed", { state: "resolved" }), "말벗: 원래 자리로 돌아가지 못함");
});

test("whole-body check events read as short history lines (mockup 25)", () => {
  const label = (eventKind, reason, change = {}) => fallEventLabel(event({ eventKind, reason, ...change }));
  assert.equal(label("person_check_completed", "partial_body"), "클라우드 AI: 가까이에서 보니 사람 · 몸 일부만 보임");
  assert.equal(label("person_check_completed", "whole_body"), "클라우드 AI: 가까이에서 보니 사람 · 몸 전체 보임");
  assert.equal(label("posture_check_completed", "posture_unconfirmed"), "클라우드 AI: 자세를 확인하지 못함");
  assert.equal(label("view_completed", "arrived"), "말벗: 몸 전체가 보이는 자리로 옮김");
  assert.equal(label("view_completed", "no_path"), "말벗: 몸 전체가 보이는 자리로 못 감 (갈 길 없음)");
  assert.equal(label("posture_check_completed", "sitting_off_floor", { analysis: analysis({
    purpose: "posture_check", assessment: "normal_activity", explanation: "침대 가장자리에 걸터앉아 있습니다." }) }),
  "클라우드 AI: 몸 전체를 보니 침대나 의자에 앉아 있음 (침대 가장자리에 걸터앉아 있음)");
  assert.equal(label("posture_check_completed", "standing"), "클라우드 AI: 몸 전체를 보니 서 있음");
  assert.equal(label("posture_check_completed", "on_floor"), "클라우드 AI: 몸 전체를 보니 바닥에 있음");
  assert.equal(label("posture_check_completed", "body_unseen"), "말벗: 몸 전체를 못 봄 → 이 자리에서 물어봄");
  assert.equal(label("posture_check_completed", "judgements_differ"), "말벗: 잰 값과 클라우드 AI 판정이 다름 → 물어봄");
  assert.equal(label("incident_resolved", "off_floor_verified", { state: "resolved" }),
    "말벗: 사건 종료 · 바닥에 있지 않음 (묻지 않음)");
  // 2026-10-14: sitting up busy on the floor (a low table) is not asked either.
  assert.equal(label("posture_check_completed", "active_on_floor"), "클라우드 AI: 몸 전체를 보니 바닥에 앉아 활동 중");
  assert.equal(label("incident_resolved", "floor_activity_verified", { state: "resolved" }),
    "말벗: 사건 종료 · 바닥에 앉아 활동 중 (묻지 않음)");
});

test("the list says the robot is checking before it asks, and off-floor closures say so (mockup 25)", () => {
  const view = load("app/fall-incident-view.ts");
  const base = { incidentId: "i", origin: "robot", category: "check", state: "verifying", fallSeen: false,
    assessment: "suspected_fall", answer: null, notificationRank: 0, occurredAt: "2026-10-06T05:54:00.641Z",
    updatedAt: "2026-10-06T05:54:00.641Z", reviewState: "open", closedAt: null, closedBy: null, closedByName: null,
    reopenedAt: null, needsCheck: true, aiFailed: false, unacknowledged: false, reviewPending: true, foundDown: false,
    reportedBy: null, reportedByName: null, reportedMomentAt: null, opinionCounts: {} };
  const at = (kind, reason = null, change = {}) => ({ ...base, approach: { kind, reason }, ...change });
  for (const [incident, title] of [
    [at("approach_started"), "가까이 가는 중"],
    [at("person_check_completed", "partial_body"), "몸 전체가 보이는 자리로 옮기는 중"],
    [at("view_completed", "arrived"), "몸 전체를 보는 중"],
  ]) {
    assert.equal(view.incidentTitle(incident), title);
    assert.deepEqual(view.incidentBadges(incident), [["is-info", "확인 중"], ["is-step", "검토 전"]]);
  }
  // Once the person is asked, the title is the suspicion again.
  const asked = at("view_completed", "arrived", { asked: true });
  assert.equal(view.incidentTitle(asked), "낙상 의심");
  assert.deepEqual(view.incidentBadges(asked)[0], ["is-check", "확인 필요"]);
  // Off-floor closures: the title is the conclusion, the subtitle what the robot did, one badge.
  for (const [reason, title] of [["sitting_off_floor", "침대나 의자에 앉아 있음"], ["standing", "서 있음"],
    ["active_on_floor", "바닥에 앉아 활동 중"]]) {
    const closed = at("posture_check_completed", reason, { category: "off_floor" });
    assert.equal(view.incidentTitle(closed), title);
    assert.match(view.incidentSubtitle(closed), / · 몸 일부만 보여 가서 확인 · 묻지 않음$/);
    assert.deepEqual(view.incidentBadges(closed), [["is-neutral", "처리 완료"]]);
  }
  const panel = readFileSync(new URL("../app/components/fall-incidents-panel.tsx", import.meta.url), "utf8");
  assert.ok(panel.includes("묻지도, 알리지도 않았어요."));
});

test("incident badges: one status and one review step while open, only 처리 완료 once closed (mockup 35·36)", () => {
  const view = load("app/fall-incident-view.ts");
  const base = { incidentId: "i", origin: "robot", category: "check", state: "notified", fallSeen: false,
    assessment: "suspected_fall", answer: null, notificationRank: 2, occurredAt: "2026-10-06T05:54:00.641Z",
    updatedAt: "2026-10-06T05:54:00.641Z", reviewState: "open", closedAt: null, closedBy: null, closedByName: null,
    reopenedAt: null, needsCheck: true, aiFailed: false, unacknowledged: false, reviewPending: true, foundDown: false,
    reportedBy: null, reportedByName: null, reportedMomentAt: null, opinionCounts: {} };
  const badges = (change) => view.incidentBadges({ ...base, ...change }).map(([, text]) => text);
  assert.deepEqual(badges({}), ["확인 필요", "검토 전"]);
  assert.deepEqual(badges({ unacknowledged: true }), ["응답 없음", "검토 전"]);
  assert.deepEqual(badges({ robotCheck: { reason: "x", waited: true, stopped: false, failed: null, revisit: null } }),
    ["긴급", "검토 전"]);
  assert.deepEqual(badges({ category: "normal", answer: "okay" }), ["정상", "검토 전"]);
  assert.deepEqual(badges({ origin: "user_report", category: "report" }), ["직접 신고", "검토 전"]);
  assert.deepEqual(badges({ opinionCounts: { fall: 1 } }), ["확인 필요", "처리 전"]);
  assert.deepEqual(badges({ opinionCounts: { fall: 2, normal: 0 } }), ["확인 필요", "처리 전"]);
  assert.deepEqual(badges({ opinionCounts: { fall: 1, normal: 1 } }), ["확인 필요", "의견 다름"]);
  for (const change of [{ reviewState: "closed" }, { category: "not_person" }, { category: "merged" }]) {
    assert.deepEqual(badges({ ...change, unacknowledged: true, opinionCounts: { fall: 1, normal: 1 } }), ["처리 완료"]);
  }
  // Titles: the conclusion; subtitles: when · 재확인 · N회차 · what happened.
  assert.equal(view.incidentTitle({ ...base, fallSeen: true }), "낙상");
  assert.equal(view.incidentTitle({ ...base, category: "normal", answer: "okay" }), "괜찮다고 답함");
  assert.equal(view.incidentTitle({ ...base, category: "merged" }), "다른 사건에 병합됨");
  assert.match(view.incidentSubtitle({ ...base, checkResult: { round: 2, title: "t", note: null,
    summary: "바닥에 있음 · 대답 없음", looking: null } }), / · 재확인 · 2회차 · 바닥에 있음 · 대답 없음$/);
  // The bottom line: sends, reopen and AI failure, never on a closed incident's sends.
  const notification = { level: "urgent", sent: 2, total: 3 };
  assert.deepEqual(view.incidentExtras({ ...base, notification, reopenedAt: base.occurredAt, aiFailed: true }),
    ["알림 2/3", "다시 열림", "AI 판정 실패"]);
  assert.deepEqual(view.incidentExtras({ ...base, notification: { ...notification, sent: 1 } }), []);
  assert.deepEqual(view.incidentExtras({ ...base, notification: { ...notification, sent: 1 }, unacknowledged: true }),
    ["알림 1/3"]);
  assert.deepEqual(view.incidentExtras({ ...base, notification, reviewState: "closed", reopenedAt: base.occurredAt }), []);
});

test("whole-body check events and the off-floor closure are strict contracts", () => {
  const ok = (change) => assert.ok(parseFallEvent(event(change)), JSON.stringify(change));
  const bad = (change) => assert.equal(parseFallEvent(event(change)), null, JSON.stringify(change));
  ok({ eventKind: "person_check_completed", reason: "partial_body",
    analysis: analysis({ purpose: "person_check" }) });
  ok({ eventKind: "view_completed", reason: "arrived" });
  ok({ eventKind: "view_completed", reason: "no_path" });
  ok({ eventKind: "person_check_completed", reason: "whole_body" });
  for (const reason of ["sitting_off_floor", "standing", "active_on_floor", "on_floor", "body_unseen",
    "judgements_differ", "posture_unconfirmed"]) {
    ok({ eventKind: "posture_check_completed", reason });
  }
  ok({ eventKind: "posture_check_completed", reason: "sitting_off_floor",
    analysis: analysis({ purpose: "posture_check", assessment: "normal_activity" }) });
  ok({ eventKind: "incident_resolved", state: "resolved", reason: "off_floor_verified" });
  ok({ eventKind: "incident_resolved", state: "resolved", reason: "floor_activity_verified" });
  bad({ eventKind: "posture_check_completed", reason: "floor_active" });  // A Cloud verdict, not a reason.
  bad({ eventKind: "view_completed", reason: null });
  bad({ eventKind: "view_completed", reason: "returned" });
  bad({ eventKind: "posture_check_completed", reason: null });
  bad({ eventKind: "posture_check_completed", reason: "sitting" });
  bad({ eventKind: "posture_check_completed", reason: "standing", analysis: analysis() });  // purpose crosscheck
  bad({ eventKind: "posture_check_completed", reason: "standing", analysis: analysis({ purpose: "person_check" }) });
  bad({ eventKind: "analysis_completed", analysis: analysis({ purpose: "posture_check" }) });
  bad({ eventKind: "view_completed", state: "resolved", reason: "arrived" });
});

test("approach events and the not-a-person closure are strict contracts", () => {
  const ok = (change) => assert.ok(parseFallEvent(event(change)), JSON.stringify(change));
  const bad = (change) => assert.equal(parseFallEvent(event(change)), null, JSON.stringify(change));
  ok({ eventKind: "approach_started", reason: null });
  ok({ eventKind: "approach_started", reason: "follow_stopped" });
  ok({ eventKind: "approach_completed", reason: "timeout" });
  ok({ eventKind: "person_check_completed", reason: "person" });
  ok({ eventKind: "person_check_completed", reason: "not_a_person",
    analysis: analysis({ purpose: "person_check", assessment: "normal_activity" }) });
  ok({ eventKind: "incident_resolved", state: "resolved", reason: "not_a_person" });
  ok({ eventKind: "approach_returned", state: "resolved", reason: "returned" });
  bad({ eventKind: "approach_started", reason: "guess" });
  bad({ eventKind: "approach_completed", reason: null });
  bad({ eventKind: "person_check_completed", reason: "maybe" });
  bad({ eventKind: "person_check_completed", reason: "person", analysis: analysis() });  // purpose crosscheck
  bad({ eventKind: "approach_started", reason: null, analysis: analysis({ purpose: "person_check" }) });
  bad({ eventKind: "approach_started", state: "resolved", reason: null });
  bad({ eventKind: "incident_resolved", state: "resolved", reason: "nobody_there" });
});

test("an alert not sent because one went out from the same place reads two lines (SWM25-267, mockup 55 ③)", () => {
  const grouped = (reason, change = {}) => fallEventLabel(event({ eventKind: "notification_grouped", reason, ...change }));
  assert.equal(grouped("confirmation_help_required", { answer: "help_request", state: "help_required" }),
    "말벗: 같은 자리에서 다시 확인함 · 도움 요청 / 알림: 10분 안 같은 단계라 다시 보내지 않음");
  assert.equal(grouped("person_no_response"),
    "말벗: 같은 자리에서 다시 확인함 · 물어봤지만 대답이 없음 / 알림: 10분 안 같은 단계라 다시 보내지 않음");
  assert.equal(grouped("unknown_future_reason"),
    "말벗: 같은 자리에서 다시 확인함 / 알림: 10분 안 같은 단계라 다시 보내지 않음");
  const ok = (change) => assert.ok(parseFallEvent(event(change)), JSON.stringify(change));
  const bad = (change) => assert.equal(parseFallEvent(event(change)), null, JSON.stringify(change));
  ok({ eventKind: "notification_grouped", reason: "confirmation_help_required", state: "help_required",
    answer: "help_request" });
  // The incident that sent the first alert may be closed already.
  ok({ eventKind: "notification_grouped", reason: "help_requested", state: "resolved" });
  ok({ eventKind: "notification_grouped", reason: "left_unseen", checkRound: 2 });
  bad({ eventKind: "notification_grouped", reason: null });
  bad({ eventKind: "notification_grouped", reason: "reopened_by_opinion" });  // Web-only.
  bad({ eventKind: "notification_grouped", reason: "made_up" });
  bad({ eventKind: "notification_grouped", reason: "help_requested", notificationLevel: "urgent" });
});
