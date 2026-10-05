import { userCanViewDevice } from "../db/homecam";
import { noStore } from "./api-response";
import { getRequestUserId } from "./server-auth";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

/** Signed-in owner or shared member of the device; incidents are hidden (404) otherwise. */
export async function fallMember(request: Request, deviceId: string, incidentId?: string) {
  const userId = await getRequestUserId(request);
  if (!userId) return { response: noStore({ error: "로그인이 필요합니다." }, 401) };
  if ((incidentId !== undefined && !uuid.test(incidentId)) || !(await userCanViewDevice(deviceId, userId))) {
    return { response: noStore({ error: "사건을 찾을 수 없습니다." }, 404) };
  }
  return { userId };
}

export function fallReviewFailure(error: unknown) {
  const code = error instanceof Error ? error.message : "";
  if (code === "FALL_INCIDENT_NOT_FOUND") return noStore({ error: "사건을 찾을 수 없습니다." }, 404);
  if (code === "FALL_CLOSE_NEEDS_OPINION") {
    return noStore({ error: "의견을 먼저 남겨 주세요. 누구든 의견이 하나 있어야 처리 완료할 수 있어요.", reason: "needs_opinion" }, 409);
  }
  if (code === "FALL_REPORT_INVALID") return noStore({ error: "신고 시각 형식을 확인해 주세요." }, 400);
  if (code === "FALL_REPORT_OUT_OF_RANGE") {
    return noStore({ error: "보관 기간(7일) 안의 지난 시각만 신고할 수 있습니다." }, 400);
  }
  if (code === "FALL_REVIEW_MIGRATION_REQUIRED") return noStore({ error: "사건 DB 준비가 필요합니다." }, 503);
  return noStore({ error: "사건을 처리하지 못했습니다." }, 503);
}
