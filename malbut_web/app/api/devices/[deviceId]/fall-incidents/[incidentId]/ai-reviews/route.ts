import { listFallAiReviews, requestFallAiReview } from "../../../../../../../db/fall-ai-review";
import { consumeRequestRateLimit } from "../../../../../../../db/petcam";
import { noStore } from "../../../../../../api-response";
import { fallAiFailure } from "../../../../../../fall-ai-route";
import { startFallAiJob } from "../../../../../../fall-ai-review-worker";
import { fallMember } from "../../../../../../fall-review-route";
import { sameOriginJsonRequest } from "../../../../../../same-origin-request";

export const dynamic = "force-dynamic";
type Context = { params: Promise<{ deviceId: string; incidentId: string }> };

/** "AI에게 (다시) 검토 받기": one user-picked moment, 12 photos over 5 s, photo-only verdict. */
export async function POST(request: Request, context: Context) {
  const { deviceId, incidentId } = await context.params;
  const member = await fallMember(request, deviceId, incidentId);
  if (member.response) return member.response;
  if (!sameOriginJsonRequest(request)) return noStore({ error: "요청 출처를 확인해 주세요." }, 403);
  const body = await request.json().catch(() => null);
  if (!body || typeof body !== "object" || Array.isArray(body) || Object.keys(body).length !== 1 ||
      typeof body.momentAt !== "string") {
    return noStore({ error: "넘어진 순간 형식을 확인해 주세요." }, 400);
  }
  try {
    if (!(await consumeRequestRateLimit({ userEmail: member.email, roomCode: deviceId,
      scope: "fall-ai-review", limit: 10 }))) {
      return noStore({ error: "요청이 너무 많습니다. 1분 뒤 다시 시도해 주세요." }, 429, { "retry-after": "60" });
    }
    const { reviewId } = await requestFallAiReview(deviceId, incidentId, member.email, body.momentAt);
    // Answer now; the review runs after the response (or in the maintenance worker).
    startFallAiJob({ deviceId, jobId: reviewId });
    const review = (await listFallAiReviews(deviceId, incidentId)).find((r) => r.reviewId === reviewId);
    return noStore({ review }, 202);
  } catch (error) { return fallAiFailure(error); }
}
