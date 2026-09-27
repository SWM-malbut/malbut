import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const source = await readFile(new URL("../app/robot-contract.ts", import.meta.url), "utf8");
const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText;
const { parseRobotCommand } = await import(`data:text/javascript;base64,${Buffer.from(js).toString("base64")}`);

test("real robot commands use a bounded adapter to existing capabilities", () => {
  for (const payload of [
    { capability: "follow_person", arguments: { target_mode: 0, target_person_id: "", desired_distance_m: 0.2 } },
    { capability: "follow_person", arguments: { target_mode: 1, target_person_id: "person-1", desired_distance_m: 1 } },
    { capability: "patrol", arguments: { thoroughness: 2 } },
    { capability: "autoslam", arguments: { map_name: "home_1" } },
    { capability: "navigate_to_pose", arguments: { x: 1, y: -2, yaw: 0 } },
  ]) assert.deepEqual(parseRobotCommand({ operation: "mission_start", payload }), { operation: "mission_start", payload });
  for (const payload of [
    { capability: "follow_person", arguments: { target_mode: 0, target_person_id: "", desired_distance_m: 0.19 } },
    { capability: "follow_person", arguments: { target_mode: 1, target_person_id: "", desired_distance_m: 1 } },
    { capability: "patrol", arguments: { thoroughness: 3 } },
    { capability: "autoslam", arguments: { map_name: "../home" } },
    { capability: "navigate_to_pose", arguments: { x: Infinity, y: 0, yaw: 0 } },
    { capability: "shell", arguments: {} },
    { capability: "patrol", arguments: { thoroughness: 1 }, priority: "URGENT" },
  ]) assert.equal(parseRobotCommand({ operation: "mission_start", payload }), null);
});

test("real robot tools accept only bounded map, velocity, Zone and debug requests", () => {
  const zone = { behavior: "restricted", name: "주방", points: [[0, 0], [1, 0], [1, 1]] };
  for (const [operation, payload] of [
    ["map_delete", { map: "home.yaml" }],
    ["manual_move", { vx: 0.15, vy: 0, wz: -0.5 }],
    ["manual_move", { vx: 0, vy: 0, wz: 0 }],
    ["manual_move", { vx: -0.2, vy: 0.2, wz: 0 }],
    ["zones_save", { map: "home.yaml", zones: [zone, { behavior: "avoid", points: [[2, 2], [3, 2], [3, 3]] }] }],
    ["zones_save", { map: "home.yaml", zones: [] }],
    ["robot_ping", {}],
    ["robot_diagnostics", {}],
    ["debug_mission_start", { capability: "get_weather", arguments: { city: "Seoul" } }],
    ["mission_start", { capability: "relocalize", arguments: { method: 0 } }],
    ["mission_start", { capability: "relocalize", arguments: { method: 1, x: 1, y: -2, yaw: 0.5 } }],
  ]) assert.deepEqual(parseRobotCommand({ operation, payload }), { operation, payload });
  for (const [operation, payload] of [
    ["map_delete", { map: "../home.yaml" }],
    ["map_delete", { map: "home.yaml", files: ["home.pgm"] }],
    ["manual_move", { direction: "forward" }],
    ["manual_move", { vx: 0.25, vy: 0, wz: 0 }],
    ["manual_move", { vx: 0.1, vy: 0, wz: 0.6 }],
    ["manual_move", { vx: 0.1, vy: 0, wz: 0, hold_s: 1 }],
    ["manual_move", { vx: Infinity, vy: 0, wz: 0 }],
    ["manual_move", { vx: "0.1", vy: 0, wz: 0 }],
    ["zones_save", { map: "/tmp/home.yaml", zones: [] }],
    ["zones_save", { map: "home.yaml", zones: [{ ...zone, points: [[0, 0], [1, 0]] }] }],
    ["zones_save", { map: "home.yaml", zones: [{ ...zone, behavior: "lava" }] }],
    ["zones_save", { map: "home.yaml", zones: [{ ...zone, points: [[0, 0], [1, 0], [Infinity, 1]] }] }],
    ["zones_save", { map: "home.yaml", zones: Array.from({ length: 65 }, () => zone) }],
    ["robot_ping", { count: 3 }],
    ["debug_mission_start", { capability: "../shell", arguments: {} }],
    ["debug_mission_start", { capability: "patrol", arguments: [] }],
    ["debug_mission_start", { capability: "patrol", arguments: { note: "x".repeat(9000) } }],
    ["mission_start", { capability: "relocalize", arguments: { method: 1 } }],
    ["mission_start", { capability: "relocalize", arguments: { method: 0, x: 1, y: 1, yaw: 0 } }],
  ]) assert.equal(parseRobotCommand({ operation, payload }), null);
});

test("runtime selection does not accept paths or executable arguments", () => {
  for (const payload of [{ mode: "mapping" }, { mode: "navigation", map: "home.yaml" }]) {
    assert.ok(parseRobotCommand({ operation: "runtime_start", payload }));
  }
  for (const payload of [
    { mode: "navigation", map: "../home.yaml" },
    { mode: "navigation", map: "/tmp/home.yaml" },
    { mode: "mapping", args: "--anything" },
    { mode: "navigation", map: "home.yaml", command: "bash" },
  ]) assert.equal(parseRobotCommand({ operation: "runtime_start", payload }), null);
  for (const operation of ["mission_cancel", "runtime_stop"]) {
    assert.ok(parseRobotCommand({ operation }));
    assert.equal(parseRobotCommand({ operation, payload: { all: true } }), null);
  }
});
