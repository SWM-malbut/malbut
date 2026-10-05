import { listFallAiReviews } from "../../../../../../db/fall-ai-review";
import { getFallIncidentDetail } from "../../../../../../db/fall-review";
import { noStore } from "../../../../../api-response";
import { fallMember, fallReviewFailure } from "../../../../../fall-review-route";

export const dynamic = "force-dynamic";
type Context = { params: Promise<{ deviceId: string; incidentId: string }> };

export async function GET(request: Request, context: Context) {
  const { deviceId, incidentId } = await context.params;
  const member = await fallMember(request, deviceId, incidentId);
  if (member.response) return member.response;
  try {
    const incident = await getFallIncidentDetail(deviceId, incidentId);
    if (!incident) return noStore({ error: "사건을 찾을 수 없습니다." }, 404);
    // "AI 검토 결과": recorded next to the incident, never replacing the automatic judgment.
    const aiReviews = await listFallAiReviews(deviceId, incidentId);
    // viewerEmail lets the app mark "my" opinion; members already see each other's emails.
    return noStore({ incident: { ...incident, aiReviews, viewerUserId: member.userId } });
  } catch (error) { return fallReviewFailure(error); }
}
