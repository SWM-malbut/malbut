import {
  acquireTalkLease,
  releaseTalkLease,
  userCanViewDevice,
} from "../../../../../db/homecam";
import { isValidClientId } from "../../../../../db/homecam-validation";
import { noStore } from "../../../../api-response";
import { getRequestUserEmail } from "../../../../server-auth";

export const dynamic = "force-dynamic";

export async function POST(
  request: Request,
  context: { params: Promise<{ deviceId: string }> },
) {
  const userEmail = await getRequestUserEmail(request);
  if (!userEmail) return noStore({ error: "로그인이 필요합니다." }, 401);
  const { deviceId } = await context.params;
  if (!(await userCanViewDevice(deviceId, userEmail))) {
    return noStore({ error: "이 홈캠에서 말하기를 사용할 권한이 없습니다." }, 403);
  }
  const payload = (await request.json().catch(() => ({}))) as {
    leaseId?: unknown;
    clientId?: unknown;
  };
  if (
    !payload ||
    typeof payload !== "object" ||
    Array.isArray(payload) ||
    Object.keys(payload).some(
      (key) => key !== "leaseId" && key !== "clientId",
    ) ||
    !isValidClientId(payload.clientId) ||
    (payload.leaseId !== undefined &&
      (typeof payload.leaseId !== "string" || !isUuid(payload.leaseId)))
  ) {
    return noStore({ error: "말하기 lease 형식을 확인해 주세요." }, 400);
  }
  const lease = await acquireTalkLease({
    deviceId,
    userEmail,
    clientId: payload.clientId,
    existingLeaseId: payload.leaseId as string | undefined,
  });
  if (!lease) {
    return noStore(
      { error: "다른 가족이 말하기 기능을 사용 중입니다." },
      409,
      { "retry-after": "2" },
    );
  }
  return noStore({ lease }, 200);
}

export async function DELETE(
  request: Request,
  context: { params: Promise<{ deviceId: string }> },
) {
  const userEmail = await getRequestUserEmail(request);
  if (!userEmail) return noStore({ error: "로그인이 필요합니다." }, 401);
  const { deviceId } = await context.params;
  if (!(await userCanViewDevice(deviceId, userEmail))) {
    return noStore({ error: "이 홈캠에서 말하기를 사용할 권한이 없습니다." }, 403);
  }
  const payload = (await request.json().catch(() => null)) as {
    leaseId?: unknown;
    clientId?: unknown;
  } | null;
  if (
    !payload ||
    Object.keys(payload).some(
      (key) => key !== "leaseId" && key !== "clientId",
    ) ||
    typeof payload.leaseId !== "string" ||
    !isUuid(payload.leaseId) ||
    !isValidClientId(payload.clientId)
  ) {
    return noStore({ error: "말하기 lease ID가 필요합니다." }, 400);
  }
  const released = await releaseTalkLease({
    deviceId,
    userEmail,
    leaseId: payload.leaseId,
    clientId: payload.clientId,
  });
  return noStore({ released }, released ? 200 : 404);
}

function isUuid(value: string) {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(
    value,
  );
}
