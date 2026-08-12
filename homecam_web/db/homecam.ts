import { getD1 } from ".";
import { ensurePetcamSchema } from "./petcam";
import {
  createDeviceToken,
  hashDeviceToken,
  isCredentialActive,
  parseDeviceToken,
} from "./homecam-security";
import {
  canManageHomecam,
  canViewHomecam,
  type DeviceSettingsPatch,
  type HomecamEventInput,
} from "./homecam-validation";
import { recordingPlaybackPosition } from "../app/recording-segments";
import { ensureDatabaseSchema } from "./migration-state";

const HEARTBEAT_ONLINE_MS = 30_000;
const MEDIA_SESSION_TTL_MS = 10 * 60 * 1000;
const EVENT_RETENTION_MS = 7 * 24 * 60 * 60 * 1000;
const AUDIT_RETENTION_MS = 30 * 24 * 60 * 60 * 1000;
const TALK_LEASE_MS = 15_000;
const ROOM_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";

export type DeviceIdentity = {
  credentialId: string;
  deviceId: string;
  displayName: string;
  legacyChannelArn: string;
};

export type HomecamStreamMode = "idle" | "p2p" | "storage";

type StateRow = {
  monitoring_enabled: number;
  camera_enabled: number;
  microphone_enabled: number;
  source_profile: string;
  image_topic: string | null;
  active_stream_mode: string;
  active_session_id: string | null;
  media_healthy: number;
  detector_healthy: number;
  last_seen_at: string | null;
  updated_at: string;
};

export async function ensureHomecamSchema() {
  await ensurePetcamSchema();
  await ensureDatabaseSchema();
}

export async function getMembershipRole(deviceId: string, userEmail: string) {
  await ensureHomecamSchema();
  const row = await getD1()
    .prepare(
      "SELECT role FROM device_memberships WHERE device_id = ? AND user_email = ?",
    )
    .bind(deviceId, userEmail)
    .first<{ role: string }>();
  return row?.role ?? null;
}

export async function userCanViewDevice(deviceId: string, userEmail: string) {
  return canViewHomecam(await getMembershipRole(deviceId, userEmail));
}

export async function userCanManageDevice(deviceId: string, userEmail: string) {
  return canManageHomecam(await getMembershipRole(deviceId, userEmail));
}

export async function listHomecamDevices(userEmail: string) {
  await ensureHomecamSchema();
  await cleanupExpiredHomecamData();
  const d1 = getD1();
  await d1
    .prepare(
      `INSERT INTO device_state (device_id)
       SELECT device_id FROM device_memberships WHERE user_email = ?
       ON CONFLICT(device_id) DO NOTHING`,
    )
    .bind(userEmail)
    .run();
  await expireMediaSessions();

  const result = await d1
    .prepare(
      `SELECT
         devices.id,
         devices.display_name,
         device_memberships.role,
         device_state.monitoring_enabled,
         device_state.camera_enabled,
         device_state.microphone_enabled,
         device_state.source_profile,
         device_state.image_topic,
         device_state.active_stream_mode,
         device_state.media_healthy,
         device_state.detector_healthy,
         device_state.last_seen_at,
         device_state.updated_at,
         stream_sessions.id AS session_id,
         stream_sessions.room_code,
         stream_sessions.started_at,
         stream_sessions.expires_at
       FROM device_memberships
       INNER JOIN devices ON devices.id = device_memberships.device_id
       INNER JOIN device_state ON device_state.device_id = devices.id
       LEFT JOIN stream_sessions
         ON stream_sessions.id = device_state.active_session_id
        AND stream_sessions.status = 'active'
        AND stream_sessions.expires_at > ?
       WHERE device_memberships.user_email = ?
         AND device_memberships.role IN ('owner', 'family', 'broadcaster')
       ORDER BY devices.created_at ASC`,
    )
    .bind(new Date().toISOString(), userEmail)
    .all<{
      id: string;
      display_name: string;
      role: string;
      monitoring_enabled: number;
      camera_enabled: number;
      microphone_enabled: number;
      source_profile: string;
      image_topic: string | null;
      active_stream_mode: string;
      media_healthy: number;
      detector_healthy: number;
      last_seen_at: string | null;
      updated_at: string;
      session_id: string | null;
      room_code: string | null;
      started_at: string | null;
      expires_at: string | null;
    }>();

  const onlineCutoff = Date.now() - HEARTBEAT_ONLINE_MS;
  return result.results.map((row: {
    id: string;
    display_name: string;
    role: string;
    monitoring_enabled: number;
    camera_enabled: number;
    microphone_enabled: number;
    source_profile: string;
    image_topic: string | null;
    active_stream_mode: string;
    media_healthy: number;
    detector_healthy: number;
    last_seen_at: string | null;
    updated_at: string;
    session_id: string | null;
    room_code: string | null;
    started_at: string | null;
    expires_at: string | null;
  }) => ({
    id: row.id,
    displayName: row.display_name,
    role: row.role,
    online: Boolean(
      row.last_seen_at && Date.parse(row.last_seen_at) > onlineCutoff,
    ),
    settings: {
      monitoringEnabled: Boolean(row.monitoring_enabled),
      cameraEnabled: Boolean(row.camera_enabled),
      microphoneEnabled: Boolean(row.microphone_enabled),
    },
    status: {
      sourceProfile: row.source_profile,
      imageTopic: row.image_topic,
      streamMode: normalizeStreamMode(row.active_stream_mode),
      mediaHealthy: Boolean(row.media_healthy),
      detectorHealthy: Boolean(row.detector_healthy),
      lastSeenAt: row.last_seen_at,
      updatedAt: row.updated_at,
    },
    activeSession:
      row.session_id && row.room_code && row.started_at && row.expires_at
        ? {
            id: row.session_id,
            roomCode: row.room_code,
            storageMode: row.active_stream_mode === "storage",
            startedAt: row.started_at,
            expiresAt: row.expires_at,
          }
        : null,
  }));
}

