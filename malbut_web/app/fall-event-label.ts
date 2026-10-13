/** Human-readable history only: never used to decide or merge incidents. */
export type FallHistoryEvent = {
  eventKind: string;
  assessment?: string | null;
  answer?: string | null;
  reason?: string | null;
  notificationLevel?: string | null;
  analysis?: {
    requestId: string; purpose: "incident" | "crosscheck" | "person_check" | "posture_check" | "same_person_check";
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
  notification_requested: "알림", agent_check_failed: "질문 중 오류가 나 물을 수 없음",
  analysis_completed: "클라우드 AI", analysis_unavailable: "클라우드 AI 분석 실패",
  stale_analysis_result: "늦게 도착한 분석 결과", recheck_unavailable: "재확인 실패",
  incident_resolved: "로봇이 사건 종료", confirmation_completed: "질문에 대한 답",
  incident_merged: "사람 연결 완료 · 연결된 사건에서 계속 확인",
};

/**
 * Short noun endings for the history (관찰됩니다 → 관찰됨, 필요합니다 → 필요함,
 * 있습니다 → 있음). Display only: the stored model text stays as written, and
 * asking the model for this style changed its judgement, so it is not asked.
 */
export function nounEnding(text: string): string {
  return text
    .replace(/습니다(?=[^가-힣]|$)/g, "음")
    .replace(/([가-힣])니다(?=[^가-힣]|$)/g, (match, syllable: string) => {
      const code = syllable.charCodeAt(0);
      // Final consonant ㅂ (index 17) becomes ㅁ (16): 됩 → 됨, 합 → 함, 입 → 임.
      return (code - 0xac00) % 28 === 17 ? String.fromCharCode(code - 1) : match;
    })
    .replace(/[.]\s*$/, "");
}

const APPROACH_START: Record<string, string> = {
  patrol_stopped: "순찰을 멈추고 확인하러 감", follow_stopped: "사람 따라가기를 멈추고 확인하러 감",
};
const APPROACH_BLOCKED: Record<string, string> = {
  no_map: "지도 없음", no_path: "갈 길 없음", timeout: "시간 초과", failed: "이동 실패", rejected: "이동 실패",
};

const POSTURE: Record<string, string> = {
  sitting_off_floor: "몸 전체 확인: 침대나 의자에 앉아 있음",
  standing: "몸 전체 확인: 서 있음",
  on_floor: "몸 전체 확인: 바닥에 있음 → 물어봄",
  body_unseen: "몸 전체를 못 봄 → 이 자리에서 물어봄",
  judgements_differ: "로봇이 잰 값과 클라우드 AI 판단이 다름 → 물어봄",
  posture_unconfirmed: "자세를 확인하지 못함 → 물어봄",
};

// 목업 28번: 바로 묻지 못한 이유, 1분, 다시 가서 보기, 처음 장면과 비교.
const CHECK_WAITING: Record<string, string> = {
  manual_drive: "확인하러 가려 했지만 직접 움직이기 중 → 끝나면 확인",
  busy: "확인하러 가려 했지만 말벗이 다른 일을 하는 중 → 끝나면 확인",
};
const REVISIT_START: Record<string, string> = {
  manual_drive: "직접 움직이기 끝남 → 묻기 전에 의심 장소로 다시 감",
  busy: "다른 일이 끝남 → 묻기 전에 의심 장소로 다시 감",
};
const CHECK_FAILED: Record<string, string> = {
  speech_unavailable: "말벗 음성이 꺼져 있어 물을 수 없음",
  question_error: "질문 중 오류가 나 물을 수 없음",
  time_limit: "막혀서 10분 동안 확인하지 못함 → 확인을 멈춤",
};
// 알림 줄은 "단계 알림: 무엇"으로 통일한다(목업 28번 "긴급 알림: 도움 요청").
const LEVEL: Record<string, string> = { urgent: "긴급", check: "확인 필요", info: "일반" };
const NOTICE: Record<string, [string, string]> = {
  fall_observed_person_okay: ["info", "넘어졌지만 괜찮다고 응답"],
  person_no_response: ["check", "질문에 응답 없음"],
  check_required_not_confirmed_fall: ["check", "낙상 의심을 확인하지 못함"],
  help_requested: ["urgent", "도움 요청"],
  confirmation_help_required: ["urgent", "상황 확인 결과 도움 필요"],
  robot_could_not_confirm: ["urgent", "말벗이 직접 확인하지 못함"],
  scene_answer_unconfirmed: ["check", "괜찮다는 답, 쓰러진 분인지 모름"],
  check_stopped_by_user: ["check", "'그만'을 듣고 확인을 멈춤"],
};
const SAME_PERSON: Record<string, string> = {
  same: "같은 분으로 보임", different: "다른 분으로 보임", unclear: "같은 분인지 알 수 없음",
};
const REVISIT_NOTICE: Record<string, string> = {
  revisit_same_person_off_floor: "묻지 않음 · 일반 알림 · 사건은 열어 둠",
  revisit_person_unclear_off_floor: "묻지 않음 · 확인 필요 알림 · 사건은 열어 둠",
  revisit_nobody_there: "묻지 않음 · 확인 필요 알림 · 사건은 열어 둠",
};

/** 다시 가서 본 기록(revisit_started 뒤)은 "다시"를 붙인다. 표시 전용. */
function revisitLabel(event: FallHistoryEvent): string | null {
  const reason = event.reason ?? "";
  if (event.eventKind === "approach_completed") {
    return reason === "arrived" ? "의심 장소 1 m 앞에 다시 도착"
      : `다시 가지 못함: ${APPROACH_BLOCKED[reason] ?? "이동 실패"} → 이 자리에서 물어봄`;
  }
  if (event.eventKind === "person_check_completed") {
    if (reason === "nobody_there") return "가까이에서 다시 확인: 그 자리에 사람이 없음";
    if (reason === "person") return "가까이에서 다시 확인: 사람 (사람 확인됨)";
    if (reason === "partial_body") return "가까이에서 다시 확인: 사람 · 몸 일부만 보임";
    if (reason === "whole_body") return "가까이에서 다시 확인: 사람 · 몸 전체 보임";
  }
  return null;
}

/** 말벗이 의심 장소로 다가가 확인한 기록. 목업 22·25번 문구, 음슴체. */
function approachLabel(event: FallHistoryEvent): string | null {
  const reason = event.reason ?? "";
  if (event.eventKind === "check_waiting") return CHECK_WAITING[reason] ?? "확인하러 가려 했지만 아직 갈 수 없음";
  if (event.eventKind === "check_not_confirmed") return "1분째 확인하지 못함";
  if (event.eventKind === "check_stopped") return "'그만'이라는 말을 듣고 확인을 멈춤 (묻지 않음)";
  if (event.eventKind === "revisit_started") return REVISIT_START[reason] ?? "묻기 전에 의심 장소로 다시 감";
  if (event.eventKind === "same_person_compared") {
    const ai = event.analysis?.explanation ? ` (AI: ${nounEnding(event.analysis.explanation)})` : "";
    return `처음 장면과 비교: ${SAME_PERSON[reason] ?? SAME_PERSON.unclear}${ai}`;
  }
  if (event.eventKind === "agent_check_failed") {
    // 예전 이유(confirmation_transport_failed, agent_failed)도 질문 중 오류다.
    return Object.hasOwn(CHECK_FAILED, reason) ? CHECK_FAILED[reason] : CHECK_FAILED.question_error;
  }
  if (event.eventKind === "notification_requested" && Object.hasOwn(REVISIT_NOTICE, reason)) {
    return REVISIT_NOTICE[reason];
  }
  if (event.eventKind === "notification_requested") {
    const [defaultLevel, what] = Object.hasOwn(NOTICE, reason) ? NOTICE[reason] : ["", ""];
    const level = LEVEL[event.notificationLevel ?? defaultLevel] ?? "";
    return [level ? `${level} 알림` : "알림", what].filter(Boolean).join(": ");
  }
  if (event.eventKind === "voice_result" || event.eventKind === "confirmation_completed") {
    // 답 줄은 "질문에 대한 답 (답)"으로 통일한다. 영상 판정은 붙이지 않는다.
    // 영상만 본 장면에서 괜찮다는 답은 누운 분의 답인지 몰라 answer가 unclear다.
    if (reason === "scene_answer_unassociated") return "질문에 대한 답 (괜찮다고 답함 · 쓰러진 분인지 모름)";
    return event.answer ? `질문에 대한 답 (${ANSWER[event.answer] ?? event.answer})` : "질문에 대한 답";
  }
  if (event.eventKind === "incident_resolved" && reason === "guardian_closed") return "처리 완료 → 말벗이 확인을 멈춤";
  if (event.eventKind === "approach_started") {
    return `${APPROACH_START[reason] ?? "확인하러 감"} (사람인지 확실하지 않음)`;
  }
  if (event.eventKind === "approach_completed") {
    return reason === "arrived" ? "의심 장소 1 m 앞에 도착"
      : `가까이 가지 못함: ${APPROACH_BLOCKED[reason] ?? "이동 실패"} → 이 자리에서 물어봄`;
  }
  if (event.eventKind === "person_check_completed") {
    if (reason === "person") return "가까이에서 확인: 사람 (사람 확인됨)";
    if (reason === "partial_body") return "가까이에서 확인: 사람 · 몸 일부만 보임";
    if (reason === "whole_body") return "가까이에서 확인: 사람 · 몸 전체 보임";
    const ai = event.analysis?.explanation ? ` · AI: ${nounEnding(event.analysis.explanation)}` : "";
    return `가까이에서 확인: 사람 아님 (사람 확인 불가${ai})`;
  }
  if (event.eventKind === "view_completed") {
    return reason === "arrived" ? "몸 전체가 보이는 자리로 옮김"
      : `몸 전체가 보이는 자리로 못 감: ${APPROACH_BLOCKED[reason] ?? "이동 실패"}`;
  }
  if (event.eventKind === "posture_check_completed") {
    const ai = event.analysis?.explanation ? ` (AI: ${nounEnding(event.analysis.explanation)})` : "";
    return `${POSTURE[reason] ?? "몸 전체 확인"}${ai}`;
  }
  if (event.eventKind === "approach_returned") {
    return reason === "returned" ? "원래 자리로 돌아옴" : "원래 자리로 돌아가지 못함";
  }
  if (event.eventKind === "incident_resolved" && reason === "not_a_person") return "로봇이 사건 종료: 사람 아님";
  if (event.eventKind === "incident_resolved" && reason === "off_floor_verified") {
    return "로봇이 사건 종료: 바닥에 있지 않음 · 묻지 않음";
  }
  return null;
}

export function fallEventLabel(event: FallHistoryEvent): string {
  // A request is not proof that audio played. No hard-coded speech transcript.
  if (event.eventKind === "question_requested") return "로봇 확인 질문 요청";
  const approach = approachLabel(event);
  if (approach) return approach;

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
    return `${label} (${analysis?.explanation ? nounEnding(analysis.explanation) : "판단 이유 기록 없음"})`;
  }
  if (!opening && event.answer) label += ` (${ANSWER[event.answer] ?? event.answer})`;
  return label;
}

