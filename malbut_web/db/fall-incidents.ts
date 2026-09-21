import { randomUUID } from "node:crypto";
import { getPostgresPool } from "./postgres";
import { parseFallEvent, type FallEventInput } from "../app/fall-contract";

const rank = { info: 1, check: 2, urgent: 3 };
const LEVEL_RANK_SQL = "CASE level WHEN 'info' THEN 1 WHEN 'check' THEN 2 ELSE 3 END";

export async function ensureFallSchema() {
  const result = await getPostgresPool().query(
    "SELECT 1 FROM homecam_schema_migrations WHERE version = '0009_fall_incidents'",
  );
  if (!result.rowCount) throw new Error("FALL_MIGRATION_REQUIRED");
}

export async function storeFallEvent(deviceId: string, input: FallEventInput) {
  const event = parseFallEvent(input);
  if (!event) throw new Error("FALL_EVENT_INVALID");
  await ensureFallSchema();
  const client = await getPostgresPool().connect();
  const json = JSON.stringify(event);
  try {
    await client.query("BEGIN");
    // Serializes one device's event IDs as well as incident projections.
    await client.query("SELECT pg_advisory_xact_lock(hashtext($1))", [`fall:${deviceId}`]);
    const existing = await client.query(
      "SELECT payload_json FROM fall_incident_events WHERE device_id=$1 AND event_id=$2",
      [deviceId, event.eventId],
    );
    if (existing.rowCount) {
      if (existing.rows[0].payload_json !== json) throw new Error("FALL_IDEMPOTENCY_CONFLICT");
      await client.query("COMMIT");
      return { stored: true, created: false, eventId: event.eventId };
    }
    await client.query(
      `INSERT INTO fall_incidents(device_id,incident_id,boot_id,evidence_revision,state,occurred_at)
       VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT DO NOTHING`,
      [deviceId, event.incidentId, event.bootId, event.evidenceRevision, event.state, event.occurredAt],
    );
    const current = (await client.query(
      "SELECT * FROM fall_incidents WHERE device_id=$1 AND incident_id=$2 FOR UPDATE",
      [deviceId, event.incidentId],
    )).rows[0];
    if (current.boot_id !== event.bootId) throw new Error("FALL_BOOT_CONFLICT");
    if (event.reason === "normal_verified" && current.fall_seen) throw new Error("FALL_STATE_CONFLICT");
    if ((await client.query(
      "SELECT 1 FROM fall_incident_events WHERE device_id=$1 AND incident_id=$2 AND sequence=$3",
      [deviceId, event.incidentId, event.sequence],
    )).rowCount) throw new Error("FALL_SEQUENCE_CONFLICT");
    await client.query(
      `INSERT INTO fall_incident_events(device_id,event_id,incident_id,sequence,payload_json)
       VALUES($1,$2,$3,$4,$5)`, [deviceId, event.eventId, event.incidentId, event.sequence, json],
    );
    const noticeRank = event.notificationLevel ? rank[event.notificationLevel] : 0;
    await client.query(
      `UPDATE fall_incidents SET
         fall_seen=fall_seen OR $3, notification_rank=GREATEST(notification_rank,$4),
         state=CASE WHEN $5>latest_sequence THEN
           CASE WHEN state='help_required' AND $6<>'resolved' THEN state ELSE $6 END ELSE state END,
         evidence_revision=CASE WHEN $5>latest_sequence THEN $7 ELSE evidence_revision END,
         assessment=CASE WHEN $5>latest_sequence THEN $8 ELSE assessment END,
         answer=CASE WHEN $5>latest_sequence THEN $9 ELSE answer END,
         latest_sequence=GREATEST(latest_sequence,$5),
         occurred_at=LEAST(occurred_at,$10), updated_at=CURRENT_TIMESTAMP
       WHERE device_id=$1 AND incident_id=$2`,
      [deviceId, event.incidentId, event.fallSeen || event.assessment === "observed_fall",
        noticeRank, event.sequence, event.state, event.evidenceRevision,
        event.assessment, event.answer, event.occurredAt],
    );
    if (event.notificationLevel) {
      await client.query(
        `INSERT INTO fall_push_outbox(device_id,notification_id,incident_id,level,reason,occurred_at,status)
         VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT(device_id,incident_id,level) DO NOTHING`,
        [deviceId, event.eventId, event.incidentId, event.notificationLevel, event.reason, event.occurredAt,
          noticeRank < current.notification_rank ? "superseded" : "pending"],
      );
      await client.query(
        `UPDATE fall_push_outbox SET status='superseded',lease_id=NULL,lease_until=NULL
         WHERE device_id=$1 AND incident_id=$2 AND status='pending' AND (${LEVEL_RANK_SQL})<$3`,
        [deviceId, event.incidentId, Math.max(noticeRank, current.notification_rank)],
      );
    }
    await client.query("COMMIT");
    return { stored: true, created: true, eventId: event.eventId };
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  } finally { client.release(); }
}

