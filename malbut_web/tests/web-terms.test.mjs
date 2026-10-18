import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { moduleLoader } from "./helpers/fall-db-harness.mjs";

// 웹 용어·문구 통일(SWM25-262, 목업 33~50번).
const load = moduleLoader();
const { withRo, josa } = load("app/korean-particle.ts");

test("room buttons read 로/으로 by the last sound (목업 40번)", () => {
  assert.equal(withRo("거실"), "거실로");
  assert.equal(withRo("화장실"), "화장실로");
  assert.equal(withRo("침대방"), "침대방으로");
  assert.equal(withRo("주방"), "주방으로");
  assert.equal(withRo("서재"), "서재로");
  assert.equal(withRo("공간 1"), "공간 1로");
  assert.equal(withRo("공간 3"), "공간 3으로");
  assert.equal(withRo("Office"), "Office(으)로");
});

test("particles follow the last sound: 로/으로, 가/이, 는/은, 를/을, 와/과", () => {
  assert.deepEqual(["로", "으로", "가", "이", "는", "을", "와"].map((p) => josa("바닥", p)),
    ["바닥으로", "바닥으로", "바닥이", "바닥이", "바닥은", "바닥을", "바닥과"]);
  assert.deepEqual(["으로", "이", "은", "을", "과"].map((p) => josa("침대·의자", p)),
    ["침대·의자로", "침대·의자가", "침대·의자는", "침대·의자를", "침대·의자와"]);
  // ㄹ 받침은 "로"만 받침 없을 때와 같음.
  assert.deepEqual([josa("거실", "으로"), josa("거실", "가")], ["거실로", "거실이"]);
  assert.equal(josa("Office", "로"), "Office로");
  assert.equal(josa("바닥", "도"), "바닥도");
});

test("status names follow the agreed terms", async () => {
  const [dashboard, panel, styles, voice, settings] = await Promise.all([
    readFile(new URL("../app/components/homecam-dashboard.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/components/robot-map-panel.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/globals.css", import.meta.url), "utf8"),
    readFile(new URL("../app/components/voice-agent-panel.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/components/fall-homecam-settings.tsx", import.meta.url), "utf8"),
  ]);
  // 위쪽은 말벗 연결만(목업 33번).
  assert.match(dashboard, /const connectionText = selectedDevice\?\.online \? "온라인" : "오프라인";/);
  // 홈 칩: 영상 준비는 켜져 있을 때만, 카메라·말벗 마이크는 따로·꺼지면 뺌(목업 38번).
  assert.match(dashboard, /!selectedDevice\?\.online \|\| !selectedDevice\.cameraEnabled \? \["영상 준비 안 됨", ""\]/);
  assert.match(dashboard, /selectedDevice\.cameraEnabled && \(\s*<span className=\{selectedDevice\.microphoneEnabled/);
  // 영상 저장: 말벗이 꺼지면 "저장 오류"가 아니라 "알 수 없음"(목업 41번).
  assert.match(dashboard, /const storageError = Boolean\(\s*selectedDevice\?\.online &&/);
  assert.match(dashboard, /: !selectedDevice\.online\s*\? "알 수 없음"/);
  assert.match(dashboard, /<small>영상 저장<\/small>/);
  // 홈·지도 "지금 말벗은"은 같은 문장(목업 38·48번).
  assert.match(panel, /export function robotActivityTitle/);
  assert.match(panel, /return "집 안에서 대기하고 있어요";/);
  assert.match(panel, /: robotActivityTitle\(snapshot\)\}/);
  assert.match(panel, /\{snapshot\?\.online \? "현재 위치" : "마지막 위치"\}/);
  assert.match(panel, /<dd className=\{snapshot\?\.online \? "is-ok" : ""\}>\{snapshot\?\.online \? "온라인" : "오프라인"\}<\/dd>/);
  // 한국어 낱말 중간에서 줄이 바뀌지 않게(웹 용어 6번).
  assert.match(styles, /body \{[\s\S]*word-break: keep-all;/);
  // 음성으로 홈캠 쓰기는 다른 설정 줄과 같은 스위치, 말벗 마이크는 카메라가 꺼지면 막힘(목업 49번).
  assert.match(voice, /role="switch" aria-checked=\{enabled\} aria-label="음성으로 홈캠 쓰기"/);
  assert.doesNotMatch(voice, /type="checkbox"/);
  assert.match(settings, /title: "말벗 마이크"[\s\S]*?needsCamera: true/);
});
