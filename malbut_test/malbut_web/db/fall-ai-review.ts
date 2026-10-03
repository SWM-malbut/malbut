import { randomUUID } from "node:crypto";
import { getPostgresPool, type SqlExecutor } from "./postgres";
import { decryptFallCloudKey, encryptFallCloudKey, isValidFallCloudKey } from "../app/fall-cloud-key-crypto";

// Per-robot fall Cloud key and user-requested AI reviews. A review is a
// photo-only verdict recorded next to the incident; it never changes the
// incident, the robot's automatic judgment or anyone's opinion.

const MODEL = /^[A-Za-z0-9_.:-]{1,100}$/;
const REVIEW_PRE_MS = 10_000;
const REVIEW_POST_MS = 20_000;
// The robot's own incident checks go first on the shared key.
export const ROBOT_BUSY_WINDOW_MS = 60_000;
const MAX_ATTEMPTS = 6;

export async function ensureFallAiSchema() {
  const result = await getPostgresPool().query(
    "SELECT 1 FROM homecam_schema_migrations WHERE version = '0013_fall_ai_review'",
  );
  if (!result.rowCount) throw new Error("FALL_AI_MIGRATION_REQUIRED");
}

function iso(value: unknown) {
  return value == null ? null : new Date(value as string).toISOString();
}

async function transaction<T>(deviceId: string, work: (db: SqlExecutor) => Promise<T>) {
  await ensureFallAiSchema();
  const client = await getPostgresPool().connect();
  try {
    await client.query("BEGIN");
    await client.query("SELECT pg_advisory_xact_lock(hashtext($1))", [`fall:${deviceId}`]);
    const result = await work(client);
    await client.query("COMMIT");
    return result;
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  } finally { client.release(); }
}

async function requireOwner(db: SqlExecutor, deviceId: string, userEmail: string) {
  const role = (await db.query(
    "SELECT role FROM device_memberships WHERE device_id=$1 AND user_email=$2 FOR SHARE",
    [deviceId, userEmail],
  )).rows[0]?.role;
  if (role !== "owner") throw new Error("FALL_KEY_FORBIDDEN");
}

// ---------------------------------------------------------------- key

/** Owner only. The key is never returned to users; only its last 4 characters. */
export async function setFallCloudKey(deviceId: string, userEmail: string, apiKey: string | null, secret: string) {
  if (apiKey !== null && !isValidFallCloudKey(apiKey)) throw new Error("FALL_KEY_INVALID");
  return transaction(deviceId, async (db) => {
    await requireOwner(db, deviceId, userEmail);
    const current = (await db.query(
      "SELECT key_version FROM fall_cloud_keys WHERE device_id=$1 FOR UPDATE", [deviceId],
    )).rows[0];
    const version = (current?.key_version ?? 0) + 1;
    const ciphertext = apiKey === null ? null : await encryptFallCloudKey(apiKey, deviceId, version, secret);
    await db.query(
      `INSERT INTO fall_cloud_keys(device_id,key_version,ciphertext,last4,updated_by) VALUES($1,$2,$3,$4,$5)
       ON CONFLICT(device_id) DO UPDATE SET key_version=excluded.key_version,ciphertext=excluded.ciphertext,
         last4=excluded.last4,updated_by=excluded.updated_by,updated_at=CURRENT_TIMESTAMP`,
      [deviceId, version, ciphertext, apiKey === null ? null : apiKey.slice(-4), userEmail],
    );
    await db.query(
      `INSERT INTO access_audit_log(id,device_id,actor_type,actor_id,action,metadata_json)
       VALUES($1,$2,'user',$3,$4,$5)`,
      [randomUUID(), deviceId, userEmail, apiKey === null ? "fall_cloud_key_deleted" : "fall_cloud_key_set",
        JSON.stringify({ keyVersion: version })],
    );
    return { keyVersion: version, configured: apiKey !== null };
  });
}

export async function readFallCloudKeyView(deviceId: string) {
  await ensureFallAiSchema();
  const row = (await getPostgresPool().query(
    `SELECT key_version,last4,updated_at,robot_model,robot_key_version,robot_key_fetched_at
     FROM fall_cloud_keys WHERE device_id=$1`, [deviceId],
  )).rows[0];
  if (!row) return { configured: false, last4: null, keyVersion: 0, updatedAt: null, robotModel: null, robotHasCurrent: false };
  return { configured: row.last4 !== null, last4: row.last4, keyVersion: row.key_version,
    updatedAt: iso(row.updated_at), robotModel: row.robot_model,
    robotHasCurrent: row.robot_key_version === row.key_version };
}

