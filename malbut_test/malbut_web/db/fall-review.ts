import { randomUUID } from "node:crypto";
import { getPostgresPool, type SqlExecutor } from "./postgres";
import { quietRecordingEnd } from "./recording-end";
import { labelFor, userLabels } from "./users";
import type { FallClipInput } from "../app/fall-clip-contract";

// Clip ranges, human review and [재발신] reminders. Robot events and their
// first notifications stay in db/fall-incidents.ts; nothing here changes the
// robot's automatic judgment.

export const OPINION_LABELS = ["fall", "suspected_fall", "normal"] as const;
export type OpinionLabel = (typeof OPINION_LABELS)[number];
export const INCIDENT_FILTERS = ["all", "check", "closed", "normal", "report"] as const;
export type IncidentFilter = (typeof INCIDENT_FILTERS)[number];

export const RECORDING_RETENTION_MS = 7 * 24 * 60 * 60 * 1000;
export const REPORT_PRE_MS = 10_000;
export const REPORT_POST_MS = 20_000;
// Recently ended ranges may still be uploading to the recording archive.
const CLIP_SETTLE_MS = 15_000;
// Counts include the first notification.
export const REMINDER_RULES = {
  urgent: { intervalMs: 120_000, total: 3 },
  check: { intervalMs: 180_000, total: 2 },
} as const;
const LEVEL_RANK_SQL = "CASE level WHEN 'info' THEN 1 WHEN 'check' THEN 2 ELSE 3 END";
// Retain the source timeline/clips. Association closes its automatic queue,
// not the user's review, and is never a normal-activity judgment.
const MERGED_TARGETS_SQL = `(SELECT e.payload_json::jsonb->'mergedIntoIncidentIds'
  FROM fall_incident_events e WHERE e.device_id=i.device_id AND e.incident_id=i.incident_id
    AND e.payload_json::jsonb->>'eventKind'='incident_merged'
  ORDER BY e.sequence DESC LIMIT 1)`;
const ROBOT_MERGED_SQL = `(i.origin='robot' AND i.state='resolved' AND ${MERGED_TARGETS_SQL} IS NOT NULL)`;
const ROBOT_NORMAL_SQL =
  "(i.origin='robot' AND i.state='resolved' AND i.assessment='normal_activity' AND NOT i.fall_seen)";
// A reopened incident needs a human again even if the robot judged it normal.
const NEEDS_CHECK_SQL = `((NOT ${ROBOT_NORMAL_SQL} AND NOT ${ROBOT_MERGED_SQL}) OR i.reopened_at IS NOT NULL)`;
const FILTER_SQL: Record<IncidentFilter, string> = {
  all: "TRUE",
  check: `i.review_state='open' AND i.origin='robot' AND ${NEEDS_CHECK_SQL}`,
  closed: `(i.review_state='closed' OR (${ROBOT_MERGED_SQL} AND i.reopened_at IS NULL))`,
  normal: ROBOT_NORMAL_SQL,
  report: "i.origin='user_report'",
};
const ANALYSIS_KINDS = ["analysis_completed", "analysis_unavailable", "recheck_unavailable"];

type Queryable = SqlExecutor;

export async function ensureFallReviewSchema() {
  const result = await getPostgresPool().query(
    `SELECT count(*)::int AS n FROM homecam_schema_migrations
     WHERE version IN ('0012_fall_incident_review','0014_fall_report_memo')`,
  );
  if (result.rows[0]?.n !== 2) throw new Error("FALL_REVIEW_MIGRATION_REQUIRED");
}

async function transaction<T>(deviceId: string, work: (client: Queryable) => Promise<T>) {
  await ensureFallReviewSchema();
  const client = await getPostgresPool().connect();
  try {
    await client.query("BEGIN");
    // Same lock as robot event ingestion: one writer per device.
    await client.query("SELECT pg_advisory_xact_lock(hashtext($1))", [`fall:${deviceId}`]);
    const result = await work(client);
    await client.query("COMMIT");
    return result;
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  } finally { client.release(); }
}

function iso(value: unknown) {
  return value == null ? null : new Date(value as string).toISOString();
}

// ---------------------------------------------------------------- robot clips