export async function getDeviceSettings(deviceId: string) {
  await ensureDeviceState(deviceId);
  const row = await getD1()
    .prepare(
      `SELECT monitoring_enabled, camera_enabled, microphone_enabled,
              source_profile, image_topic, active_stream_mode, active_session_id,
              media_healthy, detector_healthy, last_seen_at, updated_at
       FROM device_state WHERE device_id = ?`,
    )
    .bind(deviceId)
    .first<StateRow>();
  if (!row) throw new Error("DEVICE_NOT_FOUND");
  return mapState(row);
}

export async function updateDeviceSettings(input: {
  deviceId: string;
  userEmail: string;
  patch: DeviceSettingsPatch;
}) {
  await getDeviceSettings(input.deviceId);
  if (
    input.patch.monitoringEnabled === true &&
    input.patch.cameraEnabled === false
  ) {
    throw new Error("CAMERA_DISABLED");
  }

  const nowIso = new Date().toISOString();
  const cameraPatch =
    input.patch.cameraEnabled === undefined
      ? null
      : input.patch.cameraEnabled
        ? 1
        : 0;
  const monitoringPatch =
    input.patch.monitoringEnabled === undefined
      ? null
      : input.patch.monitoringEnabled
        ? 1
        : 0;
  const microphonePatch =
    input.patch.microphoneEnabled === undefined
      ? null
      : input.patch.microphoneEnabled
        ? 1
        : 0;
  const mediaModeMayChange =
    input.patch.cameraEnabled !== undefined ||
    input.patch.monitoringEnabled !== undefined;
  const result = await getD1()
    .prepare(
      `UPDATE device_state
       SET monitoring_enabled = CASE
             WHEN ? = 0 THEN 0
             WHEN CAST(? AS INTEGER) IS NULL THEN monitoring_enabled
             ELSE ?
           END,
           camera_enabled = CASE
             WHEN CAST(? AS INTEGER) IS NULL THEN camera_enabled ELSE ?
           END,
           microphone_enabled =
             CASE WHEN CAST(? AS INTEGER) IS NULL THEN microphone_enabled ELSE ? END,
           media_healthy = CASE WHEN ? = 1 THEN 0 ELSE media_healthy END,
           updated_at = ?
       WHERE device_id = ?
         AND NOT (? = 1 AND COALESCE(?, camera_enabled) = 0)`,
    )
    .bind(
      cameraPatch,
      monitoringPatch,
      monitoringPatch,
      cameraPatch,
      cameraPatch,
      microphonePatch,
      microphonePatch,
      mediaModeMayChange ? 1 : 0,
      nowIso,
      input.deviceId,
      monitoringPatch,
      cameraPatch,
    )
    .run();
  if (result.meta.changes === 0) {
    throw new Error("CAMERA_DISABLED");
  }
  const updated = await getDeviceSettings(input.deviceId);
  const activeSession = mediaModeMayChange
    ? await getActiveMediaSession(input.deviceId)
    : null;
  if (
    activeSession &&
    (!updated.cameraEnabled ||
      activeSession.mode !==
        (updated.monitoringEnabled ? "storage" : "p2p"))
  ) {
    await stopDeviceMediaSession(
      input.deviceId,
      "settings_changed",
      activeSession.id,
    );
  }
  await writeAuditLog({
    deviceId: input.deviceId,
    actorType: "user",
    actorId: input.userEmail,
    action: "settings.update",
    metadata: {
      monitoringEnabled: updated.monitoringEnabled,
      cameraEnabled: updated.cameraEnabled,
      microphoneEnabled: updated.microphoneEnabled,
    },
  });
  return getDeviceSettings(input.deviceId);
}

export async function createDeviceCredential(input: {
  deviceId: string;
  userEmail: string;
  label: string;
  expiresAt?: string | null;
}) {
  await ensureHomecamSchema();
  const generated = createDeviceToken();
  const digest = await hashDeviceToken(generated.token);
  const nowIso = new Date().toISOString();
  await getD1()
    .prepare(
      `INSERT INTO device_credentials
       (id, device_id, label, token_digest, created_at, expires_at)
       VALUES (?, ?, ?, ?, ?, ?)`,
    )
    .bind(
      generated.credentialId,
      input.deviceId,
      input.label,
      digest,
      nowIso,
      input.expiresAt ?? null,
    )
    .run();
  await writeAuditLog({
    deviceId: input.deviceId,
    actorType: "user",
    actorId: input.userEmail,
    action: "credential.create",
    metadata: { credentialId: generated.credentialId, label: input.label },
  });
  return {
    id: generated.credentialId,
    deviceId: input.deviceId,
    label: input.label,
    token: generated.token,
    createdAt: nowIso,
    expiresAt: input.expiresAt ?? null,
  };
}

export async function listDeviceCredentials(deviceId: string) {
  await ensureHomecamSchema();
  const result = await getD1()
    .prepare(
      `SELECT id, label, created_at, last_used_at, expires_at, revoked_at
       FROM device_credentials WHERE device_id = ? ORDER BY created_at DESC`,
    )
    .bind(deviceId)
    .all<{
      id: string;
      label: string;
      created_at: string;
      last_used_at: string | null;
      expires_at: string | null;
      revoked_at: string | null;
    }>();
  return result.results.map((row: {
    id: string;
    label: string;
    created_at: string;
    last_used_at: string | null;
    expires_at: string | null;
    revoked_at: string | null;
  }) => ({
    id: row.id,
    label: row.label,
    createdAt: row.created_at,
    lastUsedAt: row.last_used_at,
    expiresAt: row.expires_at,
    revokedAt: row.revoked_at,
  }));
}

export async function revokeDeviceCredential(input: {
  deviceId: string;
  credentialId: string;
  userEmail: string;
}) {
  await ensureHomecamSchema();
  const nowIso = new Date().toISOString();
  const result = await getD1()
    .prepare(
      `UPDATE device_credentials SET revoked_at = COALESCE(revoked_at, ?)
       WHERE id = ? AND device_id = ?`,
    )
    .bind(nowIso, input.credentialId, input.deviceId)
    .run();
  if (result.meta.changes > 0) {
    await writeAuditLog({
      deviceId: input.deviceId,
      actorType: "user",
      actorId: input.userEmail,
      action: "credential.revoke",
      metadata: { credentialId: input.credentialId },
    });
  }
  return result.meta.changes > 0;
}

