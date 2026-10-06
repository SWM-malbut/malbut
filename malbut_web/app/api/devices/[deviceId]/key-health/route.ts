import { userCanViewDevice } from "../../../../../db/homecam";
import { orderedKeyProblems, readKeyProblems } from "../../../../../db/service-keys";
import { noStore } from "../../../../api-response";
import { getRequestUserId } from "../../../../server-auth";

export const dynamic = "force-dynamic";

/** 홈 화면 안내 (owners and guardians): which keys need checking, never the keys themselves. */
export async function GET(request: Request, context: { params: Promise<{ deviceId: string }> }) {
  const userId = await getRequestUserId(request);
  if (!userId) return noStore({ error: "로그인이 필요합니다." }, 401);
  const { deviceId } = await context.params;
  if (!(await userCanViewDevice(deviceId, userId))) return noStore({ error: "말벗을 찾을 수 없습니다." }, 404);
  return noStore({ problems: orderedKeyProblems(await readKeyProblems(deviceId)) }, 200);
}
