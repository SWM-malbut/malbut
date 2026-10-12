import { listClosedFallIncidentsForRobot } from "../../../../../../db/fall-review";
import { storeRobotState } from "../../../../../../db/robot-map";
import { parseRobotState, readRobotJson } from "../../../../../robot-contract";
import { noStore, unauthorized } from "../../../../../api-response";
import { getRequestDevice } from "../../../../../device-auth";
import { exchangeHomecamHeartbeat } from "../../../../../homecam-heartbeat";

export const dynamic = "force-dynamic";

export async function POST(request: Request) {
  const device = await getRequestDevice(request);
  if (!device) return unauthorized("유효한 장치 토큰이 필요합니다.");
  try {
    const body = await readRobotJson(request, "state");
    const state = parseRobotState(body);
    if (!state) return noStore({ error: "로봇 상태 형식을 확인해 주세요." }, 400);
    await storeRobotState(device.deviceId, state);
    const homecam = await homecamExchange(device.deviceId, body);
    return noStore({
      accepted: true, closedFallIncidents: await closedFallIncidents(device.deviceId),
      ...(homecam ? { homecam } : {}),
    }, 202);
  } catch (error) {
    return robotUploadError(error);
  }
}

/** Incidents a member closed (처리 완료): the robot stops trying to check them. */
async function closedFallIncidents(deviceId: string) {
  // The state itself is saved; a fall-store failure must not fail the upload.
  try { return await listClosedFallIncidentsForRobot(deviceId); } catch { return []; }
}

/**
 * The homecam's heartbeat rides on this upload (one web channel, 2026-10-12): the
 * reply is what POST /heartbeat answers, or its error and status. Like the closed
 * incidents, a homecam failure never fails the saved state.
 */
async function homecamExchange(deviceId: string, body: unknown) {
  if (!body || typeof body !== "object" || !Object.hasOwn(body, "homecam")) return null;
  try {
    const { status, body: reply } = await exchangeHomecamHeartbeat(
      deviceId, (body as Record<string, unknown>).homecam);
    return status === 200 ? reply : { error: reply.error, status };
  } catch {
    return { error: "홈캠 상태를 처리하지 못했습니다.", status: 500 };
  }
}

function robotUploadError(error: unknown) {
  if (!(error instanceof Error)) return noStore({ error: "로봇 상태를 저장하지 못했습니다." }, 500);
  if (error.message === "UNSUPPORTED_MEDIA_TYPE") return noStore({ error: "application/json만 지원합니다." }, 415);
  if (error.message === "PAYLOAD_TOO_LARGE") return noStore({ error: "요청 본문이 너무 큽니다." }, 413);
  if (error.message === "INVALID_JSON") return noStore({ error: "JSON 형식을 확인해 주세요." }, 400);
  return noStore({ error: "로봇 상태를 저장하지 못했습니다." }, 500);
}
