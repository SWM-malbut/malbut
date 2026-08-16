import {
  inviteFamilyMember,
  listFamilyMembers,
  revokeFamilyMember,
  userCanManageDevice,
  userCanViewDevice,
} from "../../../../../db/homecam";
import { normalizeEmail } from "../../../../../db/homecam-validation";
import { noStore } from "../../../../api-response";
import { getRequestUserEmail } from "../../../../server-auth";

export const dynamic = "force-dynamic";

export async function GET(
  request: Request,
  context: { params: Promise<{ deviceId: string }> },
) {
  const userEmail = await getRequestUserEmail(request);
  if (!userEmail) return noStore({ error: "로그인이 필요합니다." }, 401);
  const { deviceId } = await context.params;
  if (!(await userCanViewDevice(deviceId, userEmail))) {
    return noStore({ error: "가족 목록을 볼 권한이 없습니다." }, 403);
  }
  return noStore({ members: await listFamilyMembers(deviceId) }, 200);
}

export async function POST(
  request: Request,
  context: { params: Promise<{ deviceId: string }> },
) {
  const ownerEmail = await getRequestUserEmail(request);
  if (!ownerEmail) return noStore({ error: "로그인이 필요합니다." }, 401);
  const { deviceId } = await context.params;
  if (!(await userCanManageDevice(deviceId, ownerEmail))) {
    return noStore({ error: "소유자만 가족을 초대할 수 있습니다." }, 403);
  }
  const payload = (await request.json().catch(() => null)) as {
    email?: unknown;
  } | null;
  const familyEmail =
    payload && typeof payload.email === "string"
      ? normalizeEmail(payload.email)
      : null;
  if (!familyEmail || Object.keys(payload ?? {}).some((key) => key !== "email")) {
    return noStore({ error: "올바른 가족 이메일이 필요합니다." }, 400);
  }
  if (familyEmail === ownerEmail) {
    return noStore({ error: "소유자 자신은 가족으로 초대할 수 없습니다." }, 409);
  }
  try {
    const member = await inviteFamilyMember({
      deviceId,
      ownerEmail,
      familyEmail,
    });
    return noStore({ member }, 201);
  } catch (error) {
    if (error instanceof Error && error.message === "MEMBER_IS_OWNER") {
      return noStore({ error: "해당 사용자는 이미 소유자입니다." }, 409);
    }
    return noStore({ error: "가족을 초대하지 못했습니다." }, 500);
  }
}

export async function DELETE(
  request: Request,
  context: { params: Promise<{ deviceId: string }> },
) {
  const ownerEmail = await getRequestUserEmail(request);
  if (!ownerEmail) return noStore({ error: "로그인이 필요합니다." }, 401);
  const { deviceId } = await context.params;
  if (!(await userCanManageDevice(deviceId, ownerEmail))) {
    return noStore({ error: "소유자만 가족 권한을 해제할 수 있습니다." }, 403);
  }
  const payload = (await request.json().catch(() => null)) as {
    email?: unknown;
  } | null;
  const familyEmail =
    payload && typeof payload.email === "string"
      ? normalizeEmail(payload.email)
      : null;
  if (!familyEmail || Object.keys(payload ?? {}).some((key) => key !== "email")) {
    return noStore({ error: "올바른 가족 이메일이 필요합니다." }, 400);
  }
  const revoked = await revokeFamilyMember({
    deviceId,
    ownerEmail,
    familyEmail,
  });
  return noStore({ revoked }, revoked ? 200 : 404);
}