/** Newest revision wins; an older or repeated revision is acknowledged unchanged. */
export async function storeFallClip(deviceId: string, clip: FallClipInput) {
  const json = JSON.stringify(clip);
  return transaction(deviceId, async (db) => {
    const incident = (await db.query(
      "SELECT origin,boot_id FROM fall_incidents WHERE device_id=$1 AND incident_id=$2 FOR UPDATE",
      [deviceId, clip.incidentId],
    )).rows[0];
    // The incident event may still be in the robot's queue; retry, do not block.
    if (!incident) throw new Error("FALL_CLIP_INCIDENT_MISSING");
    if (incident.origin !== "robot" || incident.boot_id !== clip.bootId) throw new Error("FALL_CLIP_CONFLICT");
    const existing = (await db.query(
      "SELECT revision,payload_json FROM fall_incident_clips WHERE device_id=$1 AND incident_id=$2 AND segment_index=$3",
      [deviceId, clip.incidentId, clip.segmentIndex],
    )).rows[0];
    const ack = { stored: true, incidentId: clip.incidentId, segmentIndex: clip.segmentIndex, revision: clip.revision };
    if (existing && existing.revision > clip.revision) return { ...ack, created: false };
    if (existing && existing.revision === clip.revision) {
      if (existing.payload_json !== json) throw new Error("FALL_CLIP_CONFLICT");
      return { ...ack, created: false };
    }
    await db.query(
      `INSERT INTO fall_incident_clips(device_id,incident_id,segment_index,revision,boot_id,start_at,end_at,
         anchor_kinds,found_down,clock_stepped,payload_json)
       VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10,$11)
       ON CONFLICT(device_id,incident_id,segment_index) DO UPDATE SET revision=excluded.revision,
         start_at=excluded.start_at,end_at=excluded.end_at,anchor_kinds=excluded.anchor_kinds,
         found_down=excluded.found_down,clock_stepped=excluded.clock_stepped,
         payload_json=excluded.payload_json,updated_at=CURRENT_TIMESTAMP`,
      [deviceId, clip.incidentId, clip.segmentIndex, clip.revision, clip.bootId, clip.startAt, clip.endAt,
        JSON.stringify(clip.anchorKinds), clip.foundDown, clip.clockStepped, json],
    );
    return { ...ack, created: !existing };
  });
}

// ---------------------------------------------------------------- reading

const SUMMARY_COLUMNS = `
  i.incident_id,i.origin,i.state,i.fall_seen,i.assessment,i.answer,i.notification_rank,
  i.occurred_at,i.updated_at,i.review_state,i.closed_at,i.closed_by,i.reopened_at,
  i.unacknowledged_since,i.reported_by,i.reported_moment_at,i.report_memo,
  ${ROBOT_NORMAL_SQL} AS robot_normal,
  ${MERGED_TARGETS_SQL} AS merged_targets,
  (SELECT e.payload_json::jsonb->>'eventKind' FROM fall_incident_events e
    WHERE e.device_id=i.device_id AND e.incident_id=i.incident_id
      AND e.payload_json::jsonb->>'eventKind' = ANY($2::text[])
    ORDER BY e.sequence DESC LIMIT 1) AS last_analysis_kind,
  EXISTS(SELECT 1 FROM fall_incident_clips c WHERE c.device_id=i.device_id
    AND c.incident_id=i.incident_id AND c.found_down) AS found_down,
  (SELECT COALESCE(jsonb_object_agg(label,n),'{}'::jsonb) FROM
    (SELECT label,count(*)::int AS n FROM fall_incident_opinions o
      WHERE o.device_id=i.device_id AND o.incident_id=i.incident_id GROUP BY label) t) AS opinion_counts`;

type SummaryRow = {
  incident_id: string; origin: "robot" | "user_report"; state: string | null; fall_seen: boolean;
  assessment: string | null; answer: string | null; notification_rank: number;
  occurred_at: unknown; updated_at: unknown; review_state: "open" | "closed"; closed_at: unknown;
  closed_by: string | null; reopened_at: unknown; unacknowledged_since: unknown;
  reported_by: string | null; reported_moment_at: unknown; report_memo: string | null; robot_normal: boolean;
  last_analysis_kind: string | null; found_down: boolean; opinion_counts: Record<string, number> | null;
  merged_targets: string[] | null;
};

function summary(row: SummaryRow, names: Map<string, string> = new Map()) {
  const open = row.review_state === "open";
  const aiFailed = row.origin === "robot" && (row.assessment === "unobservable" ||
    row.last_analysis_kind === "analysis_unavailable" || row.last_analysis_kind === "recheck_unavailable");
  const category = row.origin === "user_report" ? "report"
    : row.merged_targets?.length && row.reopened_at === null ? "merged"
    : row.robot_normal && row.reopened_at === null ? "normal" : "check";
  return {
    incidentId: row.incident_id, origin: row.origin, category,
    mergedIntoIncidentIds: row.merged_targets ?? [],
    state: row.state, fallSeen: row.fall_seen, assessment: row.assessment, answer: row.answer,
    notificationRank: row.notification_rank, occurredAt: iso(row.occurred_at), updatedAt: iso(row.updated_at),
    reviewState: row.review_state, closedAt: iso(row.closed_at), closedBy: row.closed_by,
    closedByName: labelFor(names, row.closed_by),
    reopenedAt: iso(row.reopened_at),
    // Display labels; none of these change the robot's automatic judgment.
    needsCheck: open && category === "check",
    aiFailed,
    unacknowledged: open && category !== "merged" && row.unacknowledged_since !== null,
    reviewPending: open && category === "normal",
    foundDown: row.found_down,
    reportedBy: row.reported_by, reportedByName: labelFor(names, row.reported_by),
    reportedMomentAt: iso(row.reported_moment_at), reportMemo: row.report_memo,
    opinionCounts: row.opinion_counts ?? {},
  };
}

