/**
 * The owner's room and Zone edits on the map screen (SWM25-237): split and merge are worked
 * out here, for the simulator and the real robot alike, and saves wait for the robot in
 * robot_semantic_drafts. Requests come from this site only and stay bounded.
 */
import { getSpaceEditContext } from "../db/robot-semantic-drafts";
import { noStore } from "./api-response";
import { normalizeRoomFeature, RoomGeometryError, type RoomFeature } from "./room-geometry";
import { sameOriginJsonRequest } from "./same-origin-request";
import { getRequestUserId } from "./server-auth";

export const ZONE_FORMAT = "malbut-semantic-zones-v1";
const MAX_EDIT_BYTES = 768 * 1024;
const MAX_ROOMS = 512;
const MAX_ZONES = 512;
// The real robot's Zone file (malbut_bringup zones.py): 64 Zones of 3 to 64 corners.
const REAL_ROBOT_MAX_ZONES = 64;
const REAL_ROBOT_MAX_CORNERS = 64;
const MIN_ZONE_AREA_M2 = 0.01;
const ZONE_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$/;
const ROOM_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const ZONE_BEHAVIORS = new Set(["restricted", "avoid", "allow"]);

type SpaceEdit = {
  userId: string;
  body: Record<string, unknown>;
  map: { mapId: string; mapRevision: string; resolution: number };
  realRobot: boolean;
};

/** Sign-in, this site, the owner, a JSON object, and a saved map to edit; or the error response. */
export async function openSpaceEdit(request: Request, deviceId: string): Promise<SpaceEdit | Response> {
  const userId = await getRequestUserId(request);
  if (!userId) return noStore({ error: "로그인이 필요합니다." }, 401);
  if (!sameOriginJsonRequest(request)) return noStore({ error: "요청 출처를 확인해 주세요." }, 403);
  const declared = Number(request.headers.get("content-length") ?? "0");
  if (Number.isFinite(declared) && declared > MAX_EDIT_BYTES) {
    return noStore({ error: "편집한 내용이 너무 커요." }, 413);
  }
  const text = await request.text();
  if (new TextEncoder().encode(text).byteLength > MAX_EDIT_BYTES) {
    return noStore({ error: "편집한 내용이 너무 커요." }, 413);
  }
  let body: unknown;
  try {
    body = JSON.parse(text);
  } catch {
    return noStore({ error: "편집 내용을 확인해 주세요." }, 400);
  }
  if (!isObject(body)) return noStore({ error: "편집 내용을 확인해 주세요." }, 400);
  const context = await getSpaceEditContext(deviceId, userId);
  if (!context) return noStore({ error: "방·구역 편집은 소유자만 할 수 있어요." }, 403);
  if (!context.map) return noStore({ error: "말벗의 저장 지도를 아직 받지 못했어요." }, 409);
  return { userId, body, map: context.map, realRobot: context.realRobot };
}

/** The editor's English geometry errors, as the owner reads them. */
export function roomEditErrorMessage(error: unknown) {
  const message = error instanceof Error ? error.message : "";
  const translations: Record<string, string> = {
    "at least one split divider is required": "분할선을 하나 이상 만드세요.",
    "each split divider must contain at least two finite points": "각 분할선의 양 끝점을 지정하세요.",
    "split divider points must be near a Room wall": "분할선의 점을 방 벽 근처에 놓으세요.",
    "split divider endpoints must be near a Room wall": "분할선의 양 끝점을 방 벽에서 25cm 이내에 놓으세요.",
    "split divider control points must stay in the Room": "분할선의 꺾임점은 방 안에 놓으세요.",
    "split divider segments are too short": "분할선 구간이 너무 짧습니다.",
    "the divider must cut the selected Room into exactly two meaningful areas":
      "분할선을 이어서 선택한 방을 각각 1㎡ 이상인 정확히 두 공간으로 나누세요.",
    "Room is too large to split safely": "방이 너무 커서 나눌 수 없습니다.",
    "exactly two Rooms are required for a merge": "합칠 방을 정확히 두 곳 선택하세요.",
    "all selected features must be Rooms": "방으로 지정된 공간만 합칠 수 있습니다.",
    "two different Rooms are required for a merge": "현재 방과 다른 방을 선택하세요.",
    "only adjacent Rooms can be merged": "서로 맞닿아 있는 두 방만 합칠 수 있습니다.",
  };
  return translations[message] ?? "방 모양을 확인해 주세요.";
}

