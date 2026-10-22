import { INCIDENT_FILTERS, INCIDENT_LIMIT_MAX, INCIDENT_PAGE, listFallIncidentPage, type IncidentFilter } from "../../../../../db/fall-review";
import { userCanViewDevice } from "../../../../../db/homecam";
import { noStore } from "../../../../api-response";
import { getRequestUserId } from "../../../../server-auth";
import { clipCoverageCheck } from "../../../../fall-clip-coverage";

export const dynamic = "force-dynamic";

const instant = (value: string | null) => value !== null && Number.isFinite(Date.parse(value));

export async function GET(request: Request, context: { params: Promise<{ deviceId: string }> }) {
  const userId = await getRequestUserId(request);
  if (!userId) return noStore({ error: "로그인이 필요합니다." }, 401);
  const { deviceId } = await context.params;
  if (!(await userCanViewDevice(deviceId, userId))) return noStore({ error: "장치를 찾을 수 없습니다." }, 404);
  const params = new URL(request.url).searchParams;
  const filter = params.get("filter") ?? "all";
  // 날짜 필터는 휴대폰 날짜로 계산한 [from, to) 구간, 둘 다 있거나 둘 다 없다(SWM25-60).
  const from = params.get("from"), to = params.get("to");
  const ranged = from !== null || to !== null;
  const limit = Number(params.get("limit") ?? INCIDENT_PAGE);
  if (!(INCIDENT_FILTERS as readonly string[]).includes(filter) ||
    (ranged && !(instant(from) && instant(to) && Date.parse(from!) < Date.parse(to!))) ||
    !Number.isInteger(limit) || limit < 1 || limit > INCIDENT_LIMIT_MAX) {
    return noStore({ error: "사건 필터를 확인해 주세요." }, 400);
  }
  try {
    const range = ranged ? { from: new Date(from!).toISOString(), to: new Date(to!).toISOString() } : {};
    const page = await listFallIncidentPage(deviceId, filter as IncidentFilter, clipCoverageCheck(deviceId),
      { ...range, limit });
    return noStore({ filter, ...page });
  } catch {
    return noStore({ error: "사건 목록을 불러오지 못했습니다." }, 503);
  }
}
