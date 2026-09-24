import { getRuntimeEnvironment } from "./runtime-env";
import {
  listActivePushTargets,
  revokePushSubscriptionsById,
} from "../db/homecam";
import { shouldPrunePushSubscription } from "../db/homecam-validation";
import { buildFallNotification } from "../infra/aws/push-broker/fall-notification.mjs";

type PushBrokerEnv = {
  PUSH_BROKER_URL?: string;
  PUSH_BROKER_SECRET?: string;
};

type PushTarget = {
  id: string;
  endpoint: string;
  keys: { p256dh: string; auth: string };
  displayName: string;
};

const PUSH_BROKER_BATCH_SIZE = 100;

type PushNotification = {
  title: string;
  body: string;
  data: Record<string, string>;
};

type PushDispatchHooks = {
  excludeSubscriptionIds?: readonly string[];
  beforeBatch?: () => Promise<void>;
  onResults?: (results: Array<{ subscriptionId: string; status: number }>) => Promise<void>;
};

/** Trusted server boundary only. The caller must persist/deduplicate the intent
 * and authenticate the device before dispatch. No public route is exposed. */
export async function dispatchFallPush(input: {
  deviceId: string;
  notificationId: string;
  incidentId: string;
  level: "info" | "check" | "urgent";
  reason: "fall_observed_person_okay" | "person_no_response" |
    "check_required_not_confirmed_fall" | "help_requested" | "confirmation_help_required";
  occurredAt: string;
}, hooks: PushDispatchHooks = {}) {
  const notification = buildFallNotification(input);
  if (!notification) throw new Error("FALL_NOTIFICATION_INVALID");
  return dispatchDevicePush(input.deviceId, (title) => ({ title, ...notification }), hooks);
}

export async function dispatchHomecamEventPush(input: {
  deviceId: string;
  event: {
    id: string;
    eventType: string;
    occurredAt: string;
  };
}) {
  return dispatchDevicePush(input.deviceId, (title) => ({
    title,
    body: eventMessage(input.event.eventType, input.event.occurredAt),
    data: {
      deviceId: input.deviceId,
      eventId: input.event.id,
      eventType: input.event.eventType,
      occurredAt: input.event.occurredAt,
      url: `/?view=events&device=${encodeURIComponent(input.deviceId)}&event=${encodeURIComponent(input.event.id)}`,
    },
  }));
}

async function dispatchDevicePush(
  deviceId: string,
  buildNotification: (title: string) => PushNotification,
  hooks: PushDispatchHooks = {},
) {
  const activeTargets = (await listActivePushTargets(deviceId)) as PushTarget[];
  if (activeTargets.length === 0) {
    return { dispatched: false, delivered: 0, pruned: 0, reason: "no_subscribers" };
  }
  const excluded = new Set(hooks.excludeSubscriptionIds ?? []);
  const targets = activeTargets.filter((target) => !excluded.has(target.id));
  if (targets.length === 0) {
    return { dispatched: false, delivered: 0, pruned: 0, reason: "already_processed" };
  }
  const runtime = getRuntimeEnvironment() as PushBrokerEnv;
  if (!runtime.PUSH_BROKER_URL || !runtime.PUSH_BROKER_SECRET) {
    return { dispatched: false, delivered: 0, pruned: 0, reason: "not_configured" };
  }
  const brokerUrl = new URL(runtime.PUSH_BROKER_URL);
  if (brokerUrl.protocol !== "https:") throw new Error("PUSH_BROKER_URL_INVALID");

  const notification = buildNotification(targets[0].displayName);
  const results: Array<{ subscriptionId: string; status: number }> = [];
  for (let offset = 0; offset < targets.length; offset += PUSH_BROKER_BATCH_SIZE) {
    const batch = targets.slice(offset, offset + PUSH_BROKER_BATCH_SIZE);
    await hooks.beforeBatch?.();
    const batchResults = await sendPushBatch({
        brokerUrl,
        secret: runtime.PUSH_BROKER_SECRET,
        notification,
        targets: batch,
      });
    await hooks.onResults?.(batchResults);
    results.push(...batchResults);
  }
  if (results.length !== targets.length) {
    throw new Error("PUSH_BROKER_RESPONSE_INVALID");
  }
  const expiredIds = results
    .filter((result) => shouldPrunePushSubscription(result.status))
    .map((result) => result.subscriptionId);
  const pruned = await revokePushSubscriptionsById(expiredIds);
  return {
    dispatched: true,
    delivered: results.filter(
      (result) => result.status >= 200 && result.status < 300,
    ).length,
    pruned,
    failed: results.filter(
      (result) =>
        (result.status < 200 || result.status >= 300) &&
        !shouldPrunePushSubscription(result.status),
    ).length,
  };
}

async function sendPushBatch(input: {
  brokerUrl: URL;
  secret: string;
  notification: PushNotification;
  targets: PushTarget[];
}) {
  const body = JSON.stringify({
    notification: input.notification,
    subscriptions: input.targets.map(({ id, endpoint, keys }) => ({
      subscriptionId: id,
      endpoint,
      keys,
    })),
  });
  const timestamp = Math.floor(Date.now() / 1000).toString();
  const signature = await sign(`${timestamp}.${body}`, input.secret);
  const response = await fetch(input.brokerUrl, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-homecam-timestamp": timestamp,
      "x-homecam-signature": signature,
    },
    body,
    signal: AbortSignal.timeout(10_000),
  });
  if (!response.ok) throw new Error(`PUSH_BROKER_${response.status}`);
  const payload = (await response.json()) as {
    results?: Array<{ subscriptionId?: unknown; status?: unknown }>;
  };
  if (!Array.isArray(payload.results)) {
    throw new Error("PUSH_BROKER_RESPONSE_INVALID");
  }
  const allowedIds = new Set(input.targets.map((target) => target.id));
  const results = payload.results.filter(
    (result): result is { subscriptionId: string; status: number } =>
      typeof result.subscriptionId === "string" &&
      allowedIds.has(result.subscriptionId) &&
      typeof result.status === "number" &&
      Number.isInteger(result.status),
  );
  if (
    results.length !== input.targets.length ||
    new Set(results.map((result) => result.subscriptionId)).size !==
      input.targets.length
  ) {
    throw new Error("PUSH_BROKER_RESPONSE_INVALID");
  }
  return results;
}

function eventMessage(eventType: string, occurredAt: string) {
  const occurredTime = new Intl.DateTimeFormat("ko-KR", {
    timeZone: "Asia/Seoul",
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(occurredAt));
  let description: string;
  switch (eventType) {
    case "person":
      description = "사람이 감지되었습니다.";
      break;
    case "dog":
      description = "강아지가 감지되었습니다.";
      break;
    case "cat":
      description = "고양이가 감지되었습니다.";
      break;
    default:
      description = "움직임이 감지되었습니다.";
  }
  return `${occurredTime} · ${description}`;
}

async function sign(message: string, secret: string) {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const bytes = new Uint8Array(
    await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(message)),
  );
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}
