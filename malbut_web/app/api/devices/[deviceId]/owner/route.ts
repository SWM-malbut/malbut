import { transferOwnership } from "../../../../../db/guardians";
import { noStore } from "../../../../api-response";
import { sameOriginJsonRequest } from "../../../../same-origin-request";
import { getRequestUserId } from "../../../../server-auth";

export const dynamic = "force-dynamic";

/** 설정 › 소유자 넘기기: the owner hands the 말벗 to one of its guardians. */
export async function POST(request: Request, context: { params: Promise<{ deviceId: string }> }) {
  const userId = await getRequestUserId(request);
  if (!userId) return noStore({ error: "로그인이 필요합니다." }, 401);
  if (!sameOriginJsonRequest(request)) return noStore({ error: "요청 출처를 확인해 주세요." }, 403);
  const { deviceId } = await context.params;
  const payload = (await request.json().catch(() => null)) as { userId?: unknown } | null;
  if (!payload || typeof payload.userId !== "string" || !payload.userId || Object.keys(payload).some((key) => key !== "userId")) {
    return noStore({ error: "새 소유자를 확인해 주세요." }, 400);
  }
  try {
    await transferOwnership({ deviceId, ownerUserId: userId, newOwnerUserId: payload.userId });
    return noStore({ transferred: true }, 200);
  } catch (error) {
    if (error instanceof Error && error.message === "GUARDIANS_FORBIDDEN") {
      return noStore({ error: "소유자만 넘길 수 있어요." }, 403);
    }
    if (error instanceof Error && error.message === "OWNER_TARGET_INVALID") {
      return noStore({ error: "이 말벗의 보호자에게만 넘길 수 있어요." }, 409);
    }
    throw error;
  }
}
