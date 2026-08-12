import {
  claimPendingHomecamPushes,
  finishHomecamPushAttempt,
  insertHomecamEvent,
} from "../../../../../db/homecam";
import { parseHomecamEventInput } from "../../../../../db/homecam-validation";
import { noStore, unauthorized } from "../../../../api-response";
import { getRequestDevice } from "../../../../device-auth";
import { dispatchHomecamEventPush } from "../../../../push-broker";

export const dynamic = "force-dynamic";

export async function POST(request: Request) {
  const device = await getRequestDevice(request);
  if (!device) return unauthorized("유효한 장치 토큰이 필요합니다.");
  const event = parseHomecamEventInput(await request.json().catch(() => null));
  if (!event) return noStore({ error: "이벤트 형식을 확인해 주세요." }, 400);

  try {
    const result = await insertHomecamEvent(device.deviceId, event);
    const claimed = await claimPendingHomecamPushes({
      deviceId: device.deviceId,
      preferredEventId: result.event.id,
      limit: 2,
    });
    let push: Record<string, unknown> = {
      dispatched: false,
      delivered: 0,
      pruned: 0,
      reason: claimed.length > 0 ? "queued" : "duplicate_or_in_progress",
    };
    let currentPushFailed = false;
    for (const pendingEvent of claimed) {
      try {
        const outcome = await dispatchHomecamEventPush({
          deviceId: device.deviceId,
          event: pendingEvent,
        });
        const reason = "reason" in outcome ? outcome.reason : undefined;
        const failed =
          "failed" in outcome && typeof outcome.failed === "number"
            ? outcome.failed
            : 0;
        const delivered =
          reason === "no_subscribers" ||
          (outcome.dispatched === true && failed === 0);
        await finishHomecamPushAttempt({
          eventId: pendingEvent.id,
          delivered,
          error: delivered ? undefined : reason ?? "delivery_failed",
        });
        if (pendingEvent.id === result.event.id) {
          push = delivered
            ? outcome
            : { ...outcome, reason: reason ?? "delivery_failed" };
          currentPushFailed =
            !delivered && reason !== "not_configured";
        }
      } catch {
        await finishHomecamPushAttempt({
          eventId: pendingEvent.id,
          delivered: false,
          error: "broker_error",
        });
        if (pendingEvent.id === result.event.id) {
          currentPushFailed = true;
          push = {
            dispatched: false,
            delivered: 0,
            pruned: 0,
            reason: "broker_error",
          };
        }
      }
    }
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
