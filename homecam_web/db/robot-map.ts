import { getD1 } from ".";
import type { RobotMapUpload, RobotStateUpload } from "../app/robot-contract";
import {
  ensureHomecamSchema,
  userCanManageDevice,
  userCanViewDevice,
  writeAuditLog,
} from "./homecam";

const ROBOT_ONLINE_MS = 15_000;
const ACTIVE_MAPPING_STATES = new Set([
  "waiting_for_map", "waiting_for_navigation", "exploring", "navigating",
  "review", "saving",
]);

type StateRow = {
  state: string;
  message: string;
  pose_x: number | null;
  pose_y: number | null;
  pose_yaw: number | null;
  localization_state: string;
  tf_age_s: number | null;
  nav2_json: string;
  target_json: string | null;
  map_revision_counter: number;
  observed_at: string;
  updated_at: string;
};

type MapRow = {
  revision: string;
  map_id: string;
  map_revision: string;
  width: number;
  height: number;
  resolution: number;
  origin_x: number;
  origin_y: number;
  origin_yaw: number;
  preview_base64: string;
  user_map_json: string | null;
  semantic_zones_json: string | null;
  source_created_at: string | null;
  updated_at: string;
};

type CommandRow = {
  id: string;
  operation: string;
  payload_json: string;
  status: string;
  requested_by: string;
  requested_at: string;
  claimed_at: string | null;
  completed_at: string | null;
  result_json: string | null;
};

export async function storeRobotState(deviceId: string, state: RobotStateUpload) {
  await ensureHomecamSchema();
  const now = new Date().toISOString();
  await getD1()
    .prepare(
      `INSERT INTO robot_runtime_state
       (device_id, state, message, pose_x, pose_y, pose_yaw,
        localization_state, tf_age_s, nav2_json, target_json,
        map_revision_counter, observed_at, updated_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
       ON CONFLICT(device_id) DO UPDATE SET
         state = excluded.state,
         message = excluded.message,
         pose_x = excluded.pose_x,
         pose_y = excluded.pose_y,
         pose_yaw = excluded.pose_yaw,
         localization_state = excluded.localization_state,
         tf_age_s = excluded.tf_age_s,
         nav2_json = excluded.nav2_json,
         target_json = excluded.target_json,
         map_revision_counter = excluded.map_revision_counter,
         observed_at = excluded.observed_at,
         updated_at = excluded.updated_at`,
    )
    .bind(
      deviceId,
      state.state,
      state.message,
      state.pose?.x ?? null,
      state.pose?.y ?? null,
      state.pose?.yaw ?? null,
      state.localization.state,
      state.localization.tfAgeS,
      JSON.stringify(state.nav2),
      state.target ? JSON.stringify(state.target) : null,
      state.mapRevision,
      state.observedAt,
      now,
    )
    .run();
}

export async function storeRobotMap(deviceId: string, map: RobotMapUpload) {
  await ensureHomecamSchema();
  const now = new Date().toISOString();
  const table = map.finalized ? "robot_maps" : "robot_map_drafts";
  const statement = getD1()
    .prepare(
      `INSERT INTO ${table}
       (device_id, revision, map_id, map_revision, width, height, resolution,
        origin_x, origin_y, origin_yaw, preview_base64, user_map_json,
        semantic_zones_json,
        source_created_at, updated_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
       ON CONFLICT(device_id) DO UPDATE SET
         revision = excluded.revision,
         map_id = excluded.map_id,
         map_revision = excluded.map_revision,
         width = excluded.width,
         height = excluded.height,
         resolution = excluded.resolution,
         origin_x = excluded.origin_x,
         origin_y = excluded.origin_y,
         origin_yaw = excluded.origin_yaw,
         preview_base64 = excluded.preview_base64,
         user_map_json = excluded.user_map_json,
         semantic_zones_json = excluded.semantic_zones_json,
         source_created_at = excluded.source_created_at,
         updated_at = excluded.updated_at`,
    )
    .bind(
      deviceId,
      map.revision,
      map.mapId,
      map.mapRevision,
      map.geometry.width,
      map.geometry.height,
      map.geometry.resolution,
      map.geometry.originX,
      map.geometry.originY,
      map.geometry.originYaw,
      map.previewBase64,
      map.userMap ? JSON.stringify(map.userMap) : null,
      map.semanticZones ? JSON.stringify(map.semanticZones) : null,
      map.sourceCreatedAt,
      now,
    );
  if (map.finalized) {
    await getD1().batch([
      statement,
      getD1().prepare("DELETE FROM robot_map_drafts WHERE device_id = ?").bind(deviceId),
    ]);
  } else {
    await statement.run();
  }
}

