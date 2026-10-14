import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { moduleLoader } from "./helpers/fall-db-harness.mjs";

const load = moduleLoader();
const { fallEventLabel, nounEnding } = load("app/fall-event-label.ts");
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
          `자세 분석: 낙상 의심 (${expected})`);
      }
    }
  });
}

test("the 14:54 cloud scene is not attributed to YOLO; unknown history stays unknown", () => {
  for (const reason of ["target_unidentified", "cloud_crosscheck"]) {
    assert.equal(fallEventLabel(event({ eventKind: "incident_opened", reason })), "클라우드 AI 발견: 낙상 의심");
  }
  for (const reason of [null, "unknown_future_reason", "__proto__", "constructor"]) {
    assert.equal(fallEventLabel(event({ eventKind: "incident_opened", reason })), "사건 생성: 낙상 의심");
  }
});

test("decision_required: answer is parenthesized", () => {
  const eventKind = "decision_required", label = "추가 판단 필요";
  assert.equal(fallEventLabel(event({ eventKind, answer: "unclear" })), `${label}: 낙상 의심 (응답 불분명)`);
  assert.equal(fallEventLabel(event({ eventKind, answer: "no_response" })), `${label}: 낙상 의심 (응답 없음)`);
  assert.equal(fallEventLabel(event({ eventKind, assessment: null, answer: "unclear" })), `${label} (응답 불분명)`);
  assert.equal(fallEventLabel(event({ eventKind, answer: null })), `${label}: 낙상 의심`);
});

// 목업 28번: 답 줄은 "질문에 대한 답 (답)", 알림 줄은 "단계 알림: 무엇"으로 통일.
test("answers read 질문에 대한 답 (답) without the video judgment", () => {
  for (const eventKind of ["voice_result", "confirmation_completed"]) {
    assert.equal(fallEventLabel(event({ eventKind, answer: "help_request" })), "질문에 대한 답 (도움 요청)");
    assert.equal(fallEventLabel(event({ eventKind, answer: "okay", assessment: "observed_fall" })),
      "질문에 대한 답 (괜찮다고 응답)");
    assert.equal(fallEventLabel(event({ eventKind, answer: null })), "질문에 대한 답");
  }
  assert.equal(fallEventLabel(event({ eventKind: "voice_result", answer: "no_response" })), "질문에 대한 답 (응답 없음)");
  // 영상만 본 장면: 괜찮다는 답을 "응답 불분명"으로 보이지 않는다(2026-10-12 16:31).
  assert.equal(fallEventLabel(event({ eventKind: "voice_result", answer: "unclear",
    reason: "scene_answer_unassociated" })), "질문에 대한 답 (괜찮다고 답함 · 쓰러진 분인지 모름)");
});

test("every alert reads level 알림: what", () => {
  const notice = (reason, notificationLevel) => fallEventLabel(event({
    eventKind: "notification_requested", reason, notificationLevel, answer: "help_request" }));
  assert.equal(notice("help_requested", "urgent"), "긴급 알림: 도움 요청");
  assert.equal(notice("confirmation_help_required", "urgent"), "긴급 알림: 상황 확인 결과 도움 필요");
  assert.equal(notice("person_no_response", "check"), "확인 필요 알림: 질문에 응답 없음");
  assert.equal(notice("check_required_not_confirmed_fall", "check"), "확인 필요 알림: 낙상 의심을 확인하지 못함");
  assert.equal(notice("fall_observed_person_okay", "info"), "일반 알림: 넘어졌지만 괜찮다고 응답");
  assert.equal(notice("robot_could_not_confirm", "urgent"), "긴급 알림: 말벗이 직접 확인하지 못함");
  assert.equal(notice("scene_answer_unconfirmed", "check"), "확인 필요 알림: 괜찮다는 답, 쓰러진 분인지 모름");
  assert.equal(notice("help_requested", undefined), "긴급 알림: 도움 요청");
  assert.equal(notice("unknown_future_reason", "check"), "확인 필요 알림");
  // A failed question, older reasons included, reads like the reason box.
  for (const reason of ["confirmation_transport_failed", "agent_failed", "question_error"]) {
    assert.equal(fallEventLabel(event({ eventKind: "agent_check_failed", reason })), "질문 중 오류가 나 물을 수 없음");
  }
});

test("a question request never invents spoken text, delivery or an old assessment", () => {
  assert.equal(fallEventLabel(event({ eventKind: "question_requested", answer: "unclear" })), "로봇 확인 질문 요청");
});

