import { randomUUID } from "node:crypto";
import { decryptServiceKey, encryptServiceKey } from "../app/fall-cloud-key-crypto";
import { ensureDatabaseSchema } from "./migration-state";
import { getPostgresPool, type SqlExecutor } from "./postgres";

// OpenAI (대화·목소리) and KMA (날씨) keys the owner sets for one 말벗. Same rules as the
// fall Cloud key (db/fall-ai-review.ts): one row per device and service, a version that
// keeps increasing across replace/delete, version 0 = never set (the robot keeps its own key).

export const SERVICE_NAMES = ["openai", "kma"] as const;
export type ServiceName = (typeof SERVICE_NAMES)[number];
export const HEALTH_SERVICES = ["openai", "kma", "fall"] as const;
export type HealthService = (typeof HEALTH_SERVICES)[number];
export const HEALTH_STATES = ["ok", "missing", "invalid", "quota"] as const;
export type HealthState = (typeof HEALTH_STATES)[number];
export type HealthReport = Partial<Record<HealthService, { state: HealthState; code: string | null }>>;

const MODEL = /^[A-Za-z0-9_.:-]{1,100}$/;

export type ServiceKeyView = {
  configured: boolean;
  last4: string | null;
  keyVersion: number;
  updatedAt: string | null;
  robotHasCurrent: boolean;
  robotModel: string | null;
};

export const isServiceName = (value: unknown): value is ServiceName =>
  typeof value === "string" && (SERVICE_NAMES as readonly string[]).includes(value);

/** Printable ASCII without spaces, like the fall key; KMA keys may be pasted URL-encoded. */
export function isValidServiceKey(value: unknown): value is string {
  return typeof value === "string" && value.length >= 8 && value.length <= 1024 &&
    [...value].every((c) => c.charCodeAt(0) >= 33 && c.charCodeAt(0) <= 126);
}

const iso = (value: unknown) => value == null ? null : new Date(value as string).toISOString();

async function transaction<T>(deviceId: string, work: (db: SqlExecutor) => Promise<T>) {
  await ensureDatabaseSchema();
  const client = await getPostgresPool().connect();
  try {
    await client.query("BEGIN");
    await client.query("SELECT pg_advisory_xact_lock(hashtext($1))", [`keys:${deviceId}`]);
    const result = await work(client);
    await client.query("COMMIT");
    return result;
  } catch (error) { await client.query("ROLLBACK"); throw error; }
  finally { client.release(); }
}

function view(row: Record<string, unknown> | undefined): ServiceKeyView {
  if (!row) return { configured: false, last4: null, keyVersion: 0, updatedAt: null, robotHasCurrent: false, robotModel: null };
  return {
    configured: row.last4 !== null, last4: row.last4 as string | null, keyVersion: row.key_version as number,
    updatedAt: iso(row.updated_at), robotHasCurrent: row.robot_key_version === row.key_version,
    robotModel: row.robot_model as string | null,
  };
}

export async function readServiceKeyViews(deviceId: string): Promise<Record<ServiceName, ServiceKeyView>> {
  await ensureDatabaseSchema();
  const rows = (await getPostgresPool().query(
    `SELECT service,key_version,last4,updated_at,robot_model,robot_key_version
     FROM device_service_keys WHERE device_id=$1`, [deviceId],
  )).rows;
  const by = new Map(rows.map((row) => [row.service as string, row]));
  return { openai: view(by.get("openai")), kma: view(by.get("kma")) };
}

/** Owner only. The key is never returned; only its last 4 characters. null deletes it. */
export async function setServiceKey(input: {
  deviceId: string; userId: string; service: ServiceName; apiKey: string | null; secret: string;
}) {
  if (input.apiKey !== null && !isValidServiceKey(input.apiKey)) throw new Error("SERVICE_KEY_INVALID");
  return transaction(input.deviceId, async (db) => {
    const role = (await db.query(
      "SELECT role FROM device_memberships WHERE device_id=$1 AND user_id=$2 FOR SHARE", [input.deviceId, input.userId],
    )).rows[0]?.role;
    if (role !== "owner") throw new Error("SERVICE_KEY_FORBIDDEN");
    const current = (await db.query(
      "SELECT key_version FROM device_service_keys WHERE device_id=$1 AND service=$2 FOR UPDATE",
      [input.deviceId, input.service],
    )).rows[0];
    const version = (current?.key_version ?? 0) + 1;
    const ciphertext = input.apiKey === null ? null
      : await encryptServiceKey(input.apiKey, input.deviceId, input.service, version, input.secret);
    await db.query(
      `INSERT INTO device_service_keys(device_id,service,key_version,ciphertext,last4,updated_by) VALUES($1,$2,$3,$4,$5,$6)
       ON CONFLICT(device_id,service) DO UPDATE SET key_version=excluded.key_version,ciphertext=excluded.ciphertext,
         last4=excluded.last4,updated_by=excluded.updated_by,updated_at=CURRENT_TIMESTAMP`,
      [input.deviceId, input.service, version, ciphertext, input.apiKey === null ? null : input.apiKey.slice(-4), input.userId],
    );
    await db.query(
      `INSERT INTO access_audit_log(id,device_id,actor_type,actor_id,action,metadata_json)
       VALUES($1,$2,'user',$3,$4,$5)`,
      [randomUUID(), input.deviceId, input.userId, input.apiKey === null ? "service_key_deleted" : "service_key_set",
        JSON.stringify({ service: input.service, keyVersion: version })],
    );
    return { keyVersion: version, configured: input.apiKey !== null };
  });
}

