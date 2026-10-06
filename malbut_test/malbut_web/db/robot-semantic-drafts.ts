/**
 * 방·구역 편집의 반영 대기 (SWM25-237).
 *
 * The owner saves rooms or Zones here, online or not. When the robot is online and driving
 * on a saved map (navigation), the server queues the latest save as a robot command
 * (rooms_save / zones_apply). A save for another map (a new map, another saved map, or the
 * same map remade) is never applied: it turns stale and the owner is told to edit again.
 * Robot commands expire after 60 s, so saves wait here, not in the command queue.
 */
import { getD1 } from ".";
import { ensureHomecamSchema, userCanManageDevice, writeAuditLog } from "./homecam";

export type SemanticDraftKind = "rooms" | "zones";
export type SemanticDraftStatus = "pending" | "sent" | "applied" | "stale" | "failed";
/** What the edit screen shows about the latest save; the saved rooms or Zones are not sent back. */
export type SemanticDraftView = {
  status: SemanticDraftStatus;
  error: string | null;
  savedAt: string;
  resolvedAt: string | null;
};

type DraftRow = {
  kind: SemanticDraftKind;
  map_id: string;
  map_revision: string;
  payload_json: string;
  status: SemanticDraftStatus;
  error: string | null;
  command_id: string | null;
  saved_by: string;
  saved_at: string;
  resolved_at: string | null;
};

const ROBOT_ONLINE_MS = 15_000;
const OPERATIONS: Record<SemanticDraftKind, "rooms_save" | "zones_apply"> = {
  rooms: "rooms_save",
  zones: "zones_apply",
};
// The robot refuses a save for a map it no longer uses: the real robot says "changed",
// the simulator says the map_id or map_revision "does not match".
const MAP_CHANGED = /map changed|map_id does not match|map_revision does not match/i;

const iso = (value: unknown) => (value instanceof Date ? value.toISOString() : String(value));

/**
 * Keep the owner's latest rooms or Zones for the robot's current saved map, then send them
 * if the robot is ready. Throws MAP_CHANGED when the editor loaded another map.
 */
export async function saveSemanticDraft(input: {
  deviceId: string;
  userId: string;
  kind: SemanticDraftKind;
  mapId: string;
  mapRevision: string;
  payload: unknown;
}) {
  await ensureHomecamSchema();
  if (!(await userCanManageDevice(input.deviceId, input.userId))) throw new Error("FORBIDDEN");
  const map = await currentMap(input.deviceId);
  if (!map) throw new Error("MAP_NOT_FOUND");
  if (map.map_id !== input.mapId || map.map_revision !== input.mapRevision) {
    throw new Error("MAP_CHANGED");
  }
  const now = new Date().toISOString();
  await getD1()
    .prepare(
      `INSERT INTO robot_semantic_drafts
       (device_id, kind, map_id, map_revision, payload_json, status, error, command_id,
        saved_by, saved_at, resolved_at)
       VALUES (?, ?, ?, ?, ?, 'pending', NULL, NULL, ?, ?, NULL)
       ON CONFLICT(device_id, kind) DO UPDATE SET
         map_id = excluded.map_id, map_revision = excluded.map_revision,
         payload_json = excluded.payload_json, status = 'pending', error = NULL,
         command_id = NULL, saved_by = excluded.saved_by, saved_at = excluded.saved_at,
         resolved_at = NULL`,
    )
    .bind(input.deviceId, input.kind, input.mapId, input.mapRevision,
      JSON.stringify(input.payload), input.userId, now)
    .run();
  await writeAuditLog({
    deviceId: input.deviceId, actorType: "user", actorId: input.userId,
    action: `robot.${input.kind}_saved`, metadata: { mapId: input.mapId },
  });
  await dispatchSemanticDrafts(input.deviceId);
  const row = (await readDrafts(input.deviceId)).find((draft) => draft.kind === input.kind);
  return row ? draftView(row) : null;
}

/**
 * The robot's saved map as the owner sees it: saves the robot has not taken yet replace
 * its rooms or Zones, and `drafts` says where each save stands. Others see what the robot uses.
 */
