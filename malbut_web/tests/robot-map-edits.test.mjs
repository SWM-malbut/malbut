import assert from "node:assert/strict";
import test from "node:test";
import path from "node:path";
import { moduleLoader, fallDatabase } from "./helpers/fall-db-harness.mjs";

test("cleanup transforms rotated map pixels and preserves unknown cells", () => {
  const { pixelToMap, editedPixels, polygonError } = moduleLoader()("app/robot-map-edit.ts");
  const g = { width: 3, height: 2, resolution: .5, originX: 10, originY: -3, originYaw: Math.PI/2 };
  assert.deepEqual(Array.from(pixelToMap([1,1],g)), [9.5,-2.5]);
  const pixels = new Uint8ClampedArray([0,0,0,255,205,205,205,255,254,254,254,255]);
  const rectangle = [[0,0],[3,0],[3,1],[0,1]];
  const result = editedPixels(pixels,3,1,[rectangle,rectangle]);
  assert.equal(result.count,1);
  assert.equal(result.pixels[4],205);
  assert.equal(result.pixels[8],254);
  assert.equal(pixels[0],0);
  assert.ok(polygonError([[0,0],[2,2],[0,3],[3,0]]));
  assert.equal(polygonError(rectangle),null);
});

test("edit commands carry bounded map identifiers, revisions and polygons", () => {
  const { parseRobotCommand } = moduleLoader()("app/robot-contract.ts");
  for (const [operation,payload] of [
    ["map_edit_open",{ map:"home.yaml" }],
    ["map_edit_restore",{ map:"home.yaml",expectedRevision:"edit-1" }],
    ["map_edit_apply",{ map:"home.yaml",expectedRevision:"edit-1",revision:"edit-1" }],
    ["map_edit_save",{ map:"home.yaml",expectedRevision:"edit-1",polygons:[[[0,0],[1,0],[1,1]]] }],
  ]) assert.ok(parseRobotCommand({ operation,payload }));
  for (const payload of [
    { map:"../home.yaml",expectedRevision:"edit-1",polygons:[[[0,0],[1,0],[1,1]]] },
    { map:"home.yaml",expectedRevision:"edit-1",polygons:[] },
    { map:"home.yaml",expectedRevision:"edit-1",polygons:[[[0,0],[Infinity,0],[1,1]]] },
    { map:"home.yaml",expectedRevision:"",polygons:[[[0,0],[1,0],[1,1]]] },
  ]) assert.equal(parseRobotCommand({ operation:"map_edit_save",payload }),null);
});

test("saved-map previews stay separate by device, map and revision; only owners read them", async () => {
  const h = await fallDatabase();
  const load = moduleLoader({
    [path.join(h.root,"app/server-auth.ts")]: { getRequestUserId: async (r) => r.headers.get("x-test-user") },
  });
  const pg = load("db/postgres.ts"), store = load("db/robot-map-edits.ts");
  const route = load("app/api/devices/[deviceId]/robot/saved-maps/route.ts");
  const value = { map:"home.yaml",revision:"edit-1",originalRevision:"source-1",appliedRevision:"source-1",
    geometry:{ width:1,height:1,resolution:.05,originX:0,originY:0,originYaw:0 },
    previewBase64:"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII=" };
  try {
    await pg.withPostgresPoolForTest(h.pool,async () => {
      assert.ok(store.parseSavedMapEdit(value));
      await store.storeSavedMapEdit("robot-a",value);
      await store.storeSavedMapEdit("robot-a",{ ...value,map:"upstairs.yaml",revision:"edit-2" });
      const read = (user) => route.GET(new Request("https://homecam.example.com/api/devices/robot-a/robot/saved-maps?map=home.yaml",
        { headers: { "x-test-user":user } }),{ params:Promise.resolve({ deviceId:"robot-a" }) });
      assert.equal((await read("u-owner")).status,200);
      assert.equal((await (await read("u-owner")).json()).revision,"edit-1");
      assert.equal((await read("u-family")).status,403);
      const count = await h.db.query("SELECT count(*)::int AS n FROM robot_maps");
      assert.equal(count.rows[0].n,0);
      const commands = load("db/robot-map.ts");
      await assert.rejects(() => commands.createRobotCommand({ deviceId:"robot-a",userId:"u-family",operation:"map_edit_open",payload:{ map:"home.yaml" } }), /FORBIDDEN/);
      await assert.rejects(() => commands.createRobotCommand({ deviceId:"robot-a",userId:"u-owner",operation:"map_edit_open",payload:{ map:"home.yaml" } }), /UNSUPPORTED_ROBOT_COMMAND/);
      const state = { state:"ready",message:"",pose:null,localization:{state:"ok",tfAgeS:0},
        nav2:{robot_interface:"malbut_manager_v1",map_edit:"v1"},target:null,mapRevision:1,observedAt:new Date().toISOString() };
      await commands.storeRobotState("robot-a",state);
      const edit = await commands.createRobotCommand({ deviceId:"robot-a",userId:"u-owner",operation:"map_edit_open",payload:{map:"home.yaml"} });
      assert.equal(edit.status,"queued");
      await h.db.query("UPDATE robot_commands SET status='claimed' WHERE id=$1",[edit.id]);
      await commands.completeRobotCommand({deviceId:"robot-a",commandId:edit.id,ok:true,result:{revision:"edit-1"}});
      const deletion = await commands.createRobotCommand({deviceId:"robot-a",userId:"u-owner",operation:"map_delete",payload:{map:"home.yaml"}});
      await h.db.query("UPDATE robot_commands SET status='claimed' WHERE id=$1",[deletion.id]);
      await commands.completeRobotCommand({deviceId:"robot-a",commandId:deletion.id,ok:true,result:{deleted:"home.yaml"}});
      assert.equal(await store.readSavedMapEdit("robot-a","u-owner","home.yaml"),null);
      assert.equal((await store.readSavedMapEdit("robot-a","u-owner","upstairs.yaml")).revision,"edit-2");
      await commands.storeRobotState("robot-a",{...state,observedAt:new Date(Date.now()-60000).toISOString()});
      await assert.rejects(() => commands.createRobotCommand({ deviceId:"robot-a",userId:"u-owner",operation:"map_edit_open",payload:{map:"home.yaml"} }), /ROBOT_OFFLINE/);
    });
  } finally { await h.db.close(); }
});
