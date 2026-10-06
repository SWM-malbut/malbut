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

  assert.match(panel, /sendCommand\("drive_mode_start", \{ mode: "patrol" \}\)/);
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
