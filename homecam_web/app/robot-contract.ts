const MAX_STATE_BYTES = 64 * 1024;
const MAX_MAP_BYTES = 2 * 1024 * 1024;
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
};

export type RobotOperation = "start" | "finish" | "cancel" |
  "navigation_preview" | "navigation_start" | "navigation_cancel";

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
  return null;
}

export async function readRobotJson(request: Request, kind: "state" | "map") {
  const contentType = request.headers.get("content-type")?.split(";", 1)[0].trim();
  if (contentType !== "application/json") throw new Error("UNSUPPORTED_MEDIA_TYPE");
  const declaredLength = Number(request.headers.get("content-length") ?? "0");
  const limit = kind === "map" ? MAX_MAP_BYTES : MAX_STATE_BYTES;
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
  if (
    !shortString(value.revision, 128) || !REVISION.test(value.revision) ||
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
    !(value.userMap === null || isObject(value.userMap))
  ) return null;
  return {
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
  };
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