export async function listFallIncidentSummaries(deviceId: string, filter: IncidentFilter = "all") {
  await ensureFallReviewSchema();
  const rows = (await getPostgresPool().query(
    `SELECT ${SUMMARY_COLUMNS} FROM fall_incidents i
     WHERE i.device_id=$1 AND ${FILTER_SQL[filter]}
     ORDER BY (i.review_state='open' AND i.unacknowledged_since IS NOT NULL) DESC,
       i.occurred_at DESC,i.incident_id LIMIT 50`, [deviceId, ANALYSIS_KINDS],
  )).rows;
  if (!rows.length) return [];
  const pool = getPostgresPool();
  const ids = rows.map((row) => row.incident_id);
  // List cards show the scene state, same-scene incidents and how many alerts went out.
  const clips = (await pool.query(
    `SELECT incident_id,start_at,end_at FROM fall_incident_clips
     WHERE device_id=$1 AND incident_id=ANY($2::text[]) ORDER BY segment_index`, [deviceId, ids],
  )).rows;
  const spans = clips.length ? await recordingSpans(deviceId,
    new Date(Math.min(...clips.map((c) => Date.parse(iso(c.start_at)!)))).toISOString(),
    new Date(Math.max(...clips.map((c) => Date.parse(iso(c.end_at)!)))).toISOString()) : [];
  const linked = new Map((await pool.query(
    `SELECT mine.incident_id,count(DISTINCT o.incident_id)::int AS n FROM fall_incident_clips mine
     JOIN fall_incident_clips o ON o.device_id=mine.device_id AND o.incident_id<>mine.incident_id
       AND o.start_at<mine.end_at AND o.end_at>mine.start_at
     WHERE mine.device_id=$1 AND mine.incident_id=ANY($2::text[]) GROUP BY mine.incident_id`, [deviceId, ids],
  )).rows.map((r) => [r.incident_id, r.n as number]));
  const levels = new Map((await pool.query(
    `SELECT incident_id,(array_agg(level ORDER BY ${LEVEL_RANK_SQL} DESC))[1] AS level FROM fall_push_outbox
     WHERE device_id=$1 AND incident_id=ANY($2::text[]) AND status<>'superseded' GROUP BY incident_id`, [deviceId, ids],
  )).rows.map((r) => [r.incident_id, r.level as string]));
  const resends = new Map((await pool.query(
    `SELECT incident_id,count(*)::int AS n FROM fall_web_notices WHERE device_id=$1
       AND incident_id=ANY($2::text[]) AND kind='resend' AND status='accepted' GROUP BY incident_id`, [deviceId, ids],
  )).rows.map((r) => [r.incident_id, r.n as number]));
  const names = await userLabels(rows.flatMap((row) => [row.closed_by, row.reported_by]));
  return rows.map((row) => {
    const first = clips.find((c) => c.incident_id === row.incident_id);
    const level = levels.get(row.incident_id) ?? null;
    const total = level === "urgent" ? 3 : level === "check" ? 2 : 1;
    return {
      ...summary(row, names),
      sceneState: first ? clipPlaybackState(iso(first.start_at)!, iso(first.end_at)!, spans) : null,
      linkedCount: linked.get(row.incident_id) ?? 0,
      // "알림: 긴급 · 3/3회 발송": first notification plus delivered [재발신].
      notification: level ? { level, sent: Math.min(total, 1 + (resends.get(row.incident_id) ?? 0)), total } : null,
    };
  });
}

type RecordingSpan = { start: number; end: number | null; streamArn: string };

async function recordingSpans(deviceId: string, from: string, to: string, now = Date.now()): Promise<RecordingSpan[]> {
  return (await getPostgresPool().query(
    `SELECT COALESCE(r.started_at,s.started_at) AS started_at,r.ended_at,r.kvs_stream_arn,s.expires_at FROM recording_sessions r
     JOIN stream_sessions s ON s.id=r.session_id
     WHERE s.device_id=$1 AND COALESCE(r.started_at,s.started_at)<$3
       AND (r.ended_at IS NULL OR r.ended_at>$2)`, [deviceId, from, to],
  )).rows.filter((row) => row.started_at !== null).map((row) => ({
    start: Date.parse(iso(row.started_at)!),
    // Still open: a robot that stopped reporting stopped recording then, even before the server closes it.
    end: row.ended_at === null ? quietRecordingEnd(iso(row.expires_at)!, now) : Date.parse(iso(row.ended_at)!),
    streamArn: row.kvs_stream_arn,
  }));
}

/** preparing / available / partial / unavailable / expired; a missing video never removes the incident. */
export function clipPlaybackState(startAt: string, endAt: string, spans: RecordingSpan[], now = Date.now()) {
  const start = Date.parse(startAt), end = Date.parse(endAt);
  if (start < now - RECORDING_RETENTION_MS) return "expired";
  if (now < end + CLIP_SETTLE_MS) return "preparing";
  const covering = spans
    .map((s) => ({ start: Math.max(s.start, start), end: Math.min(s.end ?? now, end) }))
    .filter((s) => s.end > s.start).sort((a, b) => a.start - b.start);
  if (!covering.length) return "unavailable";
  let reached = start;
  for (const span of covering) {
    if (span.start > reached + 1000) return "partial";
    reached = Math.max(reached, span.end);
  }
  return reached >= end - 1000 ? "available" : "partial";
}