/** Rooms to save, each with its area and representative point worked out again here. */
export function normalizeRooms(value: unknown, resolution: number): RoomFeature[] | string {
  if (!Array.isArray(value) || value.length === 0) return "방이 하나 이상 있어야 해요.";
  if (value.length > MAX_ROOMS) return `방은 ${MAX_ROOMS}개까지 저장할 수 있어요.`;
  const seen = new Set<string>();
  const rooms: RoomFeature[] = [];
  for (const room of value) {
    let normalized: RoomFeature;
    try {
      normalized = normalizeRoomFeature(room as RoomFeature, resolution);
    } catch (error) {
      if (error instanceof RoomGeometryError) return "방 모양을 확인해 주세요.";
      throw error;
    }
    const id = String(normalized.id ?? normalized.properties.room_id ?? "");
    if (!ROOM_ID.test(id) || seen.has(id)) return "방 정보를 확인해 주세요. 지도를 다시 불러온 뒤 편집해 주세요.";
    seen.add(id);
    rooms.push(normalized);
  }
  return rooms;
}

/**
 * Zones the robot can take: the real robot keeps at most 64 Zones of 3 to 64 corners.
 * Returns the owner-facing reason when one cannot be saved.
 */
export function zoneSaveError(value: unknown, realRobot: boolean) {
  const limit = realRobot ? REAL_ROBOT_MAX_ZONES : MAX_ZONES;
  if (!Array.isArray(value)) return "구역 정보를 확인해 주세요.";
  if (value.length > limit) return `구역은 ${limit}개까지 저장할 수 있어요.`;
  const seen = new Set<string>();
  for (const zone of value) {
    const properties = isObject(zone) && isObject(zone.properties) ? zone.properties : null;
    const name = typeof properties?.name === "string" && properties.name.trim()
      ? properties.name.trim().slice(0, 40)
      : "구역";
    if (!isObject(zone) || zone.type !== "Feature" || !properties ||
        properties.role !== "semantic_zone" || !ZONE_BEHAVIORS.has(String(properties.behavior))) {
      return `${name}의 정보를 확인해 주세요.`;
    }
    const id = properties.zone_id;
    if (typeof id !== "string" || !ZONE_ID.test(id) || seen.has(id)) {
      return "구역 정보를 확인해 주세요. 지도를 다시 불러온 뒤 편집해 주세요.";
    }
    seen.add(id);
    const geometry = isObject(zone.geometry) ? zone.geometry : null;
    const rings = geometry?.type === "Polygon" && Array.isArray(geometry.coordinates)
      ? geometry.coordinates
      : [];
    if (!rings.length || !rings.every(closedRing)) return `${name}의 모양을 확인해 주세요.`;
    if (realRobot && rings.some((ring) => (ring as unknown[]).length > REAL_ROBOT_MAX_CORNERS + 1)) {
      return `${name}의 꼭짓점이 너무 많아요. 말벗은 구역 하나에 꼭짓점을 ${REAL_ROBOT_MAX_CORNERS}개까지 쓸 수 있어요.`;
    }
    if (ringArea(rings[0] as number[][]) < MIN_ZONE_AREA_M2) return `${name}의 크기가 너무 작아요.`;
  }
  return null;
}

function closedRing(ring: unknown) {
  if (!Array.isArray(ring) || ring.length < 4) return false;
  if (!ring.every((point) => Array.isArray(point) && point.length >= 2 &&
    typeof point[0] === "number" && typeof point[1] === "number" &&
    Number.isFinite(point[0]) && Number.isFinite(point[1]))) return false;
  const [first, last] = [ring[0] as number[], ring[ring.length - 1] as number[]];
  return first[0] === last[0] && first[1] === last[1];
}

function ringArea(ring: number[][]) {
  let twice = 0;
  for (let index = 1; index < ring.length; index += 1) {
    twice += ring[index - 1][0] * ring[index][1] - ring[index][0] * ring[index - 1][1];
  }
  return Math.abs(twice) / 2;
}

function isObject(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
