import {
  updateDeviceHeartbeat,
  type HomecamStreamMode,
} from "../../../../../db/homecam";
import { noStore, unauthorized } from "../../../../api-response";
import { getRequestDevice } from "../../../../device-auth";

export const dynamic = "force-dynamic";

export async function POST(request: Request) {
  const device = await getRequestDevice(request);
  if (!device) return unauthorized("유효한 장치 토큰이 필요합니다.");
  const payload = (await request.json().catch(() => null)) as Record<
    string,
    unknown
  > | null;
  const parsed = parseHeartbeat(payload);
  if (!parsed) return noStore({ error: "장치 상태 형식을 확인해 주세요." }, 400);

  const heartbeat = await updateDeviceHeartbeat({
    deviceId: device.deviceId,
    ...parsed,
  });
  const { activeSession, ...reportedState } = heartbeat;
  return noStore(
    {
      deviceId: device.deviceId,
      desiredState: {
        monitoringEnabled: reportedState.monitoringEnabled,
        cameraEnabled: reportedState.cameraEnabled,
        microphoneEnabled: reportedState.microphoneEnabled,
      },
      reportedState: {
        sourceProfile: reportedState.sourceProfile,
        imageTopic: reportedState.imageTopic,
        streamMode: reportedState.streamMode,
        mediaHealthy: reportedState.mediaHealthy,
        p2pHealthy: reportedState.p2pHealthy,
        storageHealthy: reportedState.storageHealthy,
        detectorHealthy: reportedState.detectorHealthy,
        lastSeenAt: reportedState.lastSeenAt,
        updatedAt: reportedState.updatedAt,
      },
      activeSession: activeSession
        ? {
            id: activeSession.id,
            roomCode: activeSession.roomCode,
            mode: activeSession.mode,
            startedAt: activeSession.startedAt,
            expiresAt: activeSession.expiresAt,
          }
        : null,
      activeSessions: {
        p2p: heartbeat.activeSessions.p2p
          ? {
              id: heartbeat.activeSessions.p2p.id,
              roomCode: heartbeat.activeSessions.p2p.roomCode,
              mode: "p2p",
              startedAt: heartbeat.activeSessions.p2p.startedAt,
              expiresAt: heartbeat.activeSessions.p2p.expiresAt,
            }
          : null,
        storage: heartbeat.activeSessions.storage
          ? {
              id: heartbeat.activeSessions.storage.id,
              roomCode: heartbeat.activeSessions.storage.roomCode,
              mode: "storage",
              startedAt: heartbeat.activeSessions.storage.startedAt,
              expiresAt: heartbeat.activeSessions.storage.expiresAt,
            }
          : null,
      },
    },
    200,
  );
}

function parseHeartbeat(value: Record<string, unknown> | null) {
  if (!value || Array.isArray(value)) return null;
  const allowed = [
    "sourceProfile",
    "imageTopic",
    "streamMode",
    "mediaHealthy",
    "p2pHealthy",
    "storageHealthy",
    "detectorHealthy",
  ];
  if (Object.keys(value).some((key) => !allowed.includes(key))) return null;
  if (
    value.sourceProfile !== undefined &&
    !["sim", "aurora", "unknown"].includes(String(value.sourceProfile))
  ) {
    return null;
  }
  if (
    value.imageTopic !== undefined &&
    value.imageTopic !== null &&
    (typeof value.imageTopic !== "string" ||
      value.imageTopic.length > 255 ||
      !value.imageTopic.startsWith("/"))
  ) {
    return null;
  }
  if (
    value.streamMode !== undefined &&
    !["idle", "p2p", "storage"].includes(String(value.streamMode))
  ) {
    return null;
  }
  if (
    value.mediaHealthy !== undefined &&
    typeof value.mediaHealthy !== "boolean"
  ) {
    return null;
  }
  if (
    value.p2pHealthy !== undefined &&
    typeof value.p2pHealthy !== "boolean"
  ) {
    return null;
  }
  if (
    value.storageHealthy !== undefined &&
    typeof value.storageHealthy !== "boolean"
  ) {
    return null;
  }
  if (
    value.detectorHealthy !== undefined &&
    typeof value.detectorHealthy !== "boolean"
  ) {
    return null;
  }
  return {
    sourceProfile: value.sourceProfile as "sim" | "aurora" | "unknown" | undefined,
    imageTopic: value.imageTopic as string | null | undefined,
    streamMode: value.streamMode as HomecamStreamMode | undefined,
    mediaHealthy: value.mediaHealthy as boolean | undefined,
    p2pHealthy: value.p2pHealthy as boolean | undefined,
    storageHealthy: value.storageHealthy as boolean | undefined,
    detectorHealthy: value.detectorHealthy as boolean | undefined,
  };
}
