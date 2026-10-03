import { getRuntimeEnvironment } from "../../../runtime-env";
import {
  claimPendingHomecamPushes,
  finishHomecamPushAttempt,
  listDevicesWithPendingHomecamPushes,
  runHomecamRetentionCleanup,
} from "../../../../db/homecam";
import { noStore } from "../../../api-response";
import { dispatchHomecamEventPush } from "../../../push-broker";
import { deliverPendingFallNotice, deliverPendingFallPush } from "../../../fall-event-push";
import { scheduleFallReminders } from "../../../../db/fall-review";
import { recoverFallAiJobs } from "../../../../db/fall-ai-review";
import { startFallAiJob } from "../../../fall-ai-review-worker";

export const dynamic = "force-dynamic";

type MaintenanceEnv = {
  MAINTENANCE_SECRET?: string;
};

export async function POST(request: Request) {
  const secret = (getRuntimeEnvironment() as MaintenanceEnv).MAINTENANCE_SECRET;
  if (!secret || !(await authorized(request, secret))) {
    return noStore({ error: "유효한 maintenance 인증이 필요합니다." }, 401);
  }
  const payload = await request.json().catch(() => null);
  if (
    !payload ||
    typeof payload !== "object" ||
    Array.isArray(payload) ||
    Object.keys(payload).length > 0
  ) {
    return noStore({ error: "maintenance 요청 본문은 비어 있어야 합니다." }, 400);
  }

  await runHomecamRetentionCleanup();
  const deviceIds = await listDevicesWithPendingHomecamPushes(5);
  const pendingEvents: Array<{
    deviceId: string;
    event: Awaited<ReturnType<typeof claimPendingHomecamPushes>>[number];
  }> = [];
  for (const deviceId of deviceIds) {
    const events = await claimPendingHomecamPushes({ deviceId, limit: 2 });
    pendingEvents.push(...events.map((event) => ({ deviceId, event })));
  }
  const outcomes = await Promise.all(
    pendingEvents.map(async ({ deviceId, event }) => {
      try {
        const outcome = await dispatchHomecamEventPush({ deviceId, event });
        const reason = "reason" in outcome ? outcome.reason : undefined;
        const failed =
          "failed" in outcome && typeof outcome.failed === "number"
            ? outcome.failed
            : 0;
        const complete =
          reason === "no_subscribers" ||
          (outcome.dispatched === true && failed === 0);
        await finishHomecamPushAttempt({
          eventId: event.id,
          delivered: complete,
          error: complete ? undefined : reason ?? "delivery_failed",
        });
        return complete;
      } catch {
        await finishHomecamPushAttempt({
          eventId: event.id,
          delivered: false,
          error: "broker_error",
        });
        return false;
      }
    }),
  );
  const delivered = outcomes.filter(Boolean).length;
  const pending = outcomes.length - delivered;
  const fallPushes = [];
  for (let index = 0; index < 2; index += 1) {
    try {
      const result = await deliverPendingFallPush();
      fallPushes.push(result);
      if (!result.processed) break;
    } catch {
      fallPushes.push({ processed: false, accepted: false, reason: "fall_worker_unavailable" });
      break;
    }
  }
  // [재발신]: create due reminders, then deliver them (and any reopen notice).
  let fallReminders: unknown = { created: 0 };
  try { fallReminders = await scheduleFallReminders(); }
  catch { fallReminders = { created: 0, reason: "fall_reminders_unavailable" }; }
  const fallNotices = [];
  for (let index = 0; index < 5; index += 1) {
    try {
      const result = await deliverPendingFallNotice();
      fallNotices.push(result);
      if (!result.processed) break;
    } catch {
      fallNotices.push({ processed: false, accepted: false, reason: "fall_notice_worker_unavailable" });
      break;
    }
  }
  // User-requested AI reviews take up to ~30 s each, longer than this
  // request may run: start one in the background and return.
  let fallAiJobs: unknown;
  try {
    await recoverFallAiJobs();
    fallAiJobs = { started: startFallAiJob() };
  } catch {
    fallAiJobs = { started: false, reason: "fall_ai_worker_unavailable" };
  }
  return noStore(
    {
      retentionCleanup: true,
      processedDevices: deviceIds.length,
      delivered,
      pending,
      fallPushes,
      fallReminders,
      fallNotices,
      fallAiJobs,
    },
    200,
  );
}

async function authorized(request: Request, expected: string) {
  const header = request.headers.get("authorization");
  if (!header?.startsWith("Bearer ")) return false;
  const received = header.slice("Bearer ".length);
  if (!received || received.length > 512) return false;
  const [left, right] = await Promise.all([
    sha256(received),
    sha256(expected),
  ]);
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left[index] ^ right[index];
  }
  return difference === 0;
}

async function sha256(value: string) {
  return new Uint8Array(
    await crypto.subtle.digest(
      "SHA-256",
      new TextEncoder().encode(value),
    ),
  );
}
