const MAX_STATE_BYTES = 64 * 1024;
const MAX_MAP_BYTES = 2 * 1024 * 1024;
const MAX_COMMAND_BYTES = 1024 * 1024;
const MAX_SEMANTIC_JSON_BYTES = 1_500_000;
const MAX_SEMANTIC_JSON_NODES = 100_000;
const MAX_SEMANTIC_JSON_DEPTH = 32;
const REVISION = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;

export type RobotPose = { x: number; y: number; yaw: number };

export type RobotStateUpload = {
  state: string;
  message: string;
  pose: RobotPose | null;
  localization: { state: string; tfAgeS: number | null };
  nav2: Record<string, string>;
  target: Record<string, unknown> | null;
  mapRevision: number;
  observedAt: string;
};

export type RobotMapUpload = {
  finalized: boolean;
  revision: string;
  mapId: string;
  mapRevision: string;
  sourceCreatedAt: string | null;
  geometry: {
    width: number;
    height: number;
    resolution: number;
    originX: number;
    originY: number;
    originYaw: number;
  };
  previewBase64: string;
  userMap: Record<string, unknown> | null;
  semanticZones?: Record<string, unknown> | null;
};

export type RobotOperation = "start" | "finish" | "cancel" |
  "navigation_preview" | "navigation_start" | "navigation_cancel" |
  "room_split" | "room_merge" | "rooms_save" | "zones_apply";

export function parseRobotCommand(value: unknown): {
  operation: RobotOperation;
  payload: Record<string, unknown>;
} | null {
  if (!isObject(value) || Object.keys(value).some((key) => !["operation", "payload"].includes(key))) return null;
  const operation = value.operation;
  const payload = value.payload === undefined ? {} : value.payload;
  if (!isObject(payload)) return null;
  if (["start", "finish", "cancel"].includes(String(operation))) {
    return Object.keys(payload).length === 0
      ? { operation: operation as RobotOperation, payload }
      : null;
  }
  if (operation === "navigation_preview") {
    return Object.keys(payload).length === 2 && finiteCoordinate(payload.x) && finiteCoordinate(payload.y)
      ? { operation, payload: { x: payload.x, y: payload.y } }
      : null;
  }
  if (operation === "navigation_start") {
    return Object.keys(payload).length === 1 && safeToken(payload.previewToken)
      ? { operation, payload: { previewToken: payload.previewToken } }
      : null;
  }
  if (operation === "navigation_cancel") {
    return Object.keys(payload).length === 1 && safeToken(payload.sessionId)
      ? { operation, payload: { sessionId: payload.sessionId } }
      : null;
  }
  if (["room_split", "room_merge", "rooms_save", "zones_apply"].includes(String(operation))) {
    if (!validSemanticCommand(operation as RobotOperation, payload)) return null;
    return { operation: operation as RobotOperation, payload };
  }
  return null;
}

export async function readRobotCommandJson(request: Request) {
  return readBoundedJson(request, MAX_COMMAND_BYTES);
}

export async function readRobotJson(request: Request, kind: "state" | "map") {
  return readBoundedJson(request, kind === "map" ? MAX_MAP_BYTES : MAX_STATE_BYTES);
}

async function readBoundedJson(request: Request, limit: number) {
  const contentType = request.headers.get("content-type")?.split(";", 1)[0].trim();
  if (contentType !== "application/json") throw new Error("UNSUPPORTED_MEDIA_TYPE");
  const declaredLength = Number(request.headers.get("content-length") ?? "0");
  if (Number.isFinite(declaredLength) && declaredLength > limit) {
    throw new Error("PAYLOAD_TOO_LARGE");
  }
  const text = await request.text();
  if (new TextEncoder().encode(text).byteLength > limit) {
    throw new Error("PAYLOAD_TOO_LARGE");
  }
  try {
    return JSON.parse(text) as unknown;
  } catch {
    throw new Error("INVALID_JSON");
  }
}

export function parseRobotState(value: unknown, nowMs = Date.now()): RobotStateUpload | null {
  if (!isObject(value)) return null;
  const pose = value.pose === null ? null : parsePose(value.pose);
  const localization = value.localization;
  if (
    !shortString(value.state, 32) ||
    !shortString(value.message, 512) ||
    (value.pose !== null && !pose) ||
    !isObject(localization) ||
    !shortString(localization.state, 32) ||
    !(localization.tfAgeS === null || finiteNumber(localization.tfAgeS)) ||
    !isStringRecord(value.nav2, 32, 64, 32) ||
    !(value.target === null || isObject(value.target)) ||
    !integerInRange(value.mapRevision, 0, Number.MAX_SAFE_INTEGER) ||
    !validObservedAt(value.observedAt, nowMs)
  ) return null;
  return {
    state: value.state,
    message: value.message,
    pose,
    localization: { state: localization.state, tfAgeS: localization.tfAgeS },
    nav2: value.nav2,
    target: value.target,
    mapRevision: value.mapRevision,
    observedAt: value.observedAt,
  };
}

