import { createRobotCommand } from "../../../../../../db/robot-map";
import { parseRobotCommand } from "../../../../../robot-contract";
import { noStore } from "../../../../../api-response";
import { getRequestUserEmail } from "../../../../../server-auth";

export const dynamic = "force-dynamic";

export async function POST(
  request: Request,
  context: { params: Promise<{ deviceId: string }> },
) {
  const userEmail = await getRequestUserEmail(request);
  if (!userEmail) return noStore({ error: "로그인이 필요합니다." }, 401);
  const { deviceId } = await context.params;
  const payload = await request.json().catch(() => null) as Record<string, unknown> | null;
  const parsed = parseRobotCommand(payload);
  if (!parsed) {
    return noStore({ error: "지도 명령 형식을 확인해 주세요." }, 400);
  }
  try {
    const command = await createRobotCommand({
      deviceId,
      userEmail,
      operation: parsed.operation,
      payload: parsed.payload,
    });
    return noStore({ command }, 202);
  } catch (error) {
    if (error instanceof Error && error.message === "FORBIDDEN") {
      return noStore({ error: "소유자만 지도 생성을 제어할 수 있습니다." }, 403);
    }
    if (error instanceof Error && error.message === "COMMAND_IN_PROGRESS") {
      return noStore({ error: "이전 지도 명령을 처리하고 있습니다." }, 409);
    }
    return noStore({ error: "지도 명령을 등록하지 못했습니다." }, 500);
  }
}