/** Segments with person boxes (사람 표시); none before migration 0015. */
async function peopleSegments(deviceId: string, incidentId: string) {
  const pool = getPostgresPool();
  const ready = (await pool.query(
    "SELECT 1 FROM homecam_schema_migrations WHERE version='0015_fall_incident_people'",
  )).rows.length === 1;
  if (!ready) return new Set<number>();
  return new Set((await pool.query(
    "SELECT segment_index FROM fall_incident_people WHERE device_id=$1 AND incident_id=$2",
    [deviceId, incidentId],
  )).rows.map((r) => r.segment_index as number));
}

async function clipsWithState(deviceId: string, incidentId: string) {
  const rows = (await getPostgresPool().query(
    `SELECT segment_index,revision,start_at,end_at,anchor_kinds,found_down,clock_stepped
     FROM fall_incident_clips WHERE device_id=$1 AND incident_id=$2 ORDER BY segment_index`,
    [deviceId, incidentId],
  )).rows;
  if (!rows.length) return [];
  const spans = await recordingSpans(deviceId, iso(rows[0].start_at)!, iso(rows[rows.length - 1].end_at)!);
  const people = await peopleSegments(deviceId, incidentId);
  return rows.map((row) => {
    const startAt = iso(row.start_at)!, endAt = iso(row.end_at)!;
    return { segmentIndex: row.segment_index, revision: row.revision, startAt, endAt,
      anchorKinds: row.anchor_kinds, foundDown: row.found_down, clockStepped: row.clock_stepped,
      playbackState: clipPlaybackState(startAt, endAt, spans), hasPeople: people.has(row.segment_index) };
  });
}

export async function getFallIncidentDetail(deviceId: string, incidentId: string) {
  await ensureFallReviewSchema();
  const pool = getPostgresPool();
  const row = (await pool.query(
    `SELECT ${SUMMARY_COLUMNS} FROM fall_incidents i WHERE i.device_id=$1 AND i.incident_id=$3`,
    [deviceId, ANALYSIS_KINDS, incidentId],
  )).rows[0];
  if (!row) return null;
  const clips = await clipsWithState(deviceId, incidentId);
  const robotEvents = (await pool.query(
    `SELECT sequence,payload_json,received_at FROM fall_incident_events
     WHERE device_id=$1 AND incident_id=$2 ORDER BY sequence`, [deviceId, incidentId],
  )).rows.map((e) => {
    const p = JSON.parse(e.payload_json);
    return { sequence: e.sequence, eventKind: p.eventKind, occurredAt: p.occurredAt, state: p.state,
      assessment: p.assessment, answer: p.answer, reason: p.reason, notificationLevel: p.notificationLevel };
  });
  const notifications = [
    ...(await pool.query(
      `SELECT level,reason,status,created_at,accepted_at FROM fall_push_outbox
       WHERE device_id=$1 AND incident_id=$2`, [deviceId, incidentId],
    )).rows.map((n) => ({ kind: "first", round: 1, level: n.level, reason: n.reason, status: n.status,
      createdAt: iso(n.created_at), acceptedAt: iso(n.accepted_at) })),
    ...(await pool.query(
      `SELECT kind,round,level,reason,status,created_at,accepted_at FROM fall_web_notices
       WHERE device_id=$1 AND incident_id=$2`, [deviceId, incidentId],
    )).rows.map((n) => ({ kind: n.kind, round: n.round, level: n.level, reason: n.reason, status: n.status,
      createdAt: iso(n.created_at), acceptedAt: iso(n.accepted_at) })),
  ].sort((a, b) => a.createdAt!.localeCompare(b.createdAt!));
  const opinions = (await pool.query(
    `SELECT o.user_id,o.label,o.memo,o.updated_at,m.role FROM fall_incident_opinions o
     LEFT JOIN device_memberships m ON m.device_id=o.device_id AND m.user_id=o.user_id
     WHERE o.device_id=$1 AND o.incident_id=$2 ORDER BY o.updated_at`, [deviceId, incidentId],
  )).rows;
  const activity = (await pool.query(
    `SELECT actor_user_id,action,label,memo,created_at FROM fall_incident_activity
     WHERE device_id=$1 AND incident_id=$2 ORDER BY created_at,id`, [deviceId, incidentId],
  )).rows;
  const names = await userLabels([row.closed_by, row.reported_by,
    ...opinions.map((o) => o.user_id), ...activity.map((a) => a.actor_user_id)]);
  // Another person's incident in the same scene: overlapping clip ranges.
  const linked = (await pool.query(
    `SELECT DISTINCT o.incident_id FROM fall_incident_clips mine
     JOIN fall_incident_clips o ON o.device_id=mine.device_id AND o.incident_id<>mine.incident_id
       AND o.start_at<mine.end_at AND o.end_at>mine.start_at
     WHERE mine.device_id=$1 AND mine.incident_id=$2 ORDER BY o.incident_id`, [deviceId, incidentId],
  )).rows.map((l) => l.incident_id);
  return {
    ...summary(row, names), clips, robotEvents, notifications,
    opinions: opinions.map((o) => ({ userId: o.user_id, userName: labelFor(names, o.user_id), role: o.role ?? null,
      label: o.label, memo: o.memo, updatedAt: iso(o.updated_at) })),
    activity: activity.map((a) => ({ actorUserId: a.actor_user_id, actorName: labelFor(names, a.actor_user_id),
      action: a.action, label: a.label, memo: a.memo, createdAt: iso(a.created_at) })),
    linkedIncidentIds: linked,
  };
}