export async function overlaySemanticDrafts<T extends {
  mapId: string;
  mapRevision: string;
  updatedAt: string;
  userMap: Record<string, unknown> | null;
  zones: Record<string, unknown> | null;
}>(deviceId: string, userId: string, semantics: T) {
  if (!(await userCanManageDevice(deviceId, userId))) return semantics;
  await settleSemanticDrafts(deviceId);
  const drafts: Record<SemanticDraftKind, SemanticDraftView | null> = { rooms: null, zones: null };
  let { userMap, zones } = semantics;
  for (const row of await readDrafts(deviceId)) {
    drafts[row.kind] = draftView(row);
    const sameMap = row.map_id === semantics.mapId && row.map_revision === semantics.mapRevision;
    // A save the robot took shows until the robot uploads the map with it.
    const waiting = row.status === "pending" || row.status === "sent" ||
      (row.status === "applied" && row.resolved_at !== null &&
        Date.parse(iso(semantics.updatedAt)) < Date.parse(iso(row.resolved_at)));
    if (!sameMap || !waiting) continue;
    const payload = parseJson(row.payload_json);
    if (row.kind === "rooms" && userMap && Array.isArray(userMap.features) && Array.isArray(payload)) {
      const kept = userMap.features.filter((feature) =>
        !(isObject(feature) && isObject(feature.properties) && feature.properties.role === "room"));
      userMap = { ...userMap, features: [...kept, ...payload] };
    } else if (row.kind === "zones" && isObject(payload)) {
      // The robot's own fields (map file, editable, message) stay.
      zones = { ...(zones ?? {}), ...payload };
    }
  }
  return { ...semantics, userMap, zones, drafts };
}

/**
 * Settle saves without sending anything: a sent save follows its robot command (applied,
 * failed, stale, or pending again when the command timed out before the robot answered),
 * and a waiting save for a map the robot no longer has turns stale.
 */
export async function settleSemanticDrafts(deviceId: string) {
  const sent = await getD1()
    .prepare(
      `SELECT d.kind, d.command_id, c.status AS command_status, c.result_json
       FROM robot_semantic_drafts d
       LEFT JOIN robot_commands c ON c.id = d.command_id
       WHERE d.device_id = ? AND d.status = 'sent'`,
    )
    .bind(deviceId)
    .all<{ kind: SemanticDraftKind; command_id: string; command_status: string | null;
      result_json: string | null }>();
  for (const row of sent.results) {
    if (row.command_status === "queued" || row.command_status === "claimed") continue;
    let status: SemanticDraftStatus = "pending";
    let error: string | null = null;
    if (row.command_status === "completed") {
      status = "applied";
    } else if (row.command_status === "failed") {
      const result = parseJson(row.result_json ?? "");
      const message = isObject(result) && typeof result.error === "string"
        ? result.error
        : "ROBOT_COMMAND_FAILED";
      if (MAP_CHANGED.test(message)) {
        status = "stale";
        error = "MAP_CHANGED";
      } else if (message !== "ROBOT_COMMAND_TIMEOUT") {
        status = "failed";
        error = message.slice(0, 300);
      }
    }
    const again = status === "pending";
    await getD1()
      .prepare(
        `UPDATE robot_semantic_drafts SET status = ?, error = ?, command_id = ?, resolved_at = ?
         WHERE device_id = ? AND kind = ? AND status = 'sent' AND command_id = ?`,
      )
      .bind(status, error, again ? null : row.command_id, again ? null : new Date().toISOString(),
        deviceId, row.kind, row.command_id)
      .run();
  }
  const map = await currentMap(deviceId);
  if (!map) return;
  await getD1()
    .prepare(
      `UPDATE robot_semantic_drafts
       SET status = 'stale', error = 'MAP_CHANGED', command_id = NULL, resolved_at = ?
       WHERE device_id = ? AND status = 'pending' AND (map_id <> ? OR map_revision <> ?)`,
    )
    .bind(new Date().toISOString(), deviceId, map.map_id, map.map_revision)
    .run();
}

/**
 * Send one waiting save (rooms before Zones) when the robot is online in navigation on the
 * save's map. Called when the owner saves and whenever the robot asks for commands.
 */
