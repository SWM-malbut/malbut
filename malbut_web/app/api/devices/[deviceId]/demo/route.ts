import { viewerDemo } from "../../../../../db/demo-feed";
import { noStore } from "../../../../api-response";
import { sessionId } from "../../../../demo-feed-contract";
import { sameOriginJsonRequest } from "../../../../same-origin-request";
import { getRequestUserId } from "../../../../server-auth";

export async function POST(
  req: Request,
  ctx: { params: Promise<{ deviceId: string }> },
) {
  const user = await getRequestUserId(req);
  if (!user) return noStore({ error: "로그인이 필요해요." }, 401);
  if (!sameOriginJsonRequest(req))
    return noStore({ error: "요청 출처를 확인해 주세요." }, 403);
  const data = await req.json().catch(() => null);
  if (
    !data ||
    !["open", "renew", "read", "exit"].includes(data.action) ||
    (data.action === "open" ? data.session !== null : !sessionId(data.session))
  )
    return noStore({ error: "잘못된 요청이에요." }, 400);
  try {
    const { deviceId } = await ctx.params;
    const view = await viewerDemo(
      deviceId,
      user,
      data.session,
      data.action === "exit",
      data.action !== "read",
    );
    return noStore(view ?? { closed: true });
  } catch (error) {
    const code = error instanceof Error ? error.message : "";
    if (code === "FORBIDDEN")
      return noStore({ error: "말벗 소유자만 볼 수 있어요." }, 403);
    if (code === "ENDED" || code === "EXPIRED")
      return noStore({ error: code }, 409);
    return noStore({ error: "시연 화면을 불러오지 못했어요." }, 503);
  }
}
