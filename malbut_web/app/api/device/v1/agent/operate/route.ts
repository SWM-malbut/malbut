import { getRequestDevice } from "../../../../../device-auth";
import { noStore } from "../../../../../api-response";
import { parseVoiceRequest } from "../../../../../voice-agent-contract";
import { readVoiceJson, voiceApiFailure } from "../../../../../voice-agent-http";
import { operateVoiceDevice } from "../../../../../../db/voice-agent";

export const dynamic = "force-dynamic";
export async function POST(request: Request) {
  try {
    const device = await getRequestDevice(request);
    if (!device) return noStore({ success: false, code: "UNAUTHORIZED", result: {}, message: "유효한 장치 토큰이 필요합니다." }, 401);
    const input = parseVoiceRequest(await readVoiceJson(request));
    if (!input) throw new Error("VOICE_INVALID_REQUEST");
    const reply = await operateVoiceDevice(device, input);
    return noStore(reply, reply.success ? 200 : reply.code === "CAMERA_DISABLED" ? 409 :
      reply.code === "VOICE_REFERENCE_NOT_FOUND" ? 404 : 403);
  } catch (error) { return voiceApiFailure(error instanceof SyntaxError ? new Error("VOICE_INVALID_REQUEST") : error); }
}