/**
 * The fall node's periodic sync. It reports its Cloud model (server reviews
 * use the same one) and gets the key only when its copy is stale.
 * keyVersion 0 means the owner never set a key: keep the robot's own file.
 */
export async function syncFallCloudKeyForDevice(deviceId: string, knownVersion: number, model: string | null,
  secret: string) {
  // Same rule as the robot provider: no local "...cloud" proxy model names.
  if (model !== null && (!MODEL.test(model) || /cloud$/.test(model))) throw new Error("FALL_MODEL_INVALID");
  return transaction(deviceId, async (db) => {
    if (model !== null) {
      await db.query(
        `INSERT INTO fall_cloud_keys(device_id,key_version,updated_by,robot_model,robot_model_reported_at)
         VALUES($1,0,'robot',$2,CURRENT_TIMESTAMP)
         ON CONFLICT(device_id) DO UPDATE SET robot_model=excluded.robot_model,robot_model_reported_at=CURRENT_TIMESTAMP`,
        [deviceId, model],
      );
    }
    const row = (await db.query(
      "SELECT key_version,ciphertext FROM fall_cloud_keys WHERE device_id=$1 FOR UPDATE", [deviceId],
    )).rows[0];
    const version = (row?.key_version ?? 0) as number;
    if (version === 0) return { keyVersion: 0, changed: false, apiKey: null };
    // Record what the robot reports holding, not what we are about to send:
    // a lost reply or failed write must not show the robot as up to date.
    await db.query(
      `UPDATE fall_cloud_keys SET robot_key_version=$2,robot_key_fetched_at=CURRENT_TIMESTAMP
       WHERE device_id=$1`, [deviceId, knownVersion],
    );
    if (knownVersion === version) return { keyVersion: version, changed: false, apiKey: null };
    const apiKey = row.ciphertext === null ? null
      : await decryptFallCloudKey(row.ciphertext, deviceId, version, secret);
    // apiKey null with changed=true means the owner deleted the key.
    return { keyVersion: version, changed: true, apiKey };
  });
}

async function decryptedKey(db: SqlExecutor, deviceId: string, secret: string) {
  const row = (await db.query(
    "SELECT key_version,ciphertext,robot_model FROM fall_cloud_keys WHERE device_id=$1", [deviceId],
  )).rows[0];
  if (!row?.ciphertext) return { apiKey: null, model: row?.robot_model ?? null };
  return { apiKey: await decryptFallCloudKey(row.ciphertext, deviceId, row.key_version, secret),
    model: row.robot_model as string | null };
}

// ---------------------------------------------------------------- reviews

async function cloudConsent(db: SqlExecutor, deviceId: string) {
  return !!(await db.query(
    "SELECT fall_cloud_consent FROM device_state WHERE device_id=$1", [deviceId],
  )).rows[0]?.fall_cloud_consent;
}

/**
 * Queue a photo-only review of one moment. The moment must lie in the
 * incident's range (10 s before to 20 s after its anchor); otherwise the
 * caller offers a new missed-fall report instead.
 */
export async function requestFallAiReview(deviceId: string, incidentId: string, userEmail: string, momentAt: string) {
  const moment = Date.parse(momentAt);
  if (!Number.isFinite(moment) || new Date(moment).toISOString() !== momentAt) throw new Error("FALL_AI_MOMENT_INVALID");
  return transaction(deviceId, async (db) => {
    const incident = (await db.query(
      `SELECT origin,occurred_at,reported_moment_at FROM fall_incidents
       WHERE device_id=$1 AND incident_id=$2 FOR UPDATE`, [deviceId, incidentId],
    )).rows[0];
    if (!incident) throw new Error("FALL_INCIDENT_NOT_FOUND");
    // A missed-fall report is reviewed at its own moment; automatic incidents
    // accept any moment inside their recorded scene ranges.
    const ranges = incident.origin === "user_report"
      ? [{ start: Date.parse(iso(incident.reported_moment_at)!), end: Date.parse(iso(incident.reported_moment_at)!) }]
      : (await db.query(
        "SELECT start_at,end_at FROM fall_incident_clips WHERE device_id=$1 AND incident_id=$2",
        [deviceId, incidentId],
      )).rows.map((c) => ({ start: Date.parse(iso(c.start_at)!), end: Date.parse(iso(c.end_at)!) }));
    if (!ranges.length) {
      const anchor = Date.parse(iso(incident.occurred_at)!);
      ranges.push({ start: anchor - REVIEW_PRE_MS, end: anchor + REVIEW_POST_MS });
    }
    if (!ranges.some((r) => moment >= r.start && moment <= r.end)) throw new Error("FALL_AI_OUTSIDE_INCIDENT");
    if (!(await cloudConsent(db, deviceId))) throw new Error("FALL_AI_CONSENT_OFF");
    const key = (await db.query(
      "SELECT ciphertext,robot_model FROM fall_cloud_keys WHERE device_id=$1", [deviceId],
    )).rows[0];
    if (!key?.ciphertext) throw new Error("FALL_AI_KEY_MISSING");
    // Reviews use the robot's model; until the robot reports it there is nothing to match.
    if (!key.robot_model) throw new Error("FALL_AI_MODEL_UNKNOWN");
    const active = await db.query(
      `SELECT 1 FROM fall_ai_reviews WHERE device_id=$1 AND incident_id=$2 AND status IN ('queued','running')`,
      [deviceId, incidentId],
    );
    if (active.rowCount) throw new Error("FALL_AI_REVIEW_IN_PROGRESS");
    const reviewId = randomUUID();
    await db.query(
      `INSERT INTO fall_ai_reviews(device_id,review_id,incident_id,requested_by,moment_at) VALUES($1,$2,$3,$4,$5)`,
      [deviceId, reviewId, incidentId, userEmail, momentAt],
    );
    return { reviewId };
  });
}

