import { allowFallUpload, storeFallEvent } from "../../../../../db/fall-incidents";
import { noStore, unauthorized } from "../../../../api-response";
import { getRequestDevice } from "../../../../device-auth";
import { readFallEvent } from "../../../../fall-contract";
import { deliverPendingFallPush } from "../../../../fall-event-push";

export const dynamic = "force-dynamic";

export async function POST(request: Request) {
  try {
    const device = await getRequestDevice(request);
    if (!device) return unauthorized("유효한 장치 토큰이 필요합니다.");
    if (request.headers.get("x-malbut-device-id") !== device.deviceId) {
      return noStore({ error: "기록의 장치와 인증된 장치가 다릅니다." }, 403);
    }
    const event = await readFallEvent(request);
    if (!event) return noStore({ error: "낙상 사건 형식을 확인해 주세요." }, 400);
    if (!(await allowFallUpload(device.deviceId))) {
      return noStore({ error: "요청이 너무 많습니다." }, 429, { "retry-after": "60" });
    }
    const result = await storeFallEvent(device.deviceId, event);
    let push: unknown = { processed: false, accepted: false, reason: "no_notification" };
    if (event.notificationLevel) {
      try {
        push = await deliverPendingFallPush({ deviceId: device.deviceId, notificationId: event.eventId });
      } catch { push = { processed: false, accepted: false, reason: "queued" }; }
    }
    // A stored intent is acknowledged even if push is pending. The server owns
    // retries now; the device must not create a new notification/event ID.
    return noStore({ ...result, push }, result.created ? 201 : 200);
  } catch (error) {
    if (error instanceof Error && ["FALL_IDEMPOTENCY_CONFLICT", "FALL_SEQUENCE_CONFLICT", "FALL_BOOT_CONFLICT", "FALL_STATE_CONFLICT"].includes(error.message)) {
      return noStore({ error: "기존 사건 식별자와 요청 내용이 다릅니다." }, 409);
    }
    return noStore({ error: "낙상 사건을 저장하지 못했습니다." }, 503);
  }
}
