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
  type HomecamEventClipInput,
  type HomecamEventInput,
} from "./homecam-validation";
import { recordingPlaybackPosition } from "../app/recording-segments";
import { ensureDatabaseSchema } from "./migration-state";

const HEARTBEAT_ONLINE_MS = 30_000;
// KVS storage sessions have a one-hour service boundary. A one-hour backend
// lease lets the device perform its own 50-minute soft refresh and 55-minute
// hard cutover instead of replacing both signaling credentials every five
// minutes. Heartbeats still extend a healthy session as a sliding lease.
const MEDIA_SESSION_TTL_MS = 60 * 60 * 1000;
const EVENT_RETENTION_MS = 7 * 24 * 60 * 60 * 1000;
const AUDIT_RETENTION_MS = 30 * 24 * 60 * 60 * 1000;
const TALK_LEASE_MS = 15_000;
const ROOM_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
const EVENT_CLIP_INCOMPLETE_MS = 3 * 60 * 1000;

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
  p2p_session_id: string | null;
  storage_session_id: string | null;
  p2p_healthy: number;
  storage_healthy: number;
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
         device_state.p2p_session_id,
         device_state.storage_session_id,
         device_state.p2p_healthy,
         device_state.storage_healthy,
         device_state.detector_healthy,
         device_state.last_seen_at,
         device_state.updated_at,
         legacy_session.id AS session_id,
         legacy_session.room_code,
         legacy_session.started_at,
         legacy_session.expires_at,
         p2p_session.room_code AS p2p_room_code,
         p2p_session.started_at AS p2p_started_at,
         p2p_session.expires_at AS p2p_expires_at,
         storage_session.room_code AS storage_room_code,
         storage_session.started_at AS storage_started_at,
         storage_session.expires_at AS storage_expires_at
       FROM device_memberships
       INNER JOIN devices ON devices.id = device_memberships.device_id
       INNER JOIN device_state ON device_state.device_id = devices.id
       LEFT JOIN stream_sessions AS legacy_session
         ON legacy_session.id = device_state.active_session_id
        AND legacy_session.status = 'active'
        AND legacy_session.expires_at > ?
       LEFT JOIN stream_sessions AS p2p_session
         ON p2p_session.id = device_state.p2p_session_id
        AND p2p_session.status = 'active'
        AND p2p_session.expires_at > ?
       LEFT JOIN stream_sessions AS storage_session
         ON storage_session.id = device_state.storage_session_id
        AND storage_session.status = 'active'
        AND storage_session.expires_at > ?
       WHERE device_memberships.user_email = ?
         AND device_memberships.role IN ('owner', 'family', 'broadcaster')
       ORDER BY devices.created_at ASC`,
    )
    .bind(
      new Date().toISOString(),
      new Date().toISOString(),
      new Date().toISOString(),
      userEmail,
    )
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
      p2p_session_id: string | null;
      storage_session_id: string | null;
      p2p_healthy: number;
      storage_healthy: number;
      detector_healthy: number;
      last_seen_at: string | null;
      updated_at: string;
      session_id: string | null;
      room_code: string | null;
      started_at: string | null;
      expires_at: string | null;
      p2p_room_code: string | null;
      p2p_started_at: string | null;
      p2p_expires_at: string | null;
      storage_room_code: string | null;
      storage_started_at: string | null;
      storage_expires_at: string | null;
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
    p2p_session_id: string | null;
    storage_session_id: string | null;
    p2p_healthy: number;
    storage_healthy: number;
    detector_healthy: number;
    last_seen_at: string | null;
    updated_at: string;
    session_id: string | null;
    room_code: string | null;
    started_at: string | null;
    expires_at: string | null;
    p2p_room_code: string | null;
    p2p_started_at: string | null;
    p2p_expires_at: string | null;
    storage_room_code: string | null;
    storage_started_at: string | null;
    storage_expires_at: string | null;
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
      p2pHealthy: Boolean(row.p2p_healthy),
      storageHealthy: Boolean(row.storage_healthy),
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
    activeSessions: {
      p2p:
        row.p2p_session_id &&
        row.p2p_room_code &&
        row.p2p_started_at &&
        row.p2p_expires_at
          ? {
              id: row.p2p_session_id,
              roomCode: row.p2p_room_code,
              mode: "p2p" as const,
              startedAt: row.p2p_started_at,
              expiresAt: row.p2p_expires_at,
            }
          : null,
      storage:
        row.storage_session_id &&
        row.storage_room_code &&
        row.storage_started_at &&
        row.storage_expires_at
          ? {
              id: row.storage_session_id,
              roomCode: row.storage_room_code,
              mode: "storage" as const,
              startedAt: row.storage_started_at,
              expiresAt: row.storage_expires_at,
            }
          : null,
    },
  }));
}

export async function getDeviceSettings(deviceId: string) {
  await ensureDeviceState(deviceId);
  const row = await getD1()
    .prepare(
      `SELECT monitoring_enabled, camera_enabled, microphone_enabled,
              source_profile, image_topic, active_stream_mode, active_session_id,
              media_healthy, p2p_session_id, storage_session_id,
              p2p_healthy, storage_healthy, detector_healthy,
              last_seen_at, updated_at
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
           p2p_healthy = CASE WHEN CAST(? AS INTEGER) IS NULL
             THEN p2p_healthy ELSE 0 END,
           storage_healthy = CASE WHEN ? = 1
             THEN 0 ELSE storage_healthy END,
           updated_at = ?
       WHERE device_id = ?
         AND NOT (COALESCE(?, 0) = 1 AND COALESCE(?, camera_enabled) = 0)`,
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
      cameraPatch,
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
  if (mediaModeMayChange) {
    const [p2pSession, storageSession] = await Promise.all([
      getActiveMediaSession(input.deviceId, "p2p"),
      getActiveMediaSession(input.deviceId, "storage"),
    ]);
    if (!updated.cameraEnabled && p2pSession) {
      await stopDeviceMediaSession(
        input.deviceId,
        "camera_disabled",
        p2pSession.id,
      );
    }
    if ((!updated.cameraEnabled || !updated.monitoringEnabled) && storageSession) {
      await stopDeviceMediaSession(
        input.deviceId,
        updated.cameraEnabled ? "storage_disabled" : "camera_disabled",
        storageSession.id,
      );
    }
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
  p2pHealthy?: boolean;
  storageHealthy?: boolean;
  detectorHealthy?: boolean;
}) {
  await ensureDeviceState(input.deviceId);
  const current = await getDeviceSettings(input.deviceId);
  const [p2pSession, storageSession] = await Promise.all([
    getActiveMediaSession(input.deviceId, "p2p"),
    getActiveMediaSession(input.deviceId, "storage"),
  ]);
  // A provisioned P2P/storage session can legitimately be idle while the
  // master waits for a viewer or the storage service's offer. Session
  // lifecycle is therefore controlled by the authenticated session endpoint
  // (DELETE), settings changes, and expiry—not inferred from one heartbeat.
  const reportedP2pHealthy =
    input.p2pHealthy ??
    (input.streamMode === "p2p" ? input.mediaHealthy : undefined);
  const reportedStorageHealthy =
    input.storageHealthy ??
    (input.streamMode === "storage" ? input.mediaHealthy : undefined);
  let p2pExpiresAt = p2pSession?.expiresAt ?? null;
  let storageExpiresAt = storageSession?.expiresAt ?? null;
  for (const [session, healthy] of [
    [p2pSession, reportedP2pHealthy],
    [storageSession, reportedStorageHealthy],
  ] as const) {
    if (!session || healthy !== true) continue;
    const nextExpiresAt = new Date(
      Date.now() + MEDIA_SESSION_TTL_MS,
    ).toISOString();
    const result = await getD1()
      .prepare(
        `UPDATE stream_sessions SET expires_at = ?
         WHERE id = ? AND status = 'active'`,
      )
      .bind(nextExpiresAt, session.id)
      .run();
    if (result.meta.changes > 0) {
      if (session.mode === "p2p") p2pExpiresAt = nextExpiresAt;
      else storageExpiresAt = nextExpiresAt;
    }
  }
  const refreshed = await getDeviceSettings(input.deviceId);
  const p2pHealthy =
    Boolean(p2pSession) &&
    refreshed.cameraEnabled &&
    reportedP2pHealthy === true;
  const storageHealthy =
    Boolean(storageSession) &&
    refreshed.cameraEnabled &&
    refreshed.monitoringEnabled &&
    reportedStorageHealthy === true;
  const legacySession = storageSession ?? p2pSession;
  const legacyHealthy = storageSession ? storageHealthy : p2pHealthy;
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
         p2p_session_id = ?, storage_session_id = ?,
         p2p_healthy = ?, storage_healthy = ?,
         active_stream_mode = ?, active_session_id = ?, media_healthy = ?,
         detector_healthy = ?,
         last_seen_at = ?,
         updated_at = ?
       WHERE device_id = ?`,
    )
    .bind(
      input.sourceProfile ?? refreshed.sourceProfile,
      input.imageTopic === undefined ? refreshed.imageTopic : input.imageTopic,
      p2pSession?.id ?? null,
      storageSession?.id ?? null,
      p2pHealthy ? 1 : 0,
      storageHealthy ? 1 : 0,
      legacySession?.mode ?? "idle",
      legacySession?.id ?? null,
      legacyHealthy ? 1 : 0,
      detectorHealthy ? 1 : 0,
      nowIso,
      nowIso,
      input.deviceId,
    )
    .run();
  if (
    storageSession && storageHealthy
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
             AND storage_healthy = 1
             AND storage_session_id = ?
         )`,
      )
      .bind(nowIso, storageSession.id, input.deviceId, storageSession.id)
      .run();
  }
  const state = await getDeviceSettings(input.deviceId);
  const activeSessions = {
    p2p:
      p2pSession && p2pExpiresAt && state.p2pSessionId === p2pSession.id
        ? { ...p2pSession, expiresAt: p2pExpiresAt }
        : null,
    storage:
      storageSession &&
      storageExpiresAt &&
      state.storageSessionId === storageSession.id
        ? { ...storageSession, expiresAt: storageExpiresAt }
        : null,
  };
  return {
    ...state,
    // Reuse the session snapshot already read for the heartbeat. This avoids
    // another global expiry sweep and SELECT in the 2-second polling route,
    // while rejecting a stale snapshot after a concurrent stop/replacement.
    activeSession: activeSessions.storage ?? activeSessions.p2p,
    activeSessions,
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
  const current = await getActiveMediaSession(input.deviceId, input.mode);

  const currentStorageMatches =
    current?.mode !== "storage" ||
    (current.channelArn === input.channelArn &&
      current.streamArn === input.streamArn);
  if (
    current &&
    current.mode === input.mode &&
    input.mode === "p2p" &&
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
  if (current && !currentStorageMatches) {
    await stopDeviceMediaSession(
      input.deviceId,
      "channel_changed",
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
             WHERE device_id = ? AND mode = ? AND status = 'active'
           )`,
        )
        .bind(nowIso, input.deviceId, input.mode),
      d1
        .prepare(
          `UPDATE stream_sessions SET status = 'ended', ended_at = ?
           WHERE device_id = ? AND mode = ? AND status = 'active'`,
        )
        .bind(nowIso, input.deviceId, input.mode),
      d1
        .prepare(
          `INSERT INTO stream_sessions
           (id, room_code, device_id, started_by, mode, status, started_at, expires_at)
           VALUES (?, ?, ?, ?, ?, 'active', ?, ?)`,
        )
        .bind(
          sessionId,
          roomCode,
          input.deviceId,
          `device:${input.deviceId}`,
          input.mode,
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
           SET p2p_session_id = CASE WHEN ? = 'p2p' THEN ? ELSE p2p_session_id END,
               storage_session_id = CASE WHEN ? = 'storage' THEN ? ELSE storage_session_id END,
               p2p_healthy = CASE WHEN ? = 'p2p' THEN 0 ELSE p2p_healthy END,
               storage_healthy = CASE WHEN ? = 'storage' THEN 0 ELSE storage_healthy END,
               active_stream_mode = CASE
                 WHEN ? = 'storage' THEN 'storage'
                 WHEN storage_session_id IS NULL THEN 'p2p'
                 ELSE active_stream_mode
               END,
               active_session_id = CASE
                 WHEN ? = 'storage' THEN ?
                 WHEN storage_session_id IS NULL THEN ?
                 ELSE active_session_id
               END,
               media_healthy = CASE
                 WHEN ? = 'storage' OR storage_session_id IS NULL THEN 0
                 ELSE storage_healthy
               END,
               updated_at = ?
           WHERE device_id = ?`,
        )
        .bind(
          input.mode,
          sessionId,
          input.mode,
          sessionId,
          input.mode,
          input.mode,
          input.mode,
          input.mode,
          sessionId,
          sessionId,
          input.mode,
          nowIso,
          input.deviceId,
        ),
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
  const session = await d1
    .prepare(
      `SELECT stream_sessions.id, stream_sessions.mode
       FROM stream_sessions
       INNER JOIN device_state ON device_state.device_id = stream_sessions.device_id
       WHERE stream_sessions.device_id = ?
         AND stream_sessions.status = 'active'
         AND stream_sessions.id = COALESCE(?, device_state.active_session_id)`,
    )
    .bind(deviceId, expectedSessionId ?? null)
    .first<{ id: string; mode: string }>();
  if (!session || (session.mode !== "p2p" && session.mode !== "storage")) {
    return false;
  }
  const sessionId = session.id;
  const mode = session.mode as "p2p" | "storage";
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
         SET p2p_session_id = CASE WHEN ? = 'p2p' THEN NULL ELSE p2p_session_id END,
             storage_session_id = CASE WHEN ? = 'storage' THEN NULL ELSE storage_session_id END,
             p2p_healthy = CASE WHEN ? = 'p2p' THEN 0 ELSE p2p_healthy END,
             storage_healthy = CASE WHEN ? = 'storage' THEN 0 ELSE storage_healthy END,
             active_stream_mode = CASE
               WHEN ? = 'storage' THEN CASE WHEN p2p_session_id IS NULL THEN 'idle' ELSE 'p2p' END
               ELSE CASE WHEN storage_session_id IS NULL THEN 'idle' ELSE 'storage' END
             END,
             active_session_id = CASE
               WHEN ? = 'storage' THEN p2p_session_id ELSE storage_session_id
             END,
             media_healthy = CASE
               WHEN ? = 'storage' THEN p2p_healthy ELSE storage_healthy
             END,
             updated_at = ?
         WHERE device_id = ?
           AND ((? = 'p2p' AND p2p_session_id = ?)
             OR (? = 'storage' AND storage_session_id = ?))`,
      )
      .bind(
        mode,
        mode,
        mode,
        mode,
        mode,
        mode,
        mode,
        nowIso,
        deviceId,
        mode,
        sessionId,
        mode,
        sessionId,
      ),
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

