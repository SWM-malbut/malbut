import { allowFallUpload } from "../../../../../db/fall-incidents";
import { storeFallPeople } from "../../../../../db/fall-people";
import { noStore, unauthorized } from "../../../../api-response";
import { getRequestDevice } from "../../../../device-auth";
import { readBoundedJson } from "../../../../fall-contract";
import { PEOPLE_MAX_BODY, parseFallPeople } from "../../../../fall-people-contract";

export const dynamic = "force-dynamic";

// Person boxes of a clip segment (사람 표시); positions only, never notifications.
export async function POST(request: Request) {
  try {
    const device = await getRequestDevice(request);
    if (!device) return unauthorized("유효한 장치 토큰이 필요합니다.");
    if (request.headers.get("x-malbut-device-id") !== device.deviceId) {
      return noStore({ error: "기록의 장치와 인증된 장치가 다릅니다." }, 403);
    }
    const people = parseFallPeople(await readBoundedJson(request, PEOPLE_MAX_BODY));
    if (!people) return noStore({ error: "사람 표시 형식을 확인해 주세요." }, 400);
    if (!(await allowFallUpload(device.deviceId))) {
      return noStore({ error: "요청이 너무 많습니다." }, 429, { "retry-after": "60" });
    }
    const { created, ...ack } = await storeFallPeople(device.deviceId, people);
    return noStore(ack, created ? 201 : 200);
  } catch (error) {
    const code = error instanceof Error ? error.message : "";
    // 503, not 409: the robot blocks on 409, but the clip range may simply arrive later.
    if (code === "FALL_PEOPLE_CLIP_MISSING") {
      return noStore({ error: "사건 클립이 아직 저장되지 않았습니다." }, 503, { "retry-after": "10" });
    }
    if (code === "FALL_PEOPLE_CONFLICT") return noStore({ error: "기존 사람 표시와 요청 내용이 다릅니다." }, 409);
    return noStore({ error: "사람 표시를 저장하지 못했습니다." }, 503);
  }
}
