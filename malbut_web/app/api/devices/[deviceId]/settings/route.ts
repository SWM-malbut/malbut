import {
  updateDeviceSettings,
  userCanManageDevice,
} from "../../../../../db/homecam";
import { parseDeviceSettingsPatch } from "../../../../../db/homecam-validation";
import { noStore } from "../../../../api-response";
import { getRequestUserEmail } from "../../../../server-auth";

export const dynamic = "force-dynamic";

export async function PATCH(
  request: Request,
  context: { params: Promise<{ deviceId: string }> },
) {
  const userEmail = await getRequestUserEmail(request);
  if (!userEmail) return noStore({ error: "로그인이 필요합니다." }, 401);
  const { deviceId } = await context.params;
  if (!(await userCanManageDevice(deviceId, userEmail))) {
    return noStore({ error: "소유자만 홈캠 설정을 변경할 수 있습니다." }, 403);
  }
  const patch = parseDeviceSettingsPatch(await request.json().catch(() => null));
  if (!patch) return noStore({ error: "설정 형식을 확인해 주세요." }, 400);
  try {
    const settings = await updateDeviceSettings({ deviceId, userEmail, patch });
    return noStore({ settings }, 200);
  } catch (error) {
    if (error instanceof Error && error.message === "CAMERA_DISABLED") {
      return noStore(
        { error: "카메라를 먼저 켠 뒤 모니터링을 활성화해 주세요." },
        409,
      );
    }
    return noStore({ error: "홈캠 설정을 저장하지 못했습니다." }, 500);
  }
}