export type ServiceKeySync = { keyVersion: number; changed: boolean; apiKey: string | null };

/**
 * The robot's periodic key sync. It says which versions it holds, the OpenAI model it
 * uses and how each key is doing, and gets a key only when its copy is stale.
 */
export async function syncServiceKeysForDevice(input: {
  deviceId: string;
  known: Record<ServiceName, number>;
  models: { openai: string | null };
  health: HealthReport;
  secret: string;
  now?: Date;
}): Promise<Record<ServiceName, ServiceKeySync>> {
  if (input.models.openai !== null && !MODEL.test(input.models.openai)) throw new Error("SERVICE_MODEL_INVALID");
  const reportedAt = (input.now ?? new Date()).toISOString();
  return transaction(input.deviceId, async (db) => {
    if (input.models.openai !== null) {
      await db.query(
        `INSERT INTO device_service_keys(device_id,service,key_version,updated_by,robot_model,robot_model_reported_at)
         VALUES($1,'openai',0,'robot',$2,$3)
         ON CONFLICT(device_id,service) DO UPDATE SET robot_model=excluded.robot_model,
           robot_model_reported_at=excluded.robot_model_reported_at`,
        [input.deviceId, input.models.openai, reportedAt],
      );
    }
    for (const service of HEALTH_SERVICES) {
      const health = input.health[service];
      if (!health) continue;
      await db.query(
        `INSERT INTO device_key_health(device_id,service,state,code,reported_at) VALUES($1,$2,$3,$4,$5)
         ON CONFLICT(device_id,service) DO UPDATE SET state=excluded.state,code=excluded.code,reported_at=excluded.reported_at`,
        [input.deviceId, service, health.state, health.code, reportedAt],
      );
    }
    const result = {} as Record<ServiceName, ServiceKeySync>;
    for (const service of SERVICE_NAMES) {
      const row = (await db.query(
        "SELECT key_version,ciphertext FROM device_service_keys WHERE device_id=$1 AND service=$2 FOR UPDATE",
        [input.deviceId, service],
      )).rows[0];
      const version = (row?.key_version ?? 0) as number;
      if (version === 0) { result[service] = { keyVersion: 0, changed: false, apiKey: null }; continue; }
      // What the robot reports holding, not what we are about to send: a lost reply
      // or failed write must not show the robot as up to date.
      await db.query(
        `UPDATE device_service_keys SET robot_key_version=$3,robot_key_fetched_at=$4
         WHERE device_id=$1 AND service=$2`, [input.deviceId, service, input.known[service], reportedAt],
      );
      if (input.known[service] === version) { result[service] = { keyVersion: version, changed: false, apiKey: null }; continue; }
      const apiKey = row.ciphertext === null ? null
        : await decryptServiceKey(row.ciphertext, input.deviceId, service, version, input.secret);
      // apiKey null with changed=true means the owner deleted the key.
      result[service] = { keyVersion: version, changed: true, apiKey };
    }
    return result;
  });
}

/** Re-registering with "지우기": the previous household's keys go, as if the owner deleted them. */
export async function deleteServiceKeysForNewHousehold(db: SqlExecutor, deviceId: string, userId: string) {
  const deleted = (await db.query(
    `UPDATE device_service_keys SET key_version=key_version+1, ciphertext=NULL, last4=NULL,
       updated_by=$2, updated_at=CURRENT_TIMESTAMP
     WHERE device_id=$1 AND ciphertext IS NOT NULL RETURNING service`,
    [deviceId, userId],
  )).rows.map((row) => row.service as string);
  await db.query("DELETE FROM device_key_health WHERE device_id=$1", [deviceId]);
  return deleted;
}
