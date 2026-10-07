import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("the cloud zone editor keeps the established map editing contract", async () => {
  const panel = await readFile(
    new URL("../app/components/robot-map-panel.tsx", import.meta.url),
    "utf8",
  );

  assert.match(panel, /type: "corner"/);
  assert.match(panel, /type: "edge"/);
  assert.match(panel, /type: "move"/);
  assert.match(panel, /zoneRingValidationError/);
  assert.match(panel, /zoneInteriorInsideBoundary/);
  assert.match(panel, /구역 내부에 벽이나 장애물이 포함될 수 없습니다/);
  assert.match(panel, /preferred_goal/);
  assert.match(panel, /role: "semantic_zone"/);
  assert.match(panel, /postSpaceEdit\("space-drafts", \{\s*kind: "zones"/);
  assert.doesNotMatch(panel, /zonePoints|setZonePoints/);
});

test("the add menu can copy a complete room into a semantic movement zone", async () => {
  const panel = await readFile(
    new URL("../app/components/robot-map-panel.tsx", import.meta.url),
    "utf8",
  );

  assert.match(panel, /function addRoomAsZone|const addRoomAsZone/);
  assert.match(panel, /polygonGeometries\(room\.geometry\)/);
  assert.match(panel, /source_room_id/);
  assert.match(panel, /방 전체 적용/);
  assert.match(panel, /저장된 방 경계를 그대로 사용/);
  assert.match(panel, /zoneCreateMode === "room"/);
});

test("virtual walls remain compatible with the semantic polygon contract", async () => {
  const panel = await readFile(
    new URL("../app/components/robot-map-panel.tsx", import.meta.url),
    "utf8",
  );

  assert.match(panel, /geometry_kind = "virtual_wall"/);
  assert.match(panel, /wall_endpoints/);
  assert.match(panel, /wall_width_m/);
  assert.match(panel, /virtualWallRing/);
  assert.match(panel, /type: "wall-endpoint"/);
  assert.match(panel, /<line/);
  assert.match(panel, /가상 벽/);
  assert.match(panel, /properties\.behavior = "restricted"/);
});

test("existing zones can be selected directly on the map", async () => {
  const [panel, styles] = await Promise.all([
    readFile(new URL("../app/components/robot-map-panel.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/globals.css", import.meta.url), "utf8"),
  ]);

  assert.match(panel, /setSelectedZoneId\(id\)/);
  assert.match(panel, /featureContains\(candidate, x, y\)/);
  assert.match(panel, /robot-map-zone-shape/);
  assert.match(panel, /robot-map-virtual-wall/);
  assert.match(panel, /robot-map-virtual-wall-hit/);
  assert.doesNotMatch(panel, /robot-map-zone-label/);
  assert.match(panel, /robot-map-list-card/);
  assert.match(panel, /robot-map-semantics.*is-interactive/);
  assert.match(panel, /setPointerCapture\(event\.pointerId\)/);
  assert.match(panel, /pointerEvents: "auto"/);
  assert.match(panel, /const deviceId = device\?\.id \?\? ""/);
  assert.match(panel, /\}, \[deviceId\]\);/);
  assert.doesNotMatch(panel, /\}, \[device, semanticRefresh/);
  assert.match(styles, /\.robot-map-virtual-wall-handle\s*\{[^}]*pointer-events:\s*all/s);
  assert.match(styles, /\.robot-map-virtual-wall-hit\s*\{[^}]*stroke-width:\s*18px/s);
});

test("room editing never redraws the saved map wall outline", async () => {
  const [panel, styles] = await Promise.all([
    readFile(new URL("../app/components/robot-map-panel.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/globals.css", import.meta.url), "utf8"),
  ]);

  assert.doesNotMatch(panel, /robot-map-room-shape/);
  assert.doesNotMatch(styles, /robot-map-room-shape/);
  assert.match(panel, /roomInternalBoundaryPath/);
  assert.match(styles, /\.robot-map-room-divider/);
});

