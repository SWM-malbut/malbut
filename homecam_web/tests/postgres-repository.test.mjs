import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { PGlite } from "@electric-sql/pglite";
import ts from "typescript";

const requireFromTest = createRequire(import.meta.url);
const testDirectory = path.dirname(fileURLToPath(import.meta.url));
const projectDirectory = path.resolve(testDirectory, "..");

test("homecam PostgreSQL repository completes the device storage event lifecycle", async () => {
  const initialMigration = await readFile(
    new URL("../db/migrations/0001_initial.sql", import.meta.url),
    "utf8",
  );
  const authMigration = await readFile(
    new URL("../db/migrations/0002_web_auth_sessions.sql", import.meta.url),
    "utf8",
  );
  const robotMigration = await readFile(
    new URL("../db/migrations/0003_robot_map.sql", import.meta.url),
    "utf8",
  );
  const robotSemanticsMigration = await readFile(
    new URL("../db/migrations/0004_robot_map_semantics.sql", import.meta.url),
    "utf8",
  );
  const eventClipsMigration = await readFile(
    new URL("../db/migrations/0005_event_clips.sql", import.meta.url),
    "utf8",
  );
  const dualMediaSessionsMigration = await readFile(
    new URL("../db/migrations/0006_dual_media_sessions.sql", import.meta.url),
    "utf8",
  );
  const robotDriveModesMigration = await readFile(
    new URL("../db/migrations/0007_robot_drive_modes.sql", import.meta.url),
    "utf8",
  );
  const database = new PGlite();
  try {
    await database.exec(initialMigration);
    await database.exec(authMigration);
    await database.exec(robotMigration);
    await database.exec(robotSemanticsMigration);
    await database.exec(eventClipsMigration);
    await database.exec(dualMediaSessionsMigration);
    await database.exec(robotDriveModesMigration);
    await database.exec(`
      CREATE TABLE homecam_schema_migrations (
        version TEXT PRIMARY KEY,
        applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
      );
      INSERT INTO homecam_schema_migrations (version)
      VALUES
        ('0001_initial'),
        ('0002_web_auth_sessions'),
        ('0003_robot_map'),
        ('0004_robot_map_semantics'),
        ('0005_event_clips'),
        ('0006_dual_media_sessions'),
        ('0007_robot_drive_modes');
    `);
    await seedDevice(database);

    const loadModule = createTypescriptModuleLoader();
    const postgres = loadModule(path.join(projectDirectory, "db/postgres.ts"));
    const homecam = loadModule(path.join(projectDirectory, "db/homecam.ts"));
    const petcam = loadModule(path.join(projectDirectory, "db/petcam.ts"));
    const robotMap = loadModule(path.join(projectDirectory, "db/robot-map.ts"));
    const robotContract = loadModule(path.join(projectDirectory, "app/robot-contract.ts"));
    const pool = pglitePoolAdapter(database);

    await postgres.withPostgresPoolForTest(pool, async () => {
      const expiresAt = new Date(Date.now() + 60 * 60 * 1000).toISOString();
      const credential = await homecam.createDeviceCredential({
        deviceId: "living-room",
        userEmail: "owner@example.com",
        label: "PGlite integration credential",
        expiresAt,
      });
      assert.match(
        credential.token,
        /^hc1\.[0-9a-f-]{36}\.[A-Za-z0-9_-]{32,}$/,
      );

      const identity = await homecam.authenticateDeviceToken(credential.token);
      assert.deepEqual(plain(identity), {
        credentialId: credential.id,
        deviceId: "living-room",
        displayName: "거실 홈캠",
        legacyChannelArn: "arn:test:kvs:p2p",
      });

      const cameraOff = await homecam.updateDeviceSettings({
        deviceId: "living-room",
        userEmail: "owner@example.com",
        patch: { cameraEnabled: false },
      });
      assert.equal(cameraOff.cameraEnabled, false);
      assert.equal(cameraOff.monitoringEnabled, false);

      const cameraAndMonitoringOn = await homecam.updateDeviceSettings({
        deviceId: "living-room",
        userEmail: "owner@example.com",
        patch: { cameraEnabled: true, monitoringEnabled: true },
      });
      assert.equal(cameraAndMonitoringOn.cameraEnabled, true);
      assert.equal(cameraAndMonitoringOn.monitoringEnabled, true);

      const p2pSession = await homecam.prepareDeviceMediaSession({
        deviceId: "living-room",
        mode: "p2p",
        channelArn: "arn:test:kvs:p2p",
      });
      const session = await homecam.prepareDeviceMediaSession({
        deviceId: "living-room",
        mode: "storage",
        channelArn: "arn:test:kvs:storage",
        streamArn: "arn:test:kvs:archive",
      });
      assert.equal(session.mode, "storage");

      const heartbeat = await homecam.updateDeviceHeartbeat({
        deviceId: "living-room",
        sourceProfile: "sim",
        imageTopic: "/camera/color/image_raw",
        streamMode: "storage",
        mediaHealthy: true,
        p2pHealthy: true,
        storageHealthy: true,
        detectorHealthy: true,
      });
      assert.equal(heartbeat.streamMode, "storage");
      assert.equal(heartbeat.mediaHealthy, true);
      assert.equal(heartbeat.detectorHealthy, true);
      assert.equal(heartbeat.activeSessionId, session.id);
      assert.equal(heartbeat.activeSession?.id, session.id);
      assert.equal(heartbeat.activeSession?.mode, "storage");
      assert.equal(heartbeat.activeSessions.p2p?.id, p2pSession.id);
      assert.equal(heartbeat.activeSessions.storage?.id, session.id);
      assert.equal(heartbeat.p2pHealthy, true);
      assert.equal(heartbeat.storageHealthy, true);
      assert.ok(
        Date.parse(heartbeat.activeSession?.expiresAt) >=
          Date.parse(session.expiresAt),
      );

      const activeSession = await homecam.getActiveMediaSession("living-room");
      assert.equal(activeSession?.id, session.id);
      assert.equal(activeSession?.streamArn, "arn:test:kvs:archive");
      assert.ok(activeSession?.recordingStartedAt);

      const occurredAt = new Date(
        Date.parse(activeSession.recordingStartedAt) + 1_000,
      ).toISOString();
      const eventInput = {
        eventType: "person",
        confidence: 0.94,
        occurredAt,
        idempotencyKey: "repository-person-0001",
        recordingOffsetMs: 1_000,
      };
      const firstEvent = await homecam.insertHomecamEvent(
        "living-room",
        eventInput,
      );
      const duplicateEvent = await homecam.insertHomecamEvent(
        "living-room",
        eventInput,
      );
      assert.equal(firstEvent.created, true);
      assert.equal(duplicateEvent.created, false);
      assert.equal(duplicateEvent.event.id, firstEvent.event.id);

      const events = await homecam.listHomecamEvents({
        deviceId: "living-room",
        eventTypes: ["person"],
        limit: 10,
      });
      assert.equal(events.length, 1);
      assert.deepEqual(plain(events[0]), plain(firstEvent.event));

      const clipStartAt = new Date(
        Date.parse(activeSession.recordingStartedAt) + 2_000,
      ).toISOString();
      const clipStarted = {
        eventGroupId: "0198a0e8-5800-7000-8000-000000000001",
        segmentIndex: 0,
        primaryType: "person",
        labels: ["person", "motion"],
        confidence: 0.92,
        detectedAt: new Date(Date.parse(clipStartAt) + 5_000).toISOString(),
        startAt: clipStartAt,
        endAt: null,
        monotonicDurationMs: null,
        bootId: "11111111-1111-4111-8111-111111111111",
        sessionIds: [session.id],
        clockSteppedDuringEvent: false,
        notificationEligible: false,
        idempotencyKey: "a".repeat(64),
      };
      const startedClip = await homecam.upsertHomecamEventClip(
        "living-room",
        "started",
        clipStarted,
      );
      const duplicateClip = await homecam.upsertHomecamEventClip(
        "living-room",
        "started",
        clipStarted,
      );
      assert.equal(startedClip.created, true);
      assert.equal(duplicateClip.created, false);
      assert.equal(startedClip.event.clipState, "recording");

      const refreshedStorageSession = await homecam.prepareDeviceMediaSession({
        deviceId: "living-room",
        mode: "storage",
        channelArn: "arn:test:kvs:storage",
        streamArn: "arn:test:kvs:archive",
      });
      assert.notEqual(refreshedStorageSession.id, session.id);
      assert.equal(
        (await homecam.getActiveMediaSession("living-room", "p2p"))?.id,
        p2pSession.id,
      );
      await homecam.updateDeviceHeartbeat({
        deviceId: "living-room",
        sourceProfile: "sim",
        imageTopic: "/camera/color/image_raw",
        streamMode: "p2p",
        mediaHealthy: true,
        p2pHealthy: true,
        storageHealthy: true,
        detectorHealthy: true,
      });

      const endedClip = await homecam.upsertHomecamEventClip(
        "living-room",
        "ended",
        {
          ...clipStarted,
          sessionIds: [session.id, refreshedStorageSession.id],
          confidence: 0.97,
          endAt: new Date(Date.parse(clipStartAt) + 120_000).toISOString(),
          monotonicDurationMs: 120_000,
          idempotencyKey: "b".repeat(64),
        },
      );
      assert.equal(endedClip.event.clipState, "ready");
      assert.equal(endedClip.event.monotonicDurationMs, 120_000);
      assert.deepEqual(endedClip.event.labels, ["person", "motion"]);
      const segmentEnds = [240_000, 300_000];
      const segmentDurations = [120_000, 60_000];
      const segmentEvents = [];
      for (const [offset, segmentIndex] of [120_000, 240_000].map(
        (value, index) => [value, index + 1],
      )) {
        const startAt = new Date(Date.parse(clipStartAt) + offset).toISOString();
        const segmentInput = {
          ...clipStarted,
          segmentIndex,
          detectedAt: startAt,
          startAt,
          sessionIds: [refreshedStorageSession.id],
          notificationEligible: false,
          idempotencyKey: (segmentIndex === 1 ? "c" : "e").repeat(64),
        };
        await homecam.upsertHomecamEventClip(
          "living-room",
          "started",
          segmentInput,
        );
        segmentEvents.push(
          await homecam.upsertHomecamEventClip(
            "living-room",
            "ended",
            {
              ...segmentInput,
              endAt: new Date(
                Date.parse(clipStartAt) + segmentEnds[segmentIndex - 1],
              ).toISOString(),
              monotonicDurationMs: segmentDurations[segmentIndex - 1],
              idempotencyKey: (segmentIndex === 1 ? "d" : "f").repeat(64),
            },
          ),
        );
      }
      const groupedEvents = await homecam.listHomecamEvents({
        deviceId: "living-room",
        eventTypes: [],
        limit: 10,
      });
      const groupedClipEvents = groupedEvents.filter(
        (event) => event.eventGroupId === clipStarted.eventGroupId,
      );
      assert.equal(groupedClipEvents.length, 1);
      assert.equal(groupedClipEvents[0].id, startedClip.event.id);
      assert.equal(groupedClipEvents[0].segmentCount, 3);
      assert.equal(groupedClipEvents[0].clipStartAt, clipStartAt);
      assert.equal(
        groupedClipEvents[0].clipEndAt,
        new Date(Date.parse(clipStartAt) + 300_000).toISOString(),
      );
      assert.equal(groupedClipEvents[0].monotonicDurationMs, 300_000);
      const playbackInfo = await homecam.getEventClipPlayback(
        "living-room",
        endedClip.event.id,
      );
      assert.equal(playbackInfo?.streamArn, "arn:test:kvs:archive");
      assert.equal(playbackInfo?.event.segmentCount, 3);
      assert.equal(playbackInfo?.event.clipStartAt, clipStartAt);
      assert.equal(
        playbackInfo?.event.clipEndAt,
        new Date(Date.parse(clipStartAt) + 300_000).toISOString(),
      );
      assert.equal(
        await homecam.softDeleteHomecamEvent({
          deviceId: "living-room",
          eventId: endedClip.event.id,
          userEmail: "owner@example.com",
        }),
        true,
      );
      assert.equal(
        await homecam.getHomecamEvent(
          "living-room",
          segmentEvents[1].event.id,
        ),
        null,
      );
      assert.equal(
        await homecam.softDeleteHomecamEvent({
          deviceId: "living-room",
          eventId: endedClip.event.id,
          userEmail: "owner@example.com",
        }),
        false,
      );
      const eventsAfterDeletion = await homecam.listHomecamEvents({
        deviceId: "living-room",
        eventTypes: ["person"],
        limit: 10,
      });
      assert.equal(
        eventsAfterDeletion.some((event) => event.id === endedClip.event.id),
        false,
      );

      const rateLimitInput = {
        userEmail: "owner@example.com",
        roomCode: session.roomCode,
        scope: "repository-integration",
        limit: 2,
      };
      assert.equal(await petcam.consumeRequestRateLimit(rateLimitInput), true);
      assert.equal(await petcam.consumeRequestRateLimit(rateLimitInput), true);
      assert.equal(await petcam.consumeRequestRateLimit(rateLimitInput), false);
      const rateKey = `${rateLimitInput.scope}:${rateLimitInput.userEmail}:${rateLimitInput.roomCode}`;
      const currentWindow = await database.query(
        `SELECT window_started_at, request_count
         FROM request_rate_limits WHERE rate_key = $1`,
        [rateKey],
      );
      assert.ok(Number(currentWindow.rows[0].window_started_at) > 2_147_483_647);
      assert.equal(currentWindow.rows[0].request_count, 3);

      await database.query(
        `UPDATE request_rate_limits
         SET window_started_at = window_started_at - 60000, request_count = 99
         WHERE rate_key = $1`,
        [rateKey],
      );
      assert.equal(await petcam.consumeRequestRateLimit(rateLimitInput), true);
      const resetWindow = await database.query(
        `SELECT request_count FROM request_rate_limits WHERE rate_key = $1`,
        [rateKey],
      );
      assert.equal(resetWindow.rows[0].request_count, 1);

      assert.equal(
        await homecam.stopDeviceMediaSession(
          "living-room",
          "integration_test_complete",
          refreshedStorageSession.id,
        ),
        true,
      );
      assert.equal(
        (await homecam.getActiveMediaSession("living-room", "p2p"))?.id,
        p2pSession.id,
      );
      assert.equal(
        await homecam.getActiveMediaSession("living-room", "storage"),
        null,
      );
      assert.equal(
        await homecam.stopDeviceMediaSession(
          "living-room",
          "integration_test_complete",
          p2pSession.id,
        ),
        true,
      );
      assert.equal(await homecam.getActiveMediaSession("living-room"), null);
      const finalState = await homecam.getDeviceSettings("living-room");
      assert.equal(finalState.streamMode, "idle");
      assert.equal(finalState.activeSessionId, null);
      assert.equal(finalState.mediaHealthy, false);

      await robotMap.storeRobotState("living-room", {
        state: "ready",
        message: "저장된 지도를 사용하고 있습니다.",
        pose: { x: 1.25, y: -0.5, yaw: 0.75 },
        localization: { state: "ok", tfAgeS: 0.02 },
        nav2: { navigator: "active", runtime_mode: "navigation" },
        target: null,
        mapRevision: 8,
        observedAt: new Date().toISOString(),
      });
      await robotMap.storeRobotMap("living-room", {
        finalized: true,
        revision: "revision-1",
        mapId: "map-1",
        mapRevision: "map-revision-1",
        sourceCreatedAt: new Date().toISOString(),
        geometry: {
          width: 375,
          height: 224,
          resolution: 0.05,
          originX: -9.1,
          originY: -4.2,
          originYaw: 0,
        },
        previewBase64: "iVBORw0KGgo=",
        userMap: { rooms: [] },
        semanticZones: { type: "FeatureCollection", features: [] },
      });
      const snapshot = await robotMap.getRobotSnapshot(
        "living-room",
        "owner@example.com",
      );
      assert.equal(snapshot.online, true);
      assert.deepEqual(plain(snapshot.state.pose), { x: 1.25, y: -0.5, yaw: 0.75 });
      assert.deepEqual(plain(snapshot.state.driveMode), {
        mode: "idle", state: "idle", sessionId: null, message: null,
      });
      assert.equal(snapshot.map.revision, "revision-1");
      const semantics = await robotMap.getRobotMapSemantics(
        "living-room",
        "owner@example.com",
      );
      assert.deepEqual(plain(semantics.userMap), { rooms: [] });
      assert.deepEqual(plain(semantics.zones), {
        type: "FeatureCollection",
        features: [],
      });

      await robotMap.storeRobotMap("living-room", {
        finalized: false,
        revision: "live-candidate-1",
        mapId: "live-candidate-map",
        mapRevision: "live-candidate-revision",
        sourceCreatedAt: null,
        geometry: {
          width: 400,
          height: 240,
          resolution: 0.05,
          originX: -10,
          originY: -5,
          originYaw: 0,
        },
        previewBase64: "iVBORw0KGgo=",
        userMap: null,
        semanticZones: null,
      });
      await robotMap.storeRobotState("living-room", {
        state: "exploring",
        message: "새 지도를 만들고 있습니다.",
        pose: { x: 1.5, y: -0.25, yaw: 0.5 },
        localization: { state: "ok", tfAgeS: 0.02 },
        nav2: { navigator: "active", runtime_mode: "mapping" },
        target: null,
        mapRevision: 9,
        observedAt: new Date().toISOString(),
      });
      const mappingSnapshot = await robotMap.getRobotSnapshot(
        "living-room", "owner@example.com",
      );
      assert.equal(mappingSnapshot.map.revision, "live-candidate-1");
      assert.equal(mappingSnapshot.map.finalized, false);
      await robotMap.storeRobotState("living-room", {
        state: "canceled",
        message: "기존 지도를 유지합니다.",
        pose: { x: 1.5, y: -0.25, yaw: 0.5 },
        localization: { state: "ok", tfAgeS: 0.02 },
        nav2: { navigator: "active", runtime_mode: "navigation" },
        target: null,
        mapRevision: 10,
        observedAt: new Date().toISOString(),
      });
      const canceledSnapshot = await robotMap.getRobotSnapshot(
        "living-room", "owner@example.com",
      );
      assert.equal(canceledSnapshot.map.revision, "revision-1");
      assert.equal(canceledSnapshot.map.finalized, true);

      const queued = await robotMap.createRobotCommand({
        deviceId: "living-room",
        userEmail: "owner@example.com",
        operation: "finish",
      });
      assert.equal(queued.status, "queued");
      await assert.rejects(
        robotMap.createRobotCommand({
          deviceId: "living-room",
          userEmail: "owner@example.com",
          operation: "start",
        }),
        /COMMAND_IN_PROGRESS/,
      );
      const claimed = await robotMap.claimRobotCommands("living-room");
      assert.equal(claimed.length, 1);
      assert.equal(claimed[0].id, queued.id);
      assert.equal(claimed[0].status, "claimed");
      const completed = await robotMap.completeRobotCommand({
        deviceId: "living-room",
        commandId: queued.id,
        ok: true,
        result: { revision: "revision-1" },
      });
      assert.equal(completed.status, "completed");
      assert.deepEqual(plain(completed.result), { revision: "revision-1" });

      const previewCommand = await robotMap.createRobotCommand({
        deviceId: "living-room",
        userEmail: "owner@example.com",
        operation: "navigation_preview",
        payload: { x: 2.5, y: -1.25 },
      });
      const claimedPreview = await robotMap.claimRobotCommands("living-room");
      assert.equal(claimedPreview[0].id, previewCommand.id);
      assert.deepEqual(plain(claimedPreview[0].payload), { x: 2.5, y: -1.25 });
      await robotMap.completeRobotCommand({
        deviceId: "living-room",
        commandId: claimedPreview[0].id,
        ok: true,
        result: { previewToken: "preview_token_123" },
      });
      assert.deepEqual(
        plain(robotContract.parseRobotCommand({
          operation: "navigation_start",
          payload: { previewToken: "preview_token_123" },
        })),
        {
          operation: "navigation_start",
          payload: { previewToken: "preview_token_123" },
        },
      );
      assert.equal(
        robotContract.parseRobotCommand({
          operation: "navigation_preview",
          payload: { x: Number.NaN, y: 0 },
        }),
        null,
      );
      assert.deepEqual(
        plain(robotContract.parseRobotCommand({
          operation: "drive_mode_start",
          payload: { mode: "patrol" },
        })),
        { operation: "drive_mode_start", payload: { mode: "patrol" } },
      );
      assert.deepEqual(
        plain(robotContract.parseRobotCommand({
          operation: "drive_mode_pause",
          payload: { mode: "roaming", sessionId: "roaming_session_1" },
        })),
        {
          operation: "drive_mode_pause",
          payload: { mode: "roaming", sessionId: "roaming_session_1" },
        },
      );
      assert.equal(robotContract.parseRobotCommand({
        operation: "drive_mode_start",
        payload: { mode: "destination" },
      }), null);

      await assert.rejects(
        robotMap.createRobotCommand({
          deviceId: "living-room",
          userEmail: "family@example.com",
          operation: "drive_mode_start",
          payload: { mode: "patrol" },
        }),
        /FORBIDDEN/,
      );
      const modeCommand = await robotMap.createRobotCommand({
        deviceId: "living-room",
        userEmail: "owner@example.com",
        operation: "drive_mode_start",
        payload: { mode: "patrol" },
      });
      const claimedMode = await robotMap.claimRobotCommands("living-room");
      assert.equal(claimedMode[0].id, modeCommand.id);
      await robotMap.completeRobotCommand({
        deviceId: "living-room",
        commandId: modeCommand.id,
        ok: true,
        result: { sessionId: "patrol_session_1" },
      });
      await robotMap.storeRobotState("living-room", {
        state: "ready",
        message: "순찰 중입니다.",
        pose: { x: 1.5, y: -0.25, yaw: 0.5 },
        localization: { state: "ok", tfAgeS: 0.02 },
        nav2: { navigator: "active", runtime_mode: "navigation" },
        target: null,
        driveMode: {
          mode: "patrol", state: "active",
          sessionId: "patrol_session_1", message: "순찰 중입니다.",
        },
        mapRevision: 11,
        observedAt: new Date().toISOString(),
      });
      await assert.rejects(
        robotMap.createRobotCommand({
          deviceId: "living-room",
          userEmail: "owner@example.com",
          operation: "navigation_preview",
          payload: { x: 1, y: 1 },
        }),
        /DRIVE_MODE_IN_PROGRESS/,
      );
      await assert.rejects(
        robotMap.createRobotCommand({
          deviceId: "living-room",
          userEmail: "owner@example.com",
          operation: "drive_mode_stop",
          payload: { mode: "patrol", sessionId: "stale_session_1" },
        }),
        /DRIVE_MODE_SESSION_MISMATCH/,
      );
      const room = (id, minimumX = 0) => ({
        type: "Feature",
        id,
        properties: { role: "room", room_id: id, name: id },
        geometry: {
          type: "Polygon",
          coordinates: [[
            [minimumX, 0], [minimumX + 2, 0], [minimumX + 2, 2],
            [minimumX, 2], [minimumX, 0],
          ]],
        },
      });
      assert.ok(robotContract.parseRobotCommand({
        operation: "room_split",
        payload: {
          room: room("room-a"),
          lines: [[[1, 0], [1, 2]], [[0, 1], [2, 1]]],
          resolution: 0.05,
          minimum_room_area: 1,
        },
      }));
      assert.equal(robotContract.parseRobotCommand({
        operation: "room_split",
        payload: { room: room("room-a"), lines: [[[1, 0]]], resolution: 0.05 },
      }), null);
      assert.ok(robotContract.parseRobotCommand({
        operation: "room_merge",
        payload: { rooms: [room("room-a"), room("room-b", 2)], resolution: 0.05 },
      }));
      assert.equal(robotContract.parseRobotCommand({
        operation: "room_merge",
        payload: { rooms: [room("room-a"), room("room-b", 2), room("room-c", 4)] },
      }), null);
      assert.equal(robotContract.parseRobotCommand({
        operation: "room_merge",
        payload: { rooms: [room("room-a"), room("room-a")] },
      }), null);
    });

    const persisted = await database.query(`
      SELECT
        (SELECT COUNT(*)::int FROM device_credentials) AS credentials,
        (SELECT COUNT(*)::int FROM homecam_events) AS events,
        (SELECT COUNT(*)::int FROM homecam_push_outbox) AS outbox,
        (SELECT status FROM stream_sessions LIMIT 1) AS session_status,
        (SELECT ended_at IS NOT NULL FROM recording_sessions LIMIT 1) AS recording_ended
    `);
    assert.deepEqual(plain(persisted.rows[0]), {
      credentials: 1,
      events: 4,
      outbox: 1,
      session_status: "ended",
      recording_ended: true,
    });
  } finally {
    await database.close();
  }
});

