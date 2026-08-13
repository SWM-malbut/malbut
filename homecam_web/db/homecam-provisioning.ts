import { getD1 } from ".";
import { ensureHomecamSchema } from "./homecam";
import type { HomecamProvisioningRequest } from "./homecam-provisioning-input";

type HomecamProvisioningInput = HomecamProvisioningRequest & {
  kvsChannelArn: string;
  migrationChannelArn: string;
  legacyDeviceId: string | null;
};

type ProvisioningSnapshot = {
  deviceById: {
    id: string;
    display_name: string;
    kvs_channel_arn: string;
  } | null;
  deviceByChannel: { id: string } | null;
  deviceByMigrationChannel: { id: string } | null;
  membership: { role: string } | null;
  membershipSummary: {
    total: number;
    exact_count: number;
  };
  state: { device_id: string } | null;
  credentialById: {
    id: string;
    device_id: string;
    label: string;
    token_digest: string;
    expires_at: string | null;
    revoked_at: string | null;
  } | null;
  credentialByDigest: { id: string } | null;
  credentialSummary: {
    total: number;
    exact_count: number;
  };
  channelOwnerSummary: {
    display_name: string;
    membership_count: number;
    requested_owner_count: number;
    credential_count: number;
    session_count: number;
    active_session_count: number;
    recording_count: number;
    event_count: number;
    state_count: number;
    last_seen_at: string | null;
  } | null;
};

export class HomecamProvisioningConflict extends Error {
  readonly details: ReturnType<typeof provisioningConflictDetails>;

  constructor(
    snapshot: ProvisioningSnapshot,
    input: HomecamProvisioningInput,
  ) {
    super("HOMECAM_PROVISIONING_CONFLICT");
    this.details = provisioningConflictDetails(snapshot, input);
  }
}

export async function provisionHomecamDevice(
  input: HomecamProvisioningInput,
) {
  await ensureHomecamSchema();
  const before = await provisioningSnapshot(input);
  if (isCompleteAndCompatible(before, input)) {
    return { deviceId: input.deviceId, created: false, migrated: false };
  }
  if (isEmpty(before)) {
    return createProvisionedDevice(input);
  }
  if (canMigrateLegacyDevice(before, input)) {
    return migrateLegacyDevice(before, input);
  }
  throw new HomecamProvisioningConflict(before, input);
}

async function createProvisionedDevice(input: HomecamProvisioningInput) {
  const d1 = getD1();
  const nowIso = new Date().toISOString();
  const statements = [
    d1
      .prepare(
        `INSERT INTO devices (id, display_name, kvs_channel_arn, created_at)
         VALUES (?, ?, ?, ?)`,
      )
      .bind(
        input.deviceId,
        input.displayName,
        input.kvsChannelArn,
        nowIso,
      ),
    d1
      .prepare(
        `INSERT INTO device_memberships
         (device_id, user_email, role, created_at)
         VALUES (?, ?, 'owner', ?)`,
      )
      .bind(input.deviceId, input.ownerEmail, nowIso),
    d1
      .prepare(
        `INSERT INTO device_state
         (device_id, monitoring_enabled, camera_enabled, microphone_enabled,
          source_profile, active_stream_mode, media_healthy,
          detector_healthy, updated_at)
         VALUES (?, 0, 1, 1, ?, 'idle', 0, 0, ?)`,
      )
      .bind(input.deviceId, input.sourceProfile, nowIso),
    d1
      .prepare(
        `INSERT INTO device_credentials
         (id, device_id, label, token_digest, created_at, expires_at)
         VALUES (?, ?, ?, ?, ?, ?)`,
      )
      .bind(
        input.credential.id,
        input.deviceId,
        input.credential.label,
        input.credential.tokenDigest,
        nowIso,
        input.credential.expiresAt,
      ),
    d1
      .prepare(
        `INSERT INTO access_audit_log
         (id, device_id, actor_type, actor_id, action, metadata_json, created_at)
         VALUES (?, ?, 'system', 'internal-provisioner',
                 'device.provision', ?, ?)`,
      )
      .bind(
        crypto.randomUUID(),
        input.deviceId,
        JSON.stringify({
          credentialId: input.credential.id,
          sourceProfile: input.sourceProfile,
        }),
        nowIso,
      ),
  ];
  try {
    await d1.batch(statements);
  } catch (error) {
    const afterRace = await provisioningSnapshot(input);
    if (isCompleteAndCompatible(afterRace, input)) {
      return { deviceId: input.deviceId, created: false, migrated: false };
    }
    if (!isEmpty(afterRace)) {
      throw new HomecamProvisioningConflict(afterRace, input);
    }
    throw error;
  }

  const after = await provisioningSnapshot(input);
  if (!isCompleteAndCompatible(after, input)) {
    throw new HomecamProvisioningConflict(after, input);
  }
  return { deviceId: input.deviceId, created: true, migrated: false };
}

