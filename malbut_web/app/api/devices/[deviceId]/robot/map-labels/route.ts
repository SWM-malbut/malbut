import { cleanMapLabel, MAP_FILE, readRobotMapLabels, saveRobotMapLabel } from "../../../../../../db/robot-map-labels";
import { noStore } from "../../../../../api-response";
import { sameOriginJsonRequest } from "../../../../../same-origin-request";
import { getRequestUserId } from "../../../../../server-auth";

export const dynamic = "force-dynamic";

/** 지도 관리: the names shown for the robot's saved maps, by map file. */
export async function GET(request: Request, context: { params: Promise<{ deviceId: string }> }) {
  const userId = await getRequestUserId(request);
  if (!userId) return noStore({ error: "로그인이 필요합니다." }, 401);
  const { deviceId } = await context.params;
  const labels = await readRobotMapLabels(deviceId, userId);
  if (!labels) return noStore({ error: "지도 관리는 소유자만 할 수 있어요." }, 403);
  return noStore({ labels }, 200);
}

/** 이름 정하기·바꾸기: when making a map and later from the saved map list. */
export async function PUT(request: Request, context: { params: Promise<{ deviceId: string }> }) {
  const userId = await getRequestUserId(request);
  if (!userId) return noStore({ error: "로그인이 필요합니다." }, 401);
  if (!sameOriginJsonRequest(request)) return noStore({ error: "요청 출처를 확인해 주세요." }, 403);
  const { deviceId } = await context.params;
  const body = await request.json().catch(() => null) as Record<string, unknown> | null;
  const name = cleanMapLabel(body?.name);
  if (!body || Object.keys(body).some((key) => key !== "map" && key !== "name") ||
      typeof body.map !== "string" || !MAP_FILE.test(body.map)) {
    return noStore({ error: "지도를 확인해 주세요." }, 400);
  }
  if (!name) return noStore({ error: "지도 이름은 1~40자로 적어 주세요." }, 422);
  try {
    await saveRobotMapLabel({ deviceId, userId, mapFile: body.map, name });
    return noStore({ map: body.map, name }, 200);
  } catch (error) {
    if (error instanceof Error && error.message === "FORBIDDEN") {
      return noStore({ error: "지도 관리는 소유자만 할 수 있어요." }, 403);
    }
    throw error;
  }
}
