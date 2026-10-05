import { getFallClipForPlayback } from "../../../../../../../../../db/fall-review";
import { writeAuditLog } from "../../../../../../../../../db/homecam";
import { consumeRequestRateLimit } from "../../../../../../../../../db/request-rate-limit";
import { noStore } from "../../../../../../../../api-response";
import { fallMember } from "../../../../../../../../fall-review-route";
import { requestBrokerEventPlayback } from "../../../../../../../../kvs-broker";
import { resolveDeviceKvsResources, type DeviceKvsEnvironment } from "../../../../../../../../kvs-device-config";
import { createFallClipPlaybackProxy } from "../../../../../../../../recording-playback-proxy";
import { getRuntimeEnvironment } from "../../../../../../../../runtime-env";

export const dynamic = "force-dynamic";
type PlaybackEnv = DeviceKvsEnvironment & { KVS_BROKER_SECRET?: string; AUTH_PUBLIC_ORIGIN?: string };
type Context = { params: Promise<{ deviceId: string; incidentId: string; segmentIndex: string }> };

const STATE_ERRORS: Record<string, [string, number]> = {
  preparing: ["장면 영상을 저장하고 있습니다. 잠시 뒤 다시 시도해 주세요.", 425],
  unavailable: ["이 시간의 녹화 영상이 없습니다.", 404],
  expired: ["보관 기간(7일)이 지나 영상이 삭제되었습니다.", 410],
};

export async function POST(request: Request, context: Context) {
  const { deviceId, incidentId, segmentIndex } = await context.params;
  const member = await fallMember(request, deviceId, incidentId);
  if (member.response) return member.response;
  if (!/^(?:[0-9]|[12][0-9]|3[01])$/.test(segmentIndex)) return noStore({ error: "장면을 찾을 수 없습니다." }, 404);
  const clip = await getFallClipForPlayback(deviceId, incidentId, Number(segmentIndex)).catch(() => null);
  if (!clip) return noStore({ error: "장면을 찾을 수 없습니다." }, 404);
  const stateError = STATE_ERRORS[clip.playbackState];
  if (stateError) {
    return noStore({ error: stateError[0], playbackState: clip.playbackState }, stateError[1],
      clip.playbackState === "preparing" ? { "retry-after": "5" } : undefined);
  }
  const runtime = getRuntimeEnvironment() as PlaybackEnv;
  let streamArn: string | null | undefined;
  try { streamArn = resolveDeviceKvsResources(runtime, deviceId)?.streamArn; }
  catch { return noStore({ error: "AWS 장치 매핑 설정이 올바르지 않습니다." }, 503); }
  // The recording sessions in this range must belong to this device's stream.
  if (!streamArn || !clip.streamArns.includes(streamArn)) {
    return noStore({ error: "이 시간의 녹화 영상이 없습니다.", playbackState: "unavailable" }, 404);
  }
  if (!(await consumeRequestRateLimit({ userEmail: member.email, roomCode: incidentId,
    scope: "fall-clip-playback", limit: 20 }))) {
    return noStore({ error: "재생 요청이 너무 많습니다. 1분 뒤 다시 시도해 주세요." }, 429, { "retry-after": "60" });
  }
  try {
    const playback = await requestBrokerEventPlayback({
      deviceId, streamArn, startAt: clip.startAt, endAt: clip.endAt, expiresSeconds: 300,
    });
    const proxy = await createFallClipPlaybackProxy({
      requestUrl: request.url, publicOrigin: runtime.AUTH_PUBLIC_ORIGIN, playbackUrl: playback.playbackUrl,
      deviceId, userEmail: member.email, expiresAt: playback.expiresAt,
    }, runtime.KVS_BROKER_SECRET ?? "");
    await writeAuditLog({ deviceId, actorType: "user", actorId: member.email, action: "fall_clip.play",
      metadata: { incidentId, segmentIndex: Number(segmentIndex) } }).catch(() => undefined);
    return noStore({
      playbackUrl: proxy.playbackUrl, expiresAt: playback.expiresAt,
      playbackState: clip.playbackState,
      seekAdjustmentSeconds: Math.max(0, (Date.parse(clip.startAt) - Date.parse(playback.alignedStartAt)) / 1000),
      durationSeconds: (Date.parse(clip.endAt) - Date.parse(clip.startAt)) / 1000,
    }, 200, { "set-cookie": proxy.setCookie });
  } catch (error) {
    if (error instanceof Error && error.message === "KVS_BROKER_404") {
      return noStore({ error: "이 시간의 녹화 영상이 없습니다.", playbackState: "unavailable" }, 404);
    }
    return noStore({ error: "장면 재생 주소를 발급하지 못했습니다." }, 503);
  }
}