export function parseRobotMap(value: unknown): RobotMapUpload | null {
  if (!isObject(value) || !isObject(value.geometry)) return null;
  const geometry = value.geometry;
  const semanticZones = isObject(value.semanticZones)
    ? value.semanticZones
    : null;
  if (
    !shortString(value.revision, 128) || !REVISION.test(value.revision) ||
    typeof value.finalized !== "boolean" ||
    !shortString(value.mapId, 128) || !REVISION.test(value.mapId) ||
    !shortString(value.mapRevision, 128) || !REVISION.test(value.mapRevision) ||
    !(value.sourceCreatedAt === null || validIso(value.sourceCreatedAt)) ||
    !integerInRange(geometry.width, 1, 8192) ||
    !integerInRange(geometry.height, 1, 8192) ||
    !finiteInRange(geometry.resolution, 0.001, 1) ||
    !finiteNumber(geometry.originX) || !finiteNumber(geometry.originY) ||
    !finiteNumber(geometry.originYaw) ||
    typeof value.previewBase64 !== "string" || value.previewBase64.length > 1_500_000 ||
    !validPngBase64(value.previewBase64) ||
    !(value.userMap === null || isObject(value.userMap)) ||
    !(
      value.semanticZones === undefined || value.semanticZones === null ||
      isObject(value.semanticZones)
    ) ||
    !validBoundedSemanticJson(value.userMap, semanticZones)
  ) return null;
  return {
    finalized: value.finalized,
    revision: value.revision,
    mapId: value.mapId,
    mapRevision: value.mapRevision,
    sourceCreatedAt: value.sourceCreatedAt,
    geometry: {
      width: geometry.width,
      height: geometry.height,
      resolution: geometry.resolution,
      originX: geometry.originX,
      originY: geometry.originY,
      originYaw: geometry.originYaw,
    },
    previewBase64: value.previewBase64,
    userMap: value.userMap,
    semanticZones,
  };
}

/**
 * Validate the two arbitrary semantic JSON trees without recursion before
 * either one can reach the recursive canonicalizer used by the Agent API.
 * Uploads originate in JSON, so accessors, sparse arrays, symbols, repeated
 * object references, and other JavaScript-only values are rejected as well.
 */
function validBoundedSemanticJson(...roots: unknown[]) {
  const pending = roots.map((item) => ({ item, depth: 0 }));
  const visited = new WeakSet<object>();
  let bytes = 32;
  let nodes = 0;

  try {
    while (pending.length > 0) {
      const current = pending.pop();
      if (!current) return false;
      nodes += 1;
      if (
        nodes > MAX_SEMANTIC_JSON_NODES ||
        current.depth > MAX_SEMANTIC_JSON_DEPTH
      ) return false;

      const item = current.item;
      if (item === null) {
        bytes += 4;
      } else if (typeof item === "boolean") {
        bytes += item ? 4 : 5;
      } else if (typeof item === "number") {
        if (!Number.isFinite(item)) return false;
        bytes += JSON.stringify(item).length;
      } else if (typeof item === "string") {
        const rawBytes = Buffer.byteLength(item, "utf8");
        if (rawBytes > MAX_SEMANTIC_JSON_BYTES) return false;
        bytes += Buffer.byteLength(JSON.stringify(item), "utf8");
      } else if (typeof item === "object") {
        if (visited.has(item)) return false;
        visited.add(item);

        if (Array.isArray(item)) {
          const keys = Object.keys(item);
          if (
            keys.length !== item.length ||
            keys.some((key, index) => key !== String(index)) ||
            nodes + pending.length + item.length > MAX_SEMANTIC_JSON_NODES
          ) return false;
          bytes += 2 + Math.max(0, item.length - 1);
          for (let index = item.length - 1; index >= 0; index -= 1) {
            pending.push({ item: item[index], depth: current.depth + 1 });
          }
        } else {
          const keys = Reflect.ownKeys(item);
          if (
            keys.some((key) => typeof key !== "string") ||
            nodes + pending.length + keys.length > MAX_SEMANTIC_JSON_NODES
          ) return false;
          const descriptors = Object.getOwnPropertyDescriptors(item);
          if (keys.some((key) => {
            const descriptor = descriptors[key as string];
            return !descriptor || !descriptor.enumerable || !("value" in descriptor);
          })) return false;
          bytes += 2 + Math.max(0, keys.length - 1);
          for (const key of keys) {
            const stringKey = key as string;
            const rawKeyBytes = Buffer.byteLength(stringKey, "utf8");
            if (rawKeyBytes > MAX_SEMANTIC_JSON_BYTES) return false;
            bytes += Buffer.byteLength(JSON.stringify(stringKey), "utf8") + 1;
            pending.push({
              item: descriptors[stringKey].value,
              depth: current.depth + 1,
            });
          }
        }
      } else {
        return false;
      }
      if (bytes > MAX_SEMANTIC_JSON_BYTES) return false;
    }
  } catch {
    return false;
  }
  return true;
}

