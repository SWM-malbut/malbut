import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { moduleLoader } from "./helpers/fall-db-harness.mjs";

const load = moduleLoader();
const copy = load("app/robot-navigation-copy.ts");

// 목업 23번: the robot names why a destination drive ended; the owner reads one line.
test("each reason the robot names reads as the mockup's line", () => {
  const expected = {
    blocked_start: "주변이 막혀 출발하지 못했어요. 말벗 둘레의 물건을 치우거나 말벗을 조금 옮긴 뒤 다시 보내 주세요.",
    zone_stuck: "금지 구역에서 나갈 길이 막혔어요. 직접 움직이기로 말벗을 구역 밖으로 옮긴 뒤 다시 보내 주세요.",
    manual_drive: "직접 움직이기 중이라 출발하지 못했어요.",
    fall_check: "낙상 확인 중이라 출발하지 못했어요.",
    fall_check_started: "낙상 확인이 시작돼 이동을 멈췄어요.",
    localization_lost: "말벗이 지도에서 위치를 잃었어요. 위치 다시 찾기를 한 뒤 보내 주세요.",
  };
  for (const [reason, line] of Object.entries(expected)) {
    assert.equal(copy.navigationFailureCopy({ state: "failed", reason }), line, reason);
  }
  assert.equal(copy.navigationFailureCopy({ reason: "blocked_way", distance_remaining_m: 1.24 }),
    "가는 길이 막혀 멈췄어요. 목적지까지 1.2m 남았어요.");
  assert.equal(copy.navigationFailureCopy({ reason: "blocked_way" }), "가는 길이 막혀 멈췄어요.");
  // An older robot names no reason; an unknown one still says what to do.
  assert.equal(copy.navigationFailureCopy({ state: "failed" }), "다시 보내 주세요.");
  assert.equal(copy.navigationFailureCopy({ reason: "teleported" }), "다시 보내 주세요.");
  assert.equal(copy.NAVIGATION_FAILED_TITLE, "목적지까지 가지 못했어요");
  assert.equal(copy.NAVIGATION_ESCAPING_TITLE, "금지 구역 밖으로 먼저 나가고 있어요");
});

test("the map card shows a failed drive until 다시 선택 and the escape with 이동 취소", () => {
  const panel = readFileSync(new URL("../app/components/robot-map-panel.tsx", import.meta.url), "utf8");
  assert.match(panel, /navigation\?\.state === "escaping"/);
  assert.match(panel, /navigation\?\.state === "failed" && navigationSession !== dismissedNavigation/);
  assert.match(panel, /setDismissedNavigation\(navigationSession\)/);
  assert.match(panel, /navigationFailureCopy\(navigation\)/);
  // No new destination, patrol or following while the robot drives out of a Zone.
  assert.match(panel, /const navigationBusy = navigationDriving \|\| navigationEscaping;/);
  assert.doesNotMatch(panel, /localization\.state !== "ok" \|\| navigationDriving \|\|/);
  const css = readFileSync(new URL("../app/globals.css", import.meta.url), "utf8");
  assert.match(css, /\.ui-map-notice\.is-warn \{ color: #7a4100; background: #fdf1e4; \}/);
});
