import { storeRobotState } from "../../../../../../db/robot-map";
import { parseRobotState, readRobotJson } from "../../../../../robot-contract";
import { noStore, unauthorized } from "../../../../../api-response";
import { getRequestDevice } from "../../../../../device-auth";

export const dynamic = "force-dynamic";

export async function POST(request: Request) {
  const device = await getRequestDevice(request);
  if (!device) return unauthorized("유효한 장치 토큰이 필요합니다.");
  try {
    const state = parseRobotState(await readRobotJson(request, "state"));
    if (!state) return noStore({ error: "로봇 상태 형식을 확인해 주세요." }, 400);
    await storeRobotState(device.deviceId, state);
    return noStore({ accepted: true }, 202);
  } catch (error) {
    return robotUploadError(error);
  }
}

function robotUploadError(error: unknown) {
  if (!(error instanceof Error)) return noStore({ error: "로봇 상태를 저장하지 못했습니다." }, 500);
  if (error.message === "UNSUPPORTED_MEDIA_TYPE") return noStore({ error: "application/json만 지원합니다." }, 415);
  if (error.message === "PAYLOAD_TOO_LARGE") return noStore({ error: "요청 본문이 너무 큽니다." }, 413);
  if (error.message === "INVALID_JSON") return noStore({ error: "JSON 형식을 확인해 주세요." }, 400);
  return noStore({ error: "로봇 상태를 저장하지 못했습니다." }, 500);
}