export async function getFallClipForPlayback(deviceId: string, incidentId: string, segmentIndex: number) {
  await ensureFallReviewSchema();
  const row = (await getPostgresPool().query(
    `SELECT c.start_at,c.end_at,i.occurred_at,i.reported_moment_at FROM fall_incident_clips c
     JOIN fall_incidents i ON i.device_id=c.device_id AND i.incident_id=c.incident_id
     WHERE c.device_id=$1 AND c.incident_id=$2 AND c.segment_index=$3`, [deviceId, incidentId, segmentIndex],
  )).rows[0];
  if (!row) return null;
  const startAt = iso(row.start_at)!, endAt = iso(row.end_at)!;
  const spans = await recordingSpans(deviceId, startAt, endAt);
  // The still (정지 사진) shows the suspected or reported moment. A segment that does not hold it
  // uses its own anchor, 10 s after its start (clips run −10 s/+20 s around the moment).
  const start = Date.parse(startAt), end = Date.parse(endAt);
  const anchor = Date.parse(iso(row.reported_moment_at ?? row.occurred_at) ?? startAt);
  const momentAt = new Date(anchor >= start && anchor < end ? anchor
    : Math.max(start, Math.min(start + REPORT_PRE_MS, end - 1_000))).toISOString();
  return { startAt, endAt, momentAt, playbackState: clipPlaybackState(startAt, endAt, spans),
    streamArns: [...new Set(spans.map((s) => s.streamArn))] };
}

// ---------------------------------------------------------------- user actions

async function lockIncident(db: Queryable, deviceId: string, incidentId: string) {
  const incident = (await db.query(
    "SELECT * FROM fall_incidents WHERE device_id=$1 AND incident_id=$2 FOR UPDATE",
    [deviceId, incidentId],
  )).rows[0];
  if (!incident) throw new Error("FALL_INCIDENT_NOT_FOUND");
  return incident;
}

/**
 * One current opinion per user (null clears it). An opinion is the
 * acknowledgement that stops [재발신] for everyone. A label that was absent
 * when the incident was closed reopens it and notifies everyone.
 */
export async function setFallOpinion(deviceId: string, incidentId: string, userId: string,
  label: OpinionLabel | null, memo: string | null) {
  return transaction(deviceId, async (db) => {
    const incident = await lockIncident(db, deviceId, incidentId);
    if (label === null) {
      const removed = await db.query(
        "DELETE FROM fall_incident_opinions WHERE device_id=$1 AND incident_id=$2 AND user_id=$3",
        [deviceId, incidentId, userId],
      );
      if (removed.rowCount) {
        await db.query(
          `INSERT INTO fall_incident_activity(device_id,incident_id,actor_user_id,action)
           VALUES($1,$2,$3,'opinion_cleared')`, [deviceId, incidentId, userId],
        );
      }
      return { reopened: false, noticeId: null };
    }
    await db.query(
      `INSERT INTO fall_incident_opinions(device_id,incident_id,user_id,label,memo) VALUES($1,$2,$3,$4,$5)
       ON CONFLICT(device_id,incident_id,user_id) DO UPDATE SET label=excluded.label,memo=excluded.memo,
         updated_at=CURRENT_TIMESTAMP`, [deviceId, incidentId, userId, label, memo],
    );
    await db.query(
      `INSERT INTO fall_incident_activity(device_id,incident_id,actor_user_id,action,label,memo)
       VALUES($1,$2,$3,'opinion_set',$4,$5)`, [deviceId, incidentId, userId, label, memo],
    );
    // Acknowledged: stop pending reminders for everyone.
    await db.query(
      `UPDATE fall_web_notices SET status='canceled',lease_id=NULL,lease_until=NULL
       WHERE device_id=$1 AND incident_id=$2 AND kind='resend' AND status='pending'`, [deviceId, incidentId],
    );
    await db.query(
      "UPDATE fall_incidents SET unacknowledged_since=NULL WHERE device_id=$1 AND incident_id=$2",
      [deviceId, incidentId],
    );
    const closedLabels: string[] = incident.closed_labels ?? [];
    if (incident.review_state !== "closed" || closedLabels.includes(label)) {
      return { reopened: false, noticeId: null };
    }
    await db.query(
      `UPDATE fall_incidents SET review_state='open',closed_at=NULL,closed_by=NULL,
         reopened_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE device_id=$1 AND incident_id=$2`,
      [deviceId, incidentId],
    );
    await db.query(
      `INSERT INTO fall_incident_activity(device_id,incident_id,actor_user_id,action,label)
       VALUES($1,$2,$3,'reopened',$4)`, [deviceId, incidentId, userId, label],
    );
    const noticeId = randomUUID();
    // created_at equals the opinion's time, so the reopening opinion itself
    // does not count as acknowledging this new cycle.
    await db.query(
      `INSERT INTO fall_web_notices(device_id,notice_id,incident_id,kind,cycle_key,round,level,reason,occurred_at)
       VALUES($1,$2,$3,'reopen',$4,1,'check','reopened_by_opinion',date_trunc('milliseconds',CURRENT_TIMESTAMP))`,
      [deviceId, noticeId, incidentId, `reopen:${noticeId}`],
    );
    return { reopened: true, noticeId };
  });
}