test("Cloud shows the actual explanation and matching request assessment, not retained stronger state", () => {
  const record = analysis();
  assert.equal(fallEventLabel(event({ assessment: "observed_fall", answer: "help_request", analysis: record })),
    `클라우드 AI: 낙상 의심 (${nounEnding(record.explanation)})`);
  assert.equal(fallEventLabel(event()), "클라우드 AI: 낙상 의심 (판단 이유 기록 없음)");
  // Person-association errors are not explanations of why the VLM suspected a fall.
  assert.equal(fallEventLabel(event({ reason: "target_unidentified" })), "클라우드 AI: 낙상 의심 (판단 이유 기록 없음)");
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


test("approach events read as short history lines (mockup 22)", () => {
  const label = (eventKind, reason, change = {}) => fallEventLabel(event({ eventKind, reason, ...change }));
  assert.equal(label("approach_started", "patrol_stopped"), "순찰을 멈추고 확인하러 감 (사람인지 확실하지 않음)");
  assert.equal(label("approach_started", null), "확인하러 감 (사람인지 확실하지 않음)");
  assert.equal(label("approach_completed", "arrived"), "의심 장소 1 m 앞에 도착");
  assert.equal(label("approach_completed", "no_path"), "가까이 가지 못함: 갈 길 없음 → 이 자리에서 물어봄");
  assert.equal(label("approach_completed", "no_map"), "가까이 가지 못함: 지도 없음 → 이 자리에서 물어봄");
  assert.equal(label("person_check_completed", "person"), "가까이에서 확인: 사람 (사람 확인됨)");
  assert.equal(label("person_check_completed", "not_a_person", { analysis: analysis({
    purpose: "person_check", assessment: "normal_activity", explanation: "가방과 옷가지만 보입니다." }) }),
  "가까이에서 확인: 사람 아님 (사람 확인 불가 · AI: 가방과 옷가지만 보임)");
  assert.equal(label("person_check_completed", "not_a_person"), "가까이에서 확인: 사람 아님 (사람 확인 불가)");
  assert.equal(label("incident_resolved", "not_a_person", { state: "resolved" }), "로봇이 사건 종료: 사람 아님");
  assert.equal(label("approach_returned", "returned", { state: "resolved" }), "원래 자리로 돌아옴");
  assert.equal(label("approach_returned", "return_failed", { state: "resolved" }), "원래 자리로 돌아가지 못함");
});

test("whole-body check events read as short history lines (mockup 25)", () => {
  const label = (eventKind, reason, change = {}) => fallEventLabel(event({ eventKind, reason, ...change }));
  assert.equal(label("person_check_completed", "partial_body"), "가까이에서 확인: 사람 · 몸 일부만 보임");
  assert.equal(label("person_check_completed", "whole_body"), "가까이에서 확인: 사람 · 몸 전체 보임");
  assert.equal(label("posture_check_completed", "posture_unconfirmed"), "자세를 확인하지 못함 → 물어봄");
  assert.equal(label("view_completed", "arrived"), "몸 전체가 보이는 자리로 옮김");
  assert.equal(label("view_completed", "no_path"), "몸 전체가 보이는 자리로 못 감: 갈 길 없음");
  assert.equal(label("posture_check_completed", "sitting_off_floor", { analysis: analysis({
    purpose: "posture_check", assessment: "normal_activity", explanation: "침대 가장자리에 걸터앉아 있습니다." }) }),
  "몸 전체 확인: 침대나 의자에 앉아 있음 (AI: 침대 가장자리에 걸터앉아 있음)");
  assert.equal(label("posture_check_completed", "standing"), "몸 전체 확인: 서 있음");
  assert.equal(label("posture_check_completed", "on_floor"), "몸 전체 확인: 바닥에 있음 → 물어봄");
  assert.equal(label("posture_check_completed", "body_unseen"), "몸 전체를 못 봄 → 이 자리에서 물어봄");
  assert.equal(label("posture_check_completed", "judgements_differ"),
    "로봇이 잰 값과 클라우드 AI 판단이 다름 → 물어봄");
  assert.equal(label("incident_resolved", "off_floor_verified", { state: "resolved" }),
    "로봇이 사건 종료: 바닥에 있지 않음 · 묻지 않음");
  // 2026-10-14: sitting up busy on the floor (a low table) is not asked either.
  assert.equal(label("posture_check_completed", "active_on_floor"), "몸 전체 확인: 바닥에 앉아 활동 중");
  assert.equal(label("incident_resolved", "floor_activity_verified", { state: "resolved" }),
    "로봇이 사건 종료: 바닥에 앉아 활동 중 · 묻지 않음");
});

test("the list says the robot is checking before it asks, and off-floor closures say so (mockup 25)", () => {
  const panel = readFileSync(new URL("../app/components/fall-incidents-panel.tsx", import.meta.url), "utf8");
  for (const copy of ["말벗이 확인하는 중", "가까이 가는 중", "몸 전체가 보이는 자리로 옮기는 중", "몸 전체를 보는 중",
    "말벗이 확인: 침대나 의자에 앉아 있음", "말벗이 확인: 서 있음", "말벗이 확인: 바닥에 앉아 활동 중",
    "바닥에서 활동 중", "몸 일부만 보여 가서 확인 · 묻지 않음",
    "묻지도, 알리지도 않았어요."]) assert.ok(panel.includes(copy), copy);
  // Once the person is asked, the title is the suspicion again.
  assert.match(panel, /!a \|\| i\.asked\) return null/);
  assert.ok(!panel.includes("말벗이 확인하러 가는 중"));
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
