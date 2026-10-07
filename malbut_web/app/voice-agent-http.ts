import { noStore } from "./api-response";
import { getRuntimeEnvironment } from "./runtime-env";

export async function readVoiceJson(request: Request) {
  if (request.headers.get("content-type")?.split(";", 1)[0].trim() !== "application/json") throw new Error("VOICE_INVALID_REQUEST");
  const reader = request.body?.getReader();
  if (!reader) throw new Error("VOICE_INVALID_REQUEST");
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > 8192) { await reader.cancel(); throw new Error("VOICE_INVALID_REQUEST"); }
      chunks.push(value);
    }
    return JSON.parse(Buffer.concat(chunks).toString("utf8"));
  } finally { reader.releaseLock(); }
}
export function sameOriginVoiceRequest(request: Request) {
  if (request.headers.get("sec-fetch-site") === "cross-site") return false;
  const configured = getRuntimeEnvironment().AUTH_PUBLIC_ORIGIN?.trim();
  const origin = configured ? new URL(configured).origin : new URL(request.url).origin;
  return request.headers.get("origin") === origin;
}
export function voiceApiFailure(error: unknown) {
  const code = error instanceof Error ? error.message : "VOICE_OPERATION_FAILED";
  const known: Record<string, [number, string]> = {
    VOICE_FORBIDDEN: [403, "소유자만 음성 홈캠 권한을 변경할 수 있습니다."],
    VOICE_MIGRATION_REQUIRED: [503, "음성 홈캠 DB 준비가 필요합니다."],
    VOICE_REQUEST_CONFLICT: [409, "같은 요청 ID에 다른 내용을 보낼 수 없습니다."],
    VOICE_INVALID_REQUEST: [400, "음성 요청 형식을 확인해 주세요."],
  };
  const [status, message] = known[code] ?? [500, "음성 요청을 처리하지 못했습니다."];
  return noStore({ success: false, code: known[code] ? code : "VOICE_OPERATION_FAILED", result: {}, message, error: message }, status);
}