function validSemanticCommand(operation: RobotOperation, payload: Record<string, unknown>) {
  if (Buffer.byteLength(JSON.stringify(payload), "utf8") > 768 * 1024) return false;
  if (operation === "room_split") {
    const dividers = payload.lines ?? payload.line;
    return roomFeature(payload.room) && validSplitDividers(dividers) &&
      optionalPositiveFinite(payload.resolution) && optionalPositiveFinite(payload.minimum_room_area);
  }
  if (operation === "room_merge") {
    return Array.isArray(payload.rooms) && payload.rooms.length === 2 &&
      payload.rooms.every(roomFeature) &&
      roomFeatureId(payload.rooms[0]) !== roomFeatureId(payload.rooms[1]) &&
      optionalPositiveFinite(payload.resolution);
  }
  if (operation === "rooms_save") {
    return safeRevision(payload.map_id) && safeRevision(payload.map_revision) &&
      Array.isArray(payload.rooms) && payload.rooms.length > 0 && payload.rooms.length <= 512 &&
      payload.rooms.every(roomFeature) && new Set(payload.rooms.map(roomFeatureId)).size === payload.rooms.length &&
      optionalPositiveFinite(payload.resolution);
  }
  return operation === "zones_apply" &&
    payload.type === "FeatureCollection" &&
    payload.format === "malbut-semantic-zones-v1" &&
    safeRevision(payload.map_id) && safeRevision(payload.map_revision) &&
    Array.isArray(payload.features) && payload.features.length <= 512 &&
    payload.features.every(isObject);
}

function optionalPositiveFinite(value: unknown) {
  return value === undefined || (finiteNumber(value) && value > 0);
}

function validSplitDividers(value: unknown) {
  if (!Array.isArray(value) || value.length === 0) return false;
  const dividers = finitePoint(value[0]) ? [value] : value;
  return dividers.every((divider) => Array.isArray(divider) && divider.length >= 2 && divider.every(finitePoint));
}

function finitePoint(value: unknown) {
  return Array.isArray(value) && value.length === 2 && value.every(finiteNumber);
}

function roomFeature(value: unknown): value is Record<string, unknown> {
  return isObject(value) && value.type === "Feature" && isObject(value.properties) &&
    value.properties.role === "room" && isObject(value.geometry) &&
    (value.geometry.type === "Polygon" || value.geometry.type === "MultiPolygon") &&
    Array.isArray(value.geometry.coordinates) && Boolean(roomFeatureId(value));
}

function roomFeatureId(value: unknown) {
  if (!isObject(value)) return "";
  if (shortString(value.id, 128)) return value.id;
  return isObject(value.properties) && shortString(value.properties.room_id, 128)
    ? value.properties.room_id
    : "";
}

function safeRevision(value: unknown): value is string {
  return typeof value === "string" && REVISION.test(value);
}

function parsePose(value: unknown): RobotPose | null {
  if (!isObject(value) || !finiteNumber(value.x) || !finiteNumber(value.y) || !finiteNumber(value.yaw)) {
    return null;
  }
  return { x: value.x, y: value.y, yaw: value.yaw };
}

function validPngBase64(value: string) {
  try {
    const bytes = Buffer.from(value, "base64");
    return bytes.length >= 8 && bytes.length <= 1_000_000 &&
      bytes.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]));
  } catch {
    return false;
  }
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function finiteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function finiteInRange(value: unknown, minimum: number, maximum: number): value is number {
  return finiteNumber(value) && value >= minimum && value <= maximum;
}

function integerInRange(value: unknown, minimum: number, maximum: number): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= minimum && value <= maximum;
}

function shortString(value: unknown, maximum: number): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= maximum;
}

function validIso(value: unknown): value is string {
  return shortString(value, 64) && Number.isFinite(Date.parse(value));
}

function validObservedAt(value: unknown, nowMs: number): value is string {
  if (!validIso(value)) return false;
  const observedAt = Date.parse(value);
  return observedAt >= nowMs - 24 * 60 * 60 * 1_000 &&
    observedAt <= nowMs + 5 * 60 * 1_000;
}

function finiteCoordinate(value: unknown): value is number {
  return finiteNumber(value) && Math.abs(value) <= 10_000;
}

function safeToken(value: unknown): value is string {
  return typeof value === "string" && /^[A-Za-z0-9_-]{8,128}$/.test(value);
}

function isStringRecord(value: unknown, maxKeys: number, maxKey: number, maxValue: number): value is Record<string, string> {
  if (!isObject(value) || Object.keys(value).length > maxKeys) return false;
  return Object.entries(value).every(([key, item]) => key.length <= maxKey && shortString(item, maxValue));
}
