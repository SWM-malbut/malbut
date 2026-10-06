import { saveSemanticDraft } from "../../../../../../db/robot-semantic-drafts";
import { noStore } from "../../../../../api-response";
import { normalizeRooms, openSpaceEdit, ZONE_FORMAT, zoneSaveError } from "../../../../../robot-space-edit";

export const dynamic = "force-dynamic";

/**
 * 방 설정 저장 · 구역 설정 저장: kept for the robot's current saved map and sent when the
 * 말벗 is on (now, or when it comes back).
 */
export async function POST(request: Request, context: { params: Promise<{ deviceId: string }> }) {
  const { deviceId } = await context.params;
  const edit = await openSpaceEdit(request, deviceId);
  if (edit instanceof Response) return edit;
  const { body, map } = edit;
  if (typeof body.mapId !== "string" || typeof body.mapRevision !== "string") {
    return noStore({ error: "편집 내용을 확인해 주세요." }, 400);
  }
  let payload: unknown;
  if (body.kind === "rooms" && Object.keys(body).length === 4 && "rooms" in body) {
    const rooms = normalizeRooms(body.rooms, map.resolution);
    if (typeof rooms === "string") return noStore({ error: rooms }, 422);
    payload = rooms;
  } else if (body.kind === "zones" && Object.keys(body).length === 4 && "features" in body) {
    const problem = zoneSaveError(body.features, edit.realRobot);
    if (problem) return noStore({ error: problem }, 422);
    payload = {
      type: "FeatureCollection", format: ZONE_FORMAT, map_id: body.mapId,
      map_revision: body.mapRevision, frame_id: "map", features: body.features,
    };
  } else {
    return noStore({ error: "편집 내용을 확인해 주세요." }, 400);
  }
  try {
    const draft = await saveSemanticDraft({
      deviceId, userId: edit.userId, kind: body.kind === "rooms" ? "rooms" : "zones", mapId: body.mapId,
      mapRevision: body.mapRevision, payload,
    });
    return noStore({ draft, ...(body.kind === "rooms" ? { rooms: payload } : {}) }, 200);
  } catch (error) {
    if (error instanceof Error && error.message === "MAP_CHANGED") {
      return noStore({ error: "그사이 말벗의 지도가 바뀌었어요. 지도를 다시 불러온 뒤 편집해 주세요." }, 409);
    }
    if (error instanceof Error && error.message === "FORBIDDEN") {
      return noStore({ error: "방·구역 편집은 소유자만 할 수 있어요." }, 403);
    }
    throw error;
  }
}