/**
 * History lines in order (목업 28번): after revisit_started the close look reads
 * "다시", and the alert right after "1분째 확인하지 못함" or a failed question
 * joins that line instead of repeating it. Display only.
 */
export function fallHistoryLines<T extends FallHistoryEvent>(events: T[]): Array<{ event: T; label: string }> {
  const lines: Array<{ event: T; label: string }> = [];
  let revisit = false;
  for (const event of events) {
    if (event.eventKind === "revisit_started") revisit = true;
    const previous = lines.at(-1);
    if (event.eventKind === "notification_requested" && event.reason === "robot_could_not_confirm" && previous) {
      const kind = previous.event.eventKind, reason = previous.event.reason ?? "";
      if (kind === "check_not_confirmed") { previous.label += " → 긴급 알림"; continue; }
      if (kind === "agent_check_failed" && Object.hasOwn(CHECK_FAILED, reason)) {
        previous.label += reason === "time_limit" ? " · 긴급 알림" : " → 바로 긴급 알림";
        continue;
      }
    }
    lines.push({ event, label: (revisit ? revisitLabel(event) : null) ?? fallEventLabel(event) });
  }
  return lines;
}

/** 말벗이 직접 확인하지 못한 사건의 요약(목업 28번): 이유, 다시 가서 본 결과, 목록 부제목. */
export type FallRobotCheck = {
  /** 말벗이 직접 확인하지 못한 이유(처음 것). */
  reason: string;
  /** 막혀서 1분 동안 묻지 못함. */
  waited: boolean;
  /** 막힌 시간이 모두 10분이 되어 스스로 멈춤. */
  stopped: boolean;
  /** 바로 실패(음성 꺼짐·질문 중 오류)한 경우의 부제목. */
  failed: string | null;
  revisit: { title: string; note: string | null; subtitle: string } | null;
};

