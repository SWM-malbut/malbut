import { noStore } from "./api-response";
export function companionFailure(error: unknown) {
  const code = error instanceof Error ? error.message : "";
  const errors: Record<string, [number, string]> = {
    COMPANION_FORBIDDEN: [403, "소유자만 일상 기억과 본인 확인을 관리할 수 있어요."],
    COMPANION_CHANGED: [409, "설정이 바뀌었어요. 새로 확인해 주세요."],
    COMPANION_NO_ENCOUNTER: [409, "기억 동의와 현재 로봇의 대화 상태를 확인해 주세요."],
  };
  const [status, message] = errors[code] ?? [503, "말벗 기억 설정에 연결하지 못했어요."];
  return noStore({ error: message }, status);
}
