import { getUserProfile, normalizeDisplayName, setDisplayName } from "../../../db/users";
import { noStore } from "../../api-response";
import { sameOriginJsonRequest } from "../../same-origin-request";
import { getRequestUserId } from "../../server-auth";

export const dynamic = "force-dynamic";

/** The signed-in person's own account: the name others see, and the email until social login. */
export async function GET(request: Request) {
  const userId = await getRequestUserId(request);
  if (!userId) return noStore({ error: "로그인이 필요합니다." }, 401);
  const profile = await getUserProfile(userId);
  return noStore({ userId, displayName: profile.displayName, email: profile.email, providers: profile.providers }, 200);
}

/** "어떻게 불러 드릴까요?" and 설정 › 이름 바꾸기. */
export async function PATCH(request: Request) {
  const userId = await getRequestUserId(request);
  if (!userId) return noStore({ error: "로그인이 필요합니다." }, 401);
  if (!sameOriginJsonRequest(request)) return noStore({ error: "요청 출처를 확인해 주세요." }, 403);
  const payload = (await request.json().catch(() => null)) as { displayName?: unknown } | null;
  if (!payload || Object.keys(payload).some((key) => key !== "displayName")) {
    return noStore({ error: "이름을 확인해 주세요." }, 400);
  }
  const displayName = normalizeDisplayName(payload.displayName);
  if (!displayName) return noStore({ error: "이름은 1~20자로 적어 주세요." }, 400);
  await setDisplayName(userId, displayName);
  return noStore({ displayName }, 200);
}