export async function authenticateDeviceToken(
  token: string,
): Promise<DeviceIdentity | null> {
  await ensureHomecamSchema();
  const parsed = parseDeviceToken(token);
  if (!parsed) return null;
  const digest = await hashDeviceToken(token);
  const row = await getD1()
    .prepare(
      `SELECT
         device_credentials.id,
         device_credentials.device_id,
         device_credentials.token_digest,
         device_credentials.expires_at,
         device_credentials.revoked_at,
         devices.display_name,
         devices.kvs_channel_arn
       FROM device_credentials
       INNER JOIN devices ON devices.id = device_credentials.device_id
       WHERE device_credentials.id = ? AND device_credentials.token_digest = ?`,
    )
    .bind(parsed.credentialId, digest)
    .first<{
      id: string;
      device_id: string;
      token_digest: string;
      expires_at: string | null;
      revoked_at: string | null;
      display_name: string;
      kvs_channel_arn: string;
    }>();
  if (
    !row ||
    !isCredentialActive({
      expiresAt: row.expires_at,
      revokedAt: row.revoked_at,
    })
  ) {
    return null;
  }
  await getD1()
    .prepare("UPDATE device_credentials SET last_used_at = ? WHERE id = ?")
    .bind(new Date().toISOString(), row.id)
    .run();
  return {
    credentialId: row.id,
    deviceId: row.device_id,
    displayName: row.display_name,
    legacyChannelArn: row.kvs_channel_arn,
  };
}

export async function updateDeviceHeartbeat(input: {
  deviceId: string;
  sourceProfile?: "sim" | "aurora" | "unknown";
  imageTopic?: string | null;
  streamMode?: HomecamStreamMode;
  mediaHealthy?: boolean;
  detectorHealthy?: boolean;
}) {
  await ensureDeviceState(input.deviceId);
  const current = await getDeviceSettings(input.deviceId);
  const activeSession = await getActiveMediaSession(input.deviceId);
  // A provisioned P2P/storage session can legitimately be idle while the
  // master waits for a viewer or the storage service's offer. Session
  // lifecycle is therefore controlled by the authenticated session endpoint
  // (DELETE), settings changes, and expiry—not inferred from one heartbeat.
  let activeSessionExpiresAt = activeSession?.expiresAt ?? null;
  if (
    activeSession &&
    input.streamMode === activeSession.mode &&
    input.mediaHealthy === true
  ) {
    const nextExpiresAt = new Date(
      Date.now() + MEDIA_SESSION_TTL_MS,
    ).toISOString();
    const result = await getD1()
      .prepare(
        `UPDATE stream_sessions SET expires_at = ?
         WHERE id = ? AND status = 'active'`,
      )
      .bind(nextExpiresAt, activeSession.id)
      .run();
    if (result.meta.changes > 0) {
      activeSessionExpiresAt = nextExpiresAt;
    }
  }
  const refreshed = await getDeviceSettings(input.deviceId);
  const activeSessionId = activeSession?.id ?? null;
  const reportedMode =
    activeSession && input.streamMode !== "idle"
      ? activeSession.mode
      : refreshed.streamMode;
  const mediaHealthy =
    Boolean(activeSession) &&
    refreshed.cameraEnabled &&
    input.streamMode === activeSession?.mode &&
    input.mediaHealthy === true;
  const detectorHealthy =
    refreshed.cameraEnabled &&
    refreshed.monitoringEnabled &&
    (input.detectorHealthy ?? current.detectorHealthy);
  const nowIso = new Date().toISOString();
  await getD1()
    .prepare(
      `UPDATE device_state SET
         source_profile = ?,
         image_topic = ?,
         active_stream_mode = CASE
           WHEN active_session_id IS NULL THEN 'idle'
           WHEN active_session_id = ? THEN ?
           ELSE active_stream_mode
         END,
         media_healthy = CASE
           WHEN active_session_id = ? THEN ? ELSE 0
         END,
         detector_healthy = ?,
         last_seen_at = ?,
         updated_at = ?
       WHERE device_id = ?`,
    )
    .bind(
      input.sourceProfile ?? refreshed.sourceProfile,
      input.imageTopic === undefined ? refreshed.imageTopic : input.imageTopic,
      activeSessionId,
      reportedMode,
      activeSessionId,
      mediaHealthy ? 1 : 0,
      detectorHealthy ? 1 : 0,
      nowIso,
      nowIso,
      input.deviceId,
    )
    .run();
  if (
    activeSession?.mode === "storage" &&
    mediaHealthy
  ) {
    await getD1()
      .prepare(
        `UPDATE recording_sessions
         SET started_at = COALESCE(started_at, ?)
         WHERE session_id = ? AND EXISTS (
           SELECT 1 FROM device_state
           WHERE device_id = ?
             AND monitoring_enabled = 1
             AND camera_enabled = 1
             AND media_healthy = 1
             AND active_stream_mode = 'storage'
             AND active_session_id = ?
         )`,
      )
      .bind(nowIso, activeSession.id, input.deviceId, activeSession.id)
      .run();
  }
  const state = await getDeviceSettings(input.deviceId);
  return {
    ...state,
    // Reuse the session snapshot already read for the heartbeat. This avoids
    // another global expiry sweep and SELECT in the 2-second polling route,
    // while rejecting a stale snapshot after a concurrent stop/replacement.
    activeSession:
      activeSession &&
      activeSessionExpiresAt &&
      state.activeSessionId === activeSession.id &&
      state.streamMode === activeSession.mode &&
      Date.parse(activeSessionExpiresAt) > Date.now()
      ? { ...activeSession, expiresAt: activeSessionExpiresAt }
      : null,
  };
}

