import { allowFallUpload } from "../../../../../db/fall-incidents";
import { storeFallClip } from "../../../../../db/fall-review";
import { noStore, unauthorized } from "../../../../api-response";
import { getRequestDevice } from "../../../../device-auth";
import { parseFallClip } from "../../../../fall-clip-contract";
import { readBoundedJson } from "../../../../fall-contract";

export const dynamic = "force-dynamic";

// Clip ranges never create notifications; they only point into the recording.
export async function POST(request: Request) {
  try {
    const device = await getRequestDevice(request);
    if (!device) return unauthorized("유효한 장치 토큰이 필요합니다.");
    if (request.headers.get("x-malbut-device-id") !== device.deviceId) {
      return noStore({ error: "기록의 장치와 인증된 장치가 다릅니다." }, 403);
    }
    const clip = parseFallClip(await readBoundedJson(request, 8192));
    if (!clip) return noStore({ error: "사건 클립 형식을 확인해 주세요." }, 400);
    if (!(await allowFallUpload(device.deviceId))) {
      return noStore({ error: "요청이 너무 많습니다." }, 429, { "retry-after": "60" });
    }
    const { created, ...ack } = await storeFallClip(device.deviceId, clip);
    return noStore(ack, created ? 201 : 200);
  } catch (error) {
    const code = error instanceof Error ? error.message : "";
    // 503, not 409: the robot blocks on 409, but the incident event may simply arrive later.
    if (code === "FALL_CLIP_INCIDENT_MISSING") {
      return noStore({ error: "사건이 아직 저장되지 않았습니다." }, 503, { "retry-after": "10" });
    }
    if (code === "FALL_CLIP_CONFLICT") return noStore({ error: "기존 사건 클립과 요청 내용이 다릅니다." }, 409);
    return noStore({ error: "사건 클립을 저장하지 못했습니다." }, 503);
  }
}
