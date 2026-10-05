import { consumeRequestRateLimit } from "../../../db/request-rate-limit";
import {
  normalizeRegistrationCode,
  redeemRegistrationCode,
  type RegistrationHistory,
} from "../../../db/registration";
import { noStore } from "../../api-response";
import { getRuntimeEnvironment } from "../../runtime-env";
import { sameOriginJsonRequest } from "../../same-origin-request";
import { getRequestUserId } from "../../server-auth";

export const dynamic = "force-dynamic";

const ATTEMPTS_PER_MINUTE = 10;

const FAILURES = {
  invalid: [404, "코드를 찾을 수 없어요. 받은 코드를 다시 확인해 주세요."],
  used: [404, "이미 사용한 코드예요. 말벗 팀에게 새 코드를 받아 주세요."],
  expired: [404, "기간이 지난 코드예요. 말벗 팀에게 새 코드를 받아 주세요."],
  already_owner: [409, "이미 이 말벗의 소유자예요."],
} as const;

/** 말벗 등록: a registration code makes the signed-in person the owner. */
export async function POST(request: Request) {
  const userId = await getRequestUserId(request);
  if (!userId) return noStore({ error: "로그인이 필요합니다." }, 401);
  if (!sameOriginJsonRequest(request)) return noStore({ error: "요청 출처를 확인해 주세요." }, 403);
  const sessionSecret = getRuntimeEnvironment().AUTH_SESSION_SECRET?.trim();
  if (!sessionSecret) return noStore({ error: "지금은 등록할 수 없어요." }, 503);
  const payload = (await request.json().catch(() => null)) as { code?: unknown; history?: unknown } | null;
  if (
    !payload ||
    typeof payload !== "object" ||
    Object.keys(payload).some((key) => key !== "code" && key !== "history") ||
    (payload.history !== undefined && payload.history !== "keep" && payload.history !== "delete")
  ) {
    return noStore({ error: "요청 형식을 확인해 주세요." }, 400);
  }
  // Every guess counts, so a code cannot be found by trying many.
  if (!(await consumeRequestRateLimit({ userId, roomCode: "-", scope: "registration", limit: ATTEMPTS_PER_MINUTE }))) {
    return noStore({ error: "잠시 후 다시 시도해 주세요." }, 429);
  }
  const code = normalizeRegistrationCode(payload.code);
  const [failureStatus, failureMessage] = FAILURES.invalid;
  if (!code) return noStore({ status: "invalid", error: failureMessage }, failureStatus);
  const result = await redeemRegistrationCode({
    code,
    userId,
    history: payload.history as RegistrationHistory | undefined,
    sessionSecret,
  });
  if (result.status === "registered") return noStore({ status: "registered" }, 200);
  if (result.status === "needs_confirmation") return noStore({ status: "needs_confirmation" }, 409);
  const [status, error] = FAILURES[result.status];
  return noStore({ status: result.status, error }, status);
}
