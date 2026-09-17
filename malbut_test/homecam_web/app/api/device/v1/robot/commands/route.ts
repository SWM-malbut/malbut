import { claimRobotCommands } from "../../../../../../db/robot-map";
import { noStore, unauthorized } from "../../../../../api-response";
import { getRequestDevice } from "../../../../../device-auth";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const device = await getRequestDevice(request);
  if (!device) return unauthorized("유효한 장치 토큰이 필요합니다.");
  return noStore({ commands: await claimRobotCommands(device.deviceId) }, 200);
}
