import { INCIDENT_FILTERS, listFallIncidentSummaries, type IncidentFilter } from "../../../../../db/fall-review";
import { userCanViewDevice } from "../../../../../db/homecam";
import { noStore } from "../../../../api-response";
import { getRequestUserId } from "../../../../server-auth";

export const dynamic = "force-dynamic";

export async function GET(request: Request, context: { params: Promise<{ deviceId: string }> }) {
  const userId = await getRequestUserId(request);
  if (!userId) return noStore({ error: "로그인이 필요합니다." }, 401);
  const { deviceId } = await context.params;
  if (!(await userCanViewDevice(deviceId, userId))) return noStore({ error: "장치를 찾을 수 없습니다." }, 404);
  const filter = new URL(request.url).searchParams.get("filter") ?? "all";
  if (!(INCIDENT_FILTERS as readonly string[]).includes(filter)) {
    return noStore({ error: "사건 필터를 확인해 주세요." }, 400);
  }
  try {
    return noStore({ filter, incidents: await listFallIncidentSummaries(deviceId, filter as IncidentFilter) });
  } catch {
    return noStore({ error: "사건 목록을 불러오지 못했습니다." }, 503);
  }
}
