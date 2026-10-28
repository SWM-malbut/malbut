import { readSavedMapEdit } from "../../../../../../db/robot-map-edits";
import { MAP_FILE } from "../../../../../../db/robot-map-labels";
import { noStore } from "../../../../../api-response";
import { getRequestUserId } from "../../../../../server-auth";

export async function GET(request: Request, context: { params: Promise<{ deviceId: string }> }) {
  const userId = await getRequestUserId(request);
  if (!userId) return noStore({ error: "로그인이 필요합니다." }, 401);
  const map = new URL(request.url).searchParams.get("map") ?? "";
  if (!MAP_FILE.test(map)) return noStore({ error: "저장 지도를 선택해 주세요." }, 400);
  const { deviceId } = await context.params;
  try {
    const snapshot = await readSavedMapEdit(deviceId, userId, map);
    return snapshot ? noStore(snapshot, 200) : noStore({ error: "로봇에서 지도를 불러오는 중입니다." }, 404);
  } catch (error) {
    if (error instanceof Error && error.message === "FORBIDDEN") return noStore({ error: "소유자만 지도를 정리할 수 있습니다." }, 403);
    throw error;
  }
}
