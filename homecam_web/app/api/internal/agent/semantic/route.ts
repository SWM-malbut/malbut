import {
  authorizedAgentSemanticRequest,
  buildAgentSemanticEnvelope,
  parseAgentSemanticBinding,
  validAgentSemanticRequest,
} from "../../../../agent-semantic-contract";
import { noStore } from "../../../../api-response";
import { getRuntimeEnvironment } from "../../../../runtime-env";
import { getAgentRobotMapSemantics } from "../../../../../db/robot-map";

export const dynamic = "force-dynamic";

const MAX_AGENT_SEMANTIC_REQUEST_BYTES = 1_024;

export async function POST(request: Request) {
  const binding = await parseAgentSemanticBinding(getRuntimeEnvironment());
  if (!binding) return noStore({ error: "찾을 수 없습니다." }, 404);
  if (!(await authorizedAgentSemanticRequest(
    request.headers.get("authorization"),
    binding.serviceSecret,
  ))) {
    return noStore({ error: "유효한 Agent 인증이 필요합니다." }, 401);
  }
  if (request.headers.get("content-type")?.split(";", 1)[0].trim() !== "application/json") {
    return noStore({ error: "application/json만 지원합니다." }, 415);
  }
  const declaredLength = Number(request.headers.get("content-length") ?? "0");
  if (
    Number.isFinite(declaredLength) &&
    declaredLength > MAX_AGENT_SEMANTIC_REQUEST_BYTES
  ) return noStore({ error: "요청이 너무 큽니다." }, 413);
  const body = await request.text();
  if (new TextEncoder().encode(body).byteLength > MAX_AGENT_SEMANTIC_REQUEST_BYTES) {
    return noStore({ error: "요청이 너무 큽니다." }, 413);
  }
  const payload = (() => {
    try {
      return JSON.parse(body) as unknown;
    } catch {
      return null;
    }
  })();
  if (!validAgentSemanticRequest(payload, binding)) {
    return noStore({ error: "Agent semantic 요청 형식을 확인해 주세요." }, 400);
  }
  const snapshot = await getAgentRobotMapSemantics(
    binding.deviceId,
    binding.userEmail,
    binding.principalSubject,
  );
  if (!snapshot) {
    return noStore({ error: "허용된 최종 지도를 찾을 수 없습니다." }, 403);
  }
  try {
    return noStore(
      await buildAgentSemanticEnvelope(binding, snapshot),
      200,
    );
  } catch {
    return noStore({ error: "semantic 응답을 만들지 못했습니다." }, 500);
  }
}
