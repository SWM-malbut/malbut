import { revokePushSubscription } from "../../../../db/homecam";
import { noStore } from "../../../api-response";
import { getRequestUserEmail } from "../../../server-auth";

export const dynamic = "force-dynamic";

export async function DELETE(
  request: Request,
  context: { params: Promise<{ subscriptionId: string }> },
) {
  const userEmail = await getRequestUserEmail(request);
  if (!userEmail) return noStore({ error: "로그인이 필요합니다." }, 401);
  const { subscriptionId } = await context.params;
  if (
    !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(
      subscriptionId,
    )
  ) {
    return noStore({ error: "Web Push 구독을 찾을 수 없습니다." }, 404);
  }
  const revoked = await revokePushSubscription(userEmail, subscriptionId);
  return noStore({ revoked }, revoked ? 200 : 404);
}
