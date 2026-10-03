import { getFallTimeline } from "../../../../../db/fall-review";
import { noStore } from "../../../../api-response";
import { fallMember, fallReviewFailure } from "../../../../fall-review-route";

export const dynamic = "force-dynamic";

/** 연속 녹화 화면: recorded spans and incident marks for one day (?from=&to=, ≤ 26 h). */
export async function GET(request: Request, context: { params: Promise<{ deviceId: string }> }) {
  const { deviceId } = await context.params;
  const member = await fallMember(request, deviceId);
  if (member.response) return member.response;
  const params = new URL(request.url).searchParams;
  const from = params.get("from") ?? "", to = params.get("to") ?? "";
  try {
    return noStore(await getFallTimeline(deviceId, from, to));
  } catch (error) {
    if (error instanceof Error && error.message === "FALL_TIMELINE_RANGE_INVALID") {
      return noStore({ error: "최근 7일 안의 하루를 골라 주세요." }, 400);
    }
    return fallReviewFailure(error);
  }
}