test("clearing a room name keeps the controlled input empty while editing", async () => {
  const panel = await readFile(
    new URL("../app/components/robot-map-panel.tsx", import.meta.url),
    "utf8",
  );

  assert.match(panel, /value=\{selectedRoom \? featureName\(selectedRoom, ""\) : ""\}/);
  assert.match(panel, /const name = typeof updates\.name === "string"\s*\? updates\.name\.slice\(0, 40\)\s*:\s*"";/s);
  assert.match(panel, /properties\.base_name = name\.trim\(\) \|\| "이름 없는 방"/);
  assert.doesNotMatch(panel, /updates\.name\.trim\(\)[\s\S]{0,100}: "이름 없는 방"/);
});

test("all map modes share room boundaries, room names, and zone drafts", async () => {
  const [panel, styles] = await Promise.all([
    readFile(new URL("../app/components/robot-map-panel.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/globals.css", import.meta.url), "utf8"),
  ]);

  assert.doesNotMatch(panel, /\(mapMode === "rooms" \|\| mapMode === "zones"\) && roomDrafts\.map/);
  assert.match(panel, /const renderedZoneFeatures = zoneDrafts;/);
  assert.match(panel, /mapMode !== "rooms" \? "is-context" : ""/);
  assert.match(panel, /방 경계·이름/);
  assert.match(panel, /roomDrafts\.length > 0 && <span><i className="is-room"/);
  assert.match(panel, /renderedZoneFeatures\.length > 0 &&/);
  assert.ok(
    panel.indexOf("{renderedZoneFeatures.map((zone)") < panel.indexOf("{roomDrafts.map((room)"),
    "room boundaries must render above zone fills",
  );
  assert.match(styles, /\.robot-map-room-divider\.is-context\s*\{[^}]*pointer-events:\s*none/s);
  assert.match(styles, /\.robot-map-room-label\.is-context\s*\{[^}]*pointer-events:\s*none/s);
});

test("navigation progress survives missing cloud ratios and remains visible at arrival", async () => {
  const panel = await readFile(
    new URL("../app/components/robot-map-panel.tsx", import.meta.url),
    "utf8",
  );

  assert.match(panel, /function navigationProgressPercent/);
  assert.match(panel, /initial_path_length_m/);
  assert.match(panel, /1 - Math\.max\(0, remaining\) \/ pathLength/);
  assert.match(panel, /value\.state === "succeeded" \? 1 : 0\.99/);
  assert.match(panel, /navigationSucceeded \? 100 : navigationProgressPercent\(navigation\)/);
  assert.match(panel, /선택한 목적지에 도착했어요/);
  assert.match(panel, /aria-valuenow=\{navigationProgress\}/);
});

