import {
  insertHomecamEvent,
} from "../../../../../db/homecam";
import { parseHomecamEventInput } from "../../../../../db/homecam-validation";
import { noStore, unauthorized } from "../../../../api-response";
import { getRequestDevice } from "../../../../device-auth";
import { deliverPendingEventPushes } from "../../../../device-event-push";

export const dynamic = "force-dynamic";

export async function POST(request: Request) {
  const device = await getRequestDevice(request);
  if (!device) return unauthorized("유효한 장치 토큰이 필요합니다.");
  const event = parseHomecamEventInput(await request.json().catch(() => null));
  if (!event) return noStore({ error: "이벤트 형식을 확인해 주세요." }, 400);

  try {
    const result = await insertHomecamEvent(device.deviceId, event);
    const { push, currentPushFailed } = await deliverPendingEventPushes({
      deviceId: device.deviceId,
      preferredEventId: result.event.id,
    });
    return noStore(
      { ...result, push },
      currentPushFailed ? 503 : result.created ? 201 : 200,
    );
  } catch (error) {
    if (
      error instanceof Error &&
      [
        "MONITORING_DISABLED",
        "STORAGE_NOT_ACTIVE",
        "EVENT_OUTSIDE_RECORDING",
        "IDEMPOTENCY_CONFLICT",
      ].includes(error.message)
    ) {
      return noStore(
        {
          error:
            error.message === "IDEMPOTENCY_CONFLICT"
              ? "같은 idempotency key가 다른 이벤트 내용에 사용되었습니다."
              : error.message === "MONITORING_DISABLED"
              ? "모니터링 모드가 꺼져 있습니다."
              : error.message === "STORAGE_NOT_ACTIVE"
                ? "저장 세션이 활성화되지 않았습니다."
                : "이벤트 시각이 현재 녹화 구간과 일치하지 않습니다.",
        },
        409,
      );
    }
    return noStore({ error: "이벤트를 저장하지 못했습니다." }, 500);
  }
}
