import { upsertHomecamEventClip } from "../../../../../../db/homecam";
import { parseHomecamEventClipInput } from "../../../../../../db/homecam-validation";
import { noStore, unauthorized } from "../../../../../api-response";
import { deliverPendingEventPushes } from "../../../../../device-event-push";
import { getRequestDevice } from "../../../../../device-auth";

export const dynamic = "force-dynamic";

export async function POST(
  request: Request,
  context: { params: Promise<{ phase: string }> },
) {
  const device = await getRequestDevice(request);
  if (!device) return unauthorized("유효한 장치 토큰이 필요합니다.");
  const { phase: rawPhase } = await context.params;
  if (rawPhase !== "started" && rawPhase !== "ended") {
    return noStore({ error: "이벤트 클립 단계를 찾을 수 없습니다." }, 404);
  }
  if (request.headers.get("content-type")?.split(";", 1)[0] !== "application/json") {
    return noStore({ error: "application/json 요청이 필요합니다." }, 415);
  }
  const payload = await request.json().catch(() => null);
  const event = parseHomecamEventClipInput(payload, rawPhase);
  if (!event) return noStore({ error: "이벤트 클립 형식을 확인해 주세요." }, 400);
  if (request.headers.get("idempotency-key") !== event.idempotencyKey) {
    return noStore({ error: "Idempotency-Key 헤더가 본문과 일치해야 합니다." }, 400);
  }

  try {
    const result = await upsertHomecamEventClip(device.deviceId, rawPhase, event);
    const pushResult =
      event.notificationEligible
        ? await deliverPendingEventPushes({
            deviceId: device.deviceId,
            preferredEventId: result.event.id,
          })
        : { push: { dispatched: false, reason: "not_requested" }, currentPushFailed: false };
    return noStore(
      { ...result, push: pushResult.push },
      pushResult.currentPushFailed ? 503 : result.created ? 201 : 200,
    );
  } catch (error) {
    const code = error instanceof Error ? error.message : "";
    if (
      [
        "MONITORING_DISABLED",
        "EVENT_SESSION_INVALID",
        "EVENT_OUTSIDE_RECORDING",
        "IDEMPOTENCY_CONFLICT",
      ].includes(code)
    ) {
      return noStore(
        {
          error:
            code === "MONITORING_DISABLED"
              ? "모니터링 모드가 꺼져 있습니다."
              : code === "EVENT_SESSION_INVALID"
                ? "이 장치의 저장 세션을 확인할 수 없습니다."
                : code === "EVENT_OUTSIDE_RECORDING"
                  ? "이벤트 구간이 저장 세션과 겹치지 않습니다."
                  : "같은 이벤트 클립 키가 다른 내용에 사용되었습니다.",
        },
        409,
      );
    }
    return noStore({ error: "이벤트 클립을 저장하지 못했습니다." }, 500);
  }
}
