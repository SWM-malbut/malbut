import { getRuntimeEnvironment } from "../../../../runtime-env";
import {
  getActiveMediaSession,
  getDeviceSettings,
  userCanViewDevice,
  writeAuditLog,
} from "../../../../../db/homecam";
import { consumeRequestRateLimit } from "../../../../../db/petcam";
import { isValidClientId } from "../../../../../db/homecam-validation";
import { noStore } from "../../../../api-response";
import {
  requestBrokerJoinStorage,
  requestBrokerSession,
} from "../../../../kvs-broker";
import {
  expectedKvsChannel,
  resolveDeviceKvsResources,
  type DeviceKvsEnvironment,
} from "../../../../kvs-device-config";
import { getRequestUserEmail } from "../../../../server-auth";

export const dynamic = "force-dynamic";

export async function GET(
  request: Request,
  context: { params: Promise<{ deviceId: string }> },
) {
  const userEmail = await getRequestUserEmail(request);
  if (!userEmail) return noStore({ authorized: false }, 401);
  const { deviceId } = await context.params;
  if (!(await userCanViewDevice(deviceId, userEmail))) {
    return noStore({ authorized: false }, 403);
  }
  return noStore({ authorized: true }, 200);
}

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
  const payload = (await request.json().catch(() => null)) as {
    clientId?: unknown;
    joinStorage?: unknown;
  } | null;
  if (
    !payload ||
    Object.keys(payload).some(
      (key) => key !== "clientId" && key !== "joinStorage",
    ) ||
    !isValidClientId(payload.clientId) ||
    (payload.joinStorage !== undefined &&
      typeof payload.joinStorage !== "boolean")
  ) {
    return noStore({ error: "올바른 viewer clientId가 필요합니다." }, 400);
  }
  const state = await getDeviceSettings(deviceId);
  const activeSession = await getActiveMediaSession(deviceId);
  if (!state.cameraEnabled || !activeSession) {
    return noStore({ error: "홈캠이 현재 송출 중이 아닙니다." }, 409);
  }
  const mode = activeSession.mode;
  const joinStorage = payload.joinStorage === true;
  if (joinStorage && mode !== "storage") {
    return noStore({ error: "P2P 세션에는 Storage join을 사용할 수 없습니다." }, 409);
  }
  if (
    joinStorage &&
    (state.streamMode !== "storage" || !state.mediaHealthy)
  ) {
    return noStore(
      { error: "홈캠이 AWS 저장 세션을 준비 중입니다." },
      425,
      { "retry-after": "2" },
    );
  }
  const canIssueCredentials = await consumeRequestRateLimit({
    userEmail,
    roomCode: activeSession.roomCode,
    scope: "homecam-viewer-credentials",
    limit: 10,
  });
  if (!canIssueCredentials) {
    return noStore(
      { error: "연결 요청이 너무 많습니다. 1분 뒤 다시 시도해 주세요." },
      429,
      { "retry-after": "60" },
    );
  }
  if (joinStorage) {
    const canJoinStorage = await consumeRequestRateLimit({
      userEmail,
      roomCode: activeSession.roomCode,
      scope: "homecam-storage-join",
      limit: 10,
    });
    if (!canJoinStorage) {
      return noStore(
        { error: "연결 요청이 너무 많습니다. 1분 뒤 다시 시도해 주세요." },
        429,
        { "retry-after": "60" },
      );
    }
  }
  const runtime = getRuntimeEnvironment() as DeviceKvsEnvironment;
  let resources;
  try {
    resources = resolveDeviceKvsResources(runtime, deviceId);
  } catch {
    return noStore({ error: "AWS 장치 매핑 설정이 올바르지 않습니다." }, 503);
  }
  if (!resources) {
    return noStore({ error: "이 장치의 AWS 리소스가 설정되지 않았습니다." }, 503);
  }
  const expectedChannelArn = expectedKvsChannel(resources, mode);
  if (!expectedChannelArn || (mode === "storage" && !resources.streamArn)) {
    return noStore({ error: "요청한 AWS 홈캠 리소스가 설정되지 않았습니다." }, 503);
  }

  try {
    const broker = await requestBrokerSession({
      deviceId,
      role: "VIEWER",
      clientId: payload.clientId,
      channelMode: mode,
    });
    if (broker.channelArn !== expectedChannelArn) {
      return noStore({ error: "AWS 홈캠 채널 설정이 일치하지 않습니다." }, 503);
    }
    if (
      !(await userCanViewDevice(deviceId, userEmail)) ||
      (await getActiveMediaSession(deviceId))?.id !== activeSession.id
    ) {
      return noStore(
        { error: "홈캠 접근 권한 또는 활성 세션이 변경되었습니다." },
        403,
      );
    }
    if (joinStorage) {
      const joined = await requestBrokerJoinStorage({
        deviceId,
        role: "VIEWER",
        clientId: payload.clientId,
        channelMode: "storage",
      });
      if (joined.channelArn !== broker.channelArn) {
        return noStore({ error: "AWS 저장 채널 설정이 일치하지 않습니다." }, 503);
      }
      if (
        !(await userCanViewDevice(deviceId, userEmail)) ||
        (await getActiveMediaSession(deviceId))?.id !== activeSession.id
      ) {
        return noStore(
          { error: "홈캠 접근 권한 또는 활성 세션이 변경되었습니다." },
          403,
        );
      }
    }
    await writeAuditLog({
      deviceId,
      actorType: "user",
      actorId: userEmail,
      action: "live.view",
      metadata: { mode, sessionId: activeSession.id },
    });
    return noStore(
      {
        ...broker,
        deviceId,
        clientId: payload.clientId,
        storageMode: mode === "storage",
        storageJoined: joinStorage,
        activeSession: {
          id: activeSession.id,
          roomCode: activeSession.roomCode,
          mode: activeSession.mode,
          startedAt: activeSession.startedAt,
          expiresAt: activeSession.expiresAt,
        },
      },
      200,
    );
  } catch {
    return noStore({ error: "AWS 실시간 연결 정보를 발급하지 못했습니다." }, 503);
  }
}
