import assert from "node:assert/strict";
import test from "node:test";
import { moduleLoader } from "./helpers/fall-db-harness.mjs";

const load = moduleLoader();
const copy = load("app/robot-map-check-copy.ts");

const check = (phase, extra = {}) => copy.readMapCheck({ mapCheck: { phase, map: "home.yaml", ...extra } });

// 목업 24번: the last map loads at start; a poor match is checked once more, then warned.
test("each pose check state reads as the mockup's card", () => {
  assert.deepEqual(copy.mapCheckCard(check("loading", { auto: true }), "우리집"), {
    blocking: true, relocalize: false, manage: false, tone: "info", title: "마지막에 쓴 지도를 불러오고 있어요",
    note: "'우리집' 지도에서 말벗의 위치를 찾고 있어요. 저장된 위치가 맞지 않으면 제자리에서 한 바퀴 돌 수 있어요.",
  });
  assert.equal(copy.mapCheckCard(check("locating"), "우리집").title, "저장 지도에서 말벗의 위치를 찾고 있어요");
  assert.equal(copy.mapCheckCard(check("retrying", { match: 0.58 }), "우리집").note,
    "지도와 맞는 정도가 낮아(58%) 위치를 다시 찾고 있어요. 제자리에서 한 바퀴 돌 수 있어요.");
  assert.equal(copy.mapCheckCard(check("retrying", { match: 0.87 }), "우리집").note,
    "위치를 다시 찾고 있어요. 제자리에서 한 바퀴 돌 수 있어요.", "the owner asked again on a good match");
  const low = copy.mapCheckCard(check("low", { match: 0.58, retried: true }), "우리집");
  assert.equal(low.title, "지도와 주변이 잘 맞지 않아요");
  assert.equal(low.note, "두 번 찾았지만 위치가 지도와 58%만 맞아요. 가구를 옮겼거나 다른 곳이면 지도가 맞지 않을 수 있어요. 이대로 보내면 말벗이 엉뚱하게 움직일 수 있어요.");
  assert.deepEqual([low.tone, low.blocking, low.relocalize, low.manage], ["warn", false, true, true]);
  assert.match(copy.mapCheckCard(check("low"), "우리집").note, /^말벗의 위치를 찾지 못했어요/);
  const none = copy.mapCheckCard(check("none"), "");
  assert.deepEqual([none.title, none.tone, none.manage, none.relocalize], ["저장 지도를 고르지 않았어요", "warn", true, false]);
  assert.equal(copy.mapCheckCard(check("ok", { match: 0.87 }), "우리집"), null, "a good fit keeps the usual card");
});

test("the facts name the map, the automatic load and the match", () => {
  assert.deepEqual(copy.mapCheckFacts(check("ok", { match: 0.87, auto: true }), "우리집"),
    { map: "우리집 (켤 때 자동으로 불러옴)", match: "87%", tone: "is-ok" });
  assert.deepEqual(copy.mapCheckFacts(check("retrying", { match: 0.58 }), "우리집"),
    { map: "우리집", match: "58% · 다시 찾는 중", tone: "" });
  assert.deepEqual(copy.mapCheckFacts(check("low", { match: 0.58 }), "우리집"),
    { map: "우리집", match: "58%", tone: "is-error is-strong" });
  assert.deepEqual(copy.mapCheckFacts(check("none"), ""), { map: "없음 (빈 기본 지도)", match: "—", tone: "" });
  assert.equal(copy.mapCheckDriveHint(check("low")), "위치를 다시 찾거나 지도를 확인한 뒤 시작해 주세요.");
  assert.equal(copy.mapCheckDriveHint(check("none")), "저장 지도를 고르면 시작할 수 있어요.");
  assert.equal(copy.mapCheckDriveHint(check("loading")), "위치를 찾는 동안은 잠시 쓸 수 없어요.");
  assert.equal(copy.mapCheckDriveHint(check("ok")), null);
});

test("an older robot without the check keeps the existing card", () => {
  assert.equal(copy.readMapCheck({}), null);
  assert.equal(copy.readMapCheck({ mapCheck: { phase: null } }), null);
  assert.equal(copy.readMapCheck({ mapCheck: { phase: "flying" } }), null);
  assert.equal(copy.mapCheckCard(null, ""), null);
  assert.equal(copy.mapCheckDriveHint(null), null);
});