/**
 * Any member may close once someone has left an opinion (a human judgment is
 * the point of closing); the labels present now decide what later reopens it.
 */
export async function closeFallIncident(deviceId: string, incidentId: string, userId: string) {
  return transaction(deviceId, async (db) => {
    const incident = await lockIncident(db, deviceId, incidentId);
    if (incident.review_state === "closed") return { closed: true, changed: false };
    const labels = (await db.query(
      `SELECT DISTINCT label FROM fall_incident_opinions WHERE device_id=$1 AND incident_id=$2 ORDER BY label`,
      [deviceId, incidentId],
    )).rows.map((r) => r.label);
    if (!labels.length) throw new Error("FALL_CLOSE_NEEDS_OPINION");
    await db.query(
      `UPDATE fall_incidents SET review_state='closed',closed_at=CURRENT_TIMESTAMP,closed_by=$3,
         closed_labels=$4::jsonb,unacknowledged_since=NULL,updated_at=CURRENT_TIMESTAMP
       WHERE device_id=$1 AND incident_id=$2`, [deviceId, incidentId, userId, JSON.stringify(labels)],
    );
    await db.query(
      `UPDATE fall_web_notices SET status='canceled',lease_id=NULL,lease_until=NULL
       WHERE device_id=$1 AND incident_id=$2 AND status='pending'`, [deviceId, incidentId],
    );
    await db.query(
      `INSERT INTO fall_incident_activity(device_id,incident_id,actor_user_id,action)
       VALUES($1,$2,$3,'closed')`, [deviceId, incidentId, userId],
    );
    return { closed: true, changed: true };
  });
}

/** Missed fall: one user-picked moment, −10 s/+20 s, recorded without notifying anyone. */
export async function reportMissedFall(deviceId: string, userId: string, momentAt: string, now = Date.now(),
  memo: string | null = null) {
  const moment = Date.parse(momentAt);
  const note = memo?.trim() || null;
  if (note !== null && note.length > 500) throw new Error("FALL_REPORT_INVALID");
  if (!Number.isFinite(moment) || new Date(moment).toISOString() !== momentAt) throw new Error("FALL_REPORT_INVALID");
  if (moment > now || moment - REPORT_PRE_MS < now - RECORDING_RETENTION_MS) throw new Error("FALL_REPORT_OUT_OF_RANGE");
  const incidentId = randomUUID();
  return transaction(deviceId, async (db) => {
    await db.query(
      `INSERT INTO fall_incidents(device_id,incident_id,origin,occurred_at,reported_by,reported_moment_at,report_memo)
       VALUES($1,$2,'user_report',$3,$4,$3,$5)`, [deviceId, incidentId, momentAt, userId, note],
    );
    await db.query(
      `INSERT INTO fall_incident_clips(device_id,incident_id,segment_index,revision,start_at,end_at,anchor_kinds)
       VALUES($1,$2,0,1,$3,$4,'["user_report"]'::jsonb)`,
      [deviceId, incidentId, new Date(moment - REPORT_PRE_MS).toISOString(),
        new Date(moment + REPORT_POST_MS).toISOString()],
    );
    await db.query(
      `INSERT INTO fall_incident_activity(device_id,incident_id,actor_user_id,action,memo)
       VALUES($1,$2,$3,'reported',$4)`, [deviceId, incidentId, userId, note],
    );
    return { incidentId };
  });
}

// ---------------------------------------------------------------- reminders

type Cycle = { key: string; level: "check" | "urgent"; reason: string; occurredAt: string; startedAt: number };

async function currentCycle(db: Queryable, deviceId: string, incidentId: string): Promise<Cycle | null> {
  const robot = (await db.query(
    `SELECT notification_id,level,reason,occurred_at,created_at FROM fall_push_outbox
     WHERE device_id=$1 AND incident_id=$2 AND status<>'superseded' AND level IN ('check','urgent')
     ORDER BY ${LEVEL_RANK_SQL} DESC,created_at DESC LIMIT 1`, [deviceId, incidentId],
  )).rows[0];
  const reopen = (await db.query(
    `SELECT notice_id,occurred_at,created_at FROM fall_web_notices
     WHERE device_id=$1 AND incident_id=$2 AND kind='reopen' ORDER BY created_at DESC LIMIT 1`,
    [deviceId, incidentId],
  )).rows[0];
  const robotCycle = robot && { key: `robot:${robot.notification_id}`, level: robot.level, reason: robot.reason,
    occurredAt: iso(robot.occurred_at)!, startedAt: Date.parse(iso(robot.created_at)!) };
  const reopenCycle = reopen && { key: `reopen:${reopen.notice_id}`, level: "check" as const,
    reason: "reopened_by_opinion", occurredAt: iso(reopen.occurred_at)!,
    startedAt: Date.parse(iso(reopen.created_at)!) };
  if (robotCycle && reopenCycle) return robotCycle.startedAt > reopenCycle.startedAt ? robotCycle : reopenCycle;
  return robotCycle || reopenCycle || null;
}

