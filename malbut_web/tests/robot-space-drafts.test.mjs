// 방·구역 편집 반영 대기 (SWM25-237): the owner edits with the 말벗 off, the server sends the
// latest save when the robot is back on the same map, and a changed map is never written.
import assert from "node:assert/strict";
import path from "node:path";
import test from "node:test";
import { fallDatabase, moduleLoader } from "./helpers/fall-db-harness.mjs";

const ORIGIN = "https://homecam.example.com";
const ENV = { AUTH_PUBLIC_ORIGIN: ORIGIN };
const REAL_ROBOT = { robot_interface: "malbut_manager_v1", runtime_mode: "navigation" };
const plain = (value) => JSON.parse(JSON.stringify(value));

function rectangle(minX, minY, maxX, maxY) {
  return [[minX, minY], [maxX, minY], [maxX, maxY], [minX, maxY], [minX, minY]];
}

function room(id, ring, name = id) {
  return {
    type: "Feature", id,
    properties: { role: "room", room_id: id, name, category: "unassigned" },
    geometry: { type: "Polygon", coordinates: [ring] },
  };
}

function zone(id, ring = rectangle(1, 1, 2, 2), behavior = "restricted") {
  return {
    type: "Feature", id,
    properties: { role: "semantic_zone", zone_id: id, name: `구역 ${id}`, behavior },
    geometry: { type: "Polygon", coordinates: [ring] },
  };
}

async function withDrafts(work) {
  const h = await fallDatabase();
  const load = moduleLoader({
    [path.join(h.root, "app/runtime-env.ts")]: { getRuntimeEnvironment: () => ENV, getRuntimeValue: (name) => ENV[name] },
    [path.join(h.root, "app/server-auth.ts")]: { async getRequestUserId(request) { return request.headers.get("x-test-user"); } },
  });
  const pg = load("db/postgres.ts");
  const robot = load("db/robot-map.ts");
  const routes = {
    split: load("app/api/devices/[deviceId]/robot/rooms/split/route.ts"),
    merge: load("app/api/devices/[deviceId]/robot/rooms/merge/route.ts"),
    drafts: load("app/api/devices/[deviceId]/robot/space-drafts/route.ts"),
    semantic: load("app/api/devices/[deviceId]/robot/semantic/route.ts"),
  };
  const params = () => ({ params: Promise.resolve({ deviceId: "robot-a" }) });
  const post = (route, userId, body, origin = ORIGIN) => routes[route].POST(new Request(
    `${ORIGIN}/api/devices/robot-a/robot/${route}`, {
      method: "POST",
      headers: { "content-type": "application/json", ...(origin ? { origin } : {}), ...(userId ? { "x-test-user": userId } : {}) },
      body: JSON.stringify(body),
    }), params());
  const semantics = async (userId = "u-owner") => (await routes.semantic.GET(new Request(
    `${ORIGIN}/api/devices/robot-a/robot/semantic`, { headers: { "x-test-user": userId } }), params())).json();
  // The robot's state upload: `secondsAgo` old (online within 15 s).
  const state = (nav2 = REAL_ROBOT, secondsAgo = 0) => robot.storeRobotState("robot-a", {
    state: "ready", message: "", pose: { x: 0, y: 0, yaw: 0 },
    localization: { state: "ok", tfAgeS: 0 }, nav2, target: null, mapRevision: 1,
    observedAt: new Date(Date.now() - secondsAgo * 1000).toISOString(),
  });
  // The robot's map upload with its rooms and Zones.
  const upload = (mapRevision = "map-1", rooms = [room("room-1", rectangle(0, 0, 10, 6), "공간 1")], zones = []) =>
    robot.storeRobotMap("robot-a", {
      finalized: true, revision: `${mapRevision}-${Date.now()}`, mapId: "real-home", mapRevision,
      sourceCreatedAt: new Date().toISOString(),
      geometry: { width: 240, height: 160, resolution: 0.05, originX: -1, originY: -1, originYaw: 0 },
      previewBase64: "iVBORw0KGgo=",
      userMap: {
        type: "FeatureCollection", map_id: "real-home", map_revision: mapRevision,
        features: [
          { type: "Feature", id: "walkable", properties: { role: "walkable_area" },
            geometry: { type: "Polygon", coordinates: [rectangle(0, 0, 10, 6)] } },
          ...rooms,
        ],
      },
      semanticZones: {
        type: "FeatureCollection", format: "malbut-semantic-zones-v1", map_id: "real-home",
        map_revision: mapRevision, frame_id: "map", map: "home.yaml", editable: true, message: "",
        features: zones,
      },
    });
  const draftRow = async (kind) => (await h.db.query(
    "SELECT status, error, command_id FROM robot_semantic_drafts WHERE device_id='robot-a' AND kind=$1", [kind])).rows[0];
  const roomsOf = (value) => value.userMap.features.filter((feature) => feature.properties.role === "room");
  try {
    await pg.withPostgresPoolForTest(h.pool, () => work({ h, robot, post, semantics, state, upload, draftRow, roomsOf }));
  } finally { await h.db.close(); }
}

