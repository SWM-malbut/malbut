import { userCanViewDevice } from "../../../../../db/homecam";
import { listVoiceHistory, readVoiceDelegation, saveVoiceDelegation } from "../../../../../db/voice-agent";
import { noStore } from "../../../../api-response";
import { getRequestUserId } from "../../../../server-auth";
import { readVoiceJson, sameOriginVoiceRequest, voiceApiFailure } from "../../../../voice-agent-http";

export const dynamic = "force-dynamic";
type Context = { params: Promise<{ deviceId: string }> };
export async function GET(request: Request, context: Context) {
  try {
    const userId = await getRequestUserId(request);
    if (!userId) return noStore({ error: "로그인이 필요합니다." }, 401);
    const { deviceId } = await context.params;
    if (!(await userCanViewDevice(deviceId, userId))) return noStore({ error: "장치를 볼 권한이 없습니다." }, 403);
    const [delegation, requests] = await Promise.all([readVoiceDelegation(deviceId), listVoiceHistory(deviceId)]);
    return noStore({ delegation, requests });
  } catch (error) { return voiceApiFailure(error); }
}
export async function PATCH(request: Request, context: Context) {
  try {
    const userId = await getRequestUserId(request);
    if (!userId) return noStore({ error: "로그인이 필요합니다." }, 401);
    if (!sameOriginVoiceRequest(request)) return noStore({ error: "요청 출처를 확인해 주세요." }, 403);
    const { deviceId } = await context.params;
    const input = await readVoiceJson(request);
    if (!input || typeof input !== "object" || Array.isArray(input) || Object.keys(input).length !== 1 || typeof input.enabled !== "boolean") throw new Error("VOICE_INVALID_REQUEST");
    return noStore({ delegation: await saveVoiceDelegation(deviceId, userId, input.enabled) });
  } catch (error) { return voiceApiFailure(error instanceof SyntaxError ? new Error("VOICE_INVALID_REQUEST") : error); }
}
