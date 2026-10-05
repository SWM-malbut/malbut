import { askFallAiQuestion, listFallAiReviews } from "../../../../../../../../../db/fall-ai-review";
import { consumeRequestRateLimit } from "../../../../../../../../../db/request-rate-limit";
import { noStore } from "../../../../../../../../api-response";
import { fallAiFailure } from "../../../../../../../../fall-ai-route";
import { startFallAiJob } from "../../../../../../../../fall-ai-review-worker";
import { fallMember } from "../../../../../../../../fall-review-route";
import { sameOriginJsonRequest } from "../../../../../../../../same-origin-request";

export const dynamic = "force-dynamic";
type Context = { params: Promise<{ deviceId: string; incidentId: string; reviewId: string }> };
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

/** Follow-up about the same photos; the answer is reference text and never changes the verdict. */
export async function POST(request: Request, context: Context) {
  const { deviceId, incidentId, reviewId } = await context.params;
  const member = await fallMember(request, deviceId, incidentId);
  if (member.response) return member.response;
  if (!uuid.test(reviewId)) return noStore({ error: "AI 검토를 찾을 수 없습니다." }, 404);
  if (!sameOriginJsonRequest(request)) return noStore({ error: "요청 출처를 확인해 주세요." }, 403);
  const body = await request.json().catch(() => null);
  if (!body || typeof body !== "object" || Array.isArray(body) ||
      !Object.keys(body).every((k) => k === "question" || k === "includeContext") ||
      typeof body.question !== "string" ||
      !(body.includeContext === undefined || typeof body.includeContext === "boolean")) {
    return noStore({ error: "질문은 1~500자로 입력해 주세요." }, 400);
  }
  try {
    if (!(await consumeRequestRateLimit({ userId: member.userId, roomCode: deviceId,
      scope: "fall-ai-question", limit: 10 }))) {
      return noStore({ error: "요청이 너무 많습니다. 1분 뒤 다시 시도해 주세요." }, 429, { "retry-after": "60" });
    }
    const { questionId } = await askFallAiQuestion(deviceId, incidentId, reviewId, member.userId, body.question,
      body.includeContext ?? true);
    // Answer now; the review runs after the response (or in the maintenance worker).
    startFallAiJob({ deviceId, jobId: questionId });
    const review = (await listFallAiReviews(deviceId, incidentId)).find((r) => r.reviewId === reviewId);
    return noStore({ review }, 202);
  } catch (error) { return fallAiFailure(error); }
}
