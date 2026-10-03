import { noStore } from "./api-response";

export function fallAiFailure(error: unknown) {
  const code = error instanceof Error ? error.message : "";
  const known: Record<string, [string, number, string]> = {
    FALL_INCIDENT_NOT_FOUND: ["사건을 찾을 수 없습니다.", 404, "not_found"],
    FALL_AI_REVIEW_NOT_FOUND: ["AI 검토를 찾을 수 없습니다.", 404, "not_found"],
    FALL_AI_MOMENT_INVALID: ["넘어진 순간 형식을 확인해 주세요.", 400, "moment_invalid"],
    FALL_AI_QUESTION_INVALID: ["질문은 1~500자로 입력해 주세요.", 400, "question_invalid"],
    // The app then asks whether to file a new missed-fall report instead.
    FALL_AI_OUTSIDE_INCIDENT: ["이 시간은 원래 사건 밖이에요. 놓친 넘어짐으로 새로 신고할까요?", 409, "outside_incident"],
    FALL_AI_REVIEW_IN_PROGRESS: ["진행 중인 AI 검토가 끝난 뒤 다시 요청해 주세요.", 409, "in_progress"],
    FALL_AI_REVIEW_NOT_COMPLETED: ["AI 검토 결과가 나온 뒤 질문할 수 있습니다.", 409, "not_completed"],
    FALL_AI_CONSENT_OFF: ["클라우드 분석 동의가 꺼져 있어 AI에게 보낼 수 없습니다.", 409, "consent_off"],
    FALL_AI_KEY_MISSING: ["이 로봇에 클라우드 AI 키가 없습니다. 소유자가 설정에서 등록해야 합니다.", 409, "key_missing"],
    FALL_AI_MODEL_UNKNOWN: ["로봇이 아직 AI 모델을 알려 주지 않았습니다. 로봇이 서버에 연결된 뒤 다시 시도해 주세요.", 409, "model_unknown"],
    FALL_KEY_FORBIDDEN: ["소유자만 클라우드 AI 키를 바꿀 수 있습니다.", 403, "forbidden"],
    FALL_KEY_INVALID: ["키 형식을 확인해 주세요.", 400, "key_invalid"],
    FALL_KEY_SECRET_MISSING: ["키 암호화 설정이 준비되지 않았습니다.", 503, "unavailable"],
    FALL_AI_MIGRATION_REQUIRED: ["AI 검토 DB 준비가 필요합니다.", 503, "unavailable"],
  };
  const [message, status, reason] = known[code] ?? ["AI 검토를 처리하지 못했습니다.", 503, "unavailable"];
  return noStore({ error: message, reason }, status);
}
