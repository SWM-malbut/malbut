import { requestFallAiReview } from "../../../../../db/fall-ai-review";
import { reportMissedFall } from "../../../../../db/fall-review";
import { consumeRequestRateLimit } from "../../../../../db/petcam";
import { noStore } from "../../../../api-response";
import { startFallAiJob } from "../../../../fall-ai-review-worker";
import { fallMember, fallReviewFailure } from "../../../../fall-review-route";
import { sameOriginJsonRequest } from "../../../../same-origin-request";

export const dynamic = "force-dynamic";

/**
 * 놓친 넘어짐 신고: one moment picked on the recording; recorded only, nobody is
 * notified. [신고하고 AI에게 검토 받기] also queues a photo-only review; if that
 * cannot start (no consent or key), the report is still kept.
 */
export async function POST(request: Request, context: { params: Promise<{ deviceId: string }> }) {
  const { deviceId } = await context.params;
  const member = await fallMember(request, deviceId);
  if (member.response) return member.response;
  if (!sameOriginJsonRequest(request)) return noStore({ error: "요청 출처를 확인해 주세요." }, 403);
  const body = await request.json().catch(() => null);
  if (!body || typeof body !== "object" || Array.isArray(body) ||
      !Object.keys(body).every((k) => ["momentAt", "requestAiReview", "memo"].includes(k)) ||
      typeof body.momentAt !== "string" ||
      !(body.requestAiReview === undefined || typeof body.requestAiReview === "boolean") ||
      !(body.memo === undefined || body.memo === null || (typeof body.memo === "string" && body.memo.length <= 500))) {
    return noStore({ error: "신고 형식을 확인해 주세요." }, 400);
  }
  try {
    if (!(await consumeRequestRateLimit({ userEmail: member.email, roomCode: deviceId,
      scope: "fall-report", limit: 10 }))) {
      return noStore({ error: "신고가 너무 많습니다. 1분 뒤 다시 시도해 주세요." }, 429, { "retry-after": "60" });
    }
    const report = await reportMissedFall(deviceId, member.email, body.momentAt, Date.now(), body.memo ?? null);
    if (!body.requestAiReview) return noStore(report, 201);
    try {
      const { reviewId } = await requestFallAiReview(deviceId, report.incidentId, member.email, body.momentAt);
    // Answer now; the review runs after the response (or in the maintenance worker).
    startFallAiJob({ deviceId, jobId: reviewId });
      return noStore({ ...report, aiReview: { reviewId } }, 201);
    } catch (error) {
      const reason = error instanceof Error ? error.message : "";
      return noStore({ ...report, aiReview: { error: reason === "FALL_AI_CONSENT_OFF" ? "consent_off"
        : reason === "FALL_AI_KEY_MISSING" ? "key_missing" : "unavailable" } }, 201);
    }
  } catch (error) { return fallReviewFailure(error); }
}