async function migrateLegacyDevice(
  before: ProvisioningSnapshot,
  input: HomecamProvisioningInput,
) {
  const legacyDeviceId = before.deviceByChannel?.id;
  if (!legacyDeviceId) {
    throw new HomecamProvisioningConflict(before, input);
  }
  const d1 = getD1();
  const nowIso = new Date().toISOString();
  const statements = [
    d1
      .prepare(
        `UPDATE devices SET kvs_channel_arn = ?
         WHERE id = ? AND kvs_channel_arn = ?
           AND NOT EXISTS (SELECT 1 FROM devices WHERE id = ?)
           AND (SELECT COUNT(*) FROM device_memberships
                WHERE device_id = ?) = 1
           AND EXISTS (
             SELECT 1 FROM device_memberships
             WHERE device_id = ? AND user_email = ? AND role = 'owner'
           )
           AND NOT EXISTS (
             SELECT 1 FROM device_credentials WHERE device_id = ?
           )
           AND NOT EXISTS (
             SELECT 1 FROM device_state WHERE device_id = ?
           )
           AND NOT EXISTS (
             SELECT 1 FROM homecam_events WHERE device_id = ?
           )
           AND NOT EXISTS (
             SELECT 1 FROM stream_sessions
             WHERE device_id = ? AND status = 'active' AND expires_at > ?
           )`,
      )
      .bind(
        input.migrationChannelArn,
        legacyDeviceId,
        input.kvsChannelArn,
        input.deviceId,
        legacyDeviceId,
        legacyDeviceId,
        input.ownerEmail,
        legacyDeviceId,
        legacyDeviceId,
        legacyDeviceId,
        legacyDeviceId,
        nowIso,
      ),
    d1
      .prepare(
        `INSERT INTO devices (id, display_name, kvs_channel_arn, created_at)
         SELECT ?, ?, ?, ?
         WHERE EXISTS (
           SELECT 1 FROM devices WHERE id = ? AND kvs_channel_arn = ?
         )`,
      )
      .bind(
        input.deviceId,
        input.displayName,
        input.kvsChannelArn,
        nowIso,
        legacyDeviceId,
        input.migrationChannelArn,
      ),
    d1
      .prepare(
        `UPDATE device_memberships SET device_id = ?
         WHERE device_id = ? AND EXISTS (
           SELECT 1 FROM devices WHERE id = ? AND kvs_channel_arn = ?
         )`,
      )
      .bind(
        input.deviceId,
        legacyDeviceId,
        legacyDeviceId,
        input.migrationChannelArn,
      ),
    d1
      .prepare(
        `UPDATE stream_sessions SET device_id = ?
         WHERE device_id = ? AND EXISTS (
           SELECT 1 FROM devices WHERE id = ? AND kvs_channel_arn = ?
         )`,
      )
      .bind(
        input.deviceId,
        legacyDeviceId,
        legacyDeviceId,
        input.migrationChannelArn,
      ),
    d1
      .prepare(
        `UPDATE device_credentials SET device_id = ?
         WHERE device_id = ? AND EXISTS (
           SELECT 1 FROM devices WHERE id = ? AND kvs_channel_arn = ?
         )`,
      )
      .bind(
        input.deviceId,
        legacyDeviceId,
        legacyDeviceId,
        input.migrationChannelArn,
      ),
    d1
      .prepare(
        `UPDATE device_state SET device_id = ?
         WHERE device_id = ? AND EXISTS (
           SELECT 1 FROM devices WHERE id = ? AND kvs_channel_arn = ?
         )`,
      )
      .bind(
        input.deviceId,
        legacyDeviceId,
        legacyDeviceId,
        input.migrationChannelArn,
      ),
    d1
      .prepare(
        `UPDATE homecam_events SET device_id = ?
         WHERE device_id = ? AND EXISTS (
           SELECT 1 FROM devices WHERE id = ? AND kvs_channel_arn = ?
         )`,
      )
      .bind(
        input.deviceId,
        legacyDeviceId,
        legacyDeviceId,
        input.migrationChannelArn,
      ),
    d1
      .prepare(
        `UPDATE homecam_push_outbox SET device_id = ?
         WHERE device_id = ? AND EXISTS (
           SELECT 1 FROM devices WHERE id = ? AND kvs_channel_arn = ?
         )`,
      )
      .bind(
        input.deviceId,
        legacyDeviceId,
        legacyDeviceId,
        input.migrationChannelArn,
      ),
    d1
      .prepare(
        `UPDATE push_subscriptions SET device_id = ?
         WHERE device_id = ? AND EXISTS (
           SELECT 1 FROM devices WHERE id = ? AND kvs_channel_arn = ?
         )`,
      )
      .bind(
        input.deviceId,
        legacyDeviceId,
        legacyDeviceId,
        input.migrationChannelArn,
      ),
    d1
      .prepare(
        `UPDATE access_audit_log SET device_id = ?
         WHERE device_id = ? AND EXISTS (
           SELECT 1 FROM devices WHERE id = ? AND kvs_channel_arn = ?
         )`,
      )
      .bind(
        input.deviceId,
        legacyDeviceId,
        legacyDeviceId,
        input.migrationChannelArn,
      ),
    d1
      .prepare(
        `UPDATE talk_leases SET device_id = ?
         WHERE device_id = ? AND EXISTS (
           SELECT 1 FROM devices WHERE id = ? AND kvs_channel_arn = ?
         )`,
      )
      .bind(
        input.deviceId,
        legacyDeviceId,
        legacyDeviceId,
        input.migrationChannelArn,
      ),
    d1
      .prepare(
        `INSERT INTO device_state
         (device_id, monitoring_enabled, camera_enabled, microphone_enabled,
          source_profile, active_stream_mode, media_healthy,
          detector_healthy, updated_at)
         SELECT ?, 0, 1, 1, ?, 'idle', 0, 0, ?
         WHERE EXISTS (
           SELECT 1 FROM devices WHERE id = ? AND kvs_channel_arn = ?
         )`,
      )
      .bind(
        input.deviceId,
        input.sourceProfile,
        nowIso,
        legacyDeviceId,
        input.migrationChannelArn,
      ),
    d1
      .prepare(
        `INSERT INTO device_credentials
         (id, device_id, label, token_digest, created_at, expires_at)
         SELECT ?, ?, ?, ?, ?, ?
         WHERE EXISTS (
           SELECT 1 FROM devices WHERE id = ? AND kvs_channel_arn = ?
         )`,
      )
      .bind(
        input.credential.id,
        input.deviceId,
        input.credential.label,
        input.credential.tokenDigest,
        nowIso,
        input.credential.expiresAt,
        legacyDeviceId,
        input.migrationChannelArn,
      ),
    d1
      .prepare(
        `INSERT INTO access_audit_log
         (id, device_id, actor_type, actor_id, action, metadata_json, created_at)
         SELECT ?, ?, 'system', 'internal-provisioner',
                'device.migrate', ?, ?
         WHERE EXISTS (
           SELECT 1 FROM devices WHERE id = ? AND kvs_channel_arn = ?
         )`,
      )
      .bind(
        crypto.randomUUID(),
        input.deviceId,
        JSON.stringify({
          fromDeviceId: legacyDeviceId,
          credentialId: input.credential.id,
          preservedSessions: before.channelOwnerSummary?.session_count ?? 0,
          preservedRecordings:
            before.channelOwnerSummary?.recording_count ?? 0,
        }),
        nowIso,
        legacyDeviceId,
        input.migrationChannelArn,
      ),
    d1
      .prepare("DELETE FROM devices WHERE id = ? AND kvs_channel_arn = ?")
      .bind(legacyDeviceId, input.migrationChannelArn),
  ];
  try {
    await d1.batch(statements);
  } catch (error) {
    const afterRace = await provisioningSnapshot(input);
    if (isCompleteAndCompatible(afterRace, input)) {
      return { deviceId: input.deviceId, created: false, migrated: true };
    }
    if (canMigrateLegacyDevice(afterRace, input)) {
      throw error;
    }
    if (!isEmpty(afterRace)) {
      throw new HomecamProvisioningConflict(afterRace, input);
    }
    throw error;
  }
  const after = await provisioningSnapshot(input);
  if (!isCompleteAndCompatible(after, input)) {
    throw new HomecamProvisioningConflict(after, input);
  }
  return { deviceId: input.deviceId, created: true, migrated: true };
}

