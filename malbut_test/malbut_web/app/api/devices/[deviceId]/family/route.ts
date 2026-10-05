import {
  listFamilyMembers,
  revokeFamilyMember,
  userCanManageDevice,
  userCanViewDevice,
} from "../../../../../db/homecam";
import { noStore } from "../../../../api-response";
import { getRequestUserId } from "../../../../server-auth";

export const dynamic = "force-dynamic";

export async function GET(
  request: Request,
  context: { params: Promise<{ deviceId: string }> },
) {
  const userId = await getRequestUserId(request);
  if (!userId) return noStore({ error: "로그인이 필요합니다." }, 401);
  const { deviceId } = await context.params;
  if (!(await userCanViewDevice(deviceId, userId))) {
    return noStore({ error: "보호자 목록을 볼 권한이 없습니다." }, 403);
  }
  return noStore({ members: await listFamilyMembers(deviceId) }, 200);
}

export async function DELETE(
  request: Request,
  context: { params: Promise<{ deviceId: string }> },
) {
  const ownerUserId = await getRequestUserId(request);
  if (!ownerUserId) return noStore({ error: "로그인이 필요합니다." }, 401);
  const { deviceId } = await context.params;
  if (!(await userCanManageDevice(deviceId, ownerUserId))) {
    return noStore({ error: "소유자만 보호자 권한을 해제할 수 있습니다." }, 403);
  }
  const payload = (await request.json().catch(() => null)) as {
    userId?: unknown;
  } | null;
  const familyUserId =
    payload && typeof payload.userId === "string" && payload.userId.trim()
      ? payload.userId.trim()
      : null;
  if (!familyUserId || Object.keys(payload ?? {}).some((key) => key !== "userId")) {
    return noStore({ error: "내보낼 보호자를 확인해 주세요." }, 400);
  }
  const revoked = await revokeFamilyMember({
    deviceId,
    ownerUserId,
    familyUserId,
  });
  return noStore({ revoked }, revoked ? 200 : 404);
}
