import { reportMissedFall } from "../../../../../db/fall-review";
import { consumeRequestRateLimit } from "../../../../../db/petcam";
import { noStore } from "../../../../api-response";
import { fallMember, fallReviewFailure } from "../../../../fall-review-route";
import { sameOriginJsonRequest } from "../../../../same-origin-request";

export const dynamic = "force-dynamic";

/** 놓친 넘어짐 신고: one moment picked on the recording; recorded only, nobody is notified. */
export async function POST(request: Request, context: { params: Promise<{ deviceId: string }> }) {
  const { deviceId } = await context.params;
  const member = await fallMember(request, deviceId);
  if (member.response) return member.response;
  if (!sameOriginJsonRequest(request)) return noStore({ error: "요청 출처를 확인해 주세요." }, 403);
  const body = await request.json().catch(() => null);
  if (!body || typeof body !== "object" || Array.isArray(body) || Object.keys(body).length !== 1 ||
      typeof body.momentAt !== "string") {
    return noStore({ error: "신고 형식을 확인해 주세요." }, 400);
  }
  try {
    if (!(await consumeRequestRateLimit({ userEmail: member.email, roomCode: deviceId,
      scope: "fall-report", limit: 10 }))) {
      return noStore({ error: "신고가 너무 많습니다. 1분 뒤 다시 시도해 주세요." }, 429, { "retry-after": "60" });
    }
    return noStore(await reportMissedFall(deviceId, member.email, body.momentAt), 201);
  } catch (error) { return fallReviewFailure(error); }
}
