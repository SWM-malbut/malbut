// 구역 편집 › 가상 벽 긋기 · 벽 넘는 사각형 구역 (목업 21번).
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const panel = () => readFile(new URL("../app/components/robot-map-panel.tsx", import.meta.url), "utf8");

test("virtual walls are drawn by tapping two points, snapping within 25 cm of a wall", async () => {
  const source = await panel();
  assert.match(source, /type ZoneCreateMode = "closed" \| "menu" \| "room" \| "wall";/);
  assert.match(source, /onClick=\{toggleWallDrawing\}[\s\S]*?>가상 벽 긋기<\/button>/);
  assert.doesNotMatch(source, /defaultVirtualWall|addVirtualWall/);
  // A tap in drawing mode places a point; near a wall it sits on the wall, elsewhere where tapped.
  assert.match(source, /if \(zoneCreateMode === "wall"\) \{\s*placeWallPoint\(/);
  assert.match(source, /const point = snapToRoomWall\(clicked, walkableArea\.geometry, WALL_SNAP_M\) \?\? clicked;/);
  assert.match(source, /const WALL_SNAP_M = 0\.25;/);
  assert.match(source, /if \(!pendingWallPoint\) \{\s*setPendingWallPoint\(point\);/);
  assert.match(source, /setDrawnWallIds\(\(current\) => \[\.\.\.current, featureId\(zone\)\]\);/);
  // Existing walls and zones do not swallow the taps (or start a drag) while drawing.
  assert.equal(source.match(/if \(mapMode !== "zones" \|\| zoneCreateMode === "wall"\) return;/g)?.length, 2);
  assert.equal(source.match(/if \(mapMode !== "zones" \|\| zoneCreateMode === "wall" \|\| !isOwner \|\| busy\) return;/g)?.length, 2);
  for (const text of [
    "시작점을 누르세요. 벽에서 25cm 안이면 벽에 붙어요.", "끝점을 누르세요.",
    "가상 벽이 생겼어요. 가운데 점을 끌면 꺾여요.", "ㄱ자로 꺾었어요. 점을 끌어 계속 고칠 수 있어요.",
    "마지막 선 되돌리기", "모두 지우기", "긋기 끝",
  ]) assert.ok(source.includes(text), text);
});

test("a wall bends once at its middle point, snapping to a right angle nearby", async () => {
  const source = await panel();
  assert.match(source, /value\.length < 2 \|\| value\.length > 3/);
  assert.match(source, /selectedWallPoints\.length === 2 && \([\s\S]*?const bent: Array<\[number, number\]> = \[selectedWallPoints\[0\], center, selectedWallPoints\[1\]\];/);
  assert.match(source, /: wallBendPoint\(point, points\[pointIndex - 1\], points\[pointIndex \+ 1\], walkableArea\.geometry\);/);
  assert.match(source, /const corner = orthogonalCorner\(point, previous, next, geometry\);/);
  // The robot keeps a closed thin Polygon: mitred at the bend.
  assert.match(source, /function virtualWallRing\(points: Array<\[number, number\]>, width: number\)/);
  assert.match(source, /return \[\.\.\.left, \.\.\.right, left\[0\]\];/);
});

test("rectangle zones may cross walls; only the shape and touching the map are checked", async () => {
  const source = await panel();
  assert.doesNotMatch(source, /function zoneGeometryValidationError/);
  assert.match(source, /const invalid = normalized\s*\.map\(\(zone\) => \[zone, zoneShapeErrorOf\(/);
  assert.match(source, /if \(zoneShapeError\(ring, walkableArea, geometry\.resolution\)\) return zone;/);
  assert.match(source, /if \(!zoneTouchesBoundary\(polygon, boundary, resolution\)\) return "구역이 지도 밖에 있어요\. 지도 안쪽으로 옮겨 주세요\.";/);
  assert.ok(source.includes("사각형 구역은 벽을 넘어도 괜찮아요. 벽은 원래 지나갈 수 없는 곳이라 말벗의 주행은 그대로예요. 두 방에 걸친 곳도 한 번에 그릴 수 있어요."));
});
