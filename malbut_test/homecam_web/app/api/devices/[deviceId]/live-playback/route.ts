import { noStore } from "../../../../api-response";
import {
  getActiveMediaSession,
  getDeviceSettings,
  userCanViewDevice,
  writeAuditLog,
} from "../../../../../db/homecam";
import { consumeRequestRateLimit } from "../../../../../db/petcam";
import { requestBrokerLivePlayback } from "../../../../kvs-broker";
import {
  resolveDeviceKvsResources,
  type DeviceKvsEnvironment,
} from "../../../../kvs-device-config";
import { createDeviceLivePlaybackProxy } from "../../../../recording-playback-proxy";
import { getRuntimeEnvironment } from "../../../../runtime-env";
import { getRequestUserEmail } from "../../../../server-auth";

export const dynamic = "force-dynamic";

type PlaybackEnv = DeviceKvsEnvironment & {
  KVS_BROKER_SECRET?: string;
  AUTH_PUBLIC_ORIGIN?: string;
};

export async function POST(
  request: Request,
  context: { params: Promise<{ deviceId: string }> },
) {
  const userEmail = await getRequestUserEmail(request);
  if (!userEmail) return noStore({ error: "로그인이 필요합니다." }, 401);
  const { deviceId } = await context.params;
  if (!(await userCanViewDevice(deviceId, userEmail))) {
    return noStore({ error: "이 홈캠을 볼 권한이 없습니다." }, 403);
  }

  const [state, session] = await Promise.all([
    getDeviceSettings(deviceId),
    getActiveMediaSession(deviceId, "storage"),
  ]);
  if (!state.cameraEnabled || !session) {
    return noStore({ error: "홈캠이 현재 송출 중이 아닙니다." }, 409);
  }
  if (
    session.mode !== "storage" ||
    !state.storageHealthy
  ) {
    return noStore(
      { error: "AWS 저장 영상을 준비하고 있습니다." },
      425,
      { "retry-after": "2" },
    );
  }

  const canIssuePlayback = await consumeRequestRateLimit({
    userEmail,
    roomCode: session.roomCode,
    scope: "homecam-live-hls",
    limit: 30,
  });
  if (!canIssuePlayback) {
    return noStore(
      { error: "재생 요청이 너무 많습니다. 잠시 뒤 다시 시도해 주세요." },
      429,
      { "retry-after": "60" },
    );
  }

  const runtime = getRuntimeEnvironment() as PlaybackEnv;
  let resources;
  try {
    resources = resolveDeviceKvsResources(runtime, deviceId);
  } catch {
    return noStore({ error: "AWS 장치 매핑 설정이 올바르지 않습니다." }, 503);
  }
  if (!resources?.streamArn) {
    return noStore({ error: "이 장치의 저장 스트림이 설정되지 않았습니다." }, 503);
  }

  try {
    const playback = await requestBrokerLivePlayback({
      deviceId,
      streamArn: resources.streamArn,
      expiresSeconds: 300,
    });
    if (
      !(await userCanViewDevice(deviceId, userEmail)) ||
      (await getActiveMediaSession(deviceId, "storage"))?.id !== session.id
    ) {
      return noStore({ error: "홈캠 접근 권한 또는 활성 세션이 변경되었습니다." }, 403);
    }
    const proxy = await createDeviceLivePlaybackProxy(
      {
        requestUrl: request.url,
        publicOrigin: runtime.AUTH_PUBLIC_ORIGIN,
        playbackUrl: playback.playbackUrl,
        deviceId,
        userEmail,
        expiresAt: playback.expiresAt,
      },
      runtime.KVS_BROKER_SECRET ?? "",
    );
    await writeAuditLog({
      deviceId,
      actorType: "user",
      actorId: userEmail,
      action: "live.view",
      metadata: { mode: "storage-hls", sessionId: session.id },
    }).catch(() => undefined);
    return noStore(
      { playbackUrl: proxy.playbackUrl, expiresAt: playback.expiresAt },
      200,
      { "set-cookie": proxy.setCookie },
    );
  } catch (error) {
    if (error instanceof Error && error.message === "KVS_BROKER_404") {
      return noStore(
        { error: "저장 영상의 첫 조각을 기다리고 있습니다." },
        425,
        { "retry-after": "2" },
      );
    }
    return noStore({ error: "저장 영상 재생 주소를 발급하지 못했습니다." }, 503);
  }
}