export async function getRobotSnapshot(deviceId: string, userEmail: string) {
  await ensureHomecamSchema();
  if (!(await userCanViewDevice(deviceId, userEmail))) return null;
  const [state, savedMap, draftMap, command] = await Promise.all([
    getD1()
      .prepare("SELECT * FROM robot_runtime_state WHERE device_id = ?")
      .bind(deviceId)
      .first<StateRow>(),
    getD1()
      .prepare("SELECT * FROM robot_maps WHERE device_id = ?")
      .bind(deviceId)
      .first<MapRow>(),
    getD1()
      .prepare("SELECT * FROM robot_map_drafts WHERE device_id = ?")
      .bind(deviceId)
      .first<MapRow>(),
    getD1()
      .prepare(
        `SELECT id, operation, payload_json, status, requested_by, requested_at, claimed_at,
                completed_at, result_json
         FROM robot_commands WHERE device_id = ?
         ORDER BY requested_at DESC LIMIT 1`,
      )
      .bind(deviceId)
      .first<CommandRow>(),
  ]);
  const mappingActive = Boolean(state && ACTIVE_MAPPING_STATES.has(state.state));
  const map = mappingActive && draftMap ? draftMap : savedMap ?? draftMap;
  return {
    online: Boolean(
      state && Date.parse(state.observed_at) >= Date.now() - ROBOT_ONLINE_MS,
    ),
    state: state ? mapState(state) : null,
    map: map ? mapMetadata(map, map === savedMap) : null,
    command: command ? mapCommand(command) : null,
  };
}

export async function getRobotMapPreview(
  deviceId: string,
  userEmail: string,
  revision = "",
) {
  await ensureHomecamSchema();
  if (!(await userCanViewDevice(deviceId, userEmail))) return null;
  const [saved, draft] = await Promise.all([
    getD1()
      .prepare("SELECT revision, preview_base64 FROM robot_maps WHERE device_id = ?")
      .bind(deviceId)
      .first<{ revision: string; preview_base64: string }>(),
    getD1()
      .prepare("SELECT revision, preview_base64 FROM robot_map_drafts WHERE device_id = ?")
      .bind(deviceId)
      .first<{ revision: string; preview_base64: string }>(),
  ]);
  if (revision) {
    return [saved, draft].find((item) => item?.revision === revision) ?? null;
  }
  return saved ?? draft;
}

export async function getRobotMapSemantics(deviceId: string, userEmail: string) {
  await ensureHomecamSchema();
  if (!(await userCanViewDevice(deviceId, userEmail))) return null;
  const map = await getD1()
    .prepare(
      `SELECT revision, map_id, map_revision, user_map_json, semantic_zones_json
       FROM robot_maps WHERE device_id = ?`,
    )
    .bind(deviceId)
    .first<Pick<MapRow, "revision" | "map_id" | "map_revision" | "user_map_json" | "semantic_zones_json">>();
  if (!map) return null;
  return {
    revision: map.revision,
    mapId: map.map_id,
    mapRevision: map.map_revision,
    userMap: map.user_map_json ? parseObject(map.user_map_json) : null,
    zones: map.semantic_zones_json ? parseObject(map.semantic_zones_json) : null,
  };
}

export async function createRobotCommand(input: {
  deviceId: string;
  userEmail: string;
  operation: "start" | "finish" | "cancel" | "navigation_preview" | "navigation_start" | "navigation_cancel" |
    "room_split" | "room_merge" | "rooms_save" | "zones_apply";
  payload?: Record<string, unknown>;
}) {
  await ensureHomecamSchema();
  if (!(await userCanManageDevice(input.deviceId, input.userEmail))) {
    throw new Error("FORBIDDEN");
  }
  await expireStaleRobotCommands(input.deviceId);
  const now = new Date().toISOString();
  const id = crypto.randomUUID();
  let result: CommandRow | null;
  try {
    result = await getD1().prepare(
      `INSERT INTO robot_commands
       (id, device_id, operation, payload_json, requested_by, status, requested_at)
       SELECT ?, ?, ?, ?, ?, 'queued', ?
       WHERE NOT EXISTS (
         SELECT 1 FROM robot_commands
         WHERE device_id = ? AND status IN ('queued', 'claimed')
       )
       RETURNING id, operation, payload_json, status, requested_by, requested_at,
                 claimed_at, completed_at, result_json`,
    )
    .bind(
      id,
      input.deviceId,
      input.operation,
      JSON.stringify(input.payload ?? {}),
      input.userEmail,
      now,
      input.deviceId,
    )
      .first<CommandRow>();
  } catch (error) {
    if (error instanceof Error && error.message.startsWith("UNIQUE_CONSTRAINT")) {
      throw new Error("COMMAND_IN_PROGRESS");
    }
    throw error;
  }
  if (!result) throw new Error("COMMAND_IN_PROGRESS");
  await writeAuditLog({
    deviceId: input.deviceId,
    actorType: "user",
    actorId: input.userEmail,
    action: `robot.${input.operation}`,
    metadata: { commandId: id },
  });
  return mapCommand(result);
}