test("test pool injection is isolated across concurrent async contexts", async () => {
  const first = new PGlite();
  const second = new PGlite();
  try {
    await first.exec("CREATE TABLE context_value (value TEXT); INSERT INTO context_value VALUES ('first')");
    await second.exec("CREATE TABLE context_value (value TEXT); INSERT INTO context_value VALUES ('second')");
    const loadModule = createTypescriptModuleLoader();
    const postgres = loadModule(path.join(projectDirectory, "db/postgres.ts"));

    const [firstValue, secondValue] = await Promise.all([
      postgres.withPostgresPoolForTest(pglitePoolAdapter(first), async () => {
        await new Promise((resolve) => setTimeout(resolve, 5));
        const result = await postgres.getPostgresPool().query(
          "SELECT value FROM context_value",
        );
        return result.rows[0].value;
      }),
      postgres.withPostgresPoolForTest(pglitePoolAdapter(second), async () => {
        await Promise.resolve();
        const result = await postgres.getPostgresPool().query(
          "SELECT value FROM context_value",
        );
        return result.rows[0].value;
      }),
    ]);
    assert.deepEqual([firstValue, secondValue], ["first", "second"]);
  } finally {
    await Promise.all([first.close(), second.close()]);
  }
});

async function seedDevice(database) {
  const createdAt = new Date().toISOString();
  await database.query(
    `INSERT INTO devices (id, display_name, kvs_channel_arn, created_at)
     VALUES ($1, $2, $3, $4)`,
    ["living-room", "거실 홈캠", "arn:test:kvs:p2p", createdAt],
  );
  await database.query(
    `INSERT INTO device_memberships (device_id, user_email, role, created_at)
     VALUES ($1, $2, 'owner', $3), ($1, 'family@example.com', 'family', $3)`,
    ["living-room", "owner@example.com", createdAt],
  );
  await database.query(
    `INSERT INTO device_state
       (device_id, monitoring_enabled, camera_enabled, microphone_enabled,
        source_profile, active_stream_mode, media_healthy, detector_healthy,
        updated_at)
     VALUES ($1, 1, 1, 1, 'sim', 'idle', 0, 0, $2)`,
    ["living-room", createdAt],
  );
}

