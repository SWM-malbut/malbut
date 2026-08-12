import {
  createDeviceCredential,
  listDeviceCredentials,
  userCanManageDevice,
} from "../../../../../db/homecam";
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
  if (!(await userCanManageDevice(deviceId, userEmail))) {
    return noStore({ error: "소유자만 장치 토큰을 관리할 수 있습니다." }, 403);
  }
  return noStore({ credentials: await listDeviceCredentials(deviceId) }, 200);
}

export async function POST(
  request: Request,
  context: { params: Promise<{ deviceId: string }> },
) {
  const userEmail = await getRequestUserEmail(request);
  if (!userEmail) return noStore({ error: "로그인이 필요합니다." }, 401);
  const { deviceId } = await context.params;
  if (!(await userCanManageDevice(deviceId, userEmail))) {
    return noStore({ error: "소유자만 장치 토큰을 만들 수 있습니다." }, 403);
  }
  const payload = (await request.json().catch(() => null)) as {
    label?: unknown;
    expiresAt?: unknown;
  } | null;
  if (
    !payload ||
    Object.keys(payload).some((key) => !["label", "expiresAt"].includes(key)) ||
    typeof payload.label !== "string" ||
    payload.label.trim().length < 1 ||
    payload.label.trim().length > 80
  ) {
    return noStore({ error: "1~80자의 장치 토큰 이름이 필요합니다." }, 400);
  }
  let expiresAt: string | null = null;
  if (payload.expiresAt !== undefined && payload.expiresAt !== null) {
    if (
      typeof payload.expiresAt !== "string" ||
      !Number.isFinite(Date.parse(payload.expiresAt)) ||
      new Date(Date.parse(payload.expiresAt)).toISOString() !== payload.expiresAt ||
      Date.parse(payload.expiresAt) <= Date.now()
    ) {
      return noStore({ error: "만료 시각은 미래의 ISO UTC 시각이어야 합니다." }, 400);
    }
    expiresAt = payload.expiresAt;
  }
  const credential = await createDeviceCredential({
    deviceId,
    userEmail,
    label: payload.label.trim(),
    expiresAt,
  });
  return noStore(
    {
      credential,
      warning: "token은 다시 표시되지 않습니다. 장치의 보호된 저장소에 보관하세요.",
    },
    201,
  );
}