export async function askFallAiQuestion(deviceId: string, incidentId: string, reviewId: string,
  userEmail: string, question: string, includeContext = true) {
  const text = question.trim();
  if (!text || text.length > 500) throw new Error("FALL_AI_QUESTION_INVALID");
  return transaction(deviceId, async (db) => {
    const review = (await db.query(
      `SELECT status FROM fall_ai_reviews WHERE device_id=$1 AND review_id=$2 AND incident_id=$3 FOR UPDATE`,
      [deviceId, reviewId, incidentId],
    )).rows[0];
    if (!review) throw new Error("FALL_AI_REVIEW_NOT_FOUND");
    if (review.status !== "completed") throw new Error("FALL_AI_REVIEW_NOT_COMPLETED");
    if (!(await cloudConsent(db, deviceId))) throw new Error("FALL_AI_CONSENT_OFF");
    if ((await db.query(
      `SELECT 1 FROM fall_ai_questions WHERE device_id=$1 AND review_id=$2 AND status IN ('queued','running')`,
      [deviceId, reviewId],
    )).rowCount) throw new Error("FALL_AI_REVIEW_IN_PROGRESS");
    const questionId = randomUUID();
    await db.query(
      `INSERT INTO fall_ai_questions(device_id,question_id,review_id,asked_by,question,include_context)
       VALUES($1,$2,$3,$4,$5,$6)`,
      [deviceId, questionId, reviewId, userEmail, text, includeContext],
    );
    return { questionId };
  });
}

export async function listFallAiReviews(deviceId: string, incidentId: string) {
  await ensureFallAiSchema();
  const pool = getPostgresPool();
  const reviews = (await pool.query(
    `SELECT review_id,requested_by,moment_at,status,model,frame_count,history_incomplete,assessment,
       explanation,error_code,created_at,completed_at FROM fall_ai_reviews
     WHERE device_id=$1 AND incident_id=$2 ORDER BY created_at`, [deviceId, incidentId],
  )).rows;
  const questions = (await pool.query(
    `SELECT q.review_id,q.asked_by,q.question,q.include_context,q.status,q.answer,q.error_code,q.created_at,q.completed_at
     FROM fall_ai_questions q JOIN fall_ai_reviews r ON r.device_id=q.device_id AND r.review_id=q.review_id
     WHERE r.device_id=$1 AND r.incident_id=$2 ORDER BY q.created_at`, [deviceId, incidentId],
  )).rows;
  return reviews.map((r) => ({
    reviewId: r.review_id, requestedBy: r.requested_by, momentAt: iso(r.moment_at), status: r.status,
    model: r.model, frameCount: r.frame_count, historyIncomplete: r.history_incomplete,
    assessment: r.assessment, explanation: r.explanation, errorCode: r.error_code,
    createdAt: iso(r.created_at), completedAt: iso(r.completed_at),
    // Reference answers only; they never change the verdict above.
    questions: questions.filter((q) => q.review_id === r.review_id).map((q) => ({
      askedBy: q.asked_by, question: q.question, includeContext: q.include_context, status: q.status, answer: q.answer,
      errorCode: q.error_code, createdAt: iso(q.created_at), completedAt: iso(q.completed_at) })),
  }));
}

