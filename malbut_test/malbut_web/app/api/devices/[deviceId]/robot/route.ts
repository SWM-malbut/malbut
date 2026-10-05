import { getRobotSnapshot } from "../../../../../db/robot-map";
import { noStore } from "../../../../api-response";
import { getRequestUserId } from "../../../../server-auth";

export const dynamic = "force-dynamic";

export async function GET(
  request: Request,
  context: { params: Promise<{ deviceId: string }> },
) {
  const userId = await getRequestUserId(request);
  if (!userId) return noStore({ error: "로그인이 필요합니다." }, 401);
  const { deviceId } = await context.params;
  const snapshot = await getRobotSnapshot(deviceId, userId);
  if (!snapshot) return noStore({ error: "이 로봇의 지도를 볼 권한이 없습니다." }, 403);
  return noStore(snapshot, 200);
}
