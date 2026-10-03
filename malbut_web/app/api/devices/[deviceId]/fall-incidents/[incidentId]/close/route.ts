import { closeFallIncident } from "../../../../../../../db/fall-review";
import { noStore } from "../../../../../../api-response";
import { fallMember, fallReviewFailure } from "../../../../../../fall-review-route";
import { sameOriginJsonRequest } from "../../../../../../same-origin-request";

export const dynamic = "force-dynamic";
type Context = { params: Promise<{ deviceId: string; incidentId: string }> };

/** "처리 완료": any member; the only way an incident closes. */
export async function POST(request: Request, context: Context) {
  const { deviceId, incidentId } = await context.params;
  const member = await fallMember(request, deviceId, incidentId);
  if (member.response) return member.response;
  if (!sameOriginJsonRequest(request)) return noStore({ error: "요청 출처를 확인해 주세요." }, 403);
  try {
    return noStore(await closeFallIncident(deviceId, incidentId, member.email));
  } catch (error) { return fallReviewFailure(error); }
}
