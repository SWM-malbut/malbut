import { storeSavedMapEdit, parseSavedMapEdit } from "../../../../../../db/robot-map-edits";
import { readRobotJson } from "../../../../../robot-contract";
import { noStore, unauthorized } from "../../../../../api-response";
import { getRequestDevice } from "../../../../../device-auth";

export async function PUT(request: Request) {
  const device = await getRequestDevice(request);
  if (!device) return unauthorized("유효한 장치 토큰이 필요합니다.");
  try {
    const value = parseSavedMapEdit(await readRobotJson(request, "map"));
    if (!value) return noStore({ error: "지도 형식을 확인해 주세요." }, 400);
    await storeSavedMapEdit(device.deviceId, value);
    return noStore({ accepted: true, revision: value.revision }, 202);
  } catch (error) {
    const code = error instanceof Error ? error.message : "";
    return noStore({ error: "지도 미리보기를 저장하지 못했습니다." },
      code === "PAYLOAD_TOO_LARGE" ? 413 : code === "UNSUPPORTED_MEDIA_TYPE" ? 415 : code === "INVALID_JSON" ? 400 : 500);
  }
}