async function provisioningSnapshot(
  input: HomecamProvisioningInput,
): Promise<ProvisioningSnapshot> {
  const d1 = getD1();
  const [
    deviceById,
    deviceByChannel,
    deviceByMigrationChannel,
    membership,
    membershipSummary,
    state,
    credentialById,
    credentialByDigest,
    credentialSummary,
  ] = await Promise.all([
    d1
      .prepare(
        "SELECT id, display_name, kvs_channel_arn FROM devices WHERE id = ?",
      )
      .bind(input.deviceId)
      .first<ProvisioningSnapshot["deviceById"]>(),
    d1
      .prepare("SELECT id FROM devices WHERE kvs_channel_arn = ?")
      .bind(input.kvsChannelArn)
      .first<ProvisioningSnapshot["deviceByChannel"]>(),
    d1
      .prepare("SELECT id FROM devices WHERE kvs_channel_arn = ?")
      .bind(input.migrationChannelArn)
      .first<ProvisioningSnapshot["deviceByMigrationChannel"]>(),
    d1
      .prepare(
        `SELECT role FROM device_memberships
         WHERE device_id = ? AND user_email = ?`,
      )
      .bind(input.deviceId, input.ownerEmail)
      .first<ProvisioningSnapshot["membership"]>(),
    d1
      .prepare(
        `SELECT
           COUNT(*) AS total,
           COALESCE(SUM(CASE
             WHEN user_email = ? AND role = 'owner' THEN 1 ELSE 0
           END), 0) AS exact_count
         FROM device_memberships WHERE device_id = ?`,
      )
      .bind(input.ownerEmail, input.deviceId)
      .first<ProvisioningSnapshot["membershipSummary"]>(),
    d1
      .prepare("SELECT device_id FROM device_state WHERE device_id = ?")
      .bind(input.deviceId)
      .first<ProvisioningSnapshot["state"]>(),
    d1
      .prepare(
        `SELECT id, device_id, label, token_digest, expires_at, revoked_at
         FROM device_credentials WHERE id = ?`,
      )
      .bind(input.credential.id)
      .first<ProvisioningSnapshot["credentialById"]>(),
    d1
      .prepare("SELECT id FROM device_credentials WHERE token_digest = ?")
      .bind(input.credential.tokenDigest)
      .first<ProvisioningSnapshot["credentialByDigest"]>(),
    d1
      .prepare(
        `SELECT
           COUNT(*) AS total,
           COALESCE(SUM(CASE
             WHEN id = ? AND label = ? AND token_digest = ?
              AND expires_at = ? AND revoked_at IS NULL
             THEN 1 ELSE 0
           END), 0) AS exact_count
         FROM device_credentials WHERE device_id = ?`,
      )
      .bind(
        input.credential.id,
        input.credential.label,
        input.credential.tokenDigest,
        input.credential.expiresAt,
        input.deviceId,
      )
      .first<ProvisioningSnapshot["credentialSummary"]>(),
  ]);
  const channelOwnerSummary =
    deviceByChannel && deviceByChannel.id !== input.deviceId
      ? await d1
          .prepare(
            `SELECT
               devices.display_name,
               (SELECT COUNT(*) FROM device_memberships
                WHERE device_id = devices.id) AS membership_count,
               (SELECT COUNT(*) FROM device_memberships
                WHERE device_id = devices.id
                  AND user_email = ? AND role = 'owner')
                 AS requested_owner_count,
               (SELECT COUNT(*) FROM device_credentials
                WHERE device_id = devices.id) AS credential_count,
               (SELECT COUNT(*) FROM stream_sessions
                WHERE device_id = devices.id) AS session_count,
               (SELECT COUNT(*) FROM stream_sessions
                WHERE device_id = devices.id AND status = 'active'
                  AND expires_at > ?) AS active_session_count,
               (SELECT COUNT(*) FROM recording_sessions
                INNER JOIN stream_sessions
                  ON stream_sessions.id = recording_sessions.session_id
                WHERE stream_sessions.device_id = devices.id)
                 AS recording_count,
               (SELECT COUNT(*) FROM homecam_events
                WHERE device_id = devices.id) AS event_count,
               (SELECT COUNT(*) FROM device_state
                WHERE device_id = devices.id) AS state_count,
               (SELECT last_seen_at FROM device_state
                WHERE device_id = devices.id) AS last_seen_at
             FROM devices WHERE devices.id = ?`,
          )
          .bind(input.ownerEmail, new Date().toISOString(), deviceByChannel.id)
          .first<ProvisioningSnapshot["channelOwnerSummary"]>()
      : null;
  return {
    deviceById,
    deviceByChannel,
    deviceByMigrationChannel,
    membership,
    membershipSummary: membershipSummary ?? { total: 0, exact_count: 0 },
    state,
    credentialById,
    credentialByDigest,
    credentialSummary: credentialSummary ?? { total: 0, exact_count: 0 },
    channelOwnerSummary,
  };
}

