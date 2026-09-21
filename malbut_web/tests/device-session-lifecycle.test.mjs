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
    /getActiveMediaSession\(device\.deviceId, mode\)/,
  );
  assert.match(sessionRoute, /confirmedSession\?\.id !== session\.id/);
  assert.match(dashboard, /const displayedMediaReady = liveViewer[\s\S]*selectedDevice\?\.online && selectedDevice\.p2pHealthy/);
  assert.match(
    database,
    /stream_sessions\.id = COALESCE\(\?, device_state\.active_session_id\)/,
  );
  assert.match(database, /expectedSessionId\?: string/);
  assert.match(database, /recording_sessions\.kvs_channel_arn/);
  assert.match(database, /current\.channelArn === input\.channelArn/);
  assert.match(database, /"channel_changed"/);
  assert.match(
    database,
    /UPDATE stream_sessions SET status = 'ended', ended_at = \?[\s\S]*WHERE device_id = \? AND mode = \? AND status = 'active'/,
  );
  assert.match(
    database,
    /SET p2p_session_id = CASE WHEN \? = 'p2p'/,
  );
  assert.match(
    database,
    /monitoring_enabled = 1[\s\S]*camera_enabled = 1[\s\S]*storage_healthy = 1[\s\S]*storage_session_id = \?/,
  );
  assert.match(
    liveRoute,
    /getActiveMediaSession\(deviceId, "p2p"\)/,
  );
  assert.doesNotMatch(liveRoute, /requestBrokerJoinStorage/);
  assert.match(database, /SET started_at = COALESCE\(started_at, \?\)/);
  assert.match(database, /EVENT_OUTSIDE_RECORDING/);
  assert.doesNotMatch(liveRoute, /activeSession,\s*\n/);
  assert.doesNotMatch(liveRoute, /activeSession\.channelArn/);
  assert.doesNotMatch(liveRoute, /activeSession\.streamArn/);
});

test("P2P and Storage keep independent sessions, senders, and SDK lifetime", async () => {
  const [
    mediaAgent,
    kvsTransport,
    mediaCmake,
    sessionClient,
    viewer,
    migration,
  ] = await Promise.all([
    readFile(
      new URL(
        "../../homecam_agent/homecam_media_agent/src/media_agent_node.cpp",
        import.meta.url,
      ),
      "utf8",
    ),
    readFile(
      new URL(
        "../../homecam_agent/homecam_media_agent/src/kvs_transport.cpp",
        import.meta.url,
      ),
      "utf8",
    ),
    readFile(
      new URL(
        "../../homecam_agent/homecam_media_agent/CMakeLists.txt",
        import.meta.url,
      ),
      "utf8",
    ),
    readFile(
      new URL(
        "../../homecam_agent/homecam_media_agent/src/session_client.cpp",
        import.meta.url,
      ),
      "utf8",
    ),
    readFile(
      new URL("../app/components/homecam-app.tsx", import.meta.url),
      "utf8",
    ),
    readFile(
      new URL("../db/migrations/0006_dual_media_sessions.sql", import.meta.url),
      "utf8",
    ),
  ]);

  assert.match(mediaAgent, /transport_ = make_kvs_transport\(\)/);
  assert.match(mediaAgent, /storage_transport_ = make_kvs_transport\(\)/);
  assert.match(mediaAgent, /p2p_sender_ = std::make_unique<TransportSender>/);
  assert.match(mediaAgent, /storage_sender_ = std::make_unique<TransportSender>/);
  assert.match(mediaAgent, /const std::string wanted_mode = "p2p"/);
  assert.match(mediaAgent, /SessionMode::kStorage/);
  assert.match(kvsTransport, /process_kvs_runtime_references/);
  assert.match(mediaCmake, /deinitKvsWebRtc=homecam_defer_deinit_kvs_webrtc/);
  assert.match(
    sessionClient,
    /mode == SessionMode::kStorage \? "storage" : "p2p"/,
  );
  assert.doesNotMatch(viewer, /connectDeviceLiveHls/);
  assert.doesNotMatch(viewer, /storageViewerTransport/);
  assert.match(migration, /stream_sessions_device_active_mode_idx/);
});