test("common drive mode blocks conflicting destination commands and stays owner-only", async () => {
  const panel = await readFile(
    new URL("../app/components/robot-map-panel.tsx", import.meta.url),
    "utf8",
  );

  assert.match(panel, /type RobotDriveModeSnapshot/);
  assert.match(panel, /const autonomousModeActive/);
  assert.match(panel, /autonomousModeActive\) return/);
  // 보내기는 보호자도 쓰지만, 자율주행 중에는 막힌다. 자율주행 시작은 소유자만.
  assert.match(panel, /disabled=\{!snapshot\?\.online \|\| autonomousModeActive/);
  assert.match(panel, /disabled=\{!isOwner \|\| !snapshot\?\.online \|\| snapshot\?\.state\?\.localization\.state !== "ok" \|\| navigationDriving \|\| autonomousModeActive/);
  assert.match(panel, /if \(!isOwner && mapMode !== "navigate"\) return;/);
  assert.match(panel, /자율주행은 소유자만 할 수 있어요/);
  assert.match(panel, /주행 모드 제어는 소유자 계정에서만/);
  assert.match(panel, /function driveModeCopy/);
});

test("autonomous controls share one owner-only drive session", async () => {
  const panel = await readFile(
    new URL("../app/components/robot-map-panel.tsx", import.meta.url),
    "utf8",
  );

  // The simulator starts patrol by mode alone; the real robot adds its thoroughness.
  assert.match(panel, /sendCommand\("drive_mode_start", patrolLevels\s*\? \{ mode: "patrol", thoroughness: patrolLevel \}\s*: \{ mode: "patrol" \}\)/);
  assert.match(panel, /sendCommand\("drive_mode_start", \{ mode: "roaming" \}\)/);
  assert.match(panel, /sendCommand\("drive_mode_start", \{ mode: "person_following" \}\)/);
  assert.match(panel, /sendCommand\("drive_mode_pause"/);
  assert.match(panel, /sendCommand\("drive_mode_resume"/);
  assert.match(panel, /sendCommand\("drive_mode_stop"/);
  assert.match(panel, /!availableAutonomousModes\.includes\("patrol"\)/);
  assert.match(panel, /!availableAutonomousModes\.includes\("roaming"\)/);
  assert.match(panel, /!availableAutonomousModes\.includes\("person_following"\)/);
  assert.match(panel, /activeAutonomousMode !== "person_following"/);
  assert.match(panel, /방 순찰 시작/);
  assert.match(panel, /자율 배회 시작/);
  assert.match(panel, /사람 따라가기/);
});

test("the home map summary reuses rooms, zones, and the live localized robot pose", async () => {
  const [dashboard, panel, styles] = await Promise.all([
    readFile(new URL("../app/components/homecam-dashboard.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/components/robot-map-panel.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/globals.css", import.meta.url), "utf8"),
  ]);

  assert.match(dashboard, /<RobotMapSummaryOverlay snapshot=\{robotSnapshot\} semantics=\{semantics\} \/>/);
  assert.match(dashboard, /window\.setInterval\(\(\) => void loadRobot\(\).*1_000\)/s);
  assert.match(panel, /export function RobotMapSummaryOverlay/);
  assert.match(panel, /semantics\?\.revision === map\.revision/);
  assert.match(panel, /featuresOf\(semantics\?\.zones\)/);
  assert.match(panel, /roomInternalBoundaryPath/);
  assert.match(panel, /snapshot\?\.state\?\.localization\.state === "ok"/);
  assert.match(panel, /function localizationCopy/);
  assert.match(panel, /부팅 후 위치 확인 필요/);
  assert.match(panel, /위치 재확인 중/);
  assert.match(panel, /robot-map-home-marker/);
  assert.match(styles, /\.homecam-home-map-preview \.robot-map-home-semantics/);
  assert.match(styles, /\.homecam-home-map-preview \.robot-map-home-marker/);
});

test("rooms and Zones are edited with the 말벗 off and saved on the server (SWM25-237)", async () => {
  const [panel, styles] = await Promise.all([
    readFile(new URL("../app/components/robot-map-panel.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/globals.css", import.meta.url), "utf8"),
  ]);

  // Split and merge are worked out by the web server, saves wait there for the robot.
  assert.match(panel, /postSpaceEdit\("rooms\/split", \{ room: selectedRoom, lines: splitLines \}\)/);
  assert.match(panel, /postSpaceEdit\("rooms\/merge", \{ rooms: \[selectedRoom, mergeTarget\] \}\)/);
  assert.match(panel, /postSpaceEdit\("space-drafts", \{\s*kind: "rooms"/);
  assert.doesNotMatch(panel, /sendCommand\("(room_split|room_merge|rooms_save|zones_apply)"/);
  assert.doesNotMatch(panel, /pendingRoomSave|pendingZoneSave|semanticRetryTimer/);
  // Map clicks edit rooms and Zones offline; destinations still need the robot.
  assert.match(panel, /if \(!snapshot\?\.online && mapMode !== "rooms" && mapMode !== "zones"\) return;/);
  // A reload on the same map keeps unsaved edits.
  assert.match(panel, /if \(!sameMap \|\| !roomsDirtyRef\.current\)/);
  assert.match(panel, /if \(!sameMap \|\| !zonesDirtyRef\.current\)/);
  // 목업 10번 states.
  for (const copy of [
    "말벗이 꺼져 있어요", "편집해 두면 말벗이 켜질 때 반영돼요.",
    "저장했어요. 말벗이 켜지면 반영돼요", "반영되기 전까지 말벗은 예전 방·구역으로 움직여요.",
    "말벗에 반영했어요", " · 꺼져 있는 동안 저장한 방·구역",
    "반영하지 못했어요", "그사이 말벗의 지도가 바뀌어 편집한 방·구역을 반영하지 못했어요. 다시 편집해 주세요.",
  ]) assert.ok(panel.includes(copy), copy);
  assert.match(panel, /className=\{`ui-map-sync is-\$\{spaceBanner\.tone\}`\} role="status"/);
  assert.match(styles, /\.ui-map-sync\.is-ok \{[^}]*var\(--ui-ok-soft\)/);
  assert.match(styles, /\.ui-map-sync\.is-danger \{[^}]*var\(--ui-danger-soft\)/);
});

test("the real robot's room patrol follows mockup 17: thoroughness, progress, stop only, results", async () => {
  const [panel, styles] = await Promise.all([
    readFile(new URL("../app/components/robot-map-panel.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/globals.css", import.meta.url), "utf8"),
  ]);

  // The robot tells the screen what it supports; the simulator sends none of these.
  assert.match(panel, /Array\.isArray\(driveMode\?\.detail\?\.thoroughness_levels\)/);
  assert.match(panel, /driveMode\?\.detail\?\.can_pause !== false/);
  assert.match(panel, /\{ mode: "patrol", thoroughness: patrolLevel \}/);
  assert.match(panel, /useState\(1\);/, "보통 is chosen first");
  for (const copy of [
    "순찰 꼼꼼함", "빠르게", "4m · 집의 80%", "보통", "3m · 집의 90%", "꼼꼼히", "2m · 집의 95%",
    "꼼꼼할수록 가까이 다가가 더 넓게 살펴보지만 오래 걸려요.",
    "경로 계산 중", "이동 중", "둘러보는 중", "% 살펴봄 · ", "곳 방문", "남은 방: ",
    "말벗과 연결이 끊겼어요. 말벗은 ", '"따라가기를" : "순찰을"', "계속하고, 다시 연결되면 지금 상태를 보여 드려요.",
    "중지한 뒤 다시 시작하면 처음부터 다시 순찰해요.",
    "순찰을 마쳤어요", "%까지 살펴봤어요", "더 갈 수 있는 곳이 없었어요 · ", "갈 수 없었던 방: ",
    "순찰이 멈췄어요", "다시 시작해 주세요.", "순찰을 중지했어요",
  ]) assert.ok(panel.includes(copy), copy);
  assert.doesNotMatch(panel, /들름/);
  // 다 마침 초록 · 목표 미달 주황 · 문제 빨강 · 직접 중지 회색.
  assert.match(panel, /tone: "ok", title: "순찰을 마쳤어요"/);
  assert.match(panel, /tone: "warn", title: `집의 \$\{percent\}%까지 살펴봤어요`/);
  assert.match(panel, /tone: "danger", title: "순찰이 멈췄어요"/);
  assert.match(panel, /tone: "neutral", title: "순찰을 중지했어요"/);
  assert.match(styles, /\.ui-map-sync\.is-warn \{[^}]*var\(--ui-warn-soft\)/);
  assert.match(styles, /\.ui-map-sync\.is-neutral \{[^}]*var\(--ui-neutral-soft\)/);
});

test("the developer screen's map, follow and drive tools reach the map tab for the real robot (mockups 18·19)", async () => {
  const [panel, manager, tools, styles] = await Promise.all([
    readFile(new URL("../app/components/robot-map-panel.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/components/real-robot-map-manager.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/components/managed-robot-tools.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/globals.css", import.meta.url), "utf8"),
  ]);

  // Only the real robot gets these; the simulator screens stay as they were.
  assert.match(panel, /const realRobot = snapshot\?\.state\?\.nav2\.robot_interface === "malbut_manager_v1";/);
  assert.match(panel, /\{realRobot \? \(\s*<RealRobotMapManager/);
  assert.match(panel, /\{isOwner && realRobot && \(\s*<MapDrivePad/);
  // The real robot has no roaming: its button and caption mention are hidden there.
  assert.match(panel, /\{!realRobot && \(\s*<button[^>]*?onClick=\{\(\) => void sendCommand\("drive_mode_start", \{ mode: "roaming" \}\)\}/s);
  assert.ok(panel.includes("방 순찰 또는 카메라로 확인한 사람 따라가기를 시작할 수 있어요."));
  // 지도 관리 uses the developer screen's robot commands.
  assert.match(manager, /sendCommand\("mission_start", \{ capability: "autoslam", arguments: \{ map_name: stem \} \}\)/);
  assert.match(manager, /sendCommand\("runtime_start", \{ mode: "navigation", map \}\)/);
  // Switching may turn the robot in place: the owner clears the way first (malbut_bringup README).
  assert.match(manager, /window\.confirm\(`[^`]*제자리에서 한 바퀴 돌 수 있어요\. 주변을 비워 주세요\.`\)\) \{\s*void sendCommand\("runtime_start"/);
  assert.match(manager, /window\.confirm\(.*\) \{\s*void sendCommand\("map_delete", \{ map \}\);/s);
  assert.match(manager, /sendCommand\("mission_cancel"\)/);
  assert.match(manager, /if \(!\(await saveLabel\(`\$\{stem\}\.yaml`, name\)\)\) return;/, "named before AutoSLAM starts");
  // Kept on the developer screen only: Bringup on/off/recovery, relocalization, debugging.
  for (const developerOnly of [/runtime_stop/, /"recovery"/, /relocalize/, /robot_diagnostics/, /debug_mission_start/]) {
    assert.doesNotMatch(manager, developerOnly);
  }
  for (const copy of [
    "지금 쓰는 지도", "없음 (빈 기본 지도)", "새 지도 만들기", "지도 이름", "만든 날짜·시간으로 채워져 있어요. 나중에 목록에서도 바꿀 수 있어요.",
    "지도 만들기 시작", "지도 만드는 중", "집을 둘러보고 있어요", "알아낸 면적 ", "아직 안 가 본 곳 ",
    "중지하면 지금까지 그린 지도는 저장되지 않아요.", "새 지도를 만들었어요", "이 지도 쓰기", "지도를 만들지 못했어요",
    "지도를 바꾸고 있어요", "주행 시스템이 꺼져 있어요", "사용 중", "이름 바꾸기", "삭제",
    "지도 만들기를 멈췄어요", "지금은 빈 지도라, 아래 목록에서 쓰던 지도를 골라 주세요.",
    "지도를 지우면 그 지도의 방·구역도 함께 지워져요. 사용 중인 지도는 지울 수 없어요.",
    "직접 움직이기", "패드를 누른 채 끄는 만큼 빨라져요(최대 0.15m/s). 손을 떼면 바로 멈춰요.",
  ]) assert.ok(manager.includes(copy), copy);
  // 사람 따라가기 at the fixed 0.6 m (robot side), with stop only.
  for (const copy of ["사람 따라가기 · 따라가는 중", "사람 확인됨 · 지금 ", "사람 따라가기 · 사람 찾는 중",
    "마지막으로 본 곳을 기준으로 사람을 다시 찾고 있어요", "말벗 앞에 보이는 사람을 "]) {
    assert.ok(panel.includes(copy), copy);
  }
  // One input handler for both pads; the developer screen's pad looks the same.
  assert.match(tools, /export function useManualDrive\(drive: Drive, enabled: boolean\)/);
  assert.match(tools, /const \{ padProps, knob, active, sent, error, strafe, setStrafe \} = useManualDrive\(drive, enabled\);/);
  assert.match(tools, /<h3>수동 조작<\/h3>/);
  assert.match(manager, /useManualDrive\(drive, enabled\)/);
  assert.match(styles, /\.ui-map-drive-pad \{[^}]*touch-action: none/s);
});