function pglitePoolAdapter(database) {
  const query = async (sql, values = []) => {
    const result = await database.query(sql, values);
    const rows = result.rows.map(normalizeRow);
    return {
      ...result,
      rows,
      rowCount: result.affectedRows || rows.length,
      command: sql.trim().split(/\s+/, 1)[0]?.toUpperCase() ?? "",
      oid: 0,
    };
  };
  return {
    query,
    async connect() {
      return { query, release() {} };
    },
  };
}

function normalizeRow(row) {
  return Object.fromEntries(
    Object.entries(row).map(([key, value]) => [
      key,
      value instanceof Date ? value.toISOString() : value,
    ]),
  );
}

function createTypescriptModuleLoader() {
  const cache = new Map();
  const loadModule = (filename) => {
    const resolved = resolveTypescriptModule(filename);
    if (cache.has(resolved)) return cache.get(resolved).exports;

    const source = requireFromTest("node:fs").readFileSync(resolved, "utf8");
    const javascript = ts.transpileModule(source, {
      // The test loader executes everything as CommonJS. TypeScript preserves
      // ESM syntax when the input filename ends in .mjs, so use a virtual .ts
      // filename for shared ESM helpers while retaining the real path for
      // dependency resolution and diagnostics.
      fileName: resolved.endsWith(".mjs") ? `${resolved}.ts` : resolved,
      compilerOptions: {
        esModuleInterop: true,
        module: ts.ModuleKind.CommonJS,
        target: ts.ScriptTarget.ES2022,
      },
    }).outputText;
    const loadedModule = { exports: {} };
    cache.set(resolved, loadedModule);
    const localRequire = (specifier) => {
      if (specifier.startsWith(".")) {
        return loadModule(path.resolve(path.dirname(resolved), specifier));
      }
      return requireFromTest(specifier);
    };
    const execute = new Function(
      "require",
      "module",
      "exports",
      "__filename",
      "__dirname",
      javascript,
    );
    execute(
      localRequire,
      loadedModule,
      loadedModule.exports,
      resolved,
      path.dirname(resolved),
    );
    return loadedModule.exports;
  };
  return loadModule;
}

function resolveTypescriptModule(candidate) {
  const fs = requireFromTest("node:fs");
  for (const resolved of [candidate, `${candidate}.ts`, path.join(candidate, "index.ts")]) {
    if (fs.existsSync(resolved) && fs.statSync(resolved).isFile()) return resolved;
  }
  throw new Error(`Cannot resolve TypeScript module: ${candidate}`);
}

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}
