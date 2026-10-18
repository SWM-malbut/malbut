import assert from "node:assert/strict";
import test from "node:test";
import { moduleLoader } from "./helpers/fall-db-harness.mjs";

process.env.TZ = "Asia/Seoul";
const { clipGroups, groupLabel, groupNote, skippedText } = moduleLoader()("app/fall-clip-groups.ts");

// 2026-10-17 21:31:15 KST and seconds after it.
const T0 = Date.parse("2026-10-17T12:31:15.000Z");
const at = (s) => new Date(T0 + s * 1000).toISOString();
let index = 0;
const clip = (from, to, anchorKinds, checkRound) => ({ segmentIndex: index++, startAt: at(from), endAt: at(to),
  anchorKinds, ...(checkRound === undefined ? {} : { checkRound }) });
const shape = (groups) => groups.map((g) => [g.kind, g.round, g.clips.map((c) => c.segmentIndex)]);

test("ranges from a robot before SWM25-268 keep one button per range", () => {
  assert.equal(clipGroups([clip(0, 30, ["pose_motion"]), clip(60, 90, ["cloud_window"])]), null);
});

test("확인 then every 재감지 in one group, in time order", () => {
  index = 0;
  const check = clip(0, 95, ["check", "pose_found_down"], 1);
  const again = [clip(200, 230, ["pose_found_down"]), clip(140, 170, ["pose_found_down"])];
  const groups = clipGroups([...again, check]);
  assert.deepEqual(shape(groups), [["check", null, [0]], ["redetect", null, [2, 1]]]);
  assert.equal(groupLabel(groups[0]), "확인 · 21:31:15");
  assert.equal(groupLabel(groups[1]), "재감지 2회 · 21:33~21:34");
  assert.equal(groupNote(groups[0], groups), "처음 의심 10초 전 ~ 판단이 끝난 뒤 5초");
  assert.equal(groupNote(groups[1], groups), "다시 감지된 순간 10초 전 ~ 20초 후 · 사진을 누르면 그 순간을 재생해요");
});

test("a long check is several ranges back to back but one 확인 button", () => {
  index = 0;
  const groups = clipGroups([clip(0, 120, ["check"], 1), clip(120, 170, ["check", "cloud_window"], 1)]);
  assert.deepEqual(shape(groups), [["check", null, [0, 1]]]);
});

test("발견 before a wait (직접 움직이기), then 확인; the skipped wait as 1분 20초", () => {
  index = 0;
  const groups = clipGroups([clip(0, 30, ["pose_found_down"]), clip(110, 160, ["check"], 1)]);
  assert.deepEqual(shape(groups), [["found", null, [0]], ["check", null, [1]]]);
  assert.equal(groupLabel(groups[0]), "발견 · 21:31:15");
  assert.equal(groupNote(groups[0], groups), "의심 시점 10초 전 ~ 20초 후");
  assert.equal(groupNote(groups[1], groups), "기다린 뒤 다시 가기 시작한 때 ~ 판단이 끝난 뒤 5초");
  assert.equal(skippedText(groups[0], groups[1]), "1분 20초");
  assert.equal(skippedText(groups[0], clipGroups([clip(0, 30, ["pose_motion"]), clip(75, 90, ["check"], 1)])[1]),
    "45초");
  assert.equal(skippedText(groups[0], clipGroups([clip(0, 30, ["pose_motion"]), clip(150, 160, ["check"], 1)])[1]),
    "2분");
});

test("each recheck round is its own 재확인 · N회차 button", () => {
  index = 0;
  const groups = clipGroups([
    clip(0, 60, ["check"], 1), clip(100, 130, ["pose_found_down"]),
    clip(1800, 1900, ["recheck"], 2), clip(1900, 1950, ["recheck", "cloud_window"], 2),
    clip(3600, 3700, ["recheck"], 3), clip(3800, 3830, ["pose_found_down"]),
  ]);
  assert.deepEqual(shape(groups),
    [["check", null, [0]], ["redetect", null, [1, 5]], ["recheck", 2, [2, 3]], ["recheck", 3, [4]]]);
  assert.equal(groupLabel(groups[2]), "재확인 · 2회차 · 22:01:15");
  assert.equal(groupNote(groups[2], groups), "다시 확인을 시작한 때 ~ 판단이 끝난 뒤 5초");
});
