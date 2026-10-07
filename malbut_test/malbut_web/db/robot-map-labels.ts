/**
 * 지도 탭 › 지도 관리 (SWM25-237): names for the robot's saved maps.
 *
 * The robot names map files in ASCII (map-20261007-1430.yaml); people see the name kept
 * here, which the owner sets when making a map and can change any time, even while the
 * 말벗 is off. Only the owner manages maps, so only the owner reads and writes names.
 */
import { getD1 } from ".";
import { ensureHomecamSchema, userCanManageDevice, writeAuditLog } from "./homecam";

export const MAP_FILE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}\.ya?ml$/;

/** A shown map name: 1 to 40 characters without control characters, trimmed. */
export function cleanMapLabel(value: unknown) {
  if (typeof value !== "string") return null;
  const name = value.trim();
  return name.length >= 1 && name.length <= 40 && !/[\u0000-\u001f\u007f]/.test(name) ? name : null;
}

export async function readRobotMapLabels(deviceId: string, userId: string) {
  await ensureHomecamSchema();
  if (!(await userCanManageDevice(deviceId, userId))) return null;
  const rows = await getD1()
    .prepare("SELECT map_file, name FROM robot_map_labels WHERE device_id = ?")
    .bind(deviceId)
    .all<{ map_file: string; name: string }>();
  return Object.fromEntries(rows.results.map((row) => [row.map_file, row.name]));
}

export async function saveRobotMapLabel(input: {
  deviceId: string;
  userId: string;
  mapFile: string;
  name: string;
}) {
  await ensureHomecamSchema();
  if (!(await userCanManageDevice(input.deviceId, input.userId))) throw new Error("FORBIDDEN");
  await getD1()
    .prepare(
      `INSERT INTO robot_map_labels (device_id, map_file, name, updated_by, updated_at)
       VALUES (?, ?, ?, ?, ?)
       ON CONFLICT(device_id, map_file) DO UPDATE SET
         name = excluded.name, updated_by = excluded.updated_by, updated_at = excluded.updated_at`,
    )
    .bind(input.deviceId, input.mapFile, input.name, input.userId, new Date().toISOString())
    .run();
  await writeAuditLog({
    deviceId: input.deviceId, actorType: "user", actorId: input.userId,
    action: "robot.map_named", metadata: { map: input.mapFile },
  });
}