function isEmpty(snapshot: ProvisioningSnapshot) {
  return (
    !snapshot.deviceById &&
    !snapshot.deviceByChannel &&
    !snapshot.deviceByMigrationChannel &&
    !snapshot.membership &&
    snapshot.membershipSummary.total === 0 &&
    !snapshot.state &&
    !snapshot.credentialById &&
    !snapshot.credentialByDigest &&
    snapshot.credentialSummary.total === 0
  );
}

function canMigrateLegacyDevice(
  snapshot: ProvisioningSnapshot,
  input: HomecamProvisioningInput,
) {
  const summary = snapshot.channelOwnerSummary;
  return (
    !snapshot.deviceById &&
    snapshot.deviceByChannel?.id === input.legacyDeviceId &&
    !snapshot.deviceByMigrationChannel &&
    Boolean(summary) &&
    summary?.membership_count === 1 &&
    summary.requested_owner_count === 1 &&
    summary.credential_count === 0 &&
    summary.active_session_count === 0 &&
    summary.event_count === 0 &&
    summary.state_count === 0 &&
    !snapshot.membership &&
    snapshot.membershipSummary.total === 0 &&
    !snapshot.state &&
    !snapshot.credentialById &&
    !snapshot.credentialByDigest &&
    snapshot.credentialSummary.total === 0
  );
}

