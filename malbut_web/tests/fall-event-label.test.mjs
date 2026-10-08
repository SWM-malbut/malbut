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

for (const [eventKind, label] of [["voice_result", "질문에 대한 답"], ["decision_required", "추가 판단 필요"]]) {
  test(`${eventKind}: answer is parenthesized`, () => {
    assert.equal(fallEventLabel(event({ eventKind, answer: "unclear" })), `${label}: 낙상 의심 (응답 불분명)`);
    assert.equal(fallEventLabel(event({ eventKind, answer: "no_response" })), `${label}: 낙상 의심 (응답 없음)`);
    assert.equal(fallEventLabel(event({ eventKind, answer: "help_request" })), `${label}: 낙상 의심 (도움 요청)`);
    assert.equal(fallEventLabel(event({ eventKind, assessment: null, answer: "unclear" })), `${label} (응답 불분명)`);
    assert.equal(fallEventLabel(event({ eventKind, answer: null })), `${label}: 낙상 의심`);
  });
}

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
  assert.ok(panel.includes("{fallEventLabel(e)}"));
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
