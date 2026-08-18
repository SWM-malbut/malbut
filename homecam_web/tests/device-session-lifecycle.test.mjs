import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("an idle heartbeat does not close a provisioned device session", async () => {
  const database = await readFile(
    new URL("../db/homecam.ts", import.meta.url),
    "utf8",
  );
  const sessionRoute = await readFile(
    new URL("../app/api/device/v1/session/route.ts", import.meta.url),
    "utf8",
  );
  const liveRoute = await readFile(
    new URL("../app/api/devices/[deviceId]/live-session/route.ts", import.meta.url),
    "utf8",
  );
  const dashboard = await readFile(
    new URL("../app/components/homecam-dashboard.tsx", import.meta.url),
    "utf8",
  );
  const heartbeatFunction =
    database.match(
      /export async function updateDeviceHeartbeat[\s\S]*?(?=export async function prepareDeviceMediaSession)/,
    )?.[0] ?? "";

  assert.ok(heartbeatFunction);
  assert.doesNotMatch(heartbeatFunction, /stopDeviceMediaSession/);
  assert.doesNotMatch(database, /heartbeat_idle/);
  assert.match(sessionRoute, /export async function DELETE/);
  assert.match(sessionRoute, /stopDeviceMediaSession/);
  assert.match(
    sessionRoute,
    /"device_stop",\s*sessionId/,
  );
  assert.ok(
    [...sessionRoute.matchAll(/getDeviceSettings\(device\.deviceId\)/g)].length >= 3,
    "device settings are checked before and after the broker/session boundary",
  );
  assert.match(
    sessionRoute,
    /stopDeviceMediaSession\(\s*device\.deviceId,\s*"settings_race",\s*session\.id,\s*\)/,
  );
  assert.match(sessionRoute, /desiredState:\s*desiredState\(confirmedState\)/);
  assert.match(
    sessionRoute,
    /getActiveMediaSession\(device\.deviceId\)/,
  );
  assert.match(sessionRoute, /confirmedSession\?\.id !== session\.id/);
  assert.match(dashboard, /const displayedMediaReady = liveViewer[\s\S]*selectedDevice\?\.online && selectedDevice\.mediaHealthy/);
  assert.match(
    database,
    /WHERE device_id = \? AND active_session_id = \?/,
  );
  assert.match(database, /expectedSessionId\?: string/);
  assert.match(database, /recording_sessions\.kvs_channel_arn/);
  assert.match(database, /current\.channelArn === input\.channelArn/);
  assert.match(database, /"channel_changed"/);
  assert.match(
    database,
    /UPDATE stream_sessions SET status = 'ended', ended_at = \?[\s\S]*WHERE device_id = \? AND status = 'active'/,
  );
  assert.match(
    database,
    /SET active_stream_mode = \?, active_session_id = \?, media_healthy = 0/,
  );
  assert.match(
    database,
    /monitoring_enabled = 1[\s\S]*camera_enabled = 1[\s\S]*media_healthy = 1[\s\S]*active_stream_mode = 'storage'[\s\S]*active_session_id = \?/,
  );
  assert.match(database, /SET started_at = COALESCE\(started_at, \?\)/);
  assert.match(database, /EVENT_OUTSIDE_RECORDING/);
  assert.doesNotMatch(liveRoute, /activeSession,\s*\n/);
  assert.doesNotMatch(liveRoute, /activeSession\.channelArn/);
  assert.doesNotMatch(liveRoute, /activeSession\.streamArn/);
});
