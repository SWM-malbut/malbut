import {
  listPushSubscriptions,
  revokePushSubscription,
  upsertPushSubscription,
  userCanViewDevice,
} from "../../../db/homecam";
import { noStore } from "../../api-response";
import { getRequestUserEmail } from "../../server-auth";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const userEmail = await getRequestUserEmail(request);
  if (!userEmail) return noStore({ error: "로그인이 필요합니다." }, 401);
  return noStore(
    { subscriptions: await listPushSubscriptions(userEmail) },
    200,
  );
}

export async function POST(request: Request) {
  const userEmail = await getRequestUserEmail(request);
  if (!userEmail) return noStore({ error: "로그인이 필요합니다." }, 401);
  const payload = (await request.json().catch(() => null)) as {
    deviceId?: unknown;
    endpoint?: unknown;
    keys?: { p256dh?: unknown; auth?: unknown };
  } | null;
  if (
    !payload ||
    Object.keys(payload).some(
      (key) => !["deviceId", "endpoint", "keys"].includes(key),
    ) ||
    typeof payload.deviceId !== "string" ||
    typeof payload.endpoint !== "string" ||
    !isPushEndpoint(payload.endpoint) ||
    !payload.keys ||
    Object.keys(payload.keys).some((key) => !["p256dh", "auth"].includes(key)) ||
    !isPushKey(payload.keys.p256dh, 40, 200) ||
    !isPushKey(payload.keys.auth, 8, 100)
  ) {
    return noStore({ error: "Web Push 구독 형식을 확인해 주세요." }, 400);
  }
  if (!(await userCanViewDevice(payload.deviceId, userEmail))) {
    return noStore({ error: "이 홈캠의 알림을 구독할 권한이 없습니다." }, 403);
  }
  const subscription = await upsertPushSubscription({
    deviceId: payload.deviceId,
    userEmail,
    endpoint: payload.endpoint,
    p256dh: payload.keys.p256dh,
    auth: payload.keys.auth,
  });
  return noStore({ subscription }, 201);
}

export async function DELETE(request: Request) {
  const userEmail = await getRequestUserEmail(request);
  if (!userEmail) return noStore({ error: "로그인이 필요합니다." }, 401);
  const payload = (await request.json().catch(() => null)) as {
    id?: unknown;
  } | null;
  if (
    !payload ||
    Object.keys(payload).some((key) => key !== "id") ||
    typeof payload.id !== "string" ||
    !isUuid(payload.id)
  ) {
    return noStore({ error: "삭제할 Web Push 구독 ID가 필요합니다." }, 400);
  }
  const revoked = await revokePushSubscription(userEmail, payload.id);
  return noStore({ revoked }, revoked ? 200 : 404);
}

function isPushEndpoint(value: string) {
  if (value.length > 2_048) return false;
  try {
    return new URL(value).protocol === "https:";
  } catch {
    return false;
  }
}

function isPushKey(value: unknown, min: number, max: number): value is string {
  return (
    typeof value === "string" &&
    value.length >= min &&
    value.length <= max &&
    /^[A-Za-z0-9_-]+$/.test(value)
  );
}

function isUuid(value: string) {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(
    value,
  );
}