function isCompleteAndCompatible(
  snapshot: ProvisioningSnapshot,
  input: HomecamProvisioningInput,
) {
  return (
    Boolean(
      snapshot.deviceById &&
        snapshot.deviceByChannel &&
        snapshot.membership &&
        snapshot.state &&
        snapshot.credentialById &&
        snapshot.credentialByDigest,
    ) &&
    snapshot.membershipSummary.total === 1 &&
    snapshot.membershipSummary.exact_count === 1 &&
    snapshot.credentialSummary.total === 1 &&
    snapshot.credentialSummary.exact_count === 1 &&
    isCompatible(snapshot, input)
  );
}

function isCompatible(
  snapshot: ProvisioningSnapshot,
  input: HomecamProvisioningInput,
) {
  return (
    snapshot.deviceById?.display_name === input.displayName &&
    snapshot.deviceById.kvs_channel_arn === input.kvsChannelArn &&
    snapshot.deviceByChannel?.id === input.deviceId &&
    snapshot.membership?.role === "owner" &&
    snapshot.credentialById?.device_id === input.deviceId &&
    snapshot.credentialById.label === input.credential.label &&
    snapshot.credentialById.token_digest === input.credential.tokenDigest &&
    snapshot.credentialById.expires_at === input.credential.expiresAt &&
    snapshot.credentialById.revoked_at === null &&
    snapshot.credentialByDigest?.id === input.credential.id
  );
}

