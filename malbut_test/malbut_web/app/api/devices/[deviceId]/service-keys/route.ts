import { readFallCloudKeyView } from "../../../../../db/fall-ai-review";
import { userCanManageDevice } from "../../../../../db/homecam";
import { readKeyProblems, readServiceKeyViews } from "../../../../../db/service-keys";
import { noStore } from "../../../../api-response";
import { getRequestUserId } from "../../../../server-auth";

export const dynamic = "force-dynamic";

/** 설정 › AI·서비스 키 (소유자만): whether each key is set, its last 4 characters, never the key. */
export async function GET(request: Request, context: { params: Promise<{ deviceId: string }> }) {
  const userId = await getRequestUserId(request);
  if (!userId) return noStore({ error: "로그인이 필요합니다." }, 401);
  const { deviceId } = await context.params;
  if (!(await userCanManageDevice(deviceId, userId))) return noStore({ error: "소유자만 볼 수 있어요." }, 403);
  const [keys, fall, problems] = await Promise.all([
    readServiceKeyViews(deviceId), readFallCloudKeyView(deviceId), readKeyProblems(deviceId),
  ]);
  return noStore({
    openai: { ...keys.openai, problem: problems.openai },
    kma: { ...keys.kma, problem: problems.kma },
    fall: { ...fall, problem: problems.fall },
  }, 200);
}
