import { storeRobotMap } from "../../../../../../db/robot-map";
import { parseRobotMap, readRobotJson } from "../../../../../robot-contract";
import { noStore, unauthorized } from "../../../../../api-response";
import { getRequestDevice } from "../../../../../device-auth";

export const dynamic = "force-dynamic";

export async function PUT(request: Request) {
  const device = await getRequestDevice(request);
  if (!device) return unauthorized("유효한 장치 토큰이 필요합니다.");
  try {
    const map = parseRobotMap(await readRobotJson(request, "map"));
    if (!map) return noStore({ error: "지도 형식을 확인해 주세요." }, 400);
    await storeRobotMap(device.deviceId, map);
    return noStore({ accepted: true, revision: map.revision }, 202);
  } catch (error) {
    if (!(error instanceof Error)) return noStore({ error: "지도를 저장하지 못했습니다." }, 500);
    if (error.message === "UNSUPPORTED_MEDIA_TYPE") return noStore({ error: "application/json만 지원합니다." }, 415);
    if (error.message === "PAYLOAD_TOO_LARGE") return noStore({ error: "요청 본문이 너무 큽니다." }, 413);
    if (error.message === "INVALID_JSON") return noStore({ error: "JSON 형식을 확인해 주세요." }, 400);
    return noStore({ error: "지도를 저장하지 못했습니다." }, 500);
  }
}
