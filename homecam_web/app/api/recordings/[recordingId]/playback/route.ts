import { getRuntimeEnvironment } from "../../../../runtime-env";
import {
  consumeRequestRateLimit,
  getAuthorizedRecordingSession,
} from "../../../../../db/petcam";
import { writeAuditLog } from "../../../../../db/homecam";
import { requestBrokerPlayback } from "../../../../kvs-broker";
import {
  resolveDeviceKvsResources,
  type DeviceKvsEnvironment,
} from "../../../../kvs-device-config";
import { createRecordingPlaybackProxy } from "../../../../recording-playback-proxy";
import { resolveRecordingSegmentWindow } from "../../../../recording-segments";
import { getRequestUserEmail } from "../../../../server-auth";

export const dynamic = "force-dynamic";

type PlaybackEnv = DeviceKvsEnvironment & {
  KVS_BROKER_SECRET?: string;
};

const HLS_PLAYBACK_BUFFER_SECONDS = 60 * 60;

export async function POST(
  request: Request,
  context: { params: Promise<{ recordingId: string }> },
) {
  const userEmail = await getRequestUserEmail(request);
  if (!userEmail) return noStore({ error: "ID 로그인이 필요합니다." }, 401);

  const { recordingId } = await context.params;
  if (
    !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(
      recordingId,
    )
  ) {
    return noStore({ error: "저장 영상을 찾을 수 없습니다." }, 404);
  }

  const payload = (await request.json().catch(() => null)) as {
    segment?: number;
  } | null;
  const segment = payload?.segment ?? 0;
  if (!Number.isSafeInteger(segment) || segment < 0) {
    return noStore({ error: "올바른 녹화 구간이 필요합니다." }, 400);
  }

  const recording = await getAuthorizedRecordingSession(userEmail, recordingId).catch(
    () => null,
  );
  if (!recording) {
    return noStore({ error: "저장 영상을 찾을 수 없습니다." }, 404);
  }
  const runtime = getRuntimeEnvironment() as PlaybackEnv;
  let resources;
  try {
    resources = resolveDeviceKvsResources(runtime, recording.deviceId);
  } catch {
    return noStore({ error: "AWS 장치 매핑 설정이 올바르지 않습니다." }, 503);
  }
  if (!resources?.streamArn) {
    return noStore({ error: "AWS 저장 재생 설정이 필요합니다." }, 503);
  }
  if (recording.streamArn !== resources.streamArn) {
    return noStore({ error: "저장 영상을 찾을 수 없습니다." }, 404);
  }
  const window = resolveRecordingSegmentWindow({
    recordingStartedAt: recording.startedAt,
    recordingEndedAt: recording.endedAt,
    segment,
  });
  if (!window) return noStore({ error: "저장 영상을 찾을 수 없습니다." }, 404);
  const expiresSeconds = Math.min(
    43_200,
    Math.max(300, window.durationSeconds + HLS_PLAYBACK_BUFFER_SECONDS),
  );

  const canIssuePlayback = await consumeRequestRateLimit({
    userEmail,
    roomCode: recordingId,
    scope: "recording-playback",
    limit: 10,
  });
  if (!canIssuePlayback) return rateLimited();

  try {
    const playback = await requestBrokerPlayback({
      deviceId: recording.deviceId,
      streamArn: recording.streamArn,
      startAt: window.startAt,
      endAt: window.endAt,
      expiresSeconds,
    });
    const proxy = await createRecordingPlaybackProxy(
      {
        requestUrl: request.url,
        playbackUrl: playback.playbackUrl,
        recordingId,
        userEmail,
        expiresAt: playback.expiresAt,
      },
      runtime.KVS_BROKER_SECRET ?? "",
    );
    await writeAuditLog({
      deviceId: recording.deviceId,
      actorType: "user",
      actorId: userEmail,
      action: "recording.play",
      metadata: {
        recordingId,
        segment,
        startAt: window.startAt,
      },
    }).catch(() => undefined);
    return noStore(
      {
        playbackUrl: proxy.playbackUrl,
        expiresAt: playback.expiresAt,
        seekAdjustmentSeconds: window.trimmedStartSeconds,
      },
      200,
      { "set-cookie": proxy.setCookie },
    );
  } catch (error) {
    if (error instanceof Error && error.message === "KVS_BROKER_404") {
      return noStore({ error: "해당 시간에 저장된 영상이 없습니다." }, 404);
    }
    return noStore({ error: "저장 영상 재생 주소를 발급하지 못했습니다." }, 503);
  }
}

function noStore(body: unknown, status: number, headers?: HeadersInit) {
  const responseHeaders = new Headers(headers);
  responseHeaders.set("cache-control", "no-store");
  return Response.json(body, {
    status,
    headers: responseHeaders,
  });
}

function rateLimited() {
  return Response.json(
    { error: "재생 요청이 너무 많습니다. 1분 뒤 다시 시도해 주세요." },
    {
      status: 429,
      headers: {
        "cache-control": "no-store",
        "retry-after": "60",
      },
    },
  );
}
