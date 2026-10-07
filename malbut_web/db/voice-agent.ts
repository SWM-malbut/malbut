import { randomUUID } from "node:crypto";
import type { PoolClient } from "pg";
import { getPostgresPool } from "./postgres";
import { parseVoiceRequest, requiresVoiceDelegation, type VoiceHistory, type VoiceReply,
  type VoiceRequest } from "../app/voice-agent-contract";
import type { DeviceIdentity } from "./homecam";
import { applyReceipt } from "./media-settings";

async function requireSchema() {
  const result = await getPostgresPool().query(
    "SELECT 1 FROM homecam_schema_migrations WHERE version='0024_voice_agent'",
  );
  if (!result.rowCount) throw new Error("VOICE_MIGRATION_REQUIRED");
}
function failure(code: string): VoiceReply {
  const messages: Record<string, string> = {
    VOICE_DELEGATION_REQUIRED: "소유자가 웹에서 음성 홈캠 사용을 허용해야 합니다.",
    VOICE_CREDENTIAL_REVOKED: "장치 인증이 만료되거나 해제되었습니다.",
    VOICE_REFERENCE_NOT_FOUND: "이 장치에 속한 기록을 찾을 수 없습니다.",
    CAMERA_DISABLED: "카메라를 켠 뒤 모니터링을 시작해 주세요.",
  };
  return { success: false, code, result: {}, message: messages[code] ?? "음성 요청을 처리하지 못했습니다." };
}
async function audit(client: PoolClient, deviceId: string, actorType: string, actorId: string,
  action: string, metadata: unknown) {
  await client.query(`INSERT INTO access_audit_log(id,device_id,actor_type,actor_id,action,metadata_json)
    VALUES($1,$2,$3,$4,$5,$6)`, [randomUUID(), deviceId, actorType, actorId, action, JSON.stringify(metadata)]);
}

export async function readVoiceDelegation(deviceId: string) {
  await requireSchema();
  const result = await getPostgresPool().query(`SELECT d.enabled AND m.role='owner' AS enabled,d.updated_at
    FROM device_voice_delegations d LEFT JOIN device_memberships m
      ON m.device_id=d.device_id AND m.user_id=d.granted_by WHERE d.device_id=$1`, [deviceId]);
  return { enabled: result.rows[0]?.enabled === true, updatedAt: result.rows[0]?.updated_at ?? null };
}

export async function saveVoiceDelegation(deviceId: string, userId: string, enabled: boolean) {
  await requireSchema();
  const client = await getPostgresPool().connect();
  try {
    await client.query("BEGIN");
    const owner = await client.query("SELECT role FROM device_memberships WHERE device_id=$1 AND user_id=$2 FOR SHARE", [deviceId, userId]);
    if (owner.rows[0]?.role !== "owner") throw new Error("VOICE_FORBIDDEN");
    await client.query(`INSERT INTO device_voice_delegations(device_id,enabled,granted_by) VALUES($1,$2,$3)
      ON CONFLICT(device_id) DO UPDATE SET enabled=$2,granted_by=$3,updated_at=clock_timestamp()`, [deviceId, enabled, userId]);
    if (!enabled) await client.query(`UPDATE device_voice_requests SET state='failed',reply_json=$2,completed_at=clock_timestamp()
      WHERE device_id=$1 AND state='pending' AND requires_delegation`, [deviceId, JSON.stringify(failure("VOICE_DELEGATION_REQUIRED"))]);
    await audit(client, deviceId, "user", userId, "voice.delegation", { enabled });
    await client.query("COMMIT");
    return { enabled };
  } catch (error) { await client.query("ROLLBACK"); throw error; }
  finally { client.release(); }
}

export async function listVoiceHistory(deviceId: string): Promise<VoiceHistory[]> {
  await requireSchema();
  const result = await getPostgresPool().query(`SELECT request_id AS "requestId",operation,state,
    created_at AS "createdAt",reply_json FROM device_voice_requests WHERE device_id=$1
    ORDER BY created_at DESC,request_id DESC LIMIT 20`, [deviceId]);
  return result.rows.map(({ reply_json, ...row }) => ({ ...row, reply: reply_json ? JSON.parse(reply_json) : null })) as VoiceHistory[];
}