test("rooms split with the 말벗 off wait, then go out once it is back on the same map", async () => {
  await withDrafts(async ({ h, robot, post, semantics, state, upload, draftRow, roomsOf }) => {
    await upload();
    await state(REAL_ROBOT, 60); // off for a minute
    const original = roomsOf(await semantics())[0];

    const split = await post("split", "u-owner", { room: original, lines: [[5, 0], [5, 6]] });
    assert.equal(split.status, 200);
    const { rooms } = await split.json();
    assert.deepEqual(rooms.map((part) => part.id), ["room-1-a", "room-1-b"]);

    const saved = await post("drafts", "u-owner", { kind: "rooms", mapId: "real-home", mapRevision: "map-1", rooms });
    assert.equal(saved.status, 200);
    const body = await saved.json();
    assert.equal(body.draft.status, "pending");
    assert.ok(body.rooms.every((part) => part.properties.area_m2 > 29 && part.properties.representative_point));

    // The owner sees the saved rooms and where the save stands; guardians see what the robot uses.
    const owner = await semantics();
    assert.deepEqual(roomsOf(owner).map((part) => part.id), ["room-1-a", "room-1-b"]);
    assert.equal(owner.userMap.features[0].properties.role, "walkable_area");
    assert.equal(owner.drafts.rooms.status, "pending");
    assert.equal(owner.drafts.zones, null);
    const guardian = await semantics("u-family");
    assert.deepEqual(roomsOf(guardian).map((part) => part.id), ["room-1"]);
    assert.equal(guardian.drafts, undefined);

    // A robot that stopped reporting is not sent anything.
    assert.deepEqual(plain(await robot.claimRobotCommands("robot-a")), []);
    await state(REAL_ROBOT);
    const [command] = await robot.claimRobotCommands("robot-a");
    assert.equal(command.operation, "rooms_save");
    assert.deepEqual(Object.keys(command.payload).sort(), ["map_id", "map_revision", "resolution", "rooms"]);
    assert.equal(command.payload.map_revision, "map-1");
    assert.equal(command.payload.resolution, 0.05);
    assert.deepEqual(command.payload.rooms.map((part) => part.id), ["room-1-a", "room-1-b"]);
    assert.equal((await semantics()).drafts.rooms.status, "sent");

    await robot.completeRobotCommand({ deviceId: "robot-a", commandId: command.id, ok: true,
      result: { saved: 2, features: 3, map_id: "real-home", map_revision: "map-1" } });
    const applied = await semantics();
    assert.equal(applied.drafts.rooms.status, "applied");
    assert.ok(applied.drafts.rooms.resolvedAt);
    assert.deepEqual(roomsOf(applied).map((part) => part.id), ["room-1-a", "room-1-b"],
      "the saved rooms show until the robot uploads the map with them");

    // The robot's next upload is what the map shows from then on.
    await upload("map-1", rooms.map((part) => ({ ...part, properties: { ...part.properties, name: `로봇 ${part.id}` } })));
    assert.deepEqual(roomsOf(await semantics()).map((part) => part.properties.name), ["로봇 room-1-a", "로봇 room-1-b"]);
    assert.deepEqual(plain(await robot.claimRobotCommands("robot-a")), [], "an applied save is sent once");
    assert.equal((await draftRow("rooms")).status, "applied");
    const audit = (await h.db.query(
      "SELECT actor_id FROM access_audit_log WHERE action='robot.rooms_saved'")).rows;
    assert.deepEqual(audit, [{ actor_id: "u-owner" }]);
  });
});

