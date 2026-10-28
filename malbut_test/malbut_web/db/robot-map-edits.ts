import { getD1 } from ".";
import { ensureHomecamSchema, userCanManageDevice } from "./homecam";
import { MAP_FILE } from "./robot-map-labels";
import { parseRobotMap } from "../app/robot-contract";
import type { SavedMapEdit } from "../app/robot-map-edit";

export function parseSavedMapEdit(value: unknown): SavedMapEdit | null {
  if (!value || typeof value !== "object") return null;
  const v = value as SavedMapEdit;
  if (typeof v.map !== "string" || !MAP_FILE.test(v.map) ||
      ![v.revision, v.originalRevision, v.appliedRevision].every((r) => typeof r === "string" &&
        /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(r))) return null;
  const parsed = parseRobotMap({ finalized: true, revision: v.revision, mapId: v.map,
    mapRevision: v.revision, sourceCreatedAt: null, geometry: v.geometry,
    previewBase64: v.previewBase64, userMap: null, semanticZones: null });
  return parsed ? { map: v.map, revision: v.revision, originalRevision: v.originalRevision,
    appliedRevision: v.appliedRevision, geometry: parsed.geometry, previewBase64: parsed.previewBase64 } : null;
}

export async function storeSavedMapEdit(deviceId: string, value: SavedMapEdit) {
  await ensureHomecamSchema();
  await getD1().prepare(`INSERT INTO robot_saved_map_versions (device_id, map_file, revision, snapshot_json, updated_at)
    VALUES (?, ?, ?, ?, ?) ON CONFLICT (device_id, map_file, revision) DO UPDATE SET
      snapshot_json = excluded.snapshot_json, updated_at = excluded.updated_at`)
    .bind(deviceId, value.map, value.revision, JSON.stringify(value), new Date().toISOString()).run();
}

export async function readSavedMapEdit(deviceId: string, userId: string, map: string) {
  await ensureHomecamSchema();
  if (!(await userCanManageDevice(deviceId, userId))) throw new Error("FORBIDDEN");
  const row = await getD1().prepare(`SELECT snapshot_json FROM robot_saved_map_versions
    WHERE device_id = ? AND map_file = ? ORDER BY updated_at DESC LIMIT 1`)
    .bind(deviceId, map).first<{ snapshot_json: string }>();
  return row ? JSON.parse(row.snapshot_json) as SavedMapEdit : null;
}