/**
 * Creates due [재발신] notices: urgent every 2 min (3 total), check every
 * 3 min (2 total). A higher level or a reopen starts a new cycle and cancels
 * the old one. After the last one, an unanswered incident is flagged.
 */
export async function scheduleFallReminders(now = Date.now()) {
  await ensureFallReviewSchema();
  const candidates = (await getPostgresPool().query(
    `SELECT i.device_id,i.incident_id FROM fall_incidents i
     WHERE i.review_state='open' AND i.unacknowledged_since IS NULL
       AND (i.notification_rank>=2 OR i.reopened_at IS NOT NULL)
       AND i.updated_at>$1 ORDER BY i.updated_at DESC LIMIT 200`,
    [new Date(now - 24 * 60 * 60 * 1000).toISOString()],
  )).rows;
  let created = 0;
  for (const { device_id: deviceId, incident_id: incidentId } of candidates) {
    created += await transaction(deviceId, async (db) => {
      const incident = await lockIncident(db, deviceId, incidentId);
      if (incident.review_state !== "open") return 0;
      let cycle = await currentCycle(db, deviceId, incidentId);
      // Robot/AI judged it normal: no reminders for the robot's alert (a reopen still counts).
      const robotNormal = incident.origin === "robot" && incident.state === "resolved" &&
        incident.assessment === "normal_activity" && !incident.fall_seen;
      const robotMerged = (await db.query(
        `SELECT ${ROBOT_MERGED_SQL} AS merged FROM fall_incidents i
         WHERE i.device_id=$1 AND i.incident_id=$2`, [deviceId, incidentId],
      )).rows[0]?.merged;
      if (cycle && (robotNormal || robotMerged) && cycle.key.startsWith("robot:")) cycle = null;
      if (!cycle) {
        await db.query(
          `UPDATE fall_web_notices SET status='canceled',lease_id=NULL,lease_until=NULL
           WHERE device_id=$1 AND incident_id=$2 AND kind='resend' AND status='pending'`,
          [deviceId, incidentId],
        );
        return 0;
      }
      await db.query(
        `UPDATE fall_web_notices SET status='canceled',lease_id=NULL,lease_until=NULL
         WHERE device_id=$1 AND incident_id=$2 AND kind='resend' AND status='pending' AND cycle_key<>$3`,
        [deviceId, incidentId, cycle.key],
      );
      // Millisecond precision on both sides: the reopening opinion shares the
      // reopen notice's transaction time and must not acknowledge it.
      const acknowledged = (await db.query(
        `SELECT 1 FROM fall_incident_activity WHERE device_id=$1 AND incident_id=$2
         AND action='opinion_set' AND date_trunc('milliseconds',created_at)>$3 LIMIT 1`,
        [deviceId, incidentId, new Date(cycle.startedAt).toISOString()],
      )).rowCount;
      if (acknowledged) return 0;
      const rule = REMINDER_RULES[cycle.level];
      const sent = (await db.query(
        `SELECT COALESCE(MAX(round),1) AS last FROM fall_web_notices
         WHERE device_id=$1 AND incident_id=$2 AND cycle_key=$3`, [deviceId, incidentId, cycle.key],
      )).rows[0].last as number;
      const next = sent + 1;
      if (next > rule.total) {
        const flagAt = cycle.startedAt + rule.total * rule.intervalMs;
        if (now >= flagAt && incident.unacknowledged_since === null) {
          await db.query(
            "UPDATE fall_incidents SET unacknowledged_since=$3 WHERE device_id=$1 AND incident_id=$2",
            [deviceId, incidentId, new Date(flagAt).toISOString()],
          );
        }
        return 0;
      }
      if (now < cycle.startedAt + (next - 1) * rule.intervalMs) return 0;
      const inserted = await db.query(
        `INSERT INTO fall_web_notices(device_id,notice_id,incident_id,kind,cycle_key,round,level,reason,occurred_at)
         VALUES($1,$2,$3,'resend',$4,$5,$6,$7,$8) ON CONFLICT DO NOTHING`,
        [deviceId, randomUUID(), incidentId, cycle.key, next, cycle.level, cycle.reason, cycle.occurredAt],
      );
      return inserted.rowCount ? 1 : 0;
    });
  }
  return { created };
}

export type ClaimedFallNotice = {
  deviceId: string; noticeId: string; incidentId: string; kind: "resend" | "reopen";
  level: "check" | "urgent"; reason: string; occurredAt: string; leaseId: string;
  subscriptionResults: Record<string, number>;
};

