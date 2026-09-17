import {
  claimPendingHomecamPushes,
  finishHomecamPushAttempt,
} from "../db/homecam";
import { dispatchHomecamEventPush } from "./push-broker";

export async function deliverPendingEventPushes(input: {
  deviceId: string;
  preferredEventId: string;
}) {
  const claimed = await claimPendingHomecamPushes({
    deviceId: input.deviceId,
    preferredEventId: input.preferredEventId,
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
        deviceId: input.deviceId,
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
      if (pendingEvent.id === input.preferredEventId) {
        push = delivered
          ? outcome
          : { ...outcome, reason: reason ?? "delivery_failed" };
        currentPushFailed = !delivered && reason !== "not_configured";
      }
    } catch {
      await finishHomecamPushAttempt({
        eventId: pendingEvent.id,
        delivered: false,
        error: "broker_error",
      });
      if (pendingEvent.id === input.preferredEventId) {
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
  return { push, currentPushFailed };
}
