import { getRobotMapSemantics } from "../../../../../../db/robot-map";
import { noStore } from "../../../../../api-response";
import { getRequestUserEmail } from "../../../../../server-auth";

export const dynamic = "force-dynamic";

export async function GET(
  request: Request,
  context: { params: Promise<{ deviceId: string }> },
) {
  const userEmail = await getRequestUserEmail(request);
  if (!userEmail) return noStore({ error: "로그인이 필요합니다." }, 401);
  const { deviceId } = await context.params;
  const semantics = await getRobotMapSemantics(deviceId, userEmail);
  if (!semantics) {
    return noStore({ error: "이 로봇의 사용자 지도를 볼 권한이 없습니다." }, 403);
  }
  return noStore(semantics, 200);
}
