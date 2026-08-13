import {
  revokeDeviceCredential,
  userCanManageDevice,
} from "../../../../../../db/homecam";
import { noStore } from "../../../../../api-response";
import { getRequestUserEmail } from "../../../../../server-auth";

export const dynamic = "force-dynamic";

export async function DELETE(
  request: Request,
  context: { params: Promise<{ deviceId: string; credentialId: string }> },
) {
  const userEmail = await getRequestUserEmail(request);
  if (!userEmail) return noStore({ error: "로그인이 필요합니다." }, 401);
  const { deviceId, credentialId } = await context.params;
  if (!(await userCanManageDevice(deviceId, userEmail))) {
    return noStore({ error: "소유자만 장치 토큰을 폐기할 수 있습니다." }, 403);
  }
  if (
    !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(
      credentialId,
    )
  ) {
    return noStore({ error: "장치 토큰을 찾을 수 없습니다." }, 404);
  }
  const revoked = await revokeDeviceCredential({
    deviceId,
    credentialId,
    userEmail,
  });
  return noStore({ revoked }, revoked ? 200 : 404);
}