const WAITED_REASON: Record<string, string> = {
  waited_manual_drive: "직접 움직이기 중이라 1분 안에 묻지 못함",
  waited_busy: "말벗이 다른 일로 바빠 1분 안에 묻지 못함",
};
const FAILED_REASON: Record<string, [string, string]> = {
  speech_unavailable: ["말벗 음성이 꺼져 있어 물을 수 없음", "말벗 음성이 꺼져 있어 묻지 못함"],
  question_error: ["질문 중 오류가 나 물을 수 없음", "질문 중 오류로 묻지 못함"],
};
const REVISIT_ASK: Record<string, string> = {
  on_floor: "바닥에 있음", body_unseen: "몸 전체를 못 봄", judgements_differ: "판단이 엇갈림",
  posture_unconfirmed: "자세를 확인하지 못함", person: "사람이 있음",
};
const ASK_NOTE = "대답에 따라 알림은 지금 규칙과 같아요.";

function revisitResult(events: FallHistoryEvent[]): FallRobotCheck["revisit"] {
  let outcome: { kind: "absent" } | { kind: "unreached" } | { kind: "off"; standing: boolean } |
    { kind: "ask"; what: string; floor: boolean } | null = null;
  let compared: FallHistoryEvent | null = null;
  for (const e of events) {
    const reason = e.reason ?? "";
    if (e.eventKind === "approach_completed" && reason !== "arrived") outcome = { kind: "unreached" };
    else if (e.eventKind === "person_check_completed" && reason === "nobody_there") outcome = { kind: "absent" };
    else if (e.eventKind === "person_check_completed" && reason === "person") {
      outcome = { kind: "ask", what: REVISIT_ASK.person, floor: false };
    } else if (e.eventKind === "posture_check_completed") {
      outcome = reason === "standing" || reason === "sitting_off_floor"
        ? { kind: "off", standing: reason === "standing" }
        : { kind: "ask", what: REVISIT_ASK[reason] ?? REVISIT_ASK.posture_unconfirmed, floor: reason === "on_floor" };
    } else if (e.eventKind === "same_person_compared") compared = e;
  }
  const verdict = compared ? SAME_PERSON[compared.reason ?? ""] ?? SAME_PERSON.unclear : null;
  if (!outcome) return { title: "다시 가서 확인하는 중", note: null, subtitle: "다시 가서 확인하는 중" };
  if (outcome.kind === "absent") {
    return { title: "그 자리에 사람이 없음", subtitle: "다시 가 보니 그 자리에 사람이 없음",
      note: "일어나서 다른 곳으로 갔을 수도 있지만, 기어서 옮겼거나 다른 곳에서 다시 쓰러졌을 수도 있어요. " +
        "그래서 \"사람 아님\"으로 닫지 않아요." };
  }
  if (outcome.kind === "unreached") {
    return { title: "다시 가지 못함 → 이 자리에서 물어봄", subtitle: "다시 가지 못해 이 자리에서 물어봄",
      note: `가지 못하면 그 자리에서 물어봐요. ${ASK_NOTE}` };
  }
  if (outcome.kind === "ask") {
    return { title: `${outcome.what}${verdict ? ` · ${verdict}` : ""} → 물어봄`, subtitle: `다시 가 보니 ${outcome.what}`,
      note: `${outcome.floor ? "바닥에 있으면" : "바닥에 있지 않은지 확인하지 못하면"} 지금처럼 물어봐요. ${ASK_NOTE}` };
  }
  const posture = outcome.standing ? "서 있음" : "침대나 의자에 앉아 있음";
  const who = outcome.standing ? "서 있는" : "침대나 의자에 앉아 있는";
  const reason = compared?.reason ?? null;
  const explanation = compared?.analysis?.explanation;
  return {
    title: `${posture} · ${verdict ?? "같은 분인지 비교하는 중"}`,
    subtitle: reason === "same" ? `다시 가 보니 같은 분으로 보이는 분이 ${posture}`
      : `다시 가 보니 ${who} 분이 있음 · ${reason === "different" ? "다른 분으로 보임" : "같은 분인지 모름"}`,
    note: !compared ? "AI가 처음 장면과 옷차림을 비교하고 있어요(참고)."
      : `${explanation ? `AI가 처음 장면과 옷차림을 비교했어요(참고): ${nounEnding(explanation)}.`
        : "AI가 처음 장면과 옷차림을 비교하지 못했어요."}${reason === "unclear" && explanation ? ""
        : " 같은 분인지는 직접 확인해 주세요."}`,
  };
}

