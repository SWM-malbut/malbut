// Room split/merge on the web server (SWM25-237): the simulator's room_editor tests, ported.
import assert from "node:assert/strict";
import test from "node:test";
import { moduleLoader } from "./helpers/fall-db-harness.mjs";

const load = moduleLoader();
const {
  geometryArea, mergeRoomFeatures, normalizeRoomFeature, roomRepresentativePoint, splitRoomFeature,
} = load("app/room-geometry.ts");

const near = (actual, expected, tolerance) => assert.ok(
  Math.abs(actual - expected) <= tolerance, `${actual} is not within ${tolerance} of ${expected}`);

function rectangularRoom(id = "room-1", minimumX = 0) {
  return {
    type: "Feature", id,
    properties: {
      role: "room", room_id: id, name: id, category: "unassigned", color: "#dce8ff",
      area_m2: 60, centroid: [minimumX + 5, 3], generated: true,
    },
    geometry: { type: "Polygon", coordinates: [[
      [minimumX, 0], [minimumX + 10, 0], [minimumX + 10, 6], [minimumX, 6], [minimumX, 0],
    ]] },
  };
}

function roomWithTwoPassages() {
  const room = rectangularRoom();
  room.geometry.coordinates.push([[2, 2.5], [8, 2.5], [8, 3.5], [2, 3.5], [2, 2.5]]);
  room.properties.area_m2 = 54;
  return room;
}

function pointInRing(point, ring) {
  let inside = false;
  let previous = ring[ring.length - 1];
  for (const current of ring) {
    if ((current[1] > point[1]) !== (previous[1] > point[1]) &&
        point[0] < (previous[0] - current[0]) * (point[1] - current[1]) / (previous[1] - current[1]) + current[0]) {
      inside = !inside;
    }
    previous = current;
  }
  return inside;
}

test("a divider splits one room and each part reports its own area", () => {
  const parts = splitRoomFeature(rectangularRoom(), [[5, 0], [5, 6]]);
  assert.deepEqual(parts.map((part) => part.id), ["room-1-a", "room-1-b"]);
  assert.ok(parts.every((part) => part.properties.role === "room" && part.properties.edited));
  assert.ok(parts.every((part) => part.properties.split_from === "room-1"));
  assert.notEqual(parts[0].properties.color, parts[1].properties.color);
  near(parts.reduce((sum, part) => sum + part.properties.area_m2, 0), 60, 0.5);
  for (const part of parts) {
    near(part.properties.area_m2, geometryArea(part.geometry), 0.01);
    assert.ok(part.properties.area_m2 >= 29);
    assert.ok(part.properties.clearance_m > 0);
  }
});

test("a concave room gets an interior representative point", () => {
  const room = rectangularRoom();
  room.geometry = { type: "Polygon", coordinates: [[
    [0, 0], [8, 0], [8, 2], [2, 2], [2, 6], [8, 6], [8, 8], [0, 8], [0, 0],
  ]] };
  const [point, clearance] = roomRepresentativePoint(room.geometry);
  const normalized = normalizeRoomFeature(room);
  assert.ok(!pointInRing([4, 4], room.geometry.coordinates[0]));
  assert.ok(pointInRing(point, room.geometry.coordinates[0]));
  assert.deepEqual(normalized.properties.representative_point, point);
  assert.equal(normalized.properties.clearance_m, clearance);
  assert.ok(clearance > 0.5);
});

test("divider points must be near the selected room's walls", () => {
  assert.throws(() => splitRoomFeature(rectangularRoom(), [[-1, -1], [-0.5, -0.5]]), /near a Room wall/);
});

test("control points bend one wall-to-wall divider", () => {
  const parts = splitRoomFeature(rectangularRoom(), [[3, 0], [3, 3], [7, 3], [7, 6]]);
  assert.equal(parts.length, 2);
  near(parts.reduce((sum, part) => sum + part.properties.area_m2, 0), 60, 0.7);
});

test("independent dividers close two passages without joining", () => {
  const parts = splitRoomFeature(roomWithTwoPassages(), [[[0, 3], [2, 3]], [[8, 3], [10, 3]]]);
  assert.equal(parts.length, 2);
  near(parts.reduce((sum, part) => sum + geometryArea(part.geometry), 0),
    geometryArea(roomWithTwoPassages().geometry), 0.01);
});

test("a divider needs at least two points", () => {
  assert.throws(() => splitRoomFeature(rectangularRoom(), [[5, 3]]), /at least two finite/);
});

test("points near a wall snap to it", () => {
  assert.equal(splitRoomFeature(rectangularRoom(), [[5, 0.18], [5, 5.82]]).length, 2);
});

test("a tiny accidental fragment is refused", () => {
  assert.throws(() => splitRoomFeature(rectangularRoom(), [[0.2, 0], [0.2, 6]], 0.05, 2),
    /exactly two meaningful areas/);
});

