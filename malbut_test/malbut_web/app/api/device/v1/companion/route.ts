import { syncCompanion } from "../../../../../db/companion";
import { noStore } from "../../../../api-response";
import { getRequestDevice } from "../../../../device-auth";
import { readVoiceJson } from "../../../../voice-agent-http";
import { parseCompanionReport } from "../../../../companion-contract";
import { companionFailure } from "../../../../companion-http";

export const dynamic = "force-dynamic";
export async function POST(request: Request) {
  try {
    const device = await getRequestDevice(request);
    if (!device) return noStore({ error: "장치 인증이 필요해요." }, 401);
    const report = parseCompanionReport(await readVoiceJson(request).catch(() => null));
    if (!report) return noStore({ error: "대화 상태 형식을 확인해 주세요." }, 400);
    return noStore(await syncCompanion(device, report));
  } catch (error) { return companionFailure(error); }
}
