import { companionView, changeCompanion } from "../../../../../db/companion";
import { noStore } from "../../../../api-response";
import { getRequestUserId } from "../../../../server-auth";
import { readVoiceJson, sameOriginVoiceRequest } from "../../../../voice-agent-http";
import { parseCompanionChange } from "../../../../companion-contract";
import { companionFailure } from "../../../../companion-http";

export const dynamic = "force-dynamic";
type Context = { params: Promise<{ deviceId: string }> };
export async function GET(request: Request, context: Context) {
  try {
    const user = await getRequestUserId(request);
    if (!user) return noStore({ error: "로그인이 필요해요." }, 401);
    return noStore(await companionView((await context.params).deviceId, user));
  } catch (error) { return companionFailure(error); }
}
export async function PATCH(request: Request, context: Context) {
  try {
    const user = await getRequestUserId(request);
    if (!user) return noStore({ error: "로그인이 필요해요." }, 401);
    if (!sameOriginVoiceRequest(request)) return noStore({ error: "요청 출처를 확인해 주세요." }, 403);
    const change = parseCompanionChange(await readVoiceJson(request).catch(() => null));
    if (!change) return noStore({ error: "설정 형식을 확인해 주세요." }, 400);
    return noStore(await changeCompanion((await context.params).deviceId, user, change));
  } catch (error) { return companionFailure(error); }
}
