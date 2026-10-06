import { getFallClipForPlayback } from "../../../../../../../../../db/fall-review";
import { consumeRequestRateLimit } from "../../../../../../../../../db/request-rate-limit";
import { noStore } from "../../../../../../../../api-response";
import { fallMember } from "../../../../../../../../fall-review-route";
import { requestBrokerImages } from "../../../../../../../../kvs-broker";
import { resolveDeviceKvsResources, type DeviceKvsEnvironment } from "../../../../../../../../kvs-device-config";
import { getRuntimeEnvironment } from "../../../../../../../../runtime-env";

export const dynamic = "force-dynamic";
type Context = { params: Promise<{ deviceId: string; incidentId: string; segmentIndex: string }> };

// The archive returns at most one frame per 200 ms; two samples over half a second give the moment.
const STILL_WINDOW_MS = 500;

/**
 * 정지 사진: one JPEG of the suspected moment, shown before the scene video is played.
 * Members only, like the video. Viewing the still is not recorded (only playing the video is).
 */
export async function GET(request: Request, context: Context) {
  const { deviceId, incidentId, segmentIndex } = await context.params;
  const member = await fallMember(request, deviceId, incidentId);
  if (member.response) return member.response;
  if (!/^(?:[0-9]|[12][0-9]|3[01])$/.test(segmentIndex)) return noStore({ error: "장면을 찾을 수 없습니다." }, 404);
  const clip = await getFallClipForPlayback(deviceId, incidentId, Number(segmentIndex)).catch(() => null);
  if (!clip) return noStore({ error: "장면을 찾을 수 없습니다." }, 404);
  if (clip.playbackState === "expired") return noStore({ error: "보관 기간(7일)이 지나 영상이 삭제되었습니다." }, 410);
  if (clip.playbackState === "unavailable") return noStore({ error: "이 시간의 녹화 영상이 없습니다." }, 404);
  let streamArn: string | null | undefined;
  try { streamArn = resolveDeviceKvsResources(getRuntimeEnvironment() as DeviceKvsEnvironment, deviceId)?.streamArn; }
  catch { return noStore({ error: "AWS 장치 매핑 설정이 올바르지 않습니다." }, 503); }
  if (!streamArn || !clip.streamArns.includes(streamArn)) {
    return noStore({ error: "이 시간의 녹화 영상이 없습니다." }, 404);
  }
  if (!(await consumeRequestRateLimit({ userId: member.userId, roomCode: incidentId,
    scope: "fall-clip-still", limit: 30 }))) {
    return noStore({ error: "사진 요청이 너무 많습니다. 1분 뒤 다시 시도해 주세요." }, 429, { "retry-after": "60" });
  }
  try {
    const images = await requestBrokerImages({ deviceId, streamArn, startAt: clip.momentAt,
      endAt: new Date(Date.parse(clip.momentAt) + STILL_WINDOW_MS).toISOString(), count: 2 });
    const jpeg = images.find((image) => image.jpegBase64)?.jpegBase64;
    // A recent moment may not be archived yet: no photo now, the video still plays.
    if (!jpeg) return noStore({ error: "이 순간의 사진이 없습니다." }, 404);
    return new Response(Buffer.from(jpeg, "base64"), { headers: {
      "content-type": "image/jpeg", "cache-control": "private, max-age=600", "x-content-type-options": "nosniff",
    } });
  } catch (error) {
    if (error instanceof Error && error.message === "KVS_BROKER_404") {
      return noStore({ error: "이 순간의 사진이 없습니다." }, 404);
    }
    return noStore({ error: "사진을 가져오지 못했습니다." }, 503);
  }
}
