import { listFallIncidents } from "../../../../../db/fall-incidents";
import { userCanViewDevice } from "../../../../../db/homecam";
import { noStore } from "../../../../api-response";
import { getRequestUserEmail } from "../../../../server-auth";

export const dynamic = "force-dynamic";

export async function GET(request: Request, context: { params: Promise<{ deviceId: string }> }) {
  const email = await getRequestUserEmail(request);
  if (!email) return noStore({ error: "로그인이 필요합니다." }, 401);
  const { deviceId } = await context.params;
  if (!(await userCanViewDevice(deviceId, email))) return noStore({ error: "장치를 찾을 수 없습니다." }, 404);
  return noStore({ incidents: await listFallIncidents(deviceId) });
}
