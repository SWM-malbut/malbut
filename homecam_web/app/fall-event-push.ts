import { claimFallPush, finishFallPush, recordFallPushResults } from "../db/fall-incidents";
import { dispatchFallPush } from "./push-broker";

// The same worker is called immediately after durable ingestion and by the
// authenticated maintenance scheduler. Provider acceptance is not a read receipt.
export async function deliverPendingFallPush(input: { deviceId?: string; notificationId?: string } = {}) {
  const claim = await claimFallPush(input.deviceId, input.notificationId);
  if (!claim) return { processed: false, accepted: false, reason: "not_due_or_in_progress" };
  const prior = Object.values(claim.subscriptionResults);
  const previouslyAccepted = prior.some((status) => status >= 200 && status < 300);
  const excluded = Object.entries(claim.subscriptionResults)
    .filter(([, status]) => (status >= 200 && status < 300) || status === 404 || status === 410)
    .map(([id]) => id);
  try {
    const outcome = await dispatchFallPush(claim, {
      excludeSubscriptionIds: excluded,
      beforeBatch: () => recordFallPushResults(claim, []),
      onResults: (results) => recordFallPushResults(claim, results),
    });
    const reason = "reason" in outcome ? outcome.reason : null;
    const failed = "failed" in outcome ? outcome.failed : 0;
    const accepted = (previouslyAccepted || outcome.delivered > 0) && failed === 0 &&
      reason !== "no_subscribers" && reason !== "not_configured";
    const saved = await finishFallPush(claim, accepted,
      accepted ? null : reason ?? (failed ? "push_failed" : "no_accepted_subscription"));
    return { processed: true, accepted: accepted && saved,
      reason: saved ? accepted ? "push_service_accepted" : reason ?? "push_pending" : "lease_lost" };
  } catch {
    await finishFallPush(claim, false, "push_dispatch_failed");
    return { processed: true, accepted: false, reason: "push_dispatch_failed" };
  }
}