export async function prepareDeviceMediaSession(input: {
  deviceId: string;
  mode: Exclude<HomecamStreamMode, "idle">;
  channelArn: string;
  streamArn?: string;
}) {
  await ensureDeviceState(input.deviceId);
  if (input.mode === "storage" && !input.streamArn) {
    throw new Error("STORAGE_NOT_CONFIGURED");
  }
  await expireMediaSessions();
  const d1 = getD1();
  const now = new Date();
  const nowIso = now.toISOString();
  const expiresAt = new Date(now.getTime() + MEDIA_SESSION_TTL_MS).toISOString();
  const current = await getActiveMediaSession(input.deviceId);

  const currentStorageMatches =
    current?.mode !== "storage" ||
    (current.channelArn === input.channelArn &&
      current.streamArn === input.streamArn);
  if (
    current &&
    current.mode === input.mode &&
    currentStorageMatches
  ) {
    await d1
      .prepare(
        `UPDATE stream_sessions SET expires_at = ?
         WHERE id = ? AND status = 'active'`,
      )
      .bind(expiresAt, current.id)
      .run();
    return { ...current, expiresAt };
  }
  if (current) {
    await stopDeviceMediaSession(
      input.deviceId,
      current.mode === input.mode ? "channel_changed" : "mode_changed",
      current.id,
    );
  }

  for (let attempt = 0; attempt < 4; attempt += 1) {
    const sessionId = crypto.randomUUID();
    const roomCode = createRoomCode();
    const statements = [
      d1
        .prepare(
          `UPDATE recording_sessions SET ended_at = COALESCE(ended_at, ?)
           WHERE session_id IN (
             SELECT id FROM stream_sessions
             WHERE device_id = ? AND status = 'active'
           )`,
        )
        .bind(nowIso, input.deviceId),
      d1
        .prepare(
          `UPDATE stream_sessions SET status = 'ended', ended_at = ?
           WHERE device_id = ? AND status = 'active'`,
        )
        .bind(nowIso, input.deviceId),
      d1
        .prepare(
          `INSERT INTO stream_sessions
           (id, room_code, device_id, started_by, status, started_at, expires_at)
           VALUES (?, ?, ?, ?, 'active', ?, ?)`,
        )
        .bind(
          sessionId,
          roomCode,
          input.deviceId,
          `device:${input.deviceId}`,
          nowIso,
          expiresAt,
        ),
    ];
    if (input.mode === "storage" && input.streamArn) {
      statements.push(
        d1
          .prepare(
          `INSERT INTO recording_sessions
             (session_id, kvs_stream_arn, kvs_channel_arn, started_at)
             VALUES (?, ?, ?, ?)`,
          )
          .bind(sessionId, input.streamArn, input.channelArn, null),
      );
    }
    statements.push(
      d1
        .prepare(
          `UPDATE device_state
           SET active_stream_mode = ?, active_session_id = ?, media_healthy = 0,
               updated_at = ?
           WHERE device_id = ?`,
        )
        .bind(input.mode, sessionId, nowIso, input.deviceId),
    );

    try {
      await d1.batch(statements);
      return {
        id: sessionId,
        roomCode,
        mode: input.mode,
        startedAt: nowIso,
        expiresAt,
      };
    } catch (error) {
      if (!String(error).includes("UNIQUE")) throw error;
    }
  }
  throw new Error("ROOM_CODE_EXHAUSTED");
}

export async function stopDeviceMediaSession(
  deviceId: string,
  reason = "device_stop",
  expectedSessionId?: string,
) {
  await ensureHomecamSchema();
  const d1 = getD1();
  const nowIso = new Date().toISOString();
  const state = await d1
    .prepare("SELECT active_session_id FROM device_state WHERE device_id = ?")
    .bind(deviceId)
    .first<{ active_session_id: string | null }>();
  if (!state?.active_session_id) return false;
  if (
    expectedSessionId !== undefined &&
    state.active_session_id !== expectedSessionId
  ) {
    return false;
  }
  const sessionId = state.active_session_id;
  const results = await d1.batch([
    d1
      .prepare(
        "UPDATE recording_sessions SET ended_at = COALESCE(ended_at, ?) WHERE session_id = ?",
      )
      .bind(nowIso, sessionId),
    d1
      .prepare(
        `UPDATE stream_sessions SET status = 'ended', ended_at = ?
         WHERE id = ? AND status = 'active'`,
      )
      .bind(nowIso, sessionId),
    d1
      .prepare(
        `UPDATE device_state
         SET active_stream_mode = 'idle', active_session_id = NULL,
             media_healthy = 0, updated_at = ?
         WHERE device_id = ? AND active_session_id = ?`,
      )
      .bind(nowIso, deviceId, sessionId),
  ]);
  if (results[1].meta.changes > 0) {
    await writeAuditLog({
      deviceId,
      actorType: "device",
      actorId: deviceId,
      action: "session.stop",
      metadata: { reason },
    });
  }
  return results[1].meta.changes > 0;
}

export async function getActiveMediaSession(deviceId: string) {
  await ensureHomecamSchema();
  await expireMediaSessions();
  const row = await getD1()
    .prepare(
      `SELECT stream_sessions.id, stream_sessions.room_code,
              stream_sessions.started_at, stream_sessions.expires_at,
              device_state.active_stream_mode,
              recording_sessions.kvs_channel_arn,
              recording_sessions.kvs_stream_arn,
              recording_sessions.started_at AS recording_started_at
       FROM device_state
       INNER JOIN stream_sessions ON stream_sessions.id = device_state.active_session_id
       LEFT JOIN recording_sessions
         ON recording_sessions.session_id = stream_sessions.id
       WHERE device_state.device_id = ?
         AND stream_sessions.status = 'active'
         AND stream_sessions.expires_at > ?`,
    )
    .bind(deviceId, new Date().toISOString())
    .first<{
      id: string;
      room_code: string;
      started_at: string;
      expires_at: string;
      active_stream_mode: string;
      kvs_channel_arn: string | null;
      kvs_stream_arn: string | null;
      recording_started_at: string | null;
    }>();
  if (!row) return null;
  const mode = normalizeStreamMode(row.active_stream_mode);
  if (mode === "idle") return null;
  return {
    id: row.id,
    roomCode: row.room_code,
    startedAt: row.started_at,
    expiresAt: row.expires_at,
    mode,
    channelArn: row.kvs_channel_arn,
    streamArn: row.kvs_stream_arn,
    recordingStartedAt: row.recording_started_at,
  };
}