export async function getActiveMediaSession(
  deviceId: string,
  requestedMode?: Exclude<HomecamStreamMode, "idle">,
) {
  await ensureHomecamSchema();
  await expireMediaSessions();
  const row = await getD1()
    .prepare(
      `SELECT stream_sessions.id, stream_sessions.room_code,
              stream_sessions.started_at, stream_sessions.expires_at,
              stream_sessions.mode,
              recording_sessions.kvs_channel_arn,
              recording_sessions.kvs_stream_arn,
              recording_sessions.started_at AS recording_started_at
       FROM stream_sessions
       LEFT JOIN recording_sessions
         ON recording_sessions.session_id = stream_sessions.id
       WHERE stream_sessions.device_id = ?
         AND stream_sessions.status = 'active'
         AND stream_sessions.expires_at > ?
         AND (CAST(? AS TEXT) IS NULL OR stream_sessions.mode = ?)
       ORDER BY CASE WHEN stream_sessions.mode = 'storage' THEN 0 ELSE 1 END
       LIMIT 1`,
    )
    .bind(
      deviceId,
      new Date().toISOString(),
      requestedMode ?? null,
      requestedMode ?? null,
    )
    .first<{
      id: string;
      room_code: string;
      started_at: string;
      expires_at: string;
      mode: string;
      kvs_channel_arn: string | null;
      kvs_stream_arn: string | null;
      recording_started_at: string | null;
    }>();
  if (!row) return null;
  const mode = normalizeStreamMode(row.mode);
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
  const session = await getActiveMediaSession(deviceId, "storage");
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
         AND storage_healthy = 1
         AND storage_session_id = ?
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

export async function upsertHomecamEventClip(
  deviceId: string,
  phase: "started" | "ended",
  event: HomecamEventClipInput,
) {
  await ensureHomecamSchema();
  await cleanupExpiredHomecamData();
  const state = await getDeviceSettings(deviceId);
  if (!state.monitoringEnabled || !state.cameraEnabled) {
    throw new Error("MONITORING_DISABLED");
  }

  const sessions = await getEventClipSessions(deviceId, event.sessionIds);
  if (sessions.length !== event.sessionIds.length) {
    throw new Error("EVENT_SESSION_INVALID");
  }
  const primarySession = sessions.find(
    (session) => session.session_id === event.sessionIds[0],
  );
  if (!primarySession) throw new Error("EVENT_SESSION_INVALID");
  const recordingStartedAt =
    primarySession.recording_started_at ?? primarySession.session_started_at;
  const rangeEnd = event.endAt ?? event.detectedAt;
  if (
    sessions.some(
      (session) =>
        Date.parse(session.session_started_at) > Date.parse(rangeEnd) + 5_000 ||
        (session.recording_ended_at !== null &&
          Date.parse(session.recording_ended_at) < Date.parse(event.startAt) - 5_000),
    )
  ) {
    throw new Error("EVENT_OUTSIDE_RECORDING");
  }

  const fingerprint = await eventClipRequestFingerprint(phase, event);
  const existing = await findEventClip(deviceId, event.eventGroupId, event.segmentIndex);
  const existingPhaseKey =
    phase === "started" ? existing?.start_idempotency_key : existing?.end_idempotency_key;
  const existingPhaseFingerprint =
    phase === "started"
      ? existing?.start_request_fingerprint
      : existing?.end_request_fingerprint;
  if (existingPhaseKey) {
    if (
      existingPhaseKey !== event.idempotencyKey ||
      existingPhaseFingerprint !== fingerprint
    ) {
      throw new Error("IDEMPOTENCY_CONFLICT");
    }
    return { created: false, event: mapEvent(existing!) };
  }

  const eventId = existing?.id ?? crypto.randomUUID();
  const recordingOffsetMs = Math.max(
    0,
    Date.parse(event.startAt) - Date.parse(recordingStartedAt),
  );
  const d1 = getD1();
  if (!existing) {
    const clipState = phase === "ended" ? "ready" : "recording";
    const legacyKey = `clip:${event.eventGroupId}:${event.segmentIndex}`;
    const result = await d1
      .prepare(
        `INSERT INTO homecam_events
         (id, device_id, event_type, confidence, occurred_at,
          idempotency_key, request_fingerprint,
          recording_session_id, recording_offset_ms,
          event_group_id, segment_index, labels_json,
          clip_start_at, clip_end_at, clip_state,
          monotonic_duration_ms, boot_id, session_ids_json,
          clock_stepped, notification_suppressed,
          start_idempotency_key, end_idempotency_key,
          start_request_fingerprint, end_request_fingerprint)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
         ON CONFLICT (device_id, event_group_id, segment_index)
           WHERE event_group_id IS NOT NULL AND segment_index IS NOT NULL
         DO NOTHING`,
      )
      .bind(
        eventId,
        deviceId,
        event.primaryType,
        event.confidence,
        event.detectedAt,
        legacyKey,
        fingerprint,
        primarySession.session_id,
        recordingOffsetMs,
        event.eventGroupId,
        event.segmentIndex,
        JSON.stringify(event.labels),
        event.startAt,
        event.endAt,
        clipState,
        event.monotonicDurationMs,
        event.bootId,
        JSON.stringify(event.sessionIds),
        event.clockSteppedDuringEvent ? 1 : 0,
        event.notificationEligible ? 0 : 1,
        phase === "started" ? event.idempotencyKey : null,
        phase === "ended" ? event.idempotencyKey : null,
        phase === "started" ? fingerprint : null,
        phase === "ended" ? fingerprint : null,
      )
      .run();
    const stored = await findEventClip(deviceId, event.eventGroupId, event.segmentIndex);
    if (!stored) throw new Error("EVENT_CLIP_WRITE_FAILED");
    const storedKey =
      phase === "started" ? stored.start_idempotency_key : stored.end_idempotency_key;
    const storedFingerprint =
      phase === "started"
        ? stored.start_request_fingerprint
        : stored.end_request_fingerprint;
    if (storedKey !== event.idempotencyKey || storedFingerprint !== fingerprint) {
      throw new Error("IDEMPOTENCY_CONFLICT");
    }
    return { created: result.meta.changes > 0, event: mapEvent(stored) };
  }

  if (
    existing.boot_id !== event.bootId ||
    existing.clip_start_at !== event.startAt
  ) {
    throw new Error("IDEMPOTENCY_CONFLICT");
  }
  await d1
    .prepare(
      phase === "ended"
        ? `UPDATE homecam_events
           SET event_type = ?, confidence = ?, labels_json = ?,
               clip_end_at = ?, clip_state = 'ready', monotonic_duration_ms = ?,
               session_ids_json = ?, clock_stepped = ?,
               end_idempotency_key = ?, end_request_fingerprint = ?
           WHERE id = ? AND device_id = ? AND end_idempotency_key IS NULL`
        : `UPDATE homecam_events
           SET start_idempotency_key = ?, start_request_fingerprint = ?
           WHERE id = ? AND device_id = ? AND start_idempotency_key IS NULL`,
    )
    .bind(
      ...(phase === "ended"
        ? [
            event.primaryType,
            event.confidence,
            JSON.stringify(event.labels),
            event.endAt,
            event.monotonicDurationMs,
            JSON.stringify(event.sessionIds),
            event.clockSteppedDuringEvent ? 1 : 0,
            event.idempotencyKey,
            fingerprint,
          ]
        : [event.idempotencyKey, fingerprint]),
      eventId,
      deviceId,
    )
    .run();
  const stored = await findEventClip(deviceId, event.eventGroupId, event.segmentIndex);
  if (!stored) throw new Error("EVENT_CLIP_WRITE_FAILED");
  const storedKey =
    phase === "started" ? stored.start_idempotency_key : stored.end_idempotency_key;
  const storedFingerprint =
    phase === "started"
      ? stored.start_request_fingerprint
      : stored.end_request_fingerprint;
  if (storedKey !== event.idempotencyKey || storedFingerprint !== fingerprint) {
    throw new Error("IDEMPOTENCY_CONFLICT");
  }
  return { created: false, event: mapEvent(stored) };
}

export async function softDeleteHomecamEvent(input: {
  deviceId: string;
  eventId: string;
  userEmail: string;
}) {
  await ensureHomecamSchema();
  const event = await getHomecamEvent(input.deviceId, input.eventId);
  if (!event) return false;
  const nowIso = new Date().toISOString();
  const result = await getD1()
    .prepare(
      `UPDATE homecam_events SET deleted_at = ?
       WHERE device_id = ? AND deleted_at IS NULL
         AND (id = ? OR (event_group_id IS NOT NULL AND event_group_id = ?))`,
    )
    .bind(
      nowIso,
      input.deviceId,
      input.eventId,
      event.eventGroupId ?? "",
    )
    .run();
  if (result.meta.changes > 0) {
    await getD1()
      .prepare(
        `DELETE FROM homecam_push_outbox
         WHERE delivered_at IS NULL AND event_id IN (
           SELECT id FROM homecam_events
           WHERE device_id = ?
             AND (id = ? OR (event_group_id IS NOT NULL AND event_group_id = ?))
         )`,
      )
      .bind(input.deviceId, input.eventId, event.eventGroupId ?? "")
      .run();
    await writeAuditLog({
      deviceId: input.deviceId,
      actorType: "user",
      actorId: input.userEmail,
      action: "event.remove_from_list",
      metadata: {
        eventId: event.id,
        eventGroupId: event.eventGroupId,
        segmentCount: event.segmentCount,
      },
    });
  }
  return result.meta.changes > 0;
}

export async function getEventClipPlayback(deviceId: string, eventId: string) {
  const event = await getHomecamEvent(deviceId, eventId);
  if (
    !event ||
    event.clipState !== "ready" ||
    !event.clipStartAt ||
    !event.clipEndAt ||
    !event.recordingId
  ) {
    return null;
  }
  const clipStartAt = event.clipStartAt;
  const clipEndAt = event.clipEndAt;
  const recordingId = event.recordingId;
  const recording = await getD1()
    .prepare(
      `SELECT recording_sessions.kvs_stream_arn
       FROM recording_sessions
       INNER JOIN stream_sessions
         ON stream_sessions.id = recording_sessions.session_id
       WHERE recording_sessions.session_id = ? AND stream_sessions.device_id = ?`,
    )
    .bind(recordingId, deviceId)
    .first<{ kvs_stream_arn: string }>();
  if (!recording) return null;
  return {
    event: { ...event, clipStartAt, clipEndAt, recordingId },
    streamArn: recording.kvs_stream_arn,
  };
}

type EventClipSessionRow = {
  session_id: string;
  session_started_at: string;
  recording_started_at: string | null;
  recording_ended_at: string | null;
};

async function getEventClipSessions(deviceId: string, sessionIds: string[]) {
  const placeholders = sessionIds.map(() => "?").join(",");
  const result = await getD1()
    .prepare(
      `SELECT stream_sessions.id AS session_id,
              stream_sessions.started_at AS session_started_at,
              recording_sessions.started_at AS recording_started_at,
              recording_sessions.ended_at AS recording_ended_at
       FROM stream_sessions
       INNER JOIN recording_sessions
         ON recording_sessions.session_id = stream_sessions.id
       WHERE stream_sessions.device_id = ?
         AND stream_sessions.id IN (${placeholders})`,
    )
    .bind(deviceId, ...sessionIds)
    .all<EventClipSessionRow>();
  return result.results;
}

async function eventClipRequestFingerprint(
  phase: "started" | "ended",
  event: HomecamEventClipInput,
) {
  const canonical = JSON.stringify({ phase, ...event, idempotencyKey: undefined });
  const digest = new Uint8Array(
    await crypto.subtle.digest("SHA-256", new TextEncoder().encode(canonical)),
  );
  return Array.from(digest, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

type HomecamEventViewRow = {
  id: string;
  event_type: string;
  confidence: number | null;
  occurred_at: string;
  received_at: string;
  recording_session_id: string | null;
  recording_offset_ms: number | null;
  event_group_id?: string | null;
  segment_index?: number | null;
  labels_json?: string | null;
  clip_start_at?: string | null;
  clip_end_at?: string | null;
  clip_state?: string | null;
  monotonic_duration_ms?: number | null;
  boot_id?: string | null;
  session_ids_json?: string | null;
  clock_stepped?: number | null;
  notification_suppressed?: number | null;
  start_idempotency_key?: string | null;
  end_idempotency_key?: string | null;
  start_request_fingerprint?: string | null;
  end_request_fingerprint?: string | null;
  deleted_at?: string | null;
  ai_status?: string | null;
  ai_summary?: string | null;
  ai_labels_json?: string | null;
  ai_severity?: string | null;
  ai_confidence?: number | null;
  ai_error?: string | null;
  segment_count?: number | null;
};

type EventRowWithFingerprint = HomecamEventViewRow & {
  device_id?: string;
  request_fingerprint: string;
  ai_model_id?: string | null;
  ai_model_version?: string | null;
  ai_prompt_version?: string | null;
  ai_input_spec_json?: string | null;
  ai_analyzed_at?: string | null;
};

async function findEventClip(
  deviceId: string,
  eventGroupId: string,
  segmentIndex: number,
) {
  return getD1()
    .prepare(
      `SELECT id, device_id, event_type, confidence, occurred_at, received_at,
              request_fingerprint, recording_session_id, recording_offset_ms,
              event_group_id, segment_index, labels_json,
              clip_start_at, clip_end_at, clip_state,
              monotonic_duration_ms, boot_id, session_ids_json,
              clock_stepped, notification_suppressed,
              start_idempotency_key, end_idempotency_key,
              start_request_fingerprint, end_request_fingerprint,
              deleted_at, ai_status, ai_summary, ai_labels_json,
              ai_severity, ai_confidence, ai_model_id, ai_model_version,
              ai_prompt_version, ai_input_spec_json, ai_error, ai_analyzed_at
       FROM homecam_events
       WHERE device_id = ? AND event_group_id = ? AND segment_index = ?`,
    )
    .bind(deviceId, eventGroupId, segmentIndex)
    .first<EventRowWithFingerprint>();
}

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
      `WITH ranked_events AS (
         SELECT id, event_type, confidence, occurred_at, received_at,
                recording_session_id, recording_offset_ms,
                event_group_id, segment_index, labels_json,
                clip_start_at, clip_end_at, clip_state,
                monotonic_duration_ms, clock_stepped,
                ai_status, ai_summary, ai_labels_json,
                ai_severity, ai_confidence, ai_error,
                ROW_NUMBER() OVER (
                  PARTITION BY COALESCE(event_group_id, id)
                  ORDER BY COALESCE(segment_index, -1) DESC,
                           occurred_at DESC, id DESC
                ) AS row_rank,
                FIRST_VALUE(id) OVER (
                  PARTITION BY COALESCE(event_group_id, id)
                  ORDER BY CASE WHEN segment_index IS NULL THEN 0 ELSE 1 END,
                           segment_index ASC, occurred_at ASC, id ASC
                ) AS group_event_id,
                MIN(occurred_at) OVER (
                  PARTITION BY COALESCE(event_group_id, id)
                ) AS group_occurred_at,
                MIN(clip_start_at) OVER (
                  PARTITION BY COALESCE(event_group_id, id)
                ) AS group_clip_start_at,
                MAX(clip_end_at) OVER (
                  PARTITION BY COALESCE(event_group_id, id)
                ) AS group_clip_end_at,
                COUNT(monotonic_duration_ms) OVER (
                  PARTITION BY COALESCE(event_group_id, id)
                ) AS group_duration_count,
                SUM(monotonic_duration_ms) OVER (
                  PARTITION BY COALESCE(event_group_id, id)
                ) AS group_duration_ms,
                MAX(clock_stepped) OVER (
                  PARTITION BY COALESCE(event_group_id, id)
                ) AS group_clock_stepped,
                COUNT(*) OVER (
                  PARTITION BY COALESCE(event_group_id, id)
                ) AS group_segment_count,
                SUM(CASE WHEN clip_state = 'recording' THEN 1 ELSE 0 END) OVER (
                  PARTITION BY COALESCE(event_group_id, id)
                ) AS group_recording_count,
                SUM(CASE WHEN clip_state <> 'ready' THEN 1 ELSE 0 END) OVER (
                  PARTITION BY COALESCE(event_group_id, id)
                ) AS group_not_ready_count
         FROM homecam_events
         WHERE device_id = ? AND occurred_at >= ? AND deleted_at IS NULL
       ), grouped_events AS (
         SELECT group_event_id AS id, event_type, confidence,
                group_occurred_at AS occurred_at, received_at,
                recording_session_id, recording_offset_ms,
                event_group_id, segment_index, labels_json,
                group_clip_start_at AS clip_start_at,
                group_clip_end_at AS clip_end_at,
                CASE
                  WHEN group_recording_count > 0 THEN 'recording'
                  WHEN group_not_ready_count = 0 THEN 'ready'
                  ELSE clip_state
                END AS clip_state,
                CASE WHEN group_duration_count = 0 THEN NULL
                     ELSE group_duration_ms END AS monotonic_duration_ms,
                group_clock_stepped AS clock_stepped,
                ai_status, ai_summary, ai_labels_json,
                ai_severity, ai_confidence, ai_error,
                group_segment_count AS segment_count
         FROM ranked_events WHERE row_rank = 1
       )
       SELECT id, event_type, confidence, occurred_at, received_at,
              recording_session_id, recording_offset_ms,
              event_group_id, segment_index, labels_json,
              clip_start_at, clip_end_at, clip_state,
              monotonic_duration_ms, clock_stepped, ai_status, ai_summary,
              ai_labels_json, ai_severity, ai_confidence, ai_error,
              segment_count
       FROM grouped_events
       WHERE 1 = 1 ${typeClause} ${beforeClause}
       ORDER BY occurred_at DESC, id DESC LIMIT ?`,
    )
    .bind(...bindings)
    .all<HomecamEventViewRow>();
  return result.results.map(mapEvent);
}

export async function getHomecamEvent(deviceId: string, eventId: string) {
  await ensureHomecamSchema();
  await cleanupExpiredHomecamData();
  const row = await getD1()
    .prepare(
      `SELECT id, event_type, confidence, occurred_at, received_at,
              recording_session_id, recording_offset_ms,
              event_group_id, segment_index, labels_json,
              clip_start_at, clip_end_at, clip_state,
              monotonic_duration_ms, clock_stepped, ai_status, ai_summary,
              ai_labels_json, ai_severity, ai_confidence, ai_error
       FROM homecam_events
       WHERE id = ? AND device_id = ? AND occurred_at >= ? AND deleted_at IS NULL`,
    )
    .bind(
      eventId,
      deviceId,
      new Date(Date.now() - EVENT_RETENTION_MS).toISOString(),
    )
    .first<HomecamEventViewRow>();
  if (!row) return null;
  if (!row.event_group_id) return mapEvent(row);
  const segments = await getD1()
    .prepare(
      `SELECT id, event_type, confidence, occurred_at, received_at,
              recording_session_id, recording_offset_ms,
              event_group_id, segment_index, labels_json,
              clip_start_at, clip_end_at, clip_state,
              monotonic_duration_ms, clock_stepped, ai_status, ai_summary,
              ai_labels_json, ai_severity, ai_confidence, ai_error
       FROM homecam_events
       WHERE device_id = ? AND event_group_id = ?
         AND occurred_at >= ? AND deleted_at IS NULL
       ORDER BY segment_index ASC, occurred_at ASC, id ASC`,
    )
    .bind(
      deviceId,
      row.event_group_id,
      new Date(Date.now() - EVENT_RETENTION_MS).toISOString(),
    )
    .all<HomecamEventViewRow>();
  return segments.results.length > 0
    ? mapEvent(mergeEventSegments(segments.results))
    : null;
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
         AND EXISTS (
           SELECT 1 FROM homecam_events
           WHERE homecam_events.id = homecam_push_outbox.event_id
             AND homecam_events.deleted_at IS NULL
         )
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
       SET p2p_healthy = CASE WHEN p2p_session_id IN (
             SELECT id FROM stream_sessions WHERE status != 'active'
           ) THEN 0 ELSE p2p_healthy END,
           storage_healthy = CASE WHEN storage_session_id IN (
             SELECT id FROM stream_sessions WHERE status != 'active'
           ) THEN 0 ELSE storage_healthy END,
           p2p_session_id = CASE WHEN p2p_session_id IN (
             SELECT id FROM stream_sessions WHERE status != 'active'
           ) THEN NULL ELSE p2p_session_id END,
           storage_session_id = CASE WHEN storage_session_id IN (
             SELECT id FROM stream_sessions WHERE status != 'active'
           ) THEN NULL ELSE storage_session_id END,
           updated_at = ?
       WHERE (p2p_session_id IS NOT NULL AND p2p_session_id IN (
           SELECT id FROM stream_sessions WHERE status != 'active'
         )) OR (storage_session_id IS NOT NULL AND storage_session_id IN (
           SELECT id FROM stream_sessions WHERE status != 'active'
         ))`,
    ).bind(nowIso),
    d1.prepare(
      `UPDATE device_state
       SET active_stream_mode = CASE
             WHEN storage_session_id IS NOT NULL THEN 'storage'
             WHEN p2p_session_id IS NOT NULL THEN 'p2p'
             ELSE 'idle'
           END,
           active_session_id = COALESCE(storage_session_id, p2p_session_id),
           media_healthy = CASE
             WHEN storage_session_id IS NOT NULL THEN storage_healthy
             WHEN p2p_session_id IS NOT NULL THEN p2p_healthy
             ELSE 0
           END,
           updated_at = ?
       WHERE active_session_id IS DISTINCT FROM COALESCE(storage_session_id, p2p_session_id)
          OR active_stream_mode IS DISTINCT FROM CASE
            WHEN storage_session_id IS NOT NULL THEN 'storage'
            WHEN p2p_session_id IS NOT NULL THEN 'p2p'
            ELSE 'idle'
          END`,
    ).bind(nowIso),
  ]);
}

async function cleanupExpiredHomecamData() {
  const d1 = getD1();
  await d1.batch([
    d1
      .prepare(
        `UPDATE homecam_events
         SET clip_state = 'incomplete'
         WHERE clip_state = 'recording' AND received_at < ?`,
      )
      .bind(new Date(Date.now() - EVENT_CLIP_INCOMPLETE_MS).toISOString()),
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
    p2pSessionId: row.p2p_session_id,
    storageSessionId: row.storage_session_id,
    p2pHealthy: Boolean(row.p2p_healthy),
    storageHealthy: Boolean(row.storage_healthy),
    detectorHealthy: Boolean(row.detector_healthy),
    lastSeenAt: row.last_seen_at,
    updatedAt: row.updated_at,
  };
}

function mergeEventSegments(rows: HomecamEventViewRow[]): HomecamEventViewRow {
  if (rows.length === 0) throw new Error("EVENT_SEGMENTS_EMPTY");
  const ordered = [...rows].sort((left, right) => {
    const leftIndex = left.segment_index ?? -1;
    const rightIndex = right.segment_index ?? -1;
    if (leftIndex !== rightIndex) return leftIndex - rightIndex;
    return Date.parse(left.occurred_at) - Date.parse(right.occurred_at);
  });
  const first = ordered[0];
  const latest = ordered.at(-1)!;
  const clipStarts = ordered
    .map((row) => row.clip_start_at)
    .filter((value): value is string => Boolean(value));
  const clipEnds = ordered
    .map((row) => row.clip_end_at)
    .filter((value): value is string => Boolean(value));
  const durations = ordered
    .map((row) => row.monotonic_duration_ms)
    .filter((value): value is number => typeof value === "number");
  const allReady = ordered.every((row) => row.clip_state === "ready");
  const anyRecording = ordered.some((row) => row.clip_state === "recording");
  return {
    ...latest,
    id: first.id,
    occurred_at: ordered.reduce(
      (earliest, row) =>
        Date.parse(row.occurred_at) < Date.parse(earliest)
          ? row.occurred_at
          : earliest,
      first.occurred_at,
    ),
    clip_start_at:
      clipStarts.length > 0
        ? clipStarts.reduce((earliest, value) =>
            Date.parse(value) < Date.parse(earliest) ? value : earliest,
          )
        : null,
    clip_end_at:
      clipEnds.length > 0
        ? clipEnds.reduce((latestValue, value) =>
            Date.parse(value) > Date.parse(latestValue) ? value : latestValue,
          )
        : null,
    clip_state: anyRecording ? "recording" : allReady ? "ready" : latest.clip_state,
    monotonic_duration_ms:
      durations.length > 0
        ? durations.reduce((total, value) => total + value, 0)
        : null,
    clock_stepped: ordered.some((row) => Boolean(row.clock_stepped)) ? 1 : 0,
    segment_count: ordered.length,
  };
}

function mapEvent(row: HomecamEventViewRow) {
  const playback = recordingPlaybackPosition(row.recording_offset_ms);
  return {
    id: row.id,
    eventType: row.event_type,
    confidence: row.confidence,
    occurredAt: row.occurred_at,
    receivedAt: row.received_at,
    recordingId: row.recording_session_id,
    recordingOffsetMs: row.recording_offset_ms,
    eventGroupId: row.event_group_id ?? null,
    segmentIndex: row.segment_index ?? null,
    segmentCount: row.segment_count ?? 1,
    labels: parseStringArray(row.labels_json),
    clipStartAt: row.clip_start_at ?? null,
    clipEndAt: row.clip_end_at ?? null,
    clipState: row.clip_state ?? "detected",
    monotonicDurationMs: row.monotonic_duration_ms ?? null,
    clockSteppedDuringEvent: Boolean(row.clock_stepped),
    ai: {
      status: row.ai_status ?? "not_requested",
      summary: row.ai_summary ?? null,
      labels: parseStringArray(row.ai_labels_json),
      severity: row.ai_severity ?? null,
      confidence: row.ai_confidence ?? null,
      error: row.ai_error ?? null,
    },
    ...playback,
  };
}

function parseStringArray(value: string | null | undefined) {
  if (!value) return [];
  try {
    const parsed = JSON.parse(value);
    return Array.isArray(parsed)
      ? parsed.filter((item): item is string => typeof item === "string")
      : [];
  } catch {
    return [];
  }
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
