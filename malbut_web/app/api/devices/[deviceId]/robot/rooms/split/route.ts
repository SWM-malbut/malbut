import { consumeRequestRateLimit } from "../../../../../../../db/request-rate-limit";
import { noStore } from "../../../../../../api-response";
import { RoomGeometryError, splitRoomFeature, type RoomFeature } from "../../../../../../room-geometry";
import { openSpaceEdit, roomEditErrorMessage } from "../../../../../../robot-space-edit";

export const dynamic = "force-dynamic";

/** 방 편집 › 나누기: the two rooms a divider makes, worked out on the map's grid. */
export async function POST(request: Request, context: { params: Promise<{ deviceId: string }> }) {
  const { deviceId } = await context.params;
  const edit = await openSpaceEdit(request, deviceId);
  if (edit instanceof Response) return edit;
  if (!(await consumeRequestRateLimit({ userId: edit.userId, roomCode: deviceId,
    scope: "robot-room-geometry", limit: 60 }))) {
    return noStore({ error: "요청이 너무 많아요. 1분 뒤 다시 시도해 주세요." }, 429, { "retry-after": "60" });
  }
  try {
    const rooms = splitRoomFeature(edit.body.room as RoomFeature, edit.body.lines,
      edit.map.resolution, 1);
    return noStore({ rooms }, 200);
  } catch (error) {
    if (error instanceof RoomGeometryError || error instanceof TypeError) {
      return noStore({ error: roomEditErrorMessage(error) }, 422);
    }
    throw error;
  }
}
