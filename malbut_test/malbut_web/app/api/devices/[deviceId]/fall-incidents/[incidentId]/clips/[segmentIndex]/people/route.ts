import { getFallClipPeople } from "../../../../../../../../../db/fall-people";
import { noStore } from "../../../../../../../../api-response";
import { fallMember } from "../../../../../../../../fall-review-route";

export const dynamic = "force-dynamic";
type Context = { params: Promise<{ deviceId: string; incidentId: string; segmentIndex: string }> };

/** Person boxes for the scene video, fetched only when the user plays it. */
export async function GET(request: Request, context: Context) {
  const { deviceId, incidentId, segmentIndex } = await context.params;
  const member = await fallMember(request, deviceId, incidentId);
  if (member.response) return member.response;
  if (!/^(?:[0-9]|[12][0-9]|3[01])$/.test(segmentIndex)) return noStore({ error: "장면을 찾을 수 없습니다." }, 404);
  try {
    const people = await getFallClipPeople(deviceId, incidentId, Number(segmentIndex));
    if (!people) return noStore({ error: "이 장면에는 사람 표시가 없습니다." }, 404);
    return noStore(people);
  } catch {
    return noStore({ error: "사람 표시를 불러오지 못했습니다." }, 503);
  }
}