// ---------------------------------------------------------------- worker side

export type ClaimedReview = {
  kind: "review"; deviceId: string; reviewId: string; incidentId: string; momentAt: string;
  leaseId: string; attemptCount: number;
};
export type ClaimedQuestion = {
  kind: "question"; deviceId: string; questionId: string; reviewId: string; incidentId: string;
  momentAt: string; question: string; includeContext: boolean; leaseId: string; attemptCount: number;
};

/** Next due job; reviews and questions on a robot whose own check is in progress wait. */
export async function claimFallAiJob(deviceId?: string, jobId?: string, now = Date.now()):
  Promise<ClaimedReview | ClaimedQuestion | null> {
  await ensureFallAiSchema();
  const leaseId = randomUUID();
  const busySince = new Date(now - ROBOT_BUSY_WINDOW_MS).toISOString();
  const notBusy = `NOT EXISTS (SELECT 1 FROM fall_incidents b WHERE b.device_id=j.device_id
    AND b.origin='robot' AND b.state IN ('verifying','recheck_required') AND b.updated_at>$4)`;
  const review = (await getPostgresPool().query(
    `WITH candidate AS (
       SELECT j.device_id,j.review_id FROM fall_ai_reviews j
       WHERE j.status='queued' AND j.next_attempt_at<=CURRENT_TIMESTAMP AND j.attempt_count<$5
         AND ($1::text IS NULL OR j.device_id=$1) AND ($2::text IS NULL OR j.review_id=$2) AND ${notBusy}
       ORDER BY j.created_at FOR UPDATE OF j SKIP LOCKED LIMIT 1
     ) UPDATE fall_ai_reviews p SET status='running',lease_id=$3,
       lease_until=CURRENT_TIMESTAMP+INTERVAL '90 seconds',attempt_count=attempt_count+1
     FROM candidate c WHERE p.device_id=c.device_id AND p.review_id=c.review_id RETURNING p.*`,
    [deviceId ?? null, jobId ?? null, leaseId, busySince, MAX_ATTEMPTS],
  )).rows[0];
  if (review) {
    return { kind: "review", deviceId: review.device_id, reviewId: review.review_id, incidentId: review.incident_id,
      momentAt: iso(review.moment_at)!, leaseId, attemptCount: review.attempt_count };
  }
  const question = (await getPostgresPool().query(
    `WITH candidate AS (
       SELECT j.device_id,j.question_id FROM fall_ai_questions j
       WHERE j.status='queued' AND j.next_attempt_at<=CURRENT_TIMESTAMP AND j.attempt_count<$5
         AND ($1::text IS NULL OR j.device_id=$1) AND ($2::text IS NULL OR j.question_id=$2) AND ${notBusy}
       ORDER BY j.created_at FOR UPDATE OF j SKIP LOCKED LIMIT 1
     ) UPDATE fall_ai_questions p SET status='running',lease_id=$3,
       lease_until=CURRENT_TIMESTAMP+INTERVAL '90 seconds',attempt_count=attempt_count+1
     FROM candidate c WHERE p.device_id=c.device_id AND p.question_id=c.question_id
     RETURNING p.*,(SELECT incident_id FROM fall_ai_reviews r WHERE r.device_id=p.device_id
       AND r.review_id=p.review_id) AS incident_id,(SELECT moment_at FROM fall_ai_reviews r
       WHERE r.device_id=p.device_id AND r.review_id=p.review_id) AS moment_at`,
    [deviceId ?? null, jobId ?? null, leaseId, busySince, MAX_ATTEMPTS],
  )).rows[0];
  if (!question) return null;
  return { kind: "question", deviceId: question.device_id, questionId: question.question_id,
    reviewId: question.review_id, incidentId: question.incident_id, momentAt: iso(question.moment_at)!,
    question: question.question, includeContext: question.include_context, leaseId,
    attemptCount: question.attempt_count };
}

/** Requeue running jobs whose worker died (lease expired); give up after the attempt limit. */
export async function recoverFallAiJobs() {
  await ensureFallAiSchema();
  for (const table of ["fall_ai_reviews", "fall_ai_questions"]) {
    await getPostgresPool().query(
      `UPDATE ${table} SET lease_id=NULL,lease_until=NULL,
         status=CASE WHEN attempt_count>=$1 THEN 'failed' ELSE 'queued' END,
         error_code=CASE WHEN attempt_count>=$1 THEN 'worker_lost' ELSE error_code END,
         completed_at=CASE WHEN attempt_count>=$1 THEN CURRENT_TIMESTAMP ELSE completed_at END
       WHERE status='running' AND lease_until<CURRENT_TIMESTAMP`, [MAX_ATTEMPTS],
    );
  }
}

