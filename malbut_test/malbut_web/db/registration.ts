import { randomBytes, randomUUID } from "node:crypto";
import { ensureDatabaseSchema } from "./migration-state";
import { getPostgresPool } from "./postgres";
import { registrationCodeDigest } from "./web-auth";

export const REGISTRATION_CODE_TTL_MS = 7 * 24 * 60 * 60 * 1000;
// No 0/O or 1/I: the code is read off a note and typed by hand.
const CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
const CODE_LENGTH = 8;

/** What re-registering does with the earlier fall incidents, their opinions and activity. */
export type RegistrationHistory = "keep" | "delete";

export type RegistrationResult =
  | { status: "registered"; deviceId: string }
  | { status: "needs_confirmation" | "already_owner" | "invalid" | "used" | "expired" };

/** "7q2k 9xhm" or "7Q2K-9XHM" → "7Q2K9XHM"; anything that cannot be a code is null. */
export function normalizeRegistrationCode(value: unknown) {
  if (typeof value !== "string" || value.length > 32) return null;
  const compact = value.replace(/[\s-]/g, "").toUpperCase();
  if (compact.length !== CODE_LENGTH) return null;
  return [...compact].every((character) => CODE_ALPHABET.includes(character)) ? compact : null;
}

export function formatRegistrationCode(code: string) {
  return `${code.slice(0, 4)}-${code.slice(4)}`;
}

/** A new code for one 말벗, valid for 7 days and once. An unused earlier code stops working. */
export async function createRegistrationCode(input: { deviceId: string; sessionSecret: string; now?: Date }) {
  await ensureDatabaseSchema();
  // 256 is a multiple of 32, so every character is equally likely.
  const code = Array.from(randomBytes(CODE_LENGTH), (byte) => CODE_ALPHABET[byte % CODE_ALPHABET.length]).join("");
  const now = input.now ?? new Date();
  const expiresAt = new Date(now.getTime() + REGISTRATION_CODE_TTL_MS).toISOString();
  const client = await getPostgresPool().connect();
  try {
    await client.query("BEGIN");
    const device = await client.query("SELECT id FROM devices WHERE id=$1 FOR NO KEY UPDATE", [input.deviceId]);
    if (!device.rowCount) throw new Error("REGISTRATION_DEVICE_NOT_FOUND");
    await client.query("DELETE FROM device_registration_codes WHERE device_id=$1 AND used_at IS NULL", [input.deviceId]);
    await client.query(
      `INSERT INTO device_registration_codes(code_digest,device_id,created_at,expires_at)
       VALUES($1,$2,$3,$4)`,
      [registrationCodeDigest(code, input.sessionSecret), input.deviceId, now.toISOString(), expiresAt],
    );
    await client.query(
      `INSERT INTO access_audit_log(id,device_id,actor_type,actor_id,action,metadata_json)
       VALUES($1,$2,'system','registration-code','registration_code.created',$3)`,
      [randomUUID(), input.deviceId, JSON.stringify({ expiresAt })],
    );
    await client.query("COMMIT");
    return { code: formatRegistrationCode(code), expiresAt };
  } catch (error) { await client.query("ROLLBACK"); throw error; }
  finally { client.release(); }
}

/**
 * The person who types a valid code becomes the 말벗's only owner. If anyone else is
 * already a member, nothing changes until they confirm and choose what happens to the
 * earlier incidents: then every other owner and guardian loses access in the same step.
 */
export async function redeemRegistrationCode(input: {
  code: string;
  userId: string;
  history?: RegistrationHistory;
  sessionSecret: string;
  now?: Date;
}): Promise<RegistrationResult> {
  await ensureDatabaseSchema();
  const now = input.now ?? new Date();
  const client = await getPostgresPool().connect();
  const stop = async (status: Exclude<RegistrationResult["status"], "registered">) => {
    await client.query("ROLLBACK");
    return { status };
  };
  try {
    await client.query("BEGIN");
    const digest = registrationCodeDigest(input.code, input.sessionSecret);
    const found = await client.query<{
      device_id: string; expires_at: Date | string; used_at: Date | string | null; used_by: string | null;
    }>(
      "SELECT device_id, expires_at, used_at, used_by FROM device_registration_codes WHERE code_digest=$1 FOR UPDATE",
      [digest],
    );
    const code = found.rows[0];
    if (!code) return await stop("invalid");
    if (code.used_at) {
      // A repeated submit by the person who already used it is not an error.
      const owner = code.used_by === input.userId && (await client.query(
        "SELECT 1 FROM device_memberships WHERE device_id=$1 AND user_id=$2 AND role='owner'",
        [code.device_id, input.userId],
      )).rowCount;
      if (!owner) return await stop("used");
      await client.query("ROLLBACK");
      return { status: "registered", deviceId: code.device_id };
    }
    if (new Date(code.expires_at).getTime() <= now.getTime()) return await stop("expired");
    const deviceId = code.device_id;

    // Two codes for one 말벗 cannot both win: membership changes wait for this lock.
    await client.query("SELECT id FROM devices WHERE id=$1 FOR NO KEY UPDATE", [deviceId]);
    const members = await client.query<{ user_id: string; role: string }>(
      "SELECT user_id, role FROM device_memberships WHERE device_id=$1",
      [deviceId],
    );
    if (members.rows.some((member) => member.user_id === input.userId && member.role === "owner")) {
      return await stop("already_owner");
    }
    const others = members.rows.filter((member) => member.user_id !== input.userId);
    if (others.length && !input.history) return await stop("needs_confirmation");

    const nowIso = now.toISOString();
    await client.query("DELETE FROM device_memberships WHERE device_id=$1 AND user_id<>$2", [deviceId, input.userId]);
    await client.query(
      `UPDATE push_subscriptions SET revoked_at=$3
       WHERE device_id=$1 AND user_id<>$2 AND revoked_at IS NULL`,
      [deviceId, input.userId, nowIso],
    );
    await client.query("DELETE FROM talk_leases WHERE device_id=$1 AND user_id<>$2", [deviceId, input.userId]);
    // Opinions, activity, scenes, people boxes, reminders and AI reviews go with their incident.
    const deleted = input.history === "delete"
      ? (await client.query("DELETE FROM fall_incidents WHERE device_id=$1", [deviceId])).rowCount ?? 0
      : 0;
    await client.query(
      `INSERT INTO device_memberships(device_id,user_id,role,created_at) VALUES($1,$2,'owner',$3)
       ON CONFLICT(device_id,user_id) DO UPDATE SET role='owner'`,
      [deviceId, input.userId, nowIso],
    );
    await client.query(
      "UPDATE device_registration_codes SET used_at=$2, used_by=$3 WHERE code_digest=$1",
      [digest, nowIso, input.userId],
    );
    await client.query(
      `INSERT INTO access_audit_log(id,device_id,actor_type,actor_id,action,metadata_json,created_at)
       VALUES($1,$2,'user',$3,'device.registered',$4,$5)`,
      [randomUUID(), deviceId, input.userId, JSON.stringify({
        removedMembers: others.length,
        history: input.history ?? null,
        deletedIncidents: deleted,
      }), nowIso],
    );
    await client.query("COMMIT");
    return { status: "registered", deviceId };
  } catch (error) { await client.query("ROLLBACK"); throw error; }
  finally { client.release(); }
}