export async function operateVoiceDevice(device: DeviceIdentity, input: VoiceRequest): Promise<VoiceReply> {
  const request = parseVoiceRequest(input);
  if (!request) throw new Error("VOICE_INVALID_REQUEST");
  await requireSchema();
  const argsJson = JSON.stringify(request.arguments), delegated = requiresVoiceDelegation(request);
  // Save intent before execution. A revoked delegation cancels pending intents;
  // execution locks and checks it again, including requests received earlier.
  await getPostgresPool().query(`INSERT INTO device_voice_requests
    (device_id,request_id,credential_id,operation,arguments_json,requires_delegation)
    VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT DO NOTHING`,
  [device.deviceId, request.requestId, device.credentialId, request.operation, argsJson, delegated]);
  const client = await getPostgresPool().connect();
  try {
    await client.query("BEGIN");
    const grant = delegated ? await client.query(`SELECT enabled,granted_by FROM device_voice_delegations
      WHERE device_id=$1 FOR SHARE`, [device.deviceId]) : null;
    const owner = grant?.rows[0]?.enabled ? await client.query(`SELECT role FROM device_memberships
      WHERE device_id=$1 AND user_id=$2 FOR SHARE`, [device.deviceId, grant.rows[0].granted_by]) : null;
    const credential = await client.query(`SELECT 1 FROM device_credentials WHERE id=$1 AND device_id=$2
      AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at>clock_timestamp()) FOR SHARE`,
    [device.credentialId, device.deviceId]);
    const previous = (await client.query(`SELECT * FROM device_voice_requests
      WHERE device_id=$1 AND request_id=$2 FOR UPDATE`, [device.deviceId, request.requestId])).rows[0];
    if (previous.operation !== request.operation || previous.arguments_json !== argsJson) throw new Error("VOICE_REQUEST_CONFLICT");
    const refusal = !credential.rowCount ? failure("VOICE_CREDENTIAL_REVOKED")
      : delegated && owner?.rows[0]?.role !== "owner" ? failure("VOICE_DELEGATION_REQUIRED") : null;
    if (!refusal && previous.reply_json) {
      await client.query("COMMIT");
      return JSON.parse(previous.reply_json);
    }
    let reply = refusal;
    if (!reply) {
      await client.query("SAVEPOINT voice_operation");
      try {
        const result = await execute(client, device.deviceId, request);
        reply = { success: true, code: request.operation === "homecam_settings" ? "SETTINGS_SAVED" : "OK",
          result, message: request.operation === "homecam_settings"
            ? "설정을 저장했습니다. 로봇 적용 회신은 별도로 확인합니다." : "요청 결과를 저장했습니다." };
      } catch (error) {
        await client.query("ROLLBACK TO SAVEPOINT voice_operation");
        const code = error instanceof Error ? error.message : "VOICE_OPERATION_FAILED";
        if (!["VOICE_REFERENCE_NOT_FOUND", "CAMERA_DISABLED"].includes(code)) throw error;
        reply = failure(code);
      }
    }
    // Do not overwrite an already executed result just because a later retry
    // arrives after revocation; that retry still receives the refusal.
    if (!previous.reply_json) {
      await client.query(`UPDATE device_voice_requests SET state=$3,reply_json=$4,completed_at=clock_timestamp()
        WHERE device_id=$1 AND request_id=$2`, [device.deviceId, request.requestId,
        reply.success ? "completed" : "failed", JSON.stringify(reply)]);
      await audit(client, device.deviceId, "device", device.credentialId, "voice.operate",
        { requestId: request.requestId, operation: request.operation, code: reply.code });
    }
    await client.query("COMMIT");
    return reply;
  } catch (error) { await client.query("ROLLBACK"); throw error; }
  finally { client.release(); }
}

