import { createHmac, timingSafeEqual } from "node:crypto";
import webpush from "web-push";

const sharedSecret = process.env.BROKER_SHARED_SECRET;
const vapidSubject = process.env.PUSH_VAPID_SUBJECT;
const vapidPublicKey = process.env.PUSH_VAPID_PUBLIC_KEY;
const vapidPrivateKey = process.env.PUSH_VAPID_PRIVATE_KEY;
const eventTypes = new Set(["motion", "person", "dog", "cat"]);
const uuidPattern =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const base64UrlPattern = /^[A-Za-z0-9_-]+$/;

export async function handler(event) {
  if (event.requestContext?.http?.method !== "POST") {
    return response(405, { error: "Method not allowed" });
  }
  if (
    !sharedSecret ||
    !vapidSubject ||
    !vapidPublicKey ||
    !vapidPrivateKey
  ) {
    return response(503, { error: "Push broker is not configured" });
  }
  const rawBody = event.isBase64Encoded
    ? Buffer.from(event.body ?? "", "base64").toString("utf8")
    : event.body ?? "";
  if (!verifyRequest(event.headers ?? {}, rawBody, sharedSecret)) {
    return response(401, { error: "Unauthorized" });
  }

  let payload;
  try {
    payload = JSON.parse(rawBody);
  } catch {
    return response(400, { error: "Invalid JSON" });
  }
  const input = validatePushRequest(payload);
  if (!input) return response(400, { error: "Invalid push request" });

  try {
    webpush.setVapidDetails(vapidSubject, vapidPublicKey, vapidPrivateKey);
  } catch {
    return response(503, { error: "VAPID configuration is invalid" });
  }
  const notification = JSON.stringify(input.notification);
  const results = await Promise.all(
    input.subscriptions.map(async (subscription) => {
      try {
        const sent = await webpush.sendNotification(
          {
            endpoint: subscription.endpoint,
            keys: subscription.keys,
          },
          notification,
          { TTL: 60, urgency: "high" },
        );
        return {
          subscriptionId: subscription.subscriptionId,
          status: sent.statusCode,
        };
      } catch (error) {
        const status =
          error &&
          typeof error === "object" &&
          "statusCode" in error &&
          Number.isInteger(error.statusCode)
            ? error.statusCode
            : 503;
        return { subscriptionId: subscription.subscriptionId, status };
      }
    }),
  );
  return response(200, { results });
}

function validatePushRequest(value) {
  if (
    !isRecord(value) ||
    !hasOnlyKeys(value, ["notification", "subscriptions"]) ||
    !isRecord(value.notification) ||
    !hasOnlyKeys(value.notification, ["title", "body", "data"]) ||
    typeof value.notification.title !== "string" ||
    value.notification.title.length < 1 ||
    value.notification.title.length > 80 ||
    typeof value.notification.body !== "string" ||
    value.notification.body.length < 1 ||
    value.notification.body.length > 160 ||
    !isRecord(value.notification.data) ||
    !hasOnlyKeys(value.notification.data, [
      "deviceId",
      "eventId",
      "eventType",
      "occurredAt",
      "url",
    ]) ||
    typeof value.notification.data.deviceId !== "string" ||
    !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(
      value.notification.data.deviceId,
    ) ||
    typeof value.notification.data.eventId !== "string" ||
    !uuidPattern.test(value.notification.data.eventId) ||
    !eventTypes.has(value.notification.data.eventType) ||
    !isCanonicalTimestamp(value.notification.data.occurredAt) ||
    typeof value.notification.data.url !== "string" ||
    !/^\/\?view=events&device=[^&]{1,384}&event=[^&]{1,128}$/.test(
      value.notification.data.url,
    ) ||
    !Array.isArray(value.subscriptions) ||
    value.subscriptions.length < 1 ||
    value.subscriptions.length > 100
  ) {
    return null;
  }
  const subscriptions = value.subscriptions.map(validateSubscription);
  if (subscriptions.some((subscription) => !subscription)) return null;
  const ids = new Set(subscriptions.map((subscription) => subscription.subscriptionId));
  if (ids.size !== subscriptions.length) return null;
  return {
    notification: value.notification,
    subscriptions,
  };
}

function validateSubscription(value) {
  if (
    !isRecord(value) ||
    !hasOnlyKeys(value, ["subscriptionId", "endpoint", "keys"]) ||
    typeof value.subscriptionId !== "string" ||
    !uuidPattern.test(value.subscriptionId) ||
    typeof value.endpoint !== "string" ||
    value.endpoint.length > 2_048 ||
    !isHttpsUrl(value.endpoint) ||
    !isRecord(value.keys) ||
    !hasOnlyKeys(value.keys, ["p256dh", "auth"]) ||
    typeof value.keys.p256dh !== "string" ||
    value.keys.p256dh.length < 40 ||
    value.keys.p256dh.length > 200 ||
    !base64UrlPattern.test(value.keys.p256dh) ||
    typeof value.keys.auth !== "string" ||
    value.keys.auth.length < 8 ||
    value.keys.auth.length > 100 ||
    !base64UrlPattern.test(value.keys.auth)
  ) {
    return null;
  }
  return value;
}

function verifyRequest(headers, rawBody, secret) {
  const normalizedHeaders = Object.fromEntries(
    Object.entries(headers).map(([key, value]) => [key.toLowerCase(), value]),
  );
  const timestamp = normalizedHeaders["x-homecam-timestamp"];
  const signature = normalizedHeaders["x-homecam-signature"];
  if (
    typeof timestamp !== "string" ||
    typeof signature !== "string" ||
    !/^\d{10}$/.test(timestamp) ||
    !/^[a-f0-9]{64}$/.test(signature) ||
    Math.abs(Date.now() / 1000 - Number(timestamp)) > 30
  ) {
    return false;
  }
  const expected = createHmac("sha256", secret)
    .update(`${timestamp}.${rawBody}`)
    .digest();
  const received = Buffer.from(signature, "hex");
  return (
    received.length === expected.length && timingSafeEqual(received, expected)
  );
}

function hasOnlyKeys(value, allowed) {
  return Object.keys(value).every((key) => allowed.includes(key));
}

function isRecord(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function isHttpsUrl(value) {
  try {
    return new URL(value).protocol === "https:";
  } catch {
    return false;
  }
}

function isCanonicalTimestamp(value) {
  if (typeof value !== "string") return false;
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) && new Date(timestamp).toISOString() === value;
}

function response(statusCode, body) {
  return {
    statusCode,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
    },
    body: JSON.stringify(body),
  };
}