function provisioningConflictDetails(
  snapshot: ProvisioningSnapshot,
  input: HomecamProvisioningInput,
) {
  const targetDevice = !snapshot.deviceById
    ? "absent"
    : snapshot.deviceById.display_name === input.displayName &&
        snapshot.deviceById.kvs_channel_arn === input.kvsChannelArn
      ? "exact"
      : "mismatch";
  const channelOwner = !snapshot.deviceByChannel
    ? "free"
    : snapshot.deviceByChannel.id === input.deviceId
      ? "target"
      : "other";
  return {
    targetDevice,
    channelOwner,
    channelOwnerDeviceId:
      channelOwner === "other" ? snapshot.deviceByChannel?.id : undefined,
    membershipCount: snapshot.membershipSummary.total,
    exactMembershipCount: snapshot.membershipSummary.exact_count,
    stateExists: Boolean(snapshot.state),
    credentialCount: snapshot.credentialSummary.total,
    exactCredentialCount: snapshot.credentialSummary.exact_count,
    credentialIdMatches:
      !snapshot.credentialById ||
      snapshot.credentialById.device_id === input.deviceId,
    credentialDigestMatches:
      !snapshot.credentialByDigest ||
      snapshot.credentialByDigest.id === input.credential.id,
    channelOwnerSummary: snapshot.channelOwnerSummary
      ? {
          displayName: snapshot.channelOwnerSummary.display_name,
          membershipCount: snapshot.channelOwnerSummary.membership_count,
          requestedOwnerCount:
            snapshot.channelOwnerSummary.requested_owner_count,
          credentialCount: snapshot.channelOwnerSummary.credential_count,
          sessionCount: snapshot.channelOwnerSummary.session_count,
          activeSessionCount:
            snapshot.channelOwnerSummary.active_session_count,
          recordingCount: snapshot.channelOwnerSummary.recording_count,
          eventCount: snapshot.channelOwnerSummary.event_count,
          stateCount: snapshot.channelOwnerSummary.state_count,
          lastSeenAt: snapshot.channelOwnerSummary.last_seen_at,
        }
      : undefined,
  };
}
