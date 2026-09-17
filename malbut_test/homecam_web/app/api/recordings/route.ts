import { getRuntimeEnvironment } from "../../runtime-env";
import {
  getAuthorizedMasterSession,
  listAuthorizedRecordingSessions,
  markRecordingStarted,
} from "../../../db/petcam";
import {
  resolveDeviceKvsResources,
  type DeviceKvsEnvironment,
} from "../../kvs-device-config";
import {
  canBroadcastForConfiguredAccount,
  getRequestUserEmail,
} from "../../server-auth";

export const dynamic = "force-dynamic";

const RECORDING_SEGMENT_MS = 60 * 60 * 1000;
const RECORDING_RETENTION_MS = 7 * 24 * RECORDING_SEGMENT_MS;

export async function GET(request: Request) {
  const userEmail = await getRequestUserEmail(request);
  if (!userEmail) return noStore({ error: "ID 로그인이 필요합니다." }, 401);

  try {
    const recordings = await listAuthorizedRecordingSessions(userEmail);
    return noStore({ recordings: recordings.flatMap(segmentRecording) }, 200);
  } catch {
    return noStore({ error: "저장 영상을 불러오지 못했습니다." }, 500);
  }
}

export async function POST(request: Request) {
  const userEmail = await getRequestUserEmail(request);
  if (!userEmail) return noStore({ error: "ID 로그인이 필요합니다." }, 401);
  if (!canBroadcastForConfiguredAccount(userEmail)) {
    return noStore({ error: "이 ID에는 영상 공개 권한이 없습니다." }, 403);
  }

  const payload = (await request.json().catch(() => null)) as { roomCode?: string } | null;
  const roomCode = payload?.roomCode?.trim().toUpperCase() ?? "";
  if (!/^[A-Z2-9]{6}$/.test(roomCode)) {
    return noStore({ error: "올바른 세션 코드가 필요합니다." }, 400);
  }

  const session = await getAuthorizedMasterSession(userEmail, roomCode).catch(
    () => null,
  );
  if (!session) {
    return noStore({ error: "녹화를 시작할 세션이나 권한이 없습니다." }, 403);
  }
  const runtime = getRuntimeEnvironment() as DeviceKvsEnvironment;
  let resources;
  try {
    resources = resolveDeviceKvsResources(runtime, session.deviceId);
  } catch {
    return noStore({ error: "AWS 장치 매핑 설정이 올바르지 않습니다." }, 503);
  }
  if (!resources?.streamArn || !resources.storageChannelArn) {
    return noStore({ error: "AWS 저장 모드가 활성화되지 않았습니다." }, 409);
  }
  if (session.channelArn !== resources.storageChannelArn) {
    return noStore({ error: "AWS 저장 채널 설정이 일치하지 않습니다." }, 503);
  }

  try {
    const started = await markRecordingStarted({
      userEmail,
      roomCode,
      streamArn: resources.streamArn,
    });
    if (!started) {
      return noStore({ error: "녹화를 시작할 세션이나 권한이 없습니다." }, 403);
    }
    return noStore({ started: true }, 200);
  } catch {
    return noStore({ error: "녹화 시작 상태를 저장하지 못했습니다." }, 500);
  }
}

function segmentRecording<T extends { startedAt: string; endedAt: string | null }>(
  recording: T,
) {
  const recordingStart = Date.parse(recording.startedAt);
  const recordingEnd = recording.endedAt
    ? Math.min(Date.parse(recording.endedAt), Date.now())
    : Date.now();
  if (!Number.isFinite(recordingStart) || recordingEnd <= recordingStart) return [];

  const totalSegmentCount = Math.ceil(
    (recordingEnd - recordingStart) / RECORDING_SEGMENT_MS,
  );
  const retentionCutoff = Date.now() - RECORDING_RETENTION_MS;
  const firstSegment = Math.max(
    0,
    Math.floor((retentionCutoff - recordingStart) / RECORDING_SEGMENT_MS),
  );
  const segmentCount = Math.max(0, totalSegmentCount - firstSegment);
  return Array.from({ length: segmentCount }, (_, offset) => {
    const segment = firstSegment + offset;
    const startedAt = recordingStart + segment * RECORDING_SEGMENT_MS;
    const endedAt = Math.min(recordingEnd, startedAt + RECORDING_SEGMENT_MS);
    return {
      ...recording,
      segment,
      startedAt: new Date(startedAt).toISOString(),
      endedAt: new Date(endedAt).toISOString(),
    };
  });
}

function noStore(body: unknown, status: number) {
  return Response.json(body, {
    status,
    headers: { "cache-control": "no-store" },
  });
}