export async function insertHomecamEvent(
  deviceId: string,
  event: HomecamEventInput,
) {
  await ensureHomecamSchema();
  await cleanupExpiredHomecamData();
  const requestFingerprint = await eventRequestFingerprint(event);
  const duplicate = await getD1()
    .prepare(
      `SELECT id, event_type, confidence, occurred_at, received_at,
              request_fingerprint, recording_session_id, recording_offset_ms
       FROM homecam_events WHERE device_id = ? AND idempotency_key = ?`,
    )
    .bind(deviceId, event.idempotencyKey)
    .first<EventRowWithFingerprint>();
  if (duplicate) {
    if (duplicate.request_fingerprint !== requestFingerprint) {
      throw new Error("IDEMPOTENCY_CONFLICT");
    }
    return { created: false, event: mapEvent(duplicate) };
  }
  const state = await getDeviceSettings(deviceId);
  if (!state.monitoringEnabled || !state.cameraEnabled) {
    throw new Error("MONITORING_DISABLED");
  }
  const session = await getActiveMediaSession(deviceId);
  if (
    !session ||
    session.mode !== "storage" ||
    !session.recordingStartedAt
  ) {
    throw new Error("STORAGE_NOT_ACTIVE");
  }
  const authoritativeOffsetMs =
    Date.parse(event.occurredAt) - Date.parse(session.recordingStartedAt);
  if (
    authoritativeOffsetMs < -5_000 ||
    (event.recordingOffsetMs !== null &&
      Math.abs(event.recordingOffsetMs - Math.max(0, authoritativeOffsetMs)) >
        5_000)
  ) {
    throw new Error("EVENT_OUTSIDE_RECORDING");
  }
  const recordingOffsetMs = Math.max(0, authoritativeOffsetMs);

  const id = crypto.randomUUID();
  const result = await getD1()
    .prepare(
      `INSERT INTO homecam_events
       (id, device_id, event_type, confidence, occurred_at, idempotency_key,
        request_fingerprint, recording_session_id, recording_offset_ms)
       SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?
       FROM device_state
       WHERE device_id = ?
         AND monitoring_enabled = 1
         AND camera_enabled = 1
         AND media_healthy = 1
         AND active_stream_mode = 'storage'
         AND active_session_id = ?
         AND EXISTS (
           SELECT 1 FROM stream_sessions
           WHERE id = ? AND status = 'active' AND expires_at > ?
         )
       ON CONFLICT(device_id, idempotency_key) DO NOTHING`,
    )
    .bind(
      id,
      deviceId,
      event.eventType,
      event.confidence,
      event.occurredAt,
      event.idempotencyKey,
      requestFingerprint,
      session.id,
      recordingOffsetMs,
      deviceId,
      session.id,
      session.id,
      new Date().toISOString(),
    )
    .run();
  const stored = await getD1()
    .prepare(
      `SELECT id, event_type, confidence, occurred_at, received_at,
              request_fingerprint, recording_session_id, recording_offset_ms
       FROM homecam_events WHERE device_id = ? AND idempotency_key = ?`,
    )
    .bind(deviceId, event.idempotencyKey)
    .first<EventRowWithFingerprint>();
  if (!stored) {
    const latestState = await getDeviceSettings(deviceId);
    if (!latestState.monitoringEnabled || !latestState.cameraEnabled) {
      throw new Error("MONITORING_DISABLED");
    }
    throw new Error("STORAGE_NOT_ACTIVE");
  }
  if (stored.request_fingerprint !== requestFingerprint) {
    throw new Error("IDEMPOTENCY_CONFLICT");
  }
  return {
    created: result.meta.changes > 0,
    event: mapEvent(stored),
  };
}

type EventRowWithFingerprint = {
  id: string;
  event_type: string;
  confidence: number | null;
  occurred_at: string;
  received_at: string;
  request_fingerprint: string;
  recording_session_id: string | null;
  recording_offset_ms: number | null;
};