export async function claimFallNotice(deviceId?: string, noticeId?: string): Promise<ClaimedFallNotice | null> {
  await ensureFallReviewSchema();
  const leaseId = randomUUID();
  const result = await getPostgresPool().query(
    `WITH candidate AS (
       SELECT n.device_id,n.notice_id FROM fall_web_notices n
       JOIN fall_incidents i ON i.device_id=n.device_id AND i.incident_id=n.incident_id
       WHERE n.status='pending' AND n.next_attempt_at<=CURRENT_TIMESTAMP AND i.review_state='open'
         AND NOT (n.kind='resend' AND n.cycle_key LIKE 'robot:%' AND (${ROBOT_NORMAL_SQL} OR ${ROBOT_MERGED_SQL}))
         AND (n.lease_until IS NULL OR n.lease_until<=CURRENT_TIMESTAMP)
         AND ($1::text IS NULL OR n.device_id=$1) AND ($2::text IS NULL OR n.notice_id=$2)
       ORDER BY CASE n.level WHEN 'urgent' THEN 0 ELSE 1 END,n.created_at
       FOR UPDATE OF n SKIP LOCKED LIMIT 1
     ) UPDATE fall_web_notices p SET lease_id=$3,lease_until=CURRENT_TIMESTAMP+INTERVAL '120 seconds',
       attempt_count=attempt_count+1
       FROM candidate c WHERE p.device_id=c.device_id AND p.notice_id=c.notice_id
       RETURNING p.*`, [deviceId ?? null, noticeId ?? null, leaseId],
  );
  if (!result.rowCount) return null;
  const row = result.rows[0];
  return { deviceId: row.device_id, noticeId: row.notice_id, incidentId: row.incident_id, kind: row.kind,
    level: row.level, reason: row.reason, occurredAt: iso(row.occurred_at)!, leaseId,
    subscriptionResults: row.subscription_results };
}

export async function recordFallNoticeResults(claim: ClaimedFallNotice,
  results: Array<{ subscriptionId: string; status: number }>) {
  const receipts = Object.fromEntries(results.map((r) => [r.subscriptionId, r.status]));
  const result = await getPostgresPool().query(
    `UPDATE fall_web_notices SET subscription_results=subscription_results || $4::jsonb,
       lease_until=CURRENT_TIMESTAMP+INTERVAL '120 seconds'
     WHERE device_id=$1 AND notice_id=$2 AND lease_id=$3 AND status='pending' AND lease_until>CURRENT_TIMESTAMP`,
    [claim.deviceId, claim.noticeId, claim.leaseId, JSON.stringify(receipts)],
  );
  if (!result.rowCount) throw new Error("FALL_NOTICE_LEASE_LOST");
}

export async function finishFallNotice(claim: ClaimedFallNotice, complete: boolean, error: string | null) {
  const result = await getPostgresPool().query(
    `UPDATE fall_web_notices SET status=CASE WHEN $4 THEN 'accepted' ELSE 'pending' END,
       accepted_at=CASE WHEN $4 THEN CURRENT_TIMESTAMP ELSE NULL END,
       lease_id=NULL,lease_until=NULL,last_error=$5,
       next_attempt_at=CURRENT_TIMESTAMP+INTERVAL '1 second' * LEAST(60,5*POWER(2,LEAST(attempt_count,4)))
     WHERE device_id=$1 AND notice_id=$2 AND lease_id=$3 AND status='pending' AND lease_until>CURRENT_TIMESTAMP`,
    [claim.deviceId, claim.noticeId, claim.leaseId, complete, error],
  );
  return !!result.rowCount;
}

// ---------------------------------------------------------------- timeline

/**
 * One day of the continuous recording for the 연속 녹화 screen: recorded
 * spans (gaps are "녹화 없음") and incident marks. Wall time, retention-bounded.
 */
export async function getFallTimeline(deviceId: string, from: string, to: string, now = Date.now()) {
  await ensureFallReviewSchema();
  const start = Date.parse(from), end = Date.parse(to);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start || end - start > 26 * 3600_000 ||
      end < now - RECORDING_RETENTION_MS || start > now + 60_000) throw new Error("FALL_TIMELINE_RANGE_INVALID");
  const spans = (await recordingSpans(deviceId, from, to, now)).map((s) => ({
    startAt: new Date(Math.max(s.start, start)).toISOString(),
    endAt: new Date(Math.min(s.end ?? now, end)).toISOString(),
  })).filter((s) => s.endAt > s.startAt).sort((a, b) => a.startAt.localeCompare(b.startAt));
  const incidents = (await getPostgresPool().query(
    `SELECT incident_id,origin,occurred_at,reported_moment_at,fall_seen,assessment FROM fall_incidents
     WHERE device_id=$1 AND COALESCE(reported_moment_at,occurred_at) BETWEEN $2 AND $3
     ORDER BY COALESCE(reported_moment_at,occurred_at) LIMIT 500`, [deviceId, from, to],
  )).rows.map((row) => ({
    incidentId: row.incident_id,
    at: iso(row.reported_moment_at ?? row.occurred_at)!,
    kind: row.origin === "user_report" ? "report"
      : row.fall_seen || row.assessment === "observed_fall" ? "fall" : "suspected",
  }));
  return { from: new Date(start).toISOString(), to: new Date(end).toISOString(), recordings: spans, incidents };
}

/** Stream ARN of the recording that covers [from, to] on this device, if any. */
export async function recordingStreamFor(deviceId: string, from: string, to: string) {
  const spans = await recordingSpans(deviceId, from, to);
  return [...new Set(spans.map((s) => s.streamArn))];
}
