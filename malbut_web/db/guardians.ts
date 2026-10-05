import { randomUUID } from "node:crypto";
import { ensureDatabaseSchema } from "./migration-state";
import { getPostgresPool, type SqlExecutor } from "./postgres";
import { labelFor, userLabels } from "./users";
import { inviteTokenDigest, newInviteToken, openInviteToken, sealInviteToken } from "./web-auth";

export const INVITE_TTL_MS = 24 * 60 * 60 * 1000;
const INVITE_TOKEN = /^[A-Za-z0-9_-]{43}$/;

export type InviteLink = { token: string; expiresAt: string; joined: number };
export type InviteAcceptResult = {
  status: "joined" | "already_family" | "owner" | "unusable";
  deviceId?: string;
};

const iso = (value: Date | string) => new Date(value).toISOString();

async function inTransaction<T>(work: (db: SqlExecutor) => Promise<T>) {
  await ensureDatabaseSchema();
  const client = await getPostgresPool().connect();
  try {
    await client.query("BEGIN");
    const result = await work(client);
    await client.query("COMMIT");
    return result;
  } catch (error) { await client.query("ROLLBACK"); throw error; }
  finally { client.release(); }
}

async function requireOwner(db: SqlExecutor, deviceId: string, userId: string) {
  // Held until COMMIT: losing ownership cannot race this change.
  const row = (await db.query(
    "SELECT role FROM device_memberships WHERE device_id=$1 AND user_id=$2 FOR SHARE", [deviceId, userId],
  )).rows[0];
  if (row?.role !== "owner") throw new Error("GUARDIANS_FORBIDDEN");
}

async function audit(db: SqlExecutor, deviceId: string, userId: string, action: string, metadata: object, at: string) {
  await db.query(
    `INSERT INTO access_audit_log(id,device_id,actor_type,actor_id,action,metadata_json,created_at)
     VALUES($1,$2,'user',$3,$4,$5,$6)`,
    [randomUUID(), deviceId, userId, action, JSON.stringify(metadata), at],
  );
}

/** A new link for 24 hours. The 말벗 has one live link at a time: making one cancels the last. */
export async function createInviteLink(input: {
  deviceId: string; ownerUserId: string; sessionSecret: string; now?: Date;
}): Promise<InviteLink> {
  const now = input.now ?? new Date();
  return inTransaction(async (db) => {
    await requireOwner(db, input.deviceId, input.ownerUserId);
    await db.query("UPDATE device_invites SET revoked_at=$2 WHERE device_id=$1 AND revoked_at IS NULL",
      [input.deviceId, now.toISOString()]);
    const token = newInviteToken(), id = randomUUID();
    const expiresAt = new Date(now.getTime() + INVITE_TTL_MS).toISOString();
    await db.query(
      `INSERT INTO device_invites(id,device_id,token_digest,token_ciphertext,created_by,created_at,expires_at)
       VALUES($1,$2,$3,$4,$5,$6,$7)`,
      [id, input.deviceId, inviteTokenDigest(token, input.sessionSecret), sealInviteToken(token, input.sessionSecret),
        input.ownerUserId, now.toISOString(), expiresAt],
    );
    await audit(db, input.deviceId, input.ownerUserId, "invite.created", { inviteId: id, expiresAt }, now.toISOString());
    return { token, expiresAt, joined: 0 };
  });
}

/** The live link, for the owner to copy or share again, and how many came in by it. */
export async function currentInviteLink(deviceId: string, sessionSecret: string, now = new Date()) {
  await ensureDatabaseSchema();
  const row = (await getPostgresPool().query(
    `SELECT i.token_ciphertext, i.expires_at,
       (SELECT count(*)::int FROM device_memberships m WHERE m.invite_id=i.id) AS joined
     FROM device_invites i WHERE i.device_id=$1 AND i.revoked_at IS NULL AND i.expires_at>$2`,
    [deviceId, now.toISOString()],
  )).rows[0];
  if (!row) return null;
  return { token: openInviteToken(row.token_ciphertext, sessionSecret), expiresAt: iso(row.expires_at),
    joined: row.joined } satisfies InviteLink;
}

/** "링크 취소": nobody new can come in; guardians already in stay. */
export async function revokeInviteLink(input: { deviceId: string; ownerUserId: string; now?: Date }) {
  const now = (input.now ?? new Date()).toISOString();
  return inTransaction(async (db) => {
    await requireOwner(db, input.deviceId, input.ownerUserId);
    const revoked = (await db.query(
      "UPDATE device_invites SET revoked_at=$2 WHERE device_id=$1 AND revoked_at IS NULL RETURNING id",
      [input.deviceId, now],
    )).rows;
    if (revoked.length) await audit(db, input.deviceId, input.ownerUserId, "invite.revoked", { inviteId: revoked[0].id }, now);
    return revoked.length > 0;
  });
}