export async function dispatchSemanticDrafts(deviceId: string) {
  await settleSemanticDrafts(deviceId);
  const pending = (await readDrafts(deviceId))
    .filter((draft) => draft.status === "pending")
    .sort((a, b) => (a.kind === "rooms" ? 0 : 1) - (b.kind === "rooms" ? 0 : 1));
  if (!pending.length) return;
  const state = await getD1()
    .prepare("SELECT nav2_json, observed_at FROM robot_runtime_state WHERE device_id = ?")
    .bind(deviceId)
    .first<{ nav2_json: string; observed_at: string }>();
  if (!state || Date.parse(iso(state.observed_at)) < Date.now() - ROBOT_ONLINE_MS) return;
  const nav2 = parseJson(state.nav2_json);
  if (!isObject(nav2) || nav2.runtime_mode !== "navigation") return;
  const map = await currentMap(deviceId);
  if (!map) return;
  // settleSemanticDrafts made every save for another map stale.
  const draft = pending.find((item) =>
    item.map_id === map.map_id && item.map_revision === map.map_revision);
  if (!draft) return;
  const saved = parseJson(draft.payload_json);
  const payload = draft.kind === "rooms"
    ? { map_id: draft.map_id, map_revision: draft.map_revision, rooms: saved,
      resolution: map.resolution }
    : saved;
  let inserted: { id: string } | null = null;
  try {
    inserted = await getD1()
      .prepare(
        `INSERT INTO robot_commands
         (id, device_id, operation, payload_json, requested_by, status, requested_at)
         SELECT ?, ?, ?, ?, ?, 'queued', ?
         WHERE NOT EXISTS (
           SELECT 1 FROM robot_commands WHERE device_id = ? AND status IN ('queued', 'claimed')
         )
         RETURNING id`,
      )
      .bind(crypto.randomUUID(), deviceId, OPERATIONS[draft.kind], JSON.stringify(payload),
        draft.saved_by, new Date().toISOString(), deviceId)
      .first<{ id: string }>();
  } catch (error) {
    // Another command took the one slot at the same moment: the next poll sends it.
    if (!(error instanceof Error && error.message.startsWith("UNIQUE_CONSTRAINT"))) throw error;
  }
  if (!inserted) return;
  await getD1()
    .prepare(
      `UPDATE robot_semantic_drafts SET status = 'sent', command_id = ?
       WHERE device_id = ? AND kind = ? AND status = 'pending' AND saved_at = ?`,
    )
    .bind(inserted.id, deviceId, draft.kind, iso(draft.saved_at))
    .run();
}

/**
 * What the owner's room and Zone edits are checked against: the robot's current saved map
 * and whether it is the real robot (its Zone limits). Null for anyone but the owner.
 */
export async function getSpaceEditContext(deviceId: string, userId: string) {
  await ensureHomecamSchema();
  if (!(await userCanManageDevice(deviceId, userId))) return null;
  const [map, state] = await Promise.all([
    currentMap(deviceId),
    getD1()
      .prepare("SELECT nav2_json FROM robot_runtime_state WHERE device_id = ?")
      .bind(deviceId)
      .first<{ nav2_json: string }>(),
  ]);
  const nav2 = state ? parseJson(state.nav2_json) : null;
  return {
    map: map
      ? { mapId: map.map_id, mapRevision: map.map_revision, resolution: Number(map.resolution) }
      : null,
    realRobot: isObject(nav2) && nav2.robot_interface === "malbut_manager_v1",
  };
}

async function currentMap(deviceId: string) {
  return getD1()
    .prepare("SELECT map_id, map_revision, resolution FROM robot_maps WHERE device_id = ?")
    .bind(deviceId)
    .first<{ map_id: string; map_revision: string; resolution: number }>();
}

async function readDrafts(deviceId: string) {
  const rows = await getD1()
    .prepare(
      `SELECT kind, map_id, map_revision, payload_json, status, error, command_id, saved_by,
              saved_at, resolved_at
       FROM robot_semantic_drafts WHERE device_id = ?`,
    )
    .bind(deviceId)
    .all<DraftRow>();
  return rows.results;
}

function draftView(row: DraftRow): SemanticDraftView {
  return {
    status: row.status,
    error: row.error,
    savedAt: iso(row.saved_at),
    resolvedAt: row.resolved_at === null ? null : iso(row.resolved_at),
  };
}

function isObject(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function parseJson(value: string): unknown {
  try {
    return JSON.parse(value);
  } catch {
    return null;
  }
}