function resultLink(deviceId: string, requestId: string) {
  return `/?device=${encodeURIComponent(deviceId)}&view=robot#voice-result-${encodeURIComponent(requestId)}`;
}
function referenceLink(deviceId: string, kind: string, referenceId: string) {
  return `/voice-results/${encodeURIComponent(deviceId)}/${kind}/${encodeURIComponent(referenceId)}`;
}
async function execute(client: PoolClient, deviceId: string, request: VoiceRequest): Promise<Record<string, unknown>> {
  const args = request.arguments, limit = Number(args.limit ?? 10);
  const href = resultLink(deviceId, request.requestId);
  if (request.operation === "homecam_settings") return saveSettings(client, deviceId, args);
  if (request.operation === "homecam_status") {
    await client.query("INSERT INTO device_state(device_id) VALUES($1) ON CONFLICT DO NOTHING", [deviceId]);
    const row = (await client.query(`SELECT camera_enabled=1 AS "cameraEnabled",microphone_enabled=1 AS "microphoneEnabled",
      monitoring_enabled=1 AS "monitoringEnabled",fall_enabled AS "fallEnabled",fall_cloud_consent AS "cloudConsent",
      fall_settings_revision::text AS "settingsRevision",media_settings_revision::text AS "mediaSettingsRevision",
      EXTRACT(EPOCH FROM clock_timestamp()-fall_settings_saved_at)::float8 AS "fallSavedAgeS",
      EXTRACT(EPOCH FROM clock_timestamp()-media_settings_saved_at)::float8 AS "mediaSavedAgeS",last_seen_at AS "lastSeenAt",
      p2p_healthy=1 AS "p2pHealthy",storage_healthy=1 AS "storageHealthy",detector_healthy=1 AS "detectorHealthy"
      FROM device_state WHERE device_id=$1`, [deviceId])).rows[0];
    const reports = await client.query(`SELECT payload_json,received_at AS "receivedAt",
      first_report_age_s+GREATEST(0,EXTRACT(EPOCH FROM clock_timestamp()-received_at))::float8 AS "reportAgeS"
      FROM fall_settings_reports WHERE device_id=$1 AND requested_revision=$2::numeric
      ORDER BY received_at DESC LIMIT 5`, [deviceId, row.settingsRevision]);
    const media = await client.query(`SELECT payload_json,received_at AS "receivedAt",
      report_age_s+GREATEST(0,EXTRACT(EPOCH FROM clock_timestamp()-received_at))::float8 AS "reportAgeS"
      FROM device_media_settings_reports WHERE device_id=$1 AND requested_revision=$2::numeric
      ORDER BY received_at DESC LIMIT 1`, [deviceId, row.mediaSettingsRevision]);
    const unpack = ({ payload_json, ...report }: { payload_json: string }) => ({ ...JSON.parse(payload_json), ...report });
    return { ...row, runtimeVerified: false,
      mediaApplyReceipt: applyReceipt(media.rows[0] ? unpack(media.rows[0]) : undefined, row.mediaSavedAgeS),
      fallApplyReceipt: applyReceipt(reports.rows[0] ? unpack(reports.rows[0]) : undefined, row.fallSavedAgeS),
      reports: reports.rows.map(unpack), href };
  }
  if (request.operation === "homecam_events") {
    const rows = await client.query(`SELECT id,event_type AS "eventType",confidence,occurred_at AS "occurredAt",
      recording_session_id AS "recordingId" FROM homecam_events WHERE device_id=$1
      AND occurred_at>clock_timestamp()-INTERVAL '7 days' AND ($3::text IS NULL OR event_type=$3)
      ORDER BY occurred_at DESC,id DESC LIMIT $2`, [deviceId, limit, args.eventType ?? null]);
    return { events: rows.rows.map((row) => ({ ...row,
      href: referenceLink(deviceId, "event", row.id) })), href };
  }
  if (request.operation === "homecam_recordings") {
    const rows = await client.query(`SELECT r.session_id AS id,r.started_at AS "startedAt",r.ended_at AS "endedAt"
      FROM recording_sessions r JOIN stream_sessions s ON s.id=r.session_id WHERE s.device_id=$1
      AND r.started_at IS NOT NULL AND COALESCE(r.ended_at,clock_timestamp())>clock_timestamp()-INTERVAL '7 days'
      ORDER BY r.started_at DESC,r.session_id DESC LIMIT $2`, [deviceId, limit]);
    return { recordings: rows.rows.map((row) => ({ ...row, href: referenceLink(deviceId, "recording", row.id) })), href };
  }
  if (request.operation === "homecam_falls") {
    const rows = await client.query(`SELECT incident_id AS id,state,assessment,answer,fall_seen AS "fallSeen",
      occurred_at AS "occurredAt",updated_at AS "updatedAt" FROM fall_incidents WHERE device_id=$1
      ORDER BY updated_at DESC,incident_id DESC LIMIT $2`, [deviceId, limit]);
    return { incidents: rows.rows.map((row) => ({ ...row, href: referenceLink(deviceId, "fall", row.id) })), href };
  }
  if (args.kind === "map") {
    const map = (await client.query("SELECT revision FROM robot_maps WHERE device_id=$1 AND map_id=$2", [deviceId, args.referenceId])).rows[0];
    if (!map) throw new Error("VOICE_REFERENCE_NOT_FOUND");
    return { resultId: request.requestId, ...args, href,
      referenceHref: `/api/devices/${encodeURIComponent(deviceId)}/robot/map?revision=${encodeURIComponent(map.revision)}` };
  }
  if (["event", "recording", "fall"].includes(String(args.kind))) {
    const queries: Record<string, string> = {
      event: "SELECT 1 FROM homecam_events WHERE device_id=$1 AND id=$2",
      recording: "SELECT 1 FROM recording_sessions r JOIN stream_sessions s ON s.id=r.session_id WHERE s.device_id=$1 AND r.session_id=$2",
      fall: "SELECT 1 FROM fall_incidents WHERE device_id=$1 AND incident_id=$2",
    };
    if (!(await client.query(queries[String(args.kind)], [deviceId, args.referenceId])).rowCount) throw new Error("VOICE_REFERENCE_NOT_FOUND");
  }
  return { resultId: request.requestId, ...args, href,
    ...(["recording", "fall"].includes(String(args.kind)) ? {
      referenceHref: referenceLink(deviceId, String(args.kind), String(args.referenceId)) } : {}),
    ...(args.kind === "homecam" ? { referenceHref: `/?device=${encodeURIComponent(deviceId)}&view=settings` } : {}),
    ...(args.kind === "event" ? { referenceHref: referenceLink(deviceId, "event", String(args.referenceId)) } : {}) };
}

