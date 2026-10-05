import { RECORDING_RETENTION_MS, recordingStreamFor } from "../../../../../db/fall-review";
import { consumeRequestRateLimit } from "../../../../../db/request-rate-limit";
import { noStore } from "../../../../api-response";
import { fallMember } from "../../../../fall-review-route";
import { requestBrokerEventPlayback } from "../../../../kvs-broker";
import { resolveDeviceKvsResources, type DeviceKvsEnvironment } from "../../../../kvs-device-config";
import { createFallClipPlaybackProxy } from "../../../../recording-playback-proxy";
import { getRuntimeEnvironment } from "../../../../runtime-env";
import { sameOriginJsonRequest } from "../../../../same-origin-request";

export const dynamic = "force-dynamic";
type PlaybackEnv = DeviceKvsEnvironment & { KVS_BROKER_SECRET?: string; AUTH_PUBLIC_ORIGIN?: string };
const MAX_WINDOW_MS = 10 * 60_000;

function isoMs(value: unknown): value is string {
  return typeof value === "string" && Number.isFinite(Date.parse(value)) && new Date(value).toISOString() === value;
}

/** 연속 녹화 화면: a ≤ 10 min window of the recording for picking a moment. */
export async function POST(request: Request, context: { params: Promise<{ deviceId: string }> }) {
  const { deviceId } = await context.params;
  const member = await fallMember(request, deviceId);
  if (member.response) return member.response;
  if (!sameOriginJsonRequest(request)) return noStore({ error: "요청 출처를 확인해 주세요." }, 403);
  const body = await request.json().catch(() => null);
  if (!body || typeof body !== "object" || Array.isArray(body) || Object.keys(body).length !== 2 ||
      !isoMs(body.startAt) || !isoMs(body.endAt)) {
    return noStore({ error: "재생 구간 형식을 확인해 주세요." }, 400);
  }
  const start = Date.parse(body.startAt), end = Date.parse(body.endAt);
  if (end <= start || end - start > MAX_WINDOW_MS || start < Date.now() - RECORDING_RETENTION_MS) {
    return noStore({ error: "최근 7일 안의 10분 이내 구간만 볼 수 있어요." }, 400);
  }
  const runtime = getRuntimeEnvironment() as PlaybackEnv;
  let streamArn: string | null | undefined;
  try { streamArn = resolveDeviceKvsResources(runtime, deviceId)?.streamArn; }
  catch { return noStore({ error: "AWS 장치 매핑 설정이 올바르지 않습니다." }, 503); }
  const streams: string[] = await recordingStreamFor(deviceId, body.startAt, body.endAt).catch(() => []);
  if (!streamArn || !streams.includes(streamArn)) {
    return noStore({ error: "이 시간의 녹화 영상이 없습니다.", playbackState: "unavailable" }, 404);
  }
  if (!(await consumeRequestRateLimit({ userId: member.userId, roomCode: deviceId,
    scope: "recording-playback", limit: 30 }))) {
    return noStore({ error: "재생 요청이 너무 많습니다. 1분 뒤 다시 시도해 주세요." }, 429, { "retry-after": "60" });
  }
  try {
    const playback = await requestBrokerEventPlayback({ deviceId, streamArn, startAt: body.startAt,
      endAt: body.endAt, expiresSeconds: 300 });
    const proxy = await createFallClipPlaybackProxy({
      requestUrl: request.url, publicOrigin: runtime.AUTH_PUBLIC_ORIGIN, playbackUrl: playback.playbackUrl,
      deviceId, userId: member.userId, expiresAt: playback.expiresAt,
    }, runtime.KVS_BROKER_SECRET ?? "");
    // Video time 0 is alignedStartAt (the first archived fragment), not startAt.
    return noStore({ playbackUrl: proxy.playbackUrl, expiresAt: playback.expiresAt,
      alignedStartAt: playback.alignedStartAt }, 200, { "set-cookie": proxy.setCookie });
  } catch (error) {
    if (error instanceof Error && error.message === "KVS_BROKER_404") {
      return noStore({ error: "이 시간의 녹화 영상이 없습니다.", playbackState: "unavailable" }, 404);
    }
    return noStore({ error: "녹화 재생 주소를 발급하지 못했습니다." }, 503);
  }
}
