const record = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" ? value as Record<string, unknown> : {};
const text = (...values: unknown[]) =>
  values.find((value): value is string => typeof value === "string" && value.length > 0);

function decodeBase64Url(value: string) {
  const padding = "=".repeat((4 - (value.length % 4)) % 4);
  const base64 = (value + padding).replace(/-/g, "+").replace(/_/g, "/");
  const decoded = window.atob(base64);
  return Uint8Array.from(decoded, (character) => character.charCodeAt(0));
}

export function fallPushSupported() {
  return typeof window !== "undefined" && "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
}

/**
 * "낙상 알림" on this phone for one 말벗: asks permission, subscribes the browser (or reuses
 * its subscription) and saves it. Settings and the invite page share this.
 */
export async function subscribeFallPush(deviceId: string) {
  if (!fallPushSupported()) throw new Error("이 브라우저는 Web Push를 지원하지 않습니다.");
  const registration = await navigator.serviceWorker.ready;
  const current = await registration.pushManager.getSubscription();
  const permission = await Notification.requestPermission();
  if (permission !== "granted") throw new Error("알림 권한이 허용되지 않았습니다.");
  let keyResponse = await fetch("/api/push-subscriptions/vapid-public-key", { cache: "no-store" });
  if (keyResponse.status === 404) {
    keyResponse = await fetch("/api/push/vapid-public-key", { cache: "no-store" });
  }
  const keyPayload = record(await keyResponse.json().catch(() => ({})));
  const publicKey = text(keyPayload.publicKey, keyPayload.vapidPublicKey);
  if (!keyResponse.ok || !publicKey) {
    throw new Error(text(keyPayload.error) ?? "푸시 공개 키를 불러오지 못했습니다.");
  }
  const subscription =
    current ??
    await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: decodeBase64Url(publicKey),
    });
  const serialized = subscription.toJSON();
  const response = await fetch("/api/push-subscriptions", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ deviceId, endpoint: serialized.endpoint, keys: serialized.keys }),
  });
  const payload = record(await response.json().catch(() => ({})));
  if (!response.ok) {
    if (!current) await subscription.unsubscribe().catch(() => undefined);
    throw new Error(text(payload.error) ?? "푸시 구독을 저장하지 못했습니다.");
  }
  return { subscriptionId: text(record(payload.subscription).id) ?? "" };
}