export type ClaimedFallPush = {
  deviceId: string; notificationId: string; incidentId: string;
  level: "info" | "check" | "urgent";
  reason: "fall_observed_person_okay" | "person_no_response" | "check_required_not_confirmed_fall" | "help_requested";
  occurredAt: string; leaseId: string; subscriptionResults: Record<string, number>;
};

export async function claimFallPush(deviceId?: string, notificationId?: string): Promise<ClaimedFallPush | null> {
  await ensureFallSchema();
  const leaseId = randomUUID();
  const result = await getPostgresPool().query(
    `WITH candidate AS (
       SELECT device_id,notification_id FROM fall_push_outbox
       WHERE status='pending' AND next_attempt_at<=CURRENT_TIMESTAMP
         AND (lease_until IS NULL OR lease_until<=CURRENT_TIMESTAMP)
         AND ($1::text IS NULL OR device_id=$1) AND ($2::text IS NULL OR notification_id=$2)
       ORDER BY CASE level WHEN 'urgent' THEN 0 WHEN 'check' THEN 1 ELSE 2 END,created_at
       FOR UPDATE SKIP LOCKED LIMIT 1
     ) UPDATE fall_push_outbox p SET lease_id=$3,lease_until=CURRENT_TIMESTAMP+INTERVAL '120 seconds',
       attempt_count=attempt_count+1
       FROM candidate c WHERE p.device_id=c.device_id AND p.notification_id=c.notification_id
       RETURNING p.*`, [deviceId ?? null, notificationId ?? null, leaseId],
  );
  if (!result.rowCount) return null;
  const row = result.rows[0];
  return { deviceId: row.device_id, notificationId: row.notification_id, incidentId: row.incident_id,
    level: row.level, reason: row.reason, occurredAt: row.occurred_at, leaseId,
    subscriptionResults: row.subscription_results };
}

export async function recordFallPushResults(claim: ClaimedFallPush, results: Array<{ subscriptionId: string; status: number }>) {
  const receipts = Object.fromEntries(results.map((r) => [r.subscriptionId, r.status]));
  const result = await getPostgresPool().query(
    `UPDATE fall_push_outbox SET subscription_results=subscription_results || $4::jsonb,
       lease_until=CURRENT_TIMESTAMP+INTERVAL '120 seconds'
     WHERE device_id=$1 AND notification_id=$2 AND lease_id=$3 AND status='pending'
       AND lease_until>CURRENT_TIMESTAMP`,
    [claim.deviceId, claim.notificationId, claim.leaseId, JSON.stringify(receipts)],
  );
  if (!result.rowCount) throw new Error("FALL_PUSH_LEASE_LOST");
}

export async function finishFallPush(claim: ClaimedFallPush, complete: boolean, error: string | null) {
  const result = await getPostgresPool().query(
    `UPDATE fall_push_outbox SET status=CASE WHEN $4 THEN 'accepted' ELSE 'pending' END,
       accepted_at=CASE WHEN $4 THEN CURRENT_TIMESTAMP ELSE NULL END,
       lease_id=NULL,lease_until=NULL,last_error=$5,
       next_attempt_at=CURRENT_TIMESTAMP+INTERVAL '1 second' * LEAST(300,5*POWER(2,LEAST(attempt_count,6)))
     WHERE device_id=$1 AND notification_id=$2 AND lease_id=$3 AND status='pending'
       AND lease_until>CURRENT_TIMESTAMP`,
    [claim.deviceId, claim.notificationId, claim.leaseId, complete, error],
  );
  return !!result.rowCount;
}

export async function listFallIncidents(deviceId: string) {
  await ensureFallSchema();
  return (await getPostgresPool().query(
    `SELECT incident_id AS "incidentId",state,fall_seen AS "fallSeen",assessment,answer,
       evidence_revision AS "evidenceRevision",notification_rank AS "notificationRank",
       occurred_at AS "occurredAt",updated_at AS "updatedAt"
     FROM fall_incidents WHERE device_id=$1 ORDER BY updated_at DESC,incident_id LIMIT 50`, [deviceId],
  )).rows;
}

export async function allowFallUpload(deviceId: string) {
  const minute = Math.floor(Date.now() / 60000) * 60000;
  const result = await getPostgresPool().query(
    `INSERT INTO request_rate_limits(rate_key,window_started_at,request_count) VALUES($1,$2,1)
     ON CONFLICT(rate_key) DO UPDATE SET window_started_at=excluded.window_started_at,
       request_count=CASE WHEN request_rate_limits.window_started_at=excluded.window_started_at
       THEN request_rate_limits.request_count+1 ELSE 1 END RETURNING request_count`, [`fall-events:${deviceId}`, minute],
  );
  return result.rows[0].request_count <= 120;
}