/** What the invite page shows before sign-in: who invited, to which 말벗. Null when the link cannot be used. */
export async function describeInvite(token: string, sessionSecret: string, now = new Date()) {
  if (!INVITE_TOKEN.test(token)) return null;
  await ensureDatabaseSchema();
  const pool = getPostgresPool();
  const invite = (await pool.query(
    `SELECT i.device_id, d.display_name FROM device_invites i JOIN devices d ON d.id=i.device_id
     WHERE i.token_digest=$1 AND i.revoked_at IS NULL AND i.expires_at>$2`,
    [inviteTokenDigest(token, sessionSecret), now.toISOString()],
  )).rows[0];
  if (!invite) return null;
  const owner = (await pool.query(
    "SELECT user_id FROM device_memberships WHERE device_id=$1 AND role='owner' ORDER BY created_at LIMIT 1",
    [invite.device_id],
  )).rows[0]?.user_id as string | undefined;
  const labels = await userLabels([owner]);
  return { deviceName: invite.display_name as string, ownerName: owner ? labelFor(labels, owner) : null };
}

/** Signed in through the link: become a guardian of that 말벗, once. */
export async function acceptInvite(input: {
  token: string; userId: string; sessionSecret: string; now?: Date;
}): Promise<InviteAcceptResult> {
  if (!INVITE_TOKEN.test(input.token)) return { status: "unusable" };
  const now = (input.now ?? new Date()).toISOString();
  const digest = inviteTokenDigest(input.token, input.sessionSecret);
  return inTransaction(async (db) => {
    const live = "SELECT id, device_id FROM device_invites WHERE token_digest=$1 AND revoked_at IS NULL AND expires_at>$2";
    const found = (await db.query(live, [digest, now])).rows[0];
    if (!found) return { status: "unusable" };
    // The 말벗 first, then the link, like re-registering: neither can wait on the other.
    await db.query("SELECT id FROM devices WHERE id=$1 FOR NO KEY UPDATE", [found.device_id]);
    const invite = (await db.query(`${live} FOR SHARE`, [digest, now])).rows[0];
    if (!invite) return { status: "unusable" };
    const deviceId = invite.device_id as string;
    const role = (await db.query(
      "SELECT role FROM device_memberships WHERE device_id=$1 AND user_id=$2", [deviceId, input.userId],
    )).rows[0]?.role;
    if (role === "owner") return { status: "owner", deviceId };
    if (role) return { status: "already_family", deviceId };
    await db.query(
      "INSERT INTO device_memberships(device_id,user_id,role,created_at,invite_id) VALUES($1,$2,'family',$3,$4)",
      [deviceId, input.userId, now, invite.id],
    );
    await audit(db, deviceId, input.userId, "family.joined", { inviteId: invite.id }, now);
    return { status: "joined", deviceId };
  });
}

/** "소유자 넘기기": a guardian becomes the owner and the owner stays on as a guardian. */
export async function transferOwnership(input: {
  deviceId: string; ownerUserId: string; newOwnerUserId: string; now?: Date;
}) {
  const now = (input.now ?? new Date()).toISOString();
  return inTransaction(async (db) => {
    await db.query("SELECT id FROM devices WHERE id=$1 FOR NO KEY UPDATE", [input.deviceId]);
    const roles = new Map((await db.query(
      "SELECT user_id, role FROM device_memberships WHERE device_id=$1 AND user_id = ANY($2::text[]) FOR UPDATE",
      [input.deviceId, [input.ownerUserId, input.newOwnerUserId]],
    )).rows.map((row) => [row.user_id as string, row.role as string]));
    if (roles.get(input.ownerUserId) !== "owner") throw new Error("GUARDIANS_FORBIDDEN");
    if (input.newOwnerUserId === input.ownerUserId || roles.get(input.newOwnerUserId) !== "family") {
      throw new Error("OWNER_TARGET_INVALID");
    }
    await db.query("UPDATE device_memberships SET role='family' WHERE device_id=$1 AND user_id=$2",
      [input.deviceId, input.ownerUserId]);
    await db.query("UPDATE device_memberships SET role='owner' WHERE device_id=$1 AND user_id=$2",
      [input.deviceId, input.newOwnerUserId]);
    await audit(db, input.deviceId, input.ownerUserId, "owner.transferred", { userId: input.newOwnerUserId }, now);
  });
}
