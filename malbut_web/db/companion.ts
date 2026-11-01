import type { PoolClient } from "pg";
import { getPostgresPool } from "./postgres";
import type { DeviceIdentity } from "./homecam";
import { companionCategories, type CompanionChange, type CompanionView } from "../app/companion-contract";

async function transaction<T>(work: (client: PoolClient) => Promise<T>): Promise<T> {
  const client = await getPostgresPool().connect();
  try { await client.query("BEGIN"); const result = await work(client); await client.query("COMMIT"); return result; }
  catch (error) { await client.query("ROLLBACK"); throw error; }
  finally { client.release(); }
}
async function policy(client: PoolClient, deviceId: string, userId?: string) {
  const owner = (await client.query("SELECT user_id FROM device_memberships WHERE device_id=$1 AND role='owner' FOR SHARE", [deviceId])).rows[0]?.user_id;
  if (!owner || (userId !== undefined && owner !== userId)) throw new Error("COMPANION_FORBIDDEN");
  await client.query("INSERT INTO device_companion_policy(device_id,owner_id) VALUES($1,$2) ON CONFLICT DO NOTHING", [deviceId, owner]);
  // Transferred devices cannot reuse a former owner's consent or confirmation.
  await client.query(`UPDATE device_companion_policy SET owner_id=$2, revision=revision+1,
    daily=FALSE,health=FALSE,family=FALSE,external_processing=FALSE,
    confirmed_session_id=NULL,confirmed_until=NULL WHERE device_id=$1 AND owner_id<>$2`, [deviceId, owner]);
  return (await client.query(`SELECT *,session_seen_at>clock_timestamp()-INTERVAL '10 seconds' AS online,
    confirmed_session_id=session_id AND confirmed_until>clock_timestamp() AS confirmed,
    GREATEST(0,LEAST(300,EXTRACT(EPOCH FROM confirmed_until-clock_timestamp())))::float8 AS remaining
    FROM device_companion_policy WHERE device_id=$1 FOR UPDATE`, [deviceId])).rows[0];
}
function view(row: Record<string, unknown>): CompanionView {
  return { revision: Number(row.revision), eraseRevision: Number(row.erase_revision),
    categories: companionCategories.filter((c) => row[c] === true), externalProcessing: row.external_processing === true,
    sessionId: row.online && typeof row.session_id === "string" ? row.session_id : null,
    confirmed: row.online === true && row.confirmed === true, online: row.online === true,
    appliedRevision: Number(row.applied_revision) };
}
export async function companionView(deviceId: string, userId: string) {
  return transaction(async (client) => view(await policy(client, deviceId, userId)));
}
export async function changeCompanion(deviceId: string, userId: string, input: CompanionChange) {
  return transaction(async (client) => {
    const row = await policy(client, deviceId, userId);
    if (Number(row.revision) !== input.expectedRevision) throw new Error("COMPANION_CHANGED");
    if (input.action === "confirm") {
      if (!row.online || row.session_id !== input.sessionId || !row.daily || !row.external_processing) throw new Error("COMPANION_NO_ENCOUNTER");
      await client.query(`UPDATE device_companion_policy SET revision=revision+1,
        confirmed_session_id=$2,confirmed_until=clock_timestamp()+INTERVAL '5 minutes' WHERE device_id=$1`, [deviceId, input.sessionId]);
    } else {
      await client.query(`UPDATE device_companion_policy SET revision=revision+1,
        confirmed_session_id=NULL,confirmed_until=NULL WHERE device_id=$1`, [deviceId]);
      if (input.action === "erase") await client.query("UPDATE device_companion_policy SET erase_revision=erase_revision+1 WHERE device_id=$1", [deviceId]);
      if (input.action === "consent") await client.query(`UPDATE device_companion_policy SET daily=$2,health=$3,family=$4,external_processing=$5 WHERE device_id=$1`,
        [deviceId, ...companionCategories.map((c) => input.categories.includes(c)), input.externalProcessing]);
    }
    return view(await policy(client, deviceId, userId));
  });
}
export async function syncCompanion(device: DeviceIdentity, report: { sessionId: string | null; appliedRevision: number }) {
  return transaction(async (client) => {
    const credential = await client.query(`SELECT 1 FROM device_credentials WHERE id=$1 AND device_id=$2
      AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at>clock_timestamp()) FOR SHARE`, [device.credentialId, device.deviceId]);
    if (!credential.rowCount) throw new Error("COMPANION_FORBIDDEN");
    const before = await policy(client, device.deviceId);
    await client.query(`UPDATE device_companion_policy SET session_id=$2,session_seen_at=clock_timestamp(),
      confirmed_session_id=CASE WHEN session_id IS NOT DISTINCT FROM $2 THEN confirmed_session_id ELSE NULL END,
      confirmed_until=CASE WHEN session_id IS NOT DISTINCT FROM $2 THEN confirmed_until ELSE NULL END,
      applied_revision=$3 WHERE device_id=$1`, [device.deviceId, report.sessionId, Math.min(report.appliedRevision, Number(before.revision))]);
    const row = await policy(client, device.deviceId);
    return { ownerId: row.owner_id, revision: Number(row.revision), eraseRevision: Number(row.erase_revision),
      categories: companionCategories.filter((c) => row[c] === true), externalProcessing: row.external_processing === true,
      confirmedSessionId: row.confirmed ? row.confirmed_session_id : null,
      remainingSeconds: row.confirmed ? Number(row.remaining) : 0 };
  });
}
