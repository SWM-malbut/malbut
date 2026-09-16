import { completeRobotCommand } from "../../../../../../../../db/robot-map";
import { noStore, unauthorized } from "../../../../../../../api-response";
import { getRequestDevice } from "../../../../../../../device-auth";

export const dynamic = "force-dynamic";

export async function POST(
  request: Request,
  context: { params: Promise<{ commandId: string }> },
) {
  const device = await getRequestDevice(request);
  if (!device) return unauthorized("유효한 장치 토큰이 필요합니다.");
  const { commandId } = await context.params;
  if (!isUuid(commandId)) return noStore({ error: "명령을 찾을 수 없습니다." }, 404);
  if (request.headers.get("content-type")?.split(";", 1)[0].trim() !== "application/json") {
    return noStore({ error: "application/json만 지원합니다." }, 415);
  }
  const length = Number(request.headers.get("content-length") ?? "0");
  if (Number.isFinite(length) && length > 256 * 1024) {
    return noStore({ error: "요청 본문이 너무 큽니다." }, 413);
  }
  const text = await request.text();
  if (new TextEncoder().encode(text).byteLength > 256 * 1024) {
    return noStore({ error: "요청 본문이 너무 큽니다." }, 413);
  }
  let payload: Record<string, unknown> | null = null;
  try {
    payload = JSON.parse(text) as Record<string, unknown>;
  } catch {
    return noStore({ error: "JSON 형식을 확인해 주세요." }, 400);
  }
  if (!payload || typeof payload.ok !== "boolean" || !("result" in payload)) {
    return noStore({ error: "명령 결과 형식을 확인해 주세요." }, 400);
  }
  const command = await completeRobotCommand({
    deviceId: device.deviceId,
    commandId,
    ok: payload.ok,
    result: payload.result,
  });
  if (!command) return noStore({ error: "처리 중인 명령을 찾을 수 없습니다." }, 404);
  return noStore({ command }, 200);
}

function isUuid(value: string) {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
}