async function eventRequestFingerprint(event: HomecamEventInput) {
  const canonical = JSON.stringify({
    eventType: event.eventType,
    confidence: event.confidence,
    occurredAt: event.occurredAt,
    recordingOffsetMs: event.recordingOffsetMs,
  });
  const digest = new Uint8Array(
    await crypto.subtle.digest(
      "SHA-256",
      new TextEncoder().encode(canonical),
    ),
  );
  return Array.from(digest, (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

export async function listHomecamEvents(input: {
  deviceId: string;
  eventTypes: string[];
  before?: { occurredAt: string; id: string };
  limit: number;
}) {
  await ensureHomecamSchema();
  await cleanupExpiredHomecamData();
  const placeholders = input.eventTypes.map(() => "?").join(",");
  const typeClause = placeholders ? `AND event_type IN (${placeholders})` : "";
  const beforeClause = input.before
    ? "AND (occurred_at < ? OR (occurred_at = ? AND id < ?))"
    : "";
  const bindings: unknown[] = [
    input.deviceId,
    new Date(Date.now() - EVENT_RETENTION_MS).toISOString(),
    ...input.eventTypes,
  ];
  if (input.before) {
    bindings.push(
      input.before.occurredAt,
      input.before.occurredAt,
      input.before.id,
    );
  }
  bindings.push(input.limit);
  const result = await getD1()
    .prepare(
      `SELECT id, event_type, confidence, occurred_at, received_at,
              recording_session_id, recording_offset_ms
       FROM homecam_events
       WHERE device_id = ? AND occurred_at >= ?
       ${typeClause} ${beforeClause}
       ORDER BY occurred_at DESC, id DESC LIMIT ?`,
    )
    .bind(...bindings)
    .all<{
      id: string;
      event_type: string;
      confidence: number | null;
      occurred_at: string;
      received_at: string;
      recording_session_id: string | null;
      recording_offset_ms: number | null;
    }>();
  return result.results.map(mapEvent);
}

export async function getHomecamEvent(deviceId: string, eventId: string) {
  await ensureHomecamSchema();
  await cleanupExpiredHomecamData();
  const row = await getD1()
    .prepare(
      `SELECT id, event_type, confidence, occurred_at, received_at,
              recording_session_id, recording_offset_ms
       FROM homecam_events
       WHERE id = ? AND device_id = ? AND occurred_at >= ?`,
    )
    .bind(
      eventId,
      deviceId,
      new Date(Date.now() - EVENT_RETENTION_MS).toISOString(),
    )
    .first<{
      id: string;
      event_type: string;
      confidence: number | null;
      occurred_at: string;
      received_at: string;
      recording_session_id: string | null;
      recording_offset_ms: number | null;
    }>();
  return row ? mapEvent(row) : null;
}

export async function claimPendingHomecamPushes(input: {
  deviceId: string;
  preferredEventId?: string;
  limit?: number;
}) {
  await ensureHomecamSchema();
  const d1 = getD1();
  const nowIso = new Date().toISOString();
  const leaseUntil = new Date(Date.now() + 30_000).toISOString();
  const limit = Math.max(1, Math.min(input.limit ?? 2, 5));
  const candidates = await d1
    .prepare(
      `SELECT event_id FROM homecam_push_outbox
       WHERE device_id = ? AND delivered_at IS NULL AND next_attempt_at <= ?
       ORDER BY CASE WHEN event_id = ? THEN 0 ELSE 1 END, created_at ASC
       LIMIT ?`,
    )
    .bind(
      input.deviceId,
      nowIso,
      input.preferredEventId ?? "",
      limit,
    )
    .all<{ event_id: string }>();
  const claimed: ReturnType<typeof mapEvent>[] = [];
  for (const candidate of candidates.results) {
    const claim = await d1
      .prepare(
        `UPDATE homecam_push_outbox
         SET attempt_count = attempt_count + 1,
             next_attempt_at = ?,
             last_error = NULL
         WHERE event_id = ? AND delivered_at IS NULL AND next_attempt_at <= ?
         RETURNING event_id`,
      )
      .bind(leaseUntil, candidate.event_id, nowIso)
      .first<{ event_id: string }>();
    if (!claim) continue;
    const event = await d1
      .prepare(
        `SELECT id, event_type, confidence, occurred_at, received_at,
                recording_session_id, recording_offset_ms
         FROM homecam_events WHERE id = ? AND device_id = ?`,
      )
      .bind(claim.event_id, input.deviceId)
      .first<{
        id: string;
        event_type: string;
        confidence: number | null;
        occurred_at: string;
        received_at: string;
        recording_session_id: string | null;
        recording_offset_ms: number | null;
      }>();
    if (event) claimed.push(mapEvent(event));
  }
  return claimed;
}

export async function finishHomecamPushAttempt(input: {
  eventId: string;
  delivered: boolean;
  error?: string;
}) {
  await ensureHomecamSchema();
  const nowIso = new Date().toISOString();
  const result = await getD1()
    .prepare(
      `UPDATE homecam_push_outbox
       SET delivered_at = CASE WHEN ? = 1 THEN ? ELSE delivered_at END,
           next_attempt_at = CASE WHEN ? = 1 THEN next_attempt_at ELSE ? END,
           last_error = ?
       WHERE event_id = ? AND delivered_at IS NULL`,
    )
    .bind(
      input.delivered ? 1 : 0,
      nowIso,
      input.delivered ? 1 : 0,
      nowIso,
      input.delivered ? null : (input.error ?? "push_failed").slice(0, 255),
      input.eventId,
    )
    .run();
  return result.meta.changes > 0;
}

export async function listDevicesWithPendingHomecamPushes(limit = 20) {
  await ensureHomecamSchema();
  const result = await getD1()
    .prepare(
      `SELECT device_id, MIN(created_at) AS oldest_created_at
       FROM homecam_push_outbox
       WHERE delivered_at IS NULL AND next_attempt_at <= ?
       GROUP BY device_id
       ORDER BY oldest_created_at ASC
       LIMIT ?`,
    )
    .bind(new Date().toISOString(), Math.max(1, Math.min(limit, 100)))
    .all<{ device_id: string; oldest_created_at: string }>();
  return result.results.map((row) => row.device_id);
}

export async function runHomecamRetentionCleanup() {
  await ensureHomecamSchema();
  await expireMediaSessions();
  await cleanupExpiredHomecamData();
}

export async function listFamilyMembers(deviceId: string) {
  await ensureHomecamSchema();
  const result = await getD1()
    .prepare(
      `SELECT user_email, role, created_at FROM device_memberships
       WHERE device_id = ? AND role IN ('owner', 'family')
       ORDER BY CASE role WHEN 'owner' THEN 0 ELSE 1 END, created_at ASC`,
    )
    .bind(deviceId)
    .all<{ user_email: string; role: string; created_at: string }>();
  return result.results.map((row: {
    user_email: string;
    role: string;
    created_at: string;
  }) => ({
    email: row.user_email,
    role: row.role,
    createdAt: row.created_at,
  }));
}

export async function inviteFamilyMember(input: {
  deviceId: string;
  ownerEmail: string;
  familyEmail: string;
}) {
  await ensureHomecamSchema();
  const existing = await getMembershipRole(input.deviceId, input.familyEmail);
  if (existing === "owner") throw new Error("MEMBER_IS_OWNER");
  const createdAt = new Date().toISOString();
  await getD1()
    .prepare(
      `INSERT INTO device_memberships (device_id, user_email, role, created_at)
       VALUES (?, ?, 'family', ?)
       ON CONFLICT(device_id, user_email) DO UPDATE SET role = 'family'`,
    )
    .bind(input.deviceId, input.familyEmail, createdAt)
    .run();
  await writeAuditLog({
    deviceId: input.deviceId,
    actorType: "user",
    actorId: input.ownerEmail,
    action: "family.invite",
    metadata: { userEmail: input.familyEmail },
  });
  return { email: input.familyEmail, role: "family" as const, createdAt };
}

export async function revokeFamilyMember(input: {
  deviceId: string;
  ownerEmail: string;
  familyEmail: string;
}) {
  await ensureHomecamSchema();
  const d1 = getD1();
  const result = await d1
    .prepare(
      `DELETE FROM device_memberships
       WHERE device_id = ? AND user_email = ? AND role = 'family'`,
    )
    .bind(input.deviceId, input.familyEmail)
    .run();
  if (result.meta.changes > 0) {
    await d1
      .prepare(
        `UPDATE push_subscriptions SET revoked_at = ?
         WHERE device_id = ? AND user_email = ? AND revoked_at IS NULL`,
      )
      .bind(new Date().toISOString(), input.deviceId, input.familyEmail)
      .run();
    await writeAuditLog({
      deviceId: input.deviceId,
      actorType: "user",
      actorId: input.ownerEmail,
      action: "family.revoke",
      metadata: { userEmail: input.familyEmail },
    });
  }
  return result.meta.changes > 0;
}

export async function listPushSubscriptions(userEmail: string) {
  await ensureHomecamSchema();
  const result = await getD1()
    .prepare(
      `SELECT id, device_id, endpoint, created_at, updated_at
       FROM push_subscriptions
       WHERE user_email = ? AND revoked_at IS NULL
       ORDER BY created_at DESC`,
    )
    .bind(userEmail)
    .all<{
      id: string;
      device_id: string;
      endpoint: string;
      created_at: string;
      updated_at: string;
    }>();
  return result.results.map((row: {
    id: string;
    device_id: string;
    endpoint: string;
    created_at: string;
    updated_at: string;
  }) => ({
    id: row.id,
    deviceId: row.device_id,
    endpoint: row.endpoint,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
  }));
}

export async function upsertPushSubscription(input: {
  deviceId: string;
  userEmail: string;
  endpoint: string;
  p256dh: string;
  auth: string;
}) {
  await ensureHomecamSchema();
  const d1 = getD1();
  const existing = await d1
    .prepare(
      `SELECT id FROM push_subscriptions
       WHERE device_id = ? AND user_email = ? AND endpoint = ?`,
    )
    .bind(input.deviceId, input.userEmail, input.endpoint)
    .first<{ id: string }>();
  const id = existing?.id ?? crypto.randomUUID();
  const nowIso = new Date().toISOString();
  await d1
    .prepare(
      `INSERT INTO push_subscriptions
       (id, device_id, user_email, endpoint, p256dh, auth, created_at, updated_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
       ON CONFLICT(user_email, device_id, endpoint) DO UPDATE SET
         p256dh = excluded.p256dh,
         auth = excluded.auth,
         updated_at = excluded.updated_at,
         revoked_at = NULL`,
    )
    .bind(
      id,
      input.deviceId,
      input.userEmail,
      input.endpoint,
      input.p256dh,
      input.auth,
      nowIso,
      nowIso,
    )
    .run();
  return { id, deviceId: input.deviceId, endpoint: input.endpoint, updatedAt: nowIso };
}

export async function revokePushSubscription(
  userEmail: string,
  subscriptionId: string,
) {
  await ensureHomecamSchema();
  const result = await getD1()
    .prepare(
      `UPDATE push_subscriptions SET revoked_at = ?
       WHERE id = ? AND user_email = ? AND revoked_at IS NULL`,
    )
    .bind(new Date().toISOString(), subscriptionId, userEmail)
    .run();
  return result.meta.changes > 0;
}

export async function listActivePushTargets(deviceId: string) {
  await ensureHomecamSchema();
  const result = await getD1()
    .prepare(
      `SELECT push_subscriptions.id, push_subscriptions.endpoint,
              push_subscriptions.p256dh, push_subscriptions.auth,
              devices.display_name
       FROM push_subscriptions
       INNER JOIN device_memberships
         ON device_memberships.device_id = push_subscriptions.device_id
        AND device_memberships.user_email = push_subscriptions.user_email
        AND device_memberships.role IN ('owner', 'family', 'broadcaster')
       INNER JOIN devices ON devices.id = push_subscriptions.device_id
       WHERE push_subscriptions.device_id = ?
         AND push_subscriptions.revoked_at IS NULL`,
    )
    .bind(deviceId)
    .all<{
      id: string;
      endpoint: string;
      p256dh: string;
      auth: string;
      display_name: string;
    }>();
  return result.results.map((row: {
    id: string;
    endpoint: string;
    p256dh: string;
    auth: string;
    display_name: string;
  }) => ({
    id: row.id,
    endpoint: row.endpoint,
    keys: { p256dh: row.p256dh, auth: row.auth },
    displayName: row.display_name,
  }));
}

export async function revokePushSubscriptionsById(ids: string[]) {
  await ensureHomecamSchema();
  if (ids.length === 0) return 0;
  const placeholders = ids.map(() => "?").join(",");
  const result = await getD1()
    .prepare(
      `UPDATE push_subscriptions SET revoked_at = ?
       WHERE id IN (${placeholders}) AND revoked_at IS NULL`,
    )
    .bind(new Date().toISOString(), ...ids)
    .run();
  return result.meta.changes;
}

export async function acquireTalkLease(input: {
  deviceId: string;
  userEmail: string;
  clientId: string;
  existingLeaseId?: string;
}) {
  await ensureHomecamSchema();
  const d1 = getD1();
  const now = new Date();
  const nowIso = now.toISOString();
  const expiresAt = new Date(now.getTime() + TALK_LEASE_MS).toISOString();
  const proposedLeaseId = crypto.randomUUID();
  const lease = await d1
    .prepare(
      `INSERT INTO talk_leases
       (device_id, lease_id, user_email, client_id, expires_at, created_at, updated_at)
       VALUES (?, ?, ?, ?, ?, ?, ?)
       ON CONFLICT(device_id) DO UPDATE SET
         lease_id = CASE
           WHEN talk_leases.expires_at <= ? THEN excluded.lease_id
           ELSE talk_leases.lease_id
         END,
         user_email = CASE
           WHEN talk_leases.expires_at <= ? THEN excluded.user_email
           ELSE talk_leases.user_email
         END,
         client_id = CASE
           WHEN talk_leases.expires_at <= ? THEN excluded.client_id
           ELSE talk_leases.client_id
         END,
         expires_at = excluded.expires_at,
         updated_at = excluded.updated_at
       WHERE talk_leases.expires_at <= ?
          OR (
            talk_leases.user_email = excluded.user_email
            AND talk_leases.client_id = excluded.client_id
            AND CAST(? AS TEXT) IS NOT NULL
            AND talk_leases.lease_id = ?
          )
       RETURNING lease_id, expires_at`,
    )
    .bind(
      input.deviceId,
      proposedLeaseId,
      input.userEmail,
      input.clientId,
      expiresAt,
      nowIso,
      nowIso,
      nowIso,
      nowIso,
      nowIso,
      nowIso,
      input.existingLeaseId ?? null,
      input.existingLeaseId ?? null,
    )
    .first<{ lease_id: string; expires_at: string }>();
  if (!lease) return null;
  await writeAuditLog({
    deviceId: input.deviceId,
    actorType: "user",
    actorId: input.userEmail,
    action: "talk.acquire",
    metadata: { leaseId: lease.lease_id, clientId: input.clientId },
  });
  return { leaseId: lease.lease_id, expiresAt: lease.expires_at };
}

export async function releaseTalkLease(input: {
  deviceId: string;
  userEmail: string;
  leaseId: string;
  clientId: string;
}) {
  await ensureHomecamSchema();
  const result = await getD1()
    .prepare(
      `DELETE FROM talk_leases
       WHERE device_id = ? AND user_email = ? AND lease_id = ? AND client_id = ?`,
    )
    .bind(input.deviceId, input.userEmail, input.leaseId, input.clientId)
    .run();
  if (result.meta.changes > 0) {
    await writeAuditLog({
      deviceId: input.deviceId,
      actorType: "user",
      actorId: input.userEmail,
      action: "talk.release",
      metadata: { leaseId: input.leaseId, clientId: input.clientId },
    });
  }
  return result.meta.changes > 0;
}

export async function writeAuditLog(input: {
  deviceId: string;
  actorType: "user" | "device" | "system";
  actorId: string;
  action: string;
  metadata?: Record<string, unknown>;
}) {
  await ensureHomecamSchema();
  const d1 = getD1();
  const nowIso = new Date().toISOString();
  await d1.batch([
    d1
      .prepare(
        `INSERT INTO access_audit_log
         (id, device_id, actor_type, actor_id, action, metadata_json, created_at)
         VALUES (?, ?, ?, ?, ?, ?, ?)`,
      )
      .bind(
        crypto.randomUUID(),
        input.deviceId,
        input.actorType,
        input.actorId,
        input.action,
        input.metadata ? JSON.stringify(input.metadata).slice(0, 4_096) : null,
        nowIso,
      ),
    d1
      .prepare("DELETE FROM access_audit_log WHERE created_at < ?")
      .bind(new Date(Date.now() - AUDIT_RETENTION_MS).toISOString()),
  ]);
}

async function ensureDeviceState(deviceId: string) {
  await ensureHomecamSchema();
  await getD1()
    .prepare(
      `INSERT INTO device_state (device_id)
       SELECT id FROM devices WHERE id = ?
       ON CONFLICT(device_id) DO NOTHING`,
    )
    .bind(deviceId)
    .run();
}

async function expireMediaSessions() {
  await ensureHomecamSchema();
  const d1 = getD1();
  const nowIso = new Date().toISOString();
  await d1.batch([
    d1
      .prepare(
        `UPDATE recording_sessions SET ended_at = COALESCE(ended_at, ?)
         WHERE session_id IN (
           SELECT id FROM stream_sessions
           WHERE status = 'active' AND expires_at <= ?
         )`,
      )
      .bind(nowIso, nowIso),
    d1
      .prepare(
        `UPDATE stream_sessions SET status = 'expired', ended_at = expires_at
         WHERE status = 'active' AND expires_at <= ?`,
      )
      .bind(nowIso),
    d1.prepare(
      `UPDATE device_state
       SET active_stream_mode = 'idle', active_session_id = NULL,
           media_healthy = 0, updated_at = ?
       WHERE active_session_id IS NOT NULL AND active_session_id IN (
         SELECT id FROM stream_sessions WHERE status != 'active'
       )`,
    ).bind(nowIso),
  ]);
}

async function cleanupExpiredHomecamData() {
  const d1 = getD1();
  await d1.batch([
    d1
      .prepare("DELETE FROM homecam_events WHERE occurred_at < ?")
      .bind(new Date(Date.now() - EVENT_RETENTION_MS).toISOString()),
    d1
      .prepare("DELETE FROM access_audit_log WHERE created_at < ?")
      .bind(new Date(Date.now() - AUDIT_RETENTION_MS).toISOString()),
  ]);
}

function mapState(row: StateRow) {
  return {
    monitoringEnabled: Boolean(row.monitoring_enabled),
    cameraEnabled: Boolean(row.camera_enabled),
    microphoneEnabled: Boolean(row.microphone_enabled),
    sourceProfile: row.source_profile,
    imageTopic: row.image_topic,
    streamMode: normalizeStreamMode(row.active_stream_mode),
    activeSessionId: row.active_session_id,
    mediaHealthy: Boolean(row.media_healthy),
    detectorHealthy: Boolean(row.detector_healthy),
    lastSeenAt: row.last_seen_at,
    updatedAt: row.updated_at,
  };
}

function mapEvent(row: {
  id: string;
  event_type: string;
  confidence: number | null;
  occurred_at: string;
  received_at: string;
  recording_session_id: string | null;
  recording_offset_ms: number | null;
}) {
  const playback = recordingPlaybackPosition(row.recording_offset_ms);
  return {
    id: row.id,
    eventType: row.event_type,
    confidence: row.confidence,
    occurredAt: row.occurred_at,
    receivedAt: row.received_at,
    recordingId: row.recording_session_id,
    recordingOffsetMs: row.recording_offset_ms,
    ...playback,
  };
}

function normalizeStreamMode(value: string): HomecamStreamMode {
  return value === "p2p" || value === "storage" ? value : "idle";
}

function createRoomCode() {
  const bytes = crypto.getRandomValues(new Uint8Array(6));
  return Array.from(bytes, (value) => ROOM_ALPHABET[value % ROOM_ALPHABET.length]).join(
    "",
  );
}
