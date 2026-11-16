import { saveOwnerVoiceProfile } from "../../../../../db/robot-map";
import { noStore } from "../../../../api-response";
import { getRequestUserId } from "../../../../server-auth";
import { readVoiceJson, sameOriginVoiceRequest } from "../../../../voice-agent-http";
import { parseVoiceProfile } from "../../../../voice-profile";

export async function PUT(request: Request, context: { params: Promise<{ deviceId: string }> }) {
  const userId = await getRequestUserId(request);
  if (!userId) return noStore({ error: "로그인이 필요해요." }, 401);
  if (!sameOriginVoiceRequest(request)) return noStore({ error: "요청 출처를 확인해 주세요." }, 403);
  const profile = parseVoiceProfile(await readVoiceJson(request).catch(() => null));
  if (!profile) return noStore({ error: "대화 설정 범위를 확인해 주세요." }, 400);
  const { deviceId } = await context.params;
  const saved = await saveOwnerVoiceProfile(deviceId, userId, profile);
  if (!saved) return noStore({ error: "이 말벗의 소유자만 대화 설정을 바꿀 수 있어요." }, 403);
  return noStore({ profile });
}
