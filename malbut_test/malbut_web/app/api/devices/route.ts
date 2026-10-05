import { listHomecamDevices } from "../../../db/homecam";
import { noStore } from "../../api-response";
import { getRequestUserId } from "../../server-auth";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const userId = await getRequestUserId(request);
  if (!userId) return noStore({ error: "로그인이 필요합니다." }, 401);
  try {
    return noStore({ devices: await listHomecamDevices(userId) }, 200);
  } catch {
    return noStore({ error: "홈캠 목록을 불러오지 못했습니다." }, 500);
  }
}
