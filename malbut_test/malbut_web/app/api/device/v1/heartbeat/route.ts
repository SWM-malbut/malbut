import { noStore, unauthorized } from "../../../../api-response";
import { getRequestDevice } from "../../../../device-auth";
import { exchangeHomecamHeartbeat } from "../../../../homecam-heartbeat";

export const dynamic = "force-dynamic";

export async function POST(request: Request) {
  const device = await getRequestDevice(request);
  if (!device) return unauthorized("유효한 장치 토큰이 필요합니다.");
  const { status, body } = await exchangeHomecamHeartbeat(device.deviceId, await request.json().catch(() => null));
  return noStore(body, status);
}
