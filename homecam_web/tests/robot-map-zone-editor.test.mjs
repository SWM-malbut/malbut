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
  assert.match(panel, /sendCommand\("zones_apply"/);
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

test("the cloud robot marker interpolates one-second pose updates while driving", async () => {
  const [panel, styles] = await Promise.all([
    readFile(new URL("../app/components/robot-map-panel.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/globals.css", import.meta.url), "utf8"),
  ]);

  assert.match(panel, /robot-map-marker \$\{navigationDriving \? "is-driving" : ""\}/);
  assert.match(styles, /\.robot-map-marker\.is-driving\s*\{[^}]*transition-duration:\s*950ms/s);
  assert.match(styles, /\.robot-map-marker\.is-driving\s*\{[^}]*transition-timing-function:\s*linear/s);
});

test("navigation progress survives missing cloud ratios and remains visible at arrival", async () => {
  const [panel, styles] = await Promise.all([
    readFile(new URL("../app/components/robot-map-panel.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/globals.css", import.meta.url), "utf8"),
  ]);

  assert.match(panel, /function navigationProgressPercent/);
  assert.match(panel, /1 - Math\.max\(0, remaining\) \/ pathLength/);
  assert.match(panel, /navigationSucceeded \? 100 : navigationProgressPercent\(navigation\)/);
  assert.match(panel, /선택한 목적지에 도착했어요/);
  assert.match(panel, /aria-valuenow=\{navigationProgress\}/);
  assert.match(styles, /\.robot-map-progress i\s*\{[^}]*transition:\s*width 950ms linear/s);
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
