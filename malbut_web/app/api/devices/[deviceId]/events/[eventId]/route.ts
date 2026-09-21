import {
  softDeleteHomecamEvent,
  userCanManageDevice,
} from "../../../../../../db/homecam";
import { noStore } from "../../../../../api-response";
import { getRequestUserEmail } from "../../../../../server-auth";

export const dynamic = "force-dynamic";

export async function DELETE(
  request: Request,
  context: { params: Promise<{ deviceId: string; eventId: string }> },
) {
  const userEmail = await getRequestUserEmail(request);
  if (!userEmail) return noStore({ error: "로그인이 필요합니다." }, 401);
  const { deviceId, eventId } = await context.params;
  if (!isUuid4(eventId) || !(await userCanManageDevice(deviceId, userEmail))) {
    return noStore({ error: "이벤트를 찾을 수 없습니다." }, 404);
  }
  const removed = await softDeleteHomecamEvent({ deviceId, eventId, userEmail });
  if (!removed) return noStore({ error: "이벤트를 찾을 수 없습니다." }, 404);
  return noStore(
    {
      removed: true,
      rawMediaDeletion: "retention",
      message: "목록에서 삭제했습니다. 원본 영상은 보관 기간이 지나면 자동 삭제됩니다.",
    },
    200,
  );
}

function isUuid4(value: string) {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
}
