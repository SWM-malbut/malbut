import { acceptInvite } from "../../../../db/guardians";
import { consumeRequestRateLimit } from "../../../../db/request-rate-limit";
import { noStore } from "../../../api-response";
import { getRuntimeEnvironment } from "../../../runtime-env";
import { sameOriginJsonRequest } from "../../../same-origin-request";
import { getRequestUserId } from "../../../server-auth";

export const dynamic = "force-dynamic";

/** Opened an invite link while signed in: join that 말벗 as a guardian. */
export async function POST(request: Request) {
  const userId = await getRequestUserId(request);
  if (!userId) return noStore({ error: "로그인이 필요합니다." }, 401);
  if (!sameOriginJsonRequest(request)) return noStore({ error: "요청 출처를 확인해 주세요." }, 403);
  const sessionSecret = getRuntimeEnvironment().AUTH_SESSION_SECRET?.trim();
  if (!sessionSecret) return noStore({ error: "지금은 초대를 받을 수 없어요." }, 503);
  const payload = (await request.json().catch(() => null)) as { token?: unknown } | null;
  if (!payload || typeof payload.token !== "string" || Object.keys(payload).some((key) => key !== "token")) {
    return noStore({ error: "요청 형식을 확인해 주세요." }, 400);
  }
  if (!(await consumeRequestRateLimit({ userId, roomCode: "-", scope: "invite", limit: 10 }))) {
    return noStore({ error: "잠시 후 다시 시도해 주세요." }, 429);
  }
  const result = await acceptInvite({ token: payload.token, userId, sessionSecret });
  return noStore({ status: result.status, deviceId: result.deviceId ?? null }, result.status === "unusable" ? 404 : 200);
}