export async function claimRobotCommands(deviceId: string) {
  await ensureHomecamSchema();
  await expireStaleRobotCommands(deviceId);
  const now = new Date().toISOString();
  const command = await getD1()
    .prepare(
      `UPDATE robot_commands
       SET status = 'claimed', claimed_at = ?
       WHERE id = (
         SELECT id FROM robot_commands
         WHERE device_id = ? AND status = 'queued'
         ORDER BY requested_at ASC
         FOR UPDATE SKIP LOCKED
         LIMIT 1
       )
       RETURNING id, operation, payload_json, status, requested_by, requested_at,
                 claimed_at, completed_at, result_json`,
    )
    .bind(now, deviceId)
    .first<CommandRow>();
  return command ? [mapCommand(command)] : [];
}

async function expireStaleRobotCommands(deviceId: string) {
  const cutoff = new Date(Date.now() - 60_000).toISOString();
  await getD1()
    .prepare(
      `UPDATE robot_commands
       SET status = 'failed', completed_at = ?,
           result_json = '{"error":"ROBOT_COMMAND_TIMEOUT"}'
       WHERE device_id = ? AND status IN ('queued', 'claimed')
         AND requested_at < ?`,
    )
    .bind(new Date().toISOString(), deviceId, cutoff)
    .run();
}

export async function completeRobotCommand(input: {
  deviceId: string;
  commandId: string;
  ok: boolean;
  result: unknown;
}) {
  await ensureHomecamSchema();
  const now = new Date().toISOString();
  const serializedResult = JSON.stringify(input.result ?? null);
  const resultJson = Buffer.byteLength(serializedResult, "utf8") <= 256 * 1024
    ? serializedResult
    : JSON.stringify({ error: "ROBOT_COMMAND_RESULT_TOO_LARGE" });
  const command = await getD1()
    .prepare(
      `UPDATE robot_commands
       SET status = ?, completed_at = ?, result_json = ?
       WHERE id = ? AND device_id = ? AND status = 'claimed'
       RETURNING id, operation, payload_json, status, requested_by, requested_at,
                 claimed_at, completed_at, result_json`,
    )
    .bind(
      input.ok ? "completed" : "failed",
      now,
      resultJson,
      input.commandId,
      input.deviceId,
    )
    .first<CommandRow>();
  return command ? mapCommand(command) : null;
}

function mapState(row: StateRow) {
  return {
    state: row.state,
    message: row.message,
    pose: row.pose_x === null || row.pose_y === null || row.pose_yaw === null
      ? null
      : { x: row.pose_x, y: row.pose_y, yaw: row.pose_yaw },
    localization: { state: row.localization_state, tfAgeS: row.tf_age_s },
    nav2: parseObject(row.nav2_json),
    target: row.target_json ? parseObject(row.target_json) : null,
    mapRevision: row.map_revision_counter,
    observedAt: row.observed_at,
    updatedAt: row.updated_at,
  };
}

function mapMetadata(row: MapRow, finalized: boolean) {
  return {
    finalized,
    revision: row.revision,
    mapId: row.map_id,
    mapRevision: row.map_revision,
    geometry: {
      width: row.width,
      height: row.height,
      resolution: row.resolution,
      originX: row.origin_x,
      originY: row.origin_y,
      originYaw: row.origin_yaw,
    },
    sourceCreatedAt: row.source_created_at,
    updatedAt: row.updated_at,
  };
}

function mapCommand(row: CommandRow) {
  return {
    id: row.id,
    operation: row.operation,
    payload: parseObject(row.payload_json),
    status: row.status,
    requestedBy: row.requested_by,
    requestedAt: row.requested_at,
    claimedAt: row.claimed_at,
    completedAt: row.completed_at,
    result: row.result_json ? parseJson(row.result_json) : null,
  };
}

function parseObject(value: string): Record<string, unknown> {
  const parsed = parseJson(value);
  return parsed && typeof parsed === "object" && !Array.isArray(parsed)
    ? parsed as Record<string, unknown>
    : {};
}

function parseJson(value: string): unknown {
  try {
    return JSON.parse(value);
  } catch {
    return null;
  }
}