test("a save for a map the 말벗 no longer has turns stale and is never written", async () => {
  await withDrafts(async ({ robot, post, semantics, state, upload, draftRow }) => {
    await upload();
    await state(REAL_ROBOT, 60);
    const saved = await post("drafts", "u-owner", {
      kind: "zones", mapId: "real-home", mapRevision: "map-1", features: [zone("kitchen")],
    });
    assert.equal(saved.status, 200);
    const pending = await semantics();
    assert.deepEqual(pending.zones.features.map((feature) => feature.id), ["kitchen"]);
    assert.equal(pending.zones.map, "home.yaml", "the robot's own Zone fields stay");
    assert.equal(pending.zones.map_revision, "map-1");

    // The 말벗 came back with the map made again (same name, new picture).
    await upload("map-2");
    await state(REAL_ROBOT);
    const changed = await semantics();
    assert.equal(changed.drafts.zones.status, "stale");
    assert.equal(changed.drafts.zones.error, "MAP_CHANGED");
    assert.deepEqual(changed.zones.features, [], "the stale Zones are not shown as the robot's");
    assert.deepEqual(plain(await robot.claimRobotCommands("robot-a")), []);
    assert.equal((await draftRow("zones")).command_id, null);

    // An editor still holding the old map is told to reload.
    const late = await post("drafts", "u-owner", {
      kind: "zones", mapId: "real-home", mapRevision: "map-1", features: [zone("kitchen")],
    });
    assert.equal(late.status, 409);
    assert.match((await late.json()).error, /지도가 바뀌었어요/);
  });
});

test("the robot's refusal, a timeout and a busy command slot each settle the save", async () => {
  await withDrafts(async ({ h, robot, post, state, upload, draftRow }) => {
    await upload();
    await state(REAL_ROBOT);
    const rooms = [room("room-1", rectangle(0, 0, 10, 6), "거실")];
    const save = () => post("drafts", "u-owner", { kind: "rooms", mapId: "real-home", mapRevision: "map-1", rooms });

    // Online on the same map: sent right away.
    assert.equal((await (await save()).json()).draft.status, "sent");
    let [command] = await robot.claimRobotCommands("robot-a");
    await robot.completeRobotCommand({ deviceId: "robot-a", commandId: command.id, ok: false,
      result: { error: "The saved map changed; reload the rooms" } });
    assert.deepEqual(await draftRow("rooms"), { status: "stale", error: "MAP_CHANGED", command_id: command.id });

    // The simulator words it differently.
    await save();
    [command] = await robot.claimRobotCommands("robot-a");
    await robot.completeRobotCommand({ deviceId: "robot-a", commandId: command.id, ok: false,
      result: { error: "Room map_revision does not match the User Map" } });
    assert.equal((await draftRow("rooms")).status, "stale");

    // The robot went away before answering: the save waits and goes out again.
    await save();
    [command] = await robot.claimRobotCommands("robot-a");
    await h.db.query("UPDATE robot_commands SET requested_at=$1 WHERE id=$2",
      [new Date(Date.now() - 120_000).toISOString(), command.id]);
    const [again] = await robot.claimRobotCommands("robot-a");
    assert.equal(again.operation, "rooms_save");
    assert.notEqual(again.id, command.id);
    assert.deepEqual(await draftRow("rooms"), { status: "sent", error: null, command_id: again.id });
    await robot.completeRobotCommand({ deviceId: "robot-a", commandId: again.id, ok: false,
      result: { error: "Every room needs an id" } });
    assert.deepEqual(await draftRow("rooms"), { status: "failed", error: "Every room needs an id", command_id: again.id });

    // Another command holds the one slot: the save waits for it, then goes out.
    const stop = await robot.createRobotCommand({ deviceId: "robot-a", userId: "u-owner", operation: "runtime_stop" });
    assert.equal((await (await save()).json()).draft.status, "pending");
    const [first] = await robot.claimRobotCommands("robot-a");
    assert.equal(first.id, stop.id);
    await robot.completeRobotCommand({ deviceId: "robot-a", commandId: stop.id, ok: true, result: {} });
    const [next] = await robot.claimRobotCommands("robot-a");
    assert.equal(next.operation, "rooms_save");

    // A newer save while one is with the robot: both go, the newer last.
    await post("drafts", "u-owner", { kind: "rooms", mapId: "real-home", mapRevision: "map-1",
      rooms: [room("room-1", rectangle(0, 0, 10, 6), "큰 거실")] });
    assert.equal((await draftRow("rooms")).status, "pending");
    await robot.completeRobotCommand({ deviceId: "robot-a", commandId: next.id, ok: true, result: {} });
    assert.equal((await draftRow("rooms")).status, "pending", "the older command does not settle the newer save");
    const [newest] = await robot.claimRobotCommands("robot-a");
    assert.equal(newest.payload.rooms[0].properties.name, "큰 거실");
  });
});