/** Events in sequence order. Null when the robot did not fail to check by itself. */
export function fallRobotCheck(events: FallHistoryEvent[]): FallRobotCheck | null {
  let reason: string | null = null, failed: string | null = null, waited = false, stopped = false;
  let revisitAt = -1;
  events.forEach((e, index) => {
    const r = e.reason ?? "";
    if (e.eventKind === "check_not_confirmed") {
      waited = true;
      reason ??= WAITED_REASON[r] ?? WAITED_REASON.waited_busy;
    } else if (e.eventKind === "check_stopped") {
      reason ??= "'그만'이라는 말을 듣고 확인을 멈춤";
    } else if (e.eventKind === "agent_check_failed" && r === "time_limit") stopped = true;
    else if (e.eventKind === "agent_check_failed" && Object.hasOwn(FAILED_REASON, r)) {
      reason ??= FAILED_REASON[r][0];
      failed = FAILED_REASON[r][1];
    } else if (e.eventKind === "revisit_started") revisitAt = index;
  });
  if (!reason && !stopped && revisitAt < 0) return null;
  return { reason: reason ?? "막혀서 10분 동안 확인하지 못함", waited, stopped, failed,
    revisit: revisitAt < 0 ? null : revisitResult(events.slice(revisitAt + 1)) };
}