/** Current key, model and consent at processing time (consent may have been withdrawn). */
export async function readFallAiContext(deviceId: string, secret: string) {
  await ensureFallAiSchema();
  const pool = getPostgresPool();
  return { ...(await decryptedKey(pool, deviceId, secret)), consent: await cloudConsent(pool, deviceId) };
}

export async function readFallAiQuestionContext(deviceId: string, incidentId: string) {
  const pool = getPostgresPool();
  const memos = (await pool.query(
    `SELECT memo FROM fall_incident_opinions WHERE device_id=$1 AND incident_id=$2 AND memo IS NOT NULL
     ORDER BY updated_at`, [deviceId, incidentId],
  )).rows.map((r) => r.memo as string);
  const verdicts = (await pool.query(
    `SELECT assessment,explanation FROM fall_ai_reviews WHERE device_id=$1 AND incident_id=$2
       AND status='completed' ORDER BY completed_at`, [deviceId, incidentId],
  )).rows.map((r) => ({ assessment: r.assessment as string, explanation: r.explanation as string }));
  return { memos, verdicts };
}

const RETRYABLE = new Set(["cloud_quota_exhausted", "cloud_timeout", "cloud_transport_error",
  "cloud_http_error", "frames_unavailable_yet", "kvs_unavailable"]);

export type FallAiOutcome =
  | { ok: true; assessment?: string; explanation?: string; answer?: string; model: string;
      frameCount: number; historyIncomplete: boolean }
  | { ok: false; errorCode: string };

export async function finishFallAiJob(job: ClaimedReview | ClaimedQuestion, outcome: FallAiOutcome) {
  const table = job.kind === "review" ? "fall_ai_reviews" : "fall_ai_questions";
  const idColumn = job.kind === "review" ? "review_id" : "question_id";
  const id = job.kind === "review" ? job.reviewId : job.questionId;
  const retry = !outcome.ok && RETRYABLE.has(outcome.errorCode) && job.attemptCount < MAX_ATTEMPTS;
  if (outcome.ok) {
    const set = job.kind === "review"
      ? "assessment=$4,explanation=$5,model=$6,frame_count=$7,history_incomplete=$8"
      : "answer=$4";
    const values = job.kind === "review"
      ? [outcome.assessment, outcome.explanation, outcome.model, outcome.frameCount, outcome.historyIncomplete]
      : [outcome.answer];
    const result = await getPostgresPool().query(
      `UPDATE ${table} SET status='completed',${set},error_code=NULL,lease_id=NULL,lease_until=NULL,
         completed_at=CURRENT_TIMESTAMP WHERE device_id=$1 AND ${idColumn}=$2 AND lease_id=$3`,
      [job.deviceId, id, job.leaseId, ...values],
    );
    return !!result.rowCount;
  }
  const result = await getPostgresPool().query(
    `UPDATE ${table} SET status=$4,error_code=$5,lease_id=NULL,lease_until=NULL,
       next_attempt_at=CURRENT_TIMESTAMP+INTERVAL '1 second'*LEAST(300,15*POWER(2,LEAST(attempt_count,4))),
       completed_at=CASE WHEN $4='failed' THEN CURRENT_TIMESTAMP ELSE NULL END
     WHERE device_id=$1 AND ${idColumn}=$2 AND lease_id=$3`,
    [job.deviceId, id, job.leaseId, retry ? "queued" : "failed", outcome.errorCode],
  );
  return !!result.rowCount;
}

/** The robot is checking an incident right now: put the job back without counting an attempt. */
export async function deferFallAiJob(job: ClaimedReview | ClaimedQuestion, seconds = 30) {
  const table = job.kind === "review" ? "fall_ai_reviews" : "fall_ai_questions";
  const idColumn = job.kind === "review" ? "review_id" : "question_id";
  await getPostgresPool().query(
    `UPDATE ${table} SET status='queued',lease_id=NULL,lease_until=NULL,attempt_count=attempt_count-1,
       next_attempt_at=CURRENT_TIMESTAMP+INTERVAL '1 second'*$4 WHERE device_id=$1 AND ${idColumn}=$2 AND lease_id=$3`,
    [job.deviceId, job.kind === "review" ? job.reviewId : job.questionId, job.leaseId, seconds],
  );
}
