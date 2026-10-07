import {
  acquireTalkLease,
  readTalkLeaseHolder,
  releaseTalkLease,
  userCanViewDevice,
} from "../../../../../db/homecam";
import { isValidClientId } from "../../../../../db/homecam-validation";
import { noStore } from "../../../../api-response";
import { getRequestUserId } from "../../../../server-auth";

export const dynamic = "force-dynamic";

/** Who else is talking through this 말벗, so the microphone switch can wait. */
export async function GET(
  request: Request,
  context: { params: Promise<{ deviceId: string }> },
) {
  const userId = await getRequestUserId(request);
  if (!userId) return noStore({ error: "로그인이 필요합니다." }, 401);
  const { deviceId } = await context.params;
  if (!(await userCanViewDevice(deviceId, userId))) {
    return noStore({ error: "이 홈캠에서 말하기를 사용할 권한이 없습니다." }, 403);
  }
  const clientId = new URL(request.url).searchParams.get("clientId") ?? undefined;
  if (clientId !== undefined && !isValidClientId(clientId)) {
    return noStore({ error: "말하기 lease 형식을 확인해 주세요." }, 400);
  }
  const lease = await readTalkLeaseHolder({ deviceId, userId, clientId });
  return noStore({ holder: lease && !lease.mine ? lease.holder : null }, 200);
}

export async function POST(
  request: Request,
  context: { params: Promise<{ deviceId: string }> },
) {
  const userId = await getRequestUserId(request);
  if (!userId) return noStore({ error: "로그인이 필요합니다." }, 401);
  const { deviceId } = await context.params;
  if (!(await userCanViewDevice(deviceId, userId))) {
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
    userId,
    clientId: payload.clientId,
    existingLeaseId: payload.leaseId as string | undefined,
  });
  if (!lease) {
    const current = await readTalkLeaseHolder({ deviceId, userId, clientId: payload.clientId });
    if (current?.mine && current.leaseId === payload.leaseId) {
      return noStore(
        { error: "3분이 지나 마이크를 껐어요.", code: "time_limit" },
        409,
      );
    }
    return noStore(
      {
        error: "다른 보호자가 말하는 중이에요.",
        code: "busy",
        holder: current && !current.mine ? current.holder : null,
      },
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
  const userId = await getRequestUserId(request);
  if (!userId) return noStore({ error: "로그인이 필요합니다." }, 401);
  const { deviceId } = await context.params;
  if (!(await userCanViewDevice(deviceId, userId))) {
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
    userId,
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