test("waiting saves go out only in navigation, rooms before Zones", async () => {
  await withDrafts(async ({ robot, post, state, upload }) => {
    await upload();
    await state({ ...REAL_ROBOT, runtime_mode: "mapping" });
    await post("drafts", "u-owner", { kind: "zones", mapId: "real-home", mapRevision: "map-1", features: [zone("a")] });
    await post("drafts", "u-owner", { kind: "rooms", mapId: "real-home", mapRevision: "map-1",
      rooms: [room("room-1", rectangle(0, 0, 10, 6))] });
    assert.deepEqual(plain(await robot.claimRobotCommands("robot-a")), [], "not while making a map");
    await state(REAL_ROBOT);
    const [rooms] = await robot.claimRobotCommands("robot-a");
    assert.equal(rooms.operation, "rooms_save");
    await robot.completeRobotCommand({ deviceId: "robot-a", commandId: rooms.id, ok: true, result: {} });
    const [zones] = await robot.claimRobotCommands("robot-a");
    assert.equal(zones.operation, "zones_apply");
    assert.deepEqual(Object.keys(zones.payload).sort(),
      ["features", "format", "frame_id", "map_id", "map_revision", "type"]);
    assert.equal(zones.payload.format, "malbut-semantic-zones-v1");
  });
});

test("only the owner edits, from this site, within the robot's limits", async () => {
  await withDrafts(async ({ post, state, upload, semantics, roomsOf }) => {
    await upload();
    await state(REAL_ROBOT, 60);
    const original = roomsOf(await semantics())[0];
    const zonesSave = (features) => post("drafts", "u-owner", { kind: "zones", mapId: "real-home", mapRevision: "map-1", features });

    assert.equal((await post("split", "u-family", { room: original, lines: [[5, 0], [5, 6]] })).status, 403);
    assert.equal((await post("drafts", "u-family", { kind: "zones", mapId: "real-home", mapRevision: "map-1", features: [] })).status, 403);
    assert.equal((await post("split", null, { room: original, lines: [[5, 0], [5, 6]] })).status, 401);
    assert.equal((await post("split", "u-owner", { room: original, lines: [[5, 0], [5, 6]] }, "https://evil.example")).status, 403);

    const outside = await post("split", "u-owner", { room: original, lines: [[-1, -1], [-0.5, -0.5]] });
    assert.equal(outside.status, 422);
    assert.equal((await outside.json()).error, "분할선의 점을 방 벽 근처에 놓으세요.");
    const apart = await post("merge", "u-owner", {
      rooms: [room("a", rectangle(0, 0, 2, 2)), room("b", rectangle(5, 5, 7, 7))],
    });
    assert.equal(apart.status, 422);
    assert.equal((await apart.json()).error, "서로 맞닿아 있는 두 방만 합칠 수 있습니다.");
    const merged = await post("merge", "u-owner", {
      rooms: [room("a", rectangle(0, 0, 5, 6)), room("b", rectangle(5, 0, 10, 6))],
    });
    assert.equal(merged.status, 200);
    assert.deepEqual((await merged.json()).room.properties.merged_from, ["a", "b"]);

    // The real robot keeps 64 Zones of up to 64 corners.
    const many = Array.from({ length: 65 }, (_, index) => zone(`z${index}`));
    assert.match((await (await zonesSave(many)).json()).error, /구역은 64개까지/);
    const circle = Array.from({ length: 70 }, (_, index) => [
      5 + 2 * Math.cos(index / 70 * 2 * Math.PI), 3 + 2 * Math.sin(index / 70 * 2 * Math.PI)]);
    const round = await zonesSave([zone("round", [...circle, circle[0]])]);
    assert.equal(round.status, 422);
    assert.match((await round.json()).error, /꼭짓점이 너무 많아요/);
    assert.equal((await zonesSave([zone("same"), zone("same")])).status, 422);
    assert.equal((await zonesSave([zone("tiny", rectangle(1, 1, 1.05, 1.05))])).status, 422);
    assert.equal((await zonesSave([{ ...zone("bad"), geometry: { type: "Polygon", coordinates: [[[0, 0], [1, 0], [1, 1]]] } }])).status, 422);
    assert.equal((await post("drafts", "u-owner", { kind: "rooms", mapId: "real-home", mapRevision: "map-1",
      rooms: [room("a", rectangle(0, 0, 5, 6)), room("a", rectangle(5, 0, 10, 6))] })).status, 422);
    assert.equal((await post("drafts", "u-owner", { kind: "rooms", mapId: "real-home", mapRevision: "map-1", rooms: [] })).status, 422);
    assert.equal((await post("drafts", "u-owner", { kind: "doors", mapId: "real-home", mapRevision: "map-1", rooms: [] })).status, 400);

    // The simulator has no 64-Zone file.
    await state({ runtime_mode: "navigation" }, 60);
    assert.equal((await zonesSave(many)).status, 200);
  });
});
