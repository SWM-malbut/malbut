import { getRuntimeEnvironment } from "../../../../runtime-env";
import {
  getActiveMediaSession,
  getDeviceSettings,
  prepareDeviceMediaSession,
  stopDeviceMediaSession,
  writeAuditLog,
} from "../../../../../db/homecam";
import { noStore, unauthorized } from "../../../../api-response";
import { getRequestDevice } from "../../../../device-auth";
import { requestBrokerDeviceCredentials } from "../../../../kvs-broker";
import {
  expectedKvsChannel,
  resolveDeviceKvsResources,
  type DeviceKvsEnvironment,
} from "../../../../kvs-device-config";

export const dynamic = "force-dynamic";

export async function POST(request: Request) {
  const device = await getRequestDevice(request);
  if (!device) return unauthorized("유효한 장치 토큰이 필요합니다.");
  const payload = await request.json().catch(() => ({}));
  if (
    !payload ||
    typeof payload !== "object" ||
    Array.isArray(payload) ||
    Object.keys(payload).some((key) => key !== "mode") ||
    ("mode" in payload && payload.mode !== "p2p" && payload.mode !== "storage")
  ) {
    return noStore({ error: "세션 모드는 p2p 또는 storage여야 합니다." }, 400);
  }

  const state = await getDeviceSettings(device.deviceId);
  if (!state.cameraEnabled) {
    return noStore({ error: "카메라가 비활성화되어 있습니다." }, 409);
  }
  const requestedMode = "mode" in payload ? payload.mode : undefined;
  const mode =
    requestedMode === "p2p" || requestedMode === "storage"
      ? requestedMode
      : sessionMode(state);
  if (!sessionModeAllowed(state, mode)) {
    return noStore(
      { error: mode === "storage" ? "영상 저장이 비활성화되어 있습니다." : "카메라가 비활성화되어 있습니다." },
      409,
    );
  }
  const runtime = getRuntimeEnvironment() as DeviceKvsEnvironment;
  let resources;
  try {
    resources = resolveDeviceKvsResources(runtime, device.deviceId);
  } catch {
    return noStore({ error: "AWS 장치 매핑 설정이 올바르지 않습니다." }, 503);
  }
  if (!resources) {
    return noStore({ error: "이 장치의 AWS 리소스가 설정되지 않았습니다." }, 503);
  }
  const expectedChannelArn = expectedKvsChannel(resources, mode);
  if (!expectedChannelArn) {
    return noStore({ error: "요청한 스트리밍 채널이 설정되지 않았습니다." }, 503);
  }
  if (mode === "storage" && !resources.streamArn) {
    return noStore({ error: "AWS 저장 스트림이 설정되지 않았습니다." }, 503);
  }

  try {
    const kvs = await requestBrokerDeviceCredentials({
      deviceId: device.deviceId,
      channelMode: mode,
    });
    if (
      kvs.channelArn !== expectedChannelArn ||
      (mode === "storage" && kvs.streamArn !== resources.streamArn) ||
      (mode === "p2p" && kvs.streamArn !== null)
    ) {
      return noStore({ error: "AWS 장치 채널 설정이 일치하지 않습니다." }, 503);
    }
    const refreshedState = await getDeviceSettings(device.deviceId);
    if (
      !refreshedState.cameraEnabled ||
      !sessionModeAllowed(refreshedState, mode)
    ) {
      return noStore(
        { error: "세션을 준비하는 동안 홈캠 설정이 변경되었습니다." },
        409,
      );
    }
    const session = await prepareDeviceMediaSession({
      deviceId: device.deviceId,
      mode,
      channelArn: kvs.channelArn,
      streamArn: kvs.streamArn ?? undefined,
    });
    const confirmedState = await getDeviceSettings(device.deviceId);
    const confirmedSession = await getActiveMediaSession(device.deviceId, mode);
    if (
      !confirmedState.cameraEnabled ||
      !sessionModeAllowed(confirmedState, mode) ||
      confirmedSession?.id !== session.id
    ) {
      await stopDeviceMediaSession(
        device.deviceId,
        "settings_race",
        session.id,
      );
      return noStore(
        { error: "세션을 준비하는 동안 홈캠 설정 또는 활성 세션이 변경되었습니다." },
        409,
      );
    }
    await writeAuditLog({
      deviceId: device.deviceId,
      actorType: "device",
      actorId: device.deviceId,
      action: "session.start",
      metadata: { mode, sessionId: session.id },
    });
    return noStore(
      {
        deviceId: device.deviceId,
        mode,
        session,
        kvs,
        desiredState: desiredState(confirmedState),
      },
      200,
    );
  } catch {
    return noStore({ error: "AWS 장치 연결 정보를 발급하지 못했습니다." }, 503);
  }
}

export async function DELETE(request: Request) {
  const device = await getRequestDevice(request);
  if (!device) return unauthorized("유효한 장치 토큰이 필요합니다.");
  const payload = (await request.json().catch(() => null)) as {
    sessionId?: unknown;
  } | null;
  const sessionId =
    payload &&
    Object.keys(payload).length === 1 &&
    typeof payload.sessionId === "string" &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(
      payload.sessionId,
    )
      ? payload.sessionId
      : null;
  if (!sessionId) {
    return noStore({ error: "종료할 장치 세션 ID가 필요합니다." }, 400);
  }
  const ended = await stopDeviceMediaSession(
    device.deviceId,
    "device_stop",
    sessionId,
  );
  return noStore({ ended }, 200);
}

function desiredState(state: {
  monitoringEnabled: boolean;
  cameraEnabled: boolean;
  microphoneEnabled: boolean;
}) {
  return {
    monitoringEnabled: state.monitoringEnabled,
    cameraEnabled: state.cameraEnabled,
    microphoneEnabled: state.microphoneEnabled,
  };
}

function sessionMode(state: { monitoringEnabled: boolean }) {
  return state.monitoringEnabled ? "storage" : "p2p";
}

function sessionModeAllowed(
  state: { cameraEnabled: boolean; monitoringEnabled: boolean },
  mode: "p2p" | "storage",
) {
  return state.cameraEnabled && (mode === "p2p" || state.monitoringEnabled);
}
