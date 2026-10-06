/** Human-readable history only: never used to decide or merge incidents. */
export type FallHistoryEvent = {
  eventKind: string;
  assessment?: string | null;
  answer?: string | null;
  reason?: string | null;
  analysis?: {
    requestId: string; purpose: "incident" | "crosscheck";
    assessment: string; explanation: string;
  } | null;
};

const ASSESSMENT: Record<string, string> = {
  observed_fall: "낙상", suspected_fall: "낙상 의심", normal_activity: "정상", unobservable: "판단 불가",
};
const ANSWER: Record<string, string> = {
  help_request: "도움 요청", okay: "괜찮다고 응답", unclear: "응답 불분명",
  no_response: "응답 없음", failed: "질문 실패",
};
const POSE_REASON: Record<string, string> = {
  pose_rapid_posture_change: "급격한 자세 변화",
  pose_sustained_horizontal_posture: "누운 자세 추정",
  pose_sustained_low_posture: "낮은 자세 지속",
  pose_candidate: "감지 근거 기록 없음",
};
const EVENT: Record<string, string> = {
  incident_opened: "사건 생성", incident_updated: "새 근거로 사건 갱신",
  voice_result: "질문에 대한 답", decision_required: "추가 판단 필요",
  notification_requested: "알림 요청", agent_check_failed: "로봇 질문 실패",
  analysis_completed: "클라우드 AI", analysis_unavailable: "클라우드 AI 분석 실패",
  stale_analysis_result: "늦게 도착한 분석 결과", recheck_unavailable: "재확인 실패",
  incident_resolved: "로봇이 사건 종료", confirmation_completed: "상황 확인 완료",
  incident_merged: "사람 연결 완료 · 연결된 사건에서 계속 확인",
};

export function fallEventLabel(event: FallHistoryEvent): string {
  // A request is not proof that audio played. No hard-coded speech transcript.
  if (event.eventKind === "question_requested") return "로봇 확인 질문 요청";

  const opening = event.eventKind === "incident_opened" || event.eventKind === "incident_updated";
  const poseReason = event.reason && Object.hasOwn(POSE_REASON, event.reason) ? POSE_REASON[event.reason] : undefined;
  if (opening && poseReason) {
    // Pose creates a suspicion, not a VLM classification. A carried-over old
    // assessment/answer must not label a newly observed Pose candidate.
    return `자세 분석: 낙상 의심 (${poseReason})`;
  }
  let label = EVENT[event.eventKind] ?? event.eventKind;
  if (opening && (event.reason === "cloud_crosscheck" || event.reason === "target_unidentified")) {
    label = event.eventKind === "incident_opened" ? "클라우드 AI 발견" : "클라우드 AI 근거 갱신";
  }
  // The incident can retain a stronger past judgment. Show THIS request's
  // classification beside its explanation, not that retained judgment.
  const analysis = event.eventKind === "analysis_completed" ? event.analysis : null;
  const assessment = analysis?.assessment ?? event.assessment;
  if (assessment) label += `: ${ASSESSMENT[assessment] ?? assessment}`;
  if (event.eventKind === "analysis_completed") {
    return `${label} (${analysis?.explanation || "판단 이유 기록 없음"})`;
  }
  if (!opening && event.answer) label += ` (${ANSWER[event.answer] ?? event.answer})`;
  return label;
}