async function saveSettings(client: PoolClient, deviceId: string, args: Record<string, unknown>) {
  await client.query("INSERT INTO device_state(device_id) VALUES($1) ON CONFLICT DO NOTHING", [deviceId]);
  const old = (await client.query("SELECT * FROM device_state WHERE device_id=$1 FOR UPDATE", [deviceId])).rows[0];
  const camera = args.cameraEnabled ?? old.camera_enabled === 1;
  const monitoring = args.monitoringEnabled ?? old.monitoring_enabled === 1;
  if (args.monitoringEnabled === true && !camera) throw new Error("CAMERA_DISABLED");
  const mediaChange = args.cameraEnabled !== undefined || args.monitoringEnabled !== undefined;
  const row = (await client.query(`UPDATE device_state SET camera_enabled=$2,microphone_enabled=$3,
    monitoring_enabled=$4,fall_enabled=$5,updated_at=clock_timestamp(),
    media_healthy=CASE WHEN $6 THEN 0 ELSE media_healthy END,
    storage_healthy=CASE WHEN $6 THEN 0 ELSE storage_healthy END,
    p2p_healthy=CASE WHEN $7 THEN 0 ELSE p2p_healthy END
    WHERE device_id=$1 RETURNING fall_settings_revision::text AS revision,media_settings_revision::text AS "mediaRevision"`,
  [deviceId, Number(camera), Number(args.microphoneEnabled ?? old.microphone_enabled === 1),
    Number(camera && monitoring), args.fallEnabled ?? old.fall_enabled, mediaChange, args.cameraEnabled !== undefined])).rows[0];
  // End the same media leases as the existing owner settings endpoint, in the
  // transaction holding the delegation lock. No viewer URL or AWS secret leaves it.
  const ending = [!camera ? old.p2p_session_id : null, !camera || !monitoring ? old.storage_session_id : null].filter(Boolean);
  if (ending.length) {
    await client.query("UPDATE recording_sessions SET ended_at=COALESCE(ended_at,clock_timestamp()) WHERE session_id=ANY($1::text[])", [ending]);
    await client.query("UPDATE stream_sessions SET status='ended',ended_at=clock_timestamp() WHERE id=ANY($1::text[]) AND status='active'", [ending]);
    const p2p = ending.includes(old.p2p_session_id) ? null : old.p2p_session_id;
    const storage = ending.includes(old.storage_session_id) ? null : old.storage_session_id;
    await client.query(`UPDATE device_state SET p2p_session_id=$2,storage_session_id=$3,active_session_id=$4,
      active_stream_mode=$5,p2p_healthy=CASE WHEN $2::text IS NULL THEN 0 ELSE p2p_healthy END,
      storage_healthy=CASE WHEN $3::text IS NULL THEN 0 ELSE storage_healthy END,
      media_healthy=CASE WHEN $3::text IS NOT NULL THEN storage_healthy WHEN $2::text IS NOT NULL THEN p2p_healthy ELSE 0 END
      WHERE device_id=$1`, [deviceId, p2p, storage, storage ?? p2p, storage ? "storage" : p2p ? "p2p" : "idle"]);
  }
  return { saved: true, savedRevision: row.revision, mediaSettingsRevision: row.mediaRevision,
    receiptState: "waiting", runtimeVerified: false,
    desiredState: { cameraEnabled: camera, monitoringEnabled: !!camera && !!monitoring,
      microphoneEnabled: args.microphoneEnabled ?? old.microphone_enabled === 1,
      fallEnabled: args.fallEnabled ?? old.fall_enabled } };
}
