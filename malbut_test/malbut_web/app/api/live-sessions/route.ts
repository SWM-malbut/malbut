import { getRuntimeEnvironment } from "../../runtime-env";
import { createLiveSession, endLiveSession } from "../../../db/petcam";
import {
  resolveDeviceKvsResources,
  type DeviceKvsEnvironment,
} from "../../kvs-device-config";
import {
  canBroadcastForConfiguredAccount,
  getRequestUserEmail,
} from "../../server-auth";

export const dynamic = "force-dynamic";

type SessionEnv = DeviceKvsEnvironment & {
  PETCAM_BROADCASTER_EMAILS?: string;
  PETCAM_SHARE_SECRET?: string;
};

export async function POST(request: Request) {
  const ownerEmail = await getRequestUserEmail(request);
  if (!ownerEmail) return noStore({ error: "로그인이 필요합니다." }, 401);

  const runtime = getRuntimeEnvironment() as SessionEnv;
  const deviceId = runtime.PETCAM_DEVICE_ID?.trim();
  if (!deviceId) {
    return noStore({ error: "레거시 홈캠 장치 ID 설정이 필요합니다." }, 503);
  }
  let resources;
  try {
    resources = resolveDeviceKvsResources(runtime, deviceId);
  } catch {
    return noStore({ error: "AWS 장치 매핑 설정이 올바르지 않습니다." }, 503);
  }
  const storageMode = Boolean(resources?.streamArn && resources.storageChannelArn);
  const channelArn = storageMode
    ? resources?.storageChannelArn
    : resources?.p2pChannelArn;
  if (!channelArn) return noStore({ error: "AWS 채널 설정이 필요합니다." }, 503);
  if (!runtime.PETCAM_SHARE_SECRET) {
    return noStore({ error: "시청 비밀번호 보안 설정이 필요합니다." }, 503);
  }
  if (!runtime.PETCAM_BROADCASTER_EMAILS?.trim()) {
    return noStore({ error: "영상 공개 ID 설정이 필요합니다." }, 503);
  }
  const canBroadcast = canBroadcastForConfiguredAccount(ownerEmail);
  if (!canBroadcast) {
    return noStore({ error: "이 ID에는 영상 공개 권한이 없습니다." }, 403);
  }

  try {
    const session = await createLiveSession({
      ownerEmail,
      deviceId,
      displayName: "노트북 카메라 01",
      channelArn,
      shareSecret: runtime.PETCAM_SHARE_SECRET,
      streamArn: storageMode ? resources?.streamArn ?? undefined : undefined,
    });
    return noStore({ session }, 201);
  } catch (error) {
    if (error instanceof Error && error.message === "DEVICE_FORBIDDEN") {
      return noStore({ error: "이 카메라를 사용할 권한이 없습니다." }, 403);
    }
    return noStore({ error: "스트리밍 세션을 만들지 못했습니다." }, 500);
  }
}

export async function DELETE(request: Request) {
  const ownerEmail = await getRequestUserEmail(request);
  if (!ownerEmail) return noStore({ error: "로그인이 필요합니다." }, 401);

  const payload = (await request.json().catch(() => null)) as { roomCode?: string } | null;
  const roomCode = payload?.roomCode?.trim().toUpperCase() ?? "";
  if (!/^[A-Z2-9]{6}$/.test(roomCode)) {
    return noStore({ error: "올바른 세션 코드가 필요합니다." }, 400);
  }

  const ended = await endLiveSession(ownerEmail, roomCode);
  return noStore({ ended }, 200);
}

function noStore(body: unknown, status: number) {
  return Response.json(body, {
    status,
    headers: { "cache-control": "no-store" },
  });
}
