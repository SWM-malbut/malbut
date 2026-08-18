import { consumeRequestRateLimit } from "../../../../../../../db/petcam";
import {
  getEventClipPlayback,
  userCanViewDevice,
  writeAuditLog,
} from "../../../../../../../db/homecam";
import { getRuntimeEnvironment } from "../../../../../../runtime-env";
import { requestBrokerEventPlayback } from "../../../../../../kvs-broker";
import {
  resolveDeviceKvsResources,
  type DeviceKvsEnvironment,
} from "../../../../../../kvs-device-config";
import { createRecordingPlaybackProxy } from "../../../../../../recording-playback-proxy";
import { getRequestUserEmail } from "../../../../../../server-auth";

export const dynamic = "force-dynamic";

type PlaybackEnv = DeviceKvsEnvironment & {
  KVS_BROKER_SECRET?: string;
  AUTH_PUBLIC_ORIGIN?: string;
};

export async function POST(
  request: Request,
  context: { params: Promise<{ deviceId: string; eventId: string }> },
) {
  const userEmail = await getRequestUserEmail(request);
  if (!userEmail) return noStore({ error: "로그인이 필요합니다." }, 401);
  const { deviceId, eventId } = await context.params;
  if (!isUuid4(eventId) || !(await userCanViewDevice(deviceId, userEmail))) {
    return noStore({ error: "이벤트 클립을 찾을 수 없습니다." }, 404);
  }
  const playbackInfo = await getEventClipPlayback(deviceId, eventId);
  if (!playbackInfo) {
    return noStore({ error: "아직 재생할 수 있는 이벤트 클립이 아닙니다." }, 409);
  }
  const { event, streamArn } = playbackInfo;
  const runtime = getRuntimeEnvironment() as PlaybackEnv;
  let resources;
  try {
    resources = resolveDeviceKvsResources(runtime, deviceId);
  } catch {
    return noStore({ error: "AWS 장치 매핑 설정이 올바르지 않습니다." }, 503);
  }
  if (!resources?.streamArn || resources.streamArn !== streamArn) {
    return noStore({ error: "이벤트 클립을 찾을 수 없습니다." }, 404);
  }
  const canIssuePlayback = await consumeRequestRateLimit({
    userEmail,
    roomCode: eventId,
    scope: "event-clip-playback",
    limit: 20,
  });
  if (!canIssuePlayback) {
    return noStore(
      { error: "재생 요청이 너무 많습니다. 1분 뒤 다시 시도해 주세요." },
      429,
      { "retry-after": "60" },
    );
  }

  try {
    const playback = await requestBrokerEventPlayback({
      deviceId,
      streamArn,
      startAt: event.clipStartAt,
      endAt: event.clipEndAt,
      expiresSeconds: 300,
    });
    const proxy = await createRecordingPlaybackProxy(
      {
        requestUrl: request.url,
        publicOrigin: runtime.AUTH_PUBLIC_ORIGIN,
        playbackUrl: playback.playbackUrl,
        recordingId: event.recordingId,
        userEmail,
        expiresAt: playback.expiresAt,
      },
      runtime.KVS_BROKER_SECRET ?? "",
    );
    const seekAdjustmentSeconds = Math.max(
      0,
      (Date.parse(event.clipStartAt) - Date.parse(playback.alignedStartAt)) / 1000,
    );
    const durationSeconds = Math.max(
      0,
      (Date.parse(event.clipEndAt) - Date.parse(event.clipStartAt)) / 1000,
    );
    await writeAuditLog({
      deviceId,
      actorType: "user",
      actorId: userEmail,
      action: "event.play",
      metadata: { eventId, eventGroupId: event.eventGroupId },
    }).catch(() => undefined);
    return noStore(
      {
        playbackUrl: proxy.playbackUrl,
        expiresAt: playback.expiresAt,
        seekAdjustmentSeconds,
        durationSeconds,
      },
      200,
      { "set-cookie": proxy.setCookie },
    );
  } catch (error) {
    if (error instanceof Error && error.message === "KVS_BROKER_404") {
      return noStore(
        { error: "이벤트 영상의 저장을 마무리하고 있습니다." },
        425,
        { "retry-after": "2" },
      );
    }
    return noStore({ error: "이벤트 클립 재생 주소를 발급하지 못했습니다." }, 503);
  }
}

function isUuid4(value: string) {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
}

function noStore(body: unknown, status: number, headers?: HeadersInit) {
  const responseHeaders = new Headers(headers);
  responseHeaders.set("cache-control", "no-store");
  return Response.json(body, { status, headers: responseHeaders });
}