test("split halves merge back into the original room", () => {
  const original = rectangularRoom();
  original.properties.name = "거실";
  original.properties.category = "living_room";
  const parts = splitRoomFeature(original, [[5, 0], [5, 6]]);
  const merged = mergeRoomFeatures(parts);
  assert.equal(merged.properties.role, "room");
  assert.equal(merged.properties.edited, true);
  assert.equal(merged.properties.generated, false);
  assert.deepEqual(merged.properties.merged_from, ["room-1-a", "room-1-b"]);
  assert.deepEqual(parts.map((part) => part.properties.name), ["거실 A", "거실 B"]);
  assert.equal(merged.properties.name, "거실");
  assert.deepEqual(merged.properties.merged_from_names, ["거실 A", "거실 B"]);
  assert.equal(merged.properties.category, "living_room");
  near(merged.properties.area_m2, 60, 1e-9);
  assert.equal(merged.geometry.type, "Polygon");
  assert.deepEqual(merged.geometry, original.geometry);
});

test("repeated split and merge never erodes the room", () => {
  let room = rectangularRoom();
  const original = room.geometry;
  for (let cycle = 0; cycle < 5; cycle += 1) {
    room = mergeRoomFeatures(splitRoomFeature(room, [[5, 0], [5, 6]]));
    assert.deepEqual(room.geometry, original);
    near(room.properties.area_m2, 60, 1e-9);
  }
});

test("merging different room types becomes unassigned", () => {
  const parts = splitRoomFeature(rectangularRoom(), [[5, 0], [5, 6]]);
  parts[0].properties.category = "living_room";
  parts[1].properties.category = "kitchen";
  assert.equal(mergeRoomFeatures(parts).properties.category, "unassigned");
});

test("rooms that do not touch cannot be merged", () => {
  assert.throws(() => mergeRoomFeatures([rectangularRoom("room-1", 0), rectangularRoom("room-2", 20)]),
    /adjacent Rooms/);
});

test("a one-cell gap from separate vectorization still merges", () => {
  const merged = mergeRoomFeatures([rectangularRoom("room-1", 0), rectangularRoom("room-2", 10.1)], 0.05);
  assert.deepEqual(merged.properties.merged_from, ["room-1", "room-2"]);
  assert.equal(merged.geometry.type, "Polygon");
  near(merged.properties.area_m2, geometryArea(merged.geometry), 0.01);
});

test("nested splits have unique names and restore their lineage", () => {
  const original = rectangularRoom();
  original.properties.name = "거실";
  const outer = splitRoomFeature(original, [[5, 0], [5, 6]]);
  const inner = splitRoomFeature(outer[0], [[2.5, 0], [2.5, 6]]);
  assert.deepEqual(new Set([outer[1].properties.name, ...inner.map((part) => part.properties.name)]),
    new Set(["거실 B", "거실 A-A", "거실 A-B"]));
  const restoredA = mergeRoomFeatures(inner);
  assert.equal(restoredA.properties.name, "거실 A");
  assert.equal(restoredA.properties.split_path, "A");
  const restored = mergeRoomFeatures([restoredA, outer[1]]);
  assert.equal(restored.properties.name, "거실");
  assert.deepEqual(restored.geometry, original.geometry);
});

test("deeply split rooms keep their area equal to their geometry", () => {
  const original = rectangularRoom();
  let leaves = [original];
  for (let step = 0; step < 5; step += 1) {
    const room = leaves.shift();
    const xs = room.geometry.coordinates[0].map((point) => point[0]);
    const divider = (Math.min(...xs) + Math.max(...xs)) / 2;
    const parts = splitRoomFeature(room, [[divider, 0], [divider, 6]], 0.05, 0.1);
    for (const part of parts) near(part.properties.area_m2, geometryArea(part.geometry), 0.01);
    leaves = [parts[0], ...leaves, parts[1]];
    near(leaves.reduce((sum, part) => sum + geometryArea(part.geometry), 0), geometryArea(original.geometry), 0.01);
  }
});

test("normalize refuses broken rooms", () => {
  const room = rectangularRoom();
  assert.throws(() => normalizeRoomFeature({ ...room, properties: { ...room.properties, role: "zone" } }), /room role/);
  assert.throws(() => normalizeRoomFeature({ ...room, id: " ", properties: { ...room.properties, room_id: " " } }), /ID/);
  assert.throws(() => normalizeRoomFeature({ ...room, geometry: { type: "Polygon", coordinates: [[[0, 0], [1, 0], [1, 1]]] } }),
    /closed/);
  const normalized = normalizeRoomFeature(room);
  assert.equal(normalized.properties.area_m2, 60);
  assert.ok(pointInRing(normalized.properties.representative_point, room.geometry.coordinates[0]));
});

test("rooms from a SLAM map (corners on cell centers) split without losing a strip", () => {
  const ring = [[-0.975, -0.975], [3.975, -0.975], [3.975, 1.025], [1.525, 1.025], [1.525, 2.975],
    [-0.975, 2.975], [-0.975, -0.975]];
  const room = { type: "Feature", id: "room-1", properties: { role: "room", room_id: "room-1", name: "공간 1" },
    geometry: { type: "Polygon", coordinates: [ring] } };
  const parts = splitRoomFeature(room, [[1.525, -0.975], [1.525, 1.025]]);
  near(parts.reduce((sum, part) => sum + geometryArea(part.geometry), 0), geometryArea(room.geometry), 0.01);
  assert.deepEqual(mergeRoomFeatures(parts).geometry, room.geometry);
});
