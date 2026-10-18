import { josa } from "./korean-particle";

/** Human-readable history only: never used to decide or merge incidents. */
export type FallHistoryEvent = {
  eventKind: string;
  assessment?: string | null;
  answer?: string | null;
  reason?: string | null;
  notificationLevel?: string | null;
  /** Sent on every event of the one check (SWM25-261); absent on the older check. */
  checkRound?: number | null;
  analysis?: {
    requestId: string; purpose: "incident" | "crosscheck" | "person_check" | "posture_check" | "same_person_check" |
      "arrival_check" | "departure_check";
    assessment: string; explanation: string;
  } | null;
};

const ASSESSMENT: Record<string, string> = {
  observed_fall: "낙상", suspected_fall: "낙상 의심", normal_activity: "정상", unobservable: "판단 불가",
};
// 기록 줄은 "누가: 무엇"(웹 용어 F): 말벗 · 클라우드 AI · 대답(집 안 사람의 답) · 알림(보호자에게 보낸 것).
type Who = "말벗" | "클라우드 AI" | "대답" | "알림";
const line = (who: Who, what: string) => `${who}: ${what}`;
const ANSWER: Record<string, string> = {
  help_request: "도움 요청", okay: "괜찮다고 함", unclear: "잘 알아듣지 못함",
  no_response: "대답 없음", failed: "질문 실패",
};
const POSE_REASON: Record<string, string> = {
  pose_rapid_posture_change: "급격한 자세 변화",
  pose_sustained_horizontal_posture: "누운 자세 추정",
  pose_sustained_low_posture: "낮은 자세 지속",
  pose_candidate: "감지 근거 기록 없음",
};
const EVENT: Record<string, string> = {
  incident_opened: "사건 생성", incident_updated: "새 근거로 사건 갱신",
  decision_required: "추가 판단 필요", incident_resolved: "사건 종료",
  incident_merged: "다른 사건에 병합 · 그 사건에서 계속 확인",
};
const CLOUD_EVENT: Record<string, string> = {
  analysis_unavailable: "판정 실패", stale_analysis_result: "늦게 도착한 판정 (쓰지 않음)",
  recheck_unavailable: "다시 확인하지 못함",
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

// 설명에 섞여 온 몸 전체 확인 분류 이름(웹 용어 10번; 실기 10-17 22:19 "floor_active로 판단됩니다").
const LABEL_WORD: Record<string, string> = {
  floor_active: "바닥에서 활동", floor: "바닥", furniture: "침대·의자", standing: "서 있음", unclear: "알 수 없음",
};
const LABEL_IN_TEXT =
  /["'`]?\b(floor_active|floor|furniture|standing|unclear)\b["'`]?(?:(으로|로|이|가|은|는|을|를|과|와)(?![가-힣]))?/gi;

/** AI 설명 속 영어 분류 이름을 한국어로, 뒤 조사도 받침에 맞게. Display only: 저장된 글은 그대로. */
export function plainLabels(text: string): string {
  return text.replace(LABEL_IN_TEXT, (_match, label: string, particle?: string) => {
    const word = LABEL_WORD[label.toLowerCase()];
    return particle ? josa(word, particle) : word;
  });
}

/** 기록에 보이는 AI 설명: 분류 이름을 풀고 끝을 짧게. */
export const aiText = (text: string) => nounEnding(plainLabels(text));

const APPROACH_START: Record<string, string> = {
  patrol_stopped: "순찰을 멈추고 확인하러 감", follow_stopped: "사람 따라가기를 멈추고 확인하러 감",
};
const APPROACH_BLOCKED: Record<string, string> = {
  no_map: "지도 없음", no_path: "갈 길 없음", timeout: "시간 초과", failed: "이동 실패", rejected: "이동 실패",
};
const blocked = (reason: string) => APPROACH_BLOCKED[reason] ?? "이동 실패";
const SPOT = "넘어진 것 같은 자리";

// 예전 확인(회차 없음)의 몸 전체 확인. 묻는 것은 다음 줄 "말벗: 물어봄"이 말한다.
const POSTURE: Record<string, [Who, string]> = {
  sitting_off_floor: ["클라우드 AI", "몸 전체를 보니 침대나 의자에 앉아 있음"],
  standing: ["클라우드 AI", "몸 전체를 보니 서 있음"],
  active_on_floor: ["클라우드 AI", "몸 전체를 보니 바닥에 앉아 활동 중"],
  on_floor: ["클라우드 AI", "몸 전체를 보니 바닥에 있음"],
  body_unseen: ["말벗", "몸 전체를 못 봄 → 이 자리에서 물어봄"],
  judgements_differ: ["말벗", "잰 값과 클라우드 AI 판정이 다름 → 물어봄"],
  posture_unconfirmed: ["클라우드 AI", "자세를 확인하지 못함"],
};

// 목업 28번: 바로 묻지 못한 이유, 1분, 다시 가서 보기, 처음 장면과 비교.
const CHECK_WAITING: Record<string, string> = {
  manual_drive: "확인하러 가려 했지만 수동 조작 중 → 끝나면 확인",
  busy: "확인하러 가려 했지만 다른 일을 하는 중 → 끝나면 확인",
};
const REVISIT_START: Record<string, string> = {
  manual_drive: `수동 조작 끝남 → 묻기 전에 ${SPOT}로 다시 감`,
  busy: `다른 일이 끝남 → 묻기 전에 ${SPOT}로 다시 감`,
};
const CHECK_FAILED: Record<string, string> = {
  speech_unavailable: "음성이 꺼져 있어 물을 수 없음",
  question_error: "질문 중 오류가 나 물을 수 없음",
  time_limit: "막혀서 10분 동안 확인하지 못함 → 확인을 멈춤",
};
// 알림 줄은 "알림: 단계 · 무엇"(푸시 문장과 같은 뜻).
const LEVEL: Record<string, string> = { urgent: "긴급", check: "확인 필요", info: "일반" };
const NOTICE: Record<string, [string, string]> = {
  fall_observed_person_okay: ["info", "넘어졌지만 괜찮다고 함"],
  person_no_response: ["check", "물어봤지만 대답이 없음"],
  check_required_not_confirmed_fall: ["check", "넘어졌는지 확실하지 않음"],
  help_requested: ["urgent", "도움 요청"],
  confirmation_help_required: ["urgent", "도움이 필요해 보임"],
  robot_could_not_confirm: ["urgent", "말벗이 직접 확인하지 못함"],
  scene_answer_unconfirmed: ["check", "괜찮다는 대답, 쓰러진 분인지 모름"],
  check_stopped_by_user: ["check", "'그만'을 듣고 확인을 멈춤"],
  person_may_differ: ["check", "처음 본 분과 다를 수 있음"],
  left_unseen: ["check", "떠나는 모습을 못 봄"],
  could_not_compare: ["check", "그 자리를 볼 수 없음"],
  person_crawled_away: ["check", "바닥을 기어서 옮김"],
  closed_after_alert: ["info", "앞서 알린 상황의 확인을 마침"],
};
const SAME_PERSON: Record<string, string> = {
  same: "같은 분으로 보임", different: "다른 분으로 보임", unclear: "같은 분인지 알 수 없음",
};
const REVISIT_NOTICE: Record<string, string> = {
  revisit_same_person_off_floor: "info",
  revisit_person_unclear_off_floor: "check",
  revisit_nobody_there: "check",
};

// 하나로 합친 확인 순서(SWM25-261, 목업 29~31번). 로봇은 이 확인의 모든 기록에
// checkRound를 붙이고, 같은 사건을 다시 확인할 때마다(recheck_started) 1씩 올린다.
const ARRIVAL: Record<string, string> = {
  same_person: "처음 본 분이 그대로 있음", person_unsure: "사람은 있는데 같은 분인지 모름",
  different_person: "처음 본 분과 다른 분이 있음", same_object: "처음 본 물건이 그대로 있음",
  gone: "처음 본 자리가 비어 있음", not_visible: "처음 본 자리가 안 보임",
};
const DEPARTURE: Record<string, string> = {
  animal_left: "동물이 떠남", person_walked: "사람이 걸어서 떠남", person_crawled: "사람이 기어서 떠남",
  not_seen: "떠나는 모습이 안 보임", failed: "떠났는지 확인하지 못함",
};
const MOMENT: Record<string, string> = {
  fall: "넘어진 것으로 보임", self: "스스로 앉거나 누운 동작으로 보임", unknown: "알 수 없음",
};
const RECHECK: Record<string, string> = {
  stood_up: "일어서는 모습을 봄", floor_recheck: "바닥에서 확인을 마친 지 30분",
  person_seen_again: "사람이 없던 자리에 누워 있는 분이 보임", agent_restart: "말벗 판단 프로그램이 다시 켜짐",
};
const FLOW_POSTURE: Record<string, string> = {
  sitting_off_floor: "침대나 의자에 앉아 있음", standing: "서 있음", active_on_floor: "바닥에 앉아 활동 중",
  on_floor: "바닥에 있음", posture_unconfirmed: "자세를 확인하지 못함",
};
const OFF_FLOOR = new Set(["sitting_off_floor", "standing"]);
// Asking right away: on the floor or not seen well (the robot asks after one look).
const ASKS = new Set(["on_floor", "posture_unconfirmed"]);

/** An incident of the one check: its events carry the check round. */
export const isFlowIncident = (events: FallHistoryEvent[]) => events.some((e) => e.checkRound != null);
const roundOf = (e: FallHistoryEvent) => e.checkRound ?? 1;
/** AI가 쓴 이유는 괄호 안에(사건 상세 안내: "괄호 안 이유는 AI가 쓴 설명"). */
const aiWhy = (e: FallHistoryEvent) => e.analysis?.explanation ? ` (${aiText(e.analysis.explanation)})` : "";

/** What came before in the same round, for the one check's labels. Display only. */
type RoundContext = {
  reverified: boolean; viewedAfterUnsure: boolean; lastArrival: string | null;
  postures: string[]; differ: boolean;
  /** 다음 "말벗: 물어봄" 줄에 붙일 묻는 이유(AI 판정 줄과 말벗 결정 줄을 나눔). */
  askReason: string | null;
};
const newRound = (): RoundContext => ({
  reverified: false, viewedAfterUnsure: false, lastArrival: null, postures: [], differ: false, askReason: null,
});

function flowLines(event: FallHistoryEvent, round: RoundContext): string[] | null {
  const reason = event.reason ?? "";
  const kind = event.eventKind;
  const lastPosture = round.postures.at(-1) ?? null;
  if (kind === "recheck_started") return [line("말벗", `다시 확인 시작 (${RECHECK[reason] ?? "다시 봄"})`)];
  if (kind === "approach_started") return [line("말벗", APPROACH_START[reason] ?? "확인하러 감")];
  if (kind === "approach_completed") {
    if (reason === "arrived") return [line("말벗", `${SPOT} 1 m 앞에 도착 (처음 본 방향)`)];
    if (reason === "no_map") return [line("말벗", "가까이 가지 못함 (지도 없음) → 이 자리에서 물어봄")];
    return [line("말벗", `1 m 앞까지 못 감 (${blocked(reason)}) → 갈 수 있는 자리에서 봄`)];
  }
  if (kind === "arrival_compared") {
    if (!Object.hasOwn(ARRIVAL, reason)) {
      return [line("클라우드 AI", `처음 장면과 비교하지 못함${aiWhy(event)}`), line("말벗", "사람이 있는 것으로 보고 계속")];
    }
    const lines = [line("클라우드 AI", `${ARRIVAL[reason]}${aiWhy(event)}`)];
    if (reason === "person_unsure" && !round.viewedAfterUnsure) lines.push(line("말벗", "몸 전체가 보이는 자리에서 다시 비교"));
    else if ((reason === "gone" || reason === "not_visible") && !round.reverified) lines.push(line("말벗", "처음 본 자리로 가서 다시 봄"));
    else if (reason === "gone") lines.push(line("말벗", "떠났는지 확인"));
    return lines;
  }
  if (kind === "reverify_completed") {
    return [line("말벗", reason === "arrived" ? "처음 본 자리로 가서 같은 방향으로 다시 봄" : `처음 본 자리로 못 감 (${blocked(reason)})`)];
  }
  if (kind === "departure_reviewed") return [line("클라우드 AI", `${DEPARTURE[reason] ?? DEPARTURE.failed}${aiWhy(event)}`)];
  if (kind === "posture_check_completed") {
    const what = FLOW_POSTURE[reason] ?? FLOW_POSTURE.posture_unconfirmed;
    const before = round.postures.at(-2);
    if (ASKS.has(reason) || before === undefined) return [line("클라우드 AI", `몸 전체를 보니 ${what}${aiWhy(event)}`)];
    if (before !== reason && (OFF_FLOOR.has(before) !== OFF_FLOOR.has(reason))) {
      round.askReason = "두 번 본 자세가 달라서";
      return [line("클라우드 AI", `몸 전체를 다시 보니 ${what}${aiWhy(event)}`)];
    }
    const lines = [line("클라우드 AI", `몸 전체를 다시 보니 ${what} (두 번 모두 같음)`)];
    if (round.differ) lines.push(line("말벗", "다른 분이라 묻지 않음 · 사건은 열어 둠"));
    return lines;
  }
  if (kind === "fall_moment_reviewed") {
    if (lastPosture && OFF_FLOOR.has(lastPosture) && reason !== "self") round.askReason = "넘어지는 장면을 봐서 일어났어도";
    else if (lastPosture === "active_on_floor") round.askReason = "넘어지는 장면을 봐서";
    return [line("클라우드 AI", `넘어지는 장면을 다시 보니 ${MOMENT[reason] ?? MOMENT.unknown}`)];
  }
  if (kind === "question_requested") {
    const why = round.askReason;
    round.askReason = null;
    return [line("말벗", `물어봄${why ? ` (${why})` : ""}`)];
  }
  if (kind === "incident_resolved" && (reason === "response_completed" || reason === "risk_cleared")) {
    // The one check looks again 30 minutes after a close anywhere but off the floor.
    return [line("말벗", `확인을 마침${lastPosture && OFF_FLOOR.has(lastPosture) ? "" : " · 30분 뒤 다시 확인"}`)];
  }
  if (kind === "incident_resolved" && reason === "left_the_spot") return [line("말벗", "사건 종료 · 그 자리를 떠남 (묻지 않음)")];
  if (kind === "incident_resolved" && reason === "guardian_closed") return [line("말벗", "보호자가 처리 완료해 이 분을 다시 확인하지 않음")];
  return null;
}

function updateRound(round: RoundContext, event: FallHistoryEvent) {
  const reason = event.reason ?? "";
  if (event.eventKind === "reverify_completed") round.reverified = true;
  if (event.eventKind === "arrival_compared") round.lastArrival = reason;
  if (event.eventKind === "view_completed" && round.lastArrival === "person_unsure") round.viewedAfterUnsure = true;
  if (event.eventKind === "posture_check_completed") round.postures.push(reason);
  if (event.eventKind === "notification_requested" && reason === "person_may_differ") round.differ = true;
}

/** 다시 가서 본 기록(revisit_started 뒤)은 "다시"를 붙인다. 표시 전용. */
function revisitLines(event: FallHistoryEvent): string[] | null {
  const reason = event.reason ?? "";
  if (event.eventKind === "approach_completed") {
    return [line("말벗", reason === "arrived" ? `${SPOT} 1 m 앞에 다시 도착` : `다시 가지 못함 (${blocked(reason)}) → 이 자리에서 물어봄`)];
  }
  if (event.eventKind === "person_check_completed") {
    if (reason === "nobody_there") return [line("클라우드 AI", "가까이에서 다시 보니 그 자리에 사람이 없음")];
    if (reason === "person") return [line("클라우드 AI", "가까이에서 다시 보니 사람")];
    if (reason === "partial_body") return [line("클라우드 AI", "가까이에서 다시 보니 사람 · 몸 일부만 보임")];
    if (reason === "whole_body") return [line("클라우드 AI", "가까이에서 다시 보니 사람 · 몸 전체 보임")];
  }
  return null;
}

/** 말벗이 넘어진 것 같은 자리로 다가가 확인한 기록(예전 확인과 함께 쓰는 줄). 목업 22·25번. */
function approachLines(event: FallHistoryEvent): string[] | null {
  const reason = event.reason ?? "";
  const kind = event.eventKind;
  if (kind === "check_waiting") return [line("말벗", CHECK_WAITING[reason] ?? "확인하러 가려 했지만 아직 갈 수 없음")];
  if (kind === "check_not_confirmed") return [line("말벗", "1분째 확인하지 못함")];
  if (kind === "check_stopped") return [line("말벗", "'그만'이라는 말을 듣고 확인을 멈춤 (묻지 않음)")];
  if (kind === "revisit_started") return [line("말벗", REVISIT_START[reason] ?? `묻기 전에 ${SPOT}로 다시 감`)];
  if (kind === "same_person_compared") {
    return [line("클라우드 AI", `처음 장면과 비교하니 ${SAME_PERSON[reason] ?? SAME_PERSON.unclear}${aiWhy(event)}`)];
  }
  if (kind === "agent_check_failed") {
    // 예전 이유(confirmation_transport_failed, agent_failed)도 질문 중 오류다.
    return [line("말벗", Object.hasOwn(CHECK_FAILED, reason) ? CHECK_FAILED[reason] : CHECK_FAILED.question_error)];
  }
  if (kind === "notification_requested" && Object.hasOwn(REVISIT_NOTICE, reason)) {
    return [line("알림", `${LEVEL[REVISIT_NOTICE[reason]]} · 묻지 않음 · 사건은 열어 둠`)];
  }
  if (kind === "notification_requested") {
    const [defaultLevel, what] = Object.hasOwn(NOTICE, reason) ? NOTICE[reason] : ["", ""];
    const level = LEVEL[event.notificationLevel ?? defaultLevel] ?? "";
    return [line("알림", [level, what].filter(Boolean).join(" · ") || "보냄")];
  }
  if (kind === "voice_result" || kind === "confirmation_completed") {
    // 영상만 본 장면에서 괜찮다는 대답은 누운 분의 대답인지 몰라 answer가 unclear다.
    if (reason === "scene_answer_unassociated") return [line("대답", "괜찮다고 함 · 쓰러진 분인지 모름")];
    if (event.answer === "failed") return [line("말벗", "질문 실패")];
    if (event.answer === "no_response") return [line("대답", "없음")];
    return [line("대답", event.answer ? ANSWER[event.answer] ?? event.answer : "기록 없음")];
  }
  if (kind === "incident_resolved" && reason === "guardian_closed") return [line("말벗", "보호자가 처리 완료해 확인을 멈춤")];
  if (kind === "approach_started") return [line("말벗", `${APPROACH_START[reason] ?? "확인하러 감"} (사람인지 확실하지 않음)`)];
  if (kind === "approach_completed") {
    return [line("말벗", reason === "arrived" ? `${SPOT} 1 m 앞에 도착` : `가까이 가지 못함 (${blocked(reason)}) → 이 자리에서 물어봄`)];
  }
  if (kind === "person_check_completed") {
    if (reason === "person") return [line("클라우드 AI", "가까이에서 보니 사람")];
    if (reason === "partial_body") return [line("클라우드 AI", "가까이에서 보니 사람 · 몸 일부만 보임")];
    if (reason === "whole_body") return [line("클라우드 AI", "가까이에서 보니 사람 · 몸 전체 보임")];
    return [line("클라우드 AI", `가까이에서 보니 사람 아님${aiWhy(event)}`)];
  }
  if (kind === "view_completed") {
    return [line("말벗", reason === "arrived" ? "몸 전체가 보이는 자리로 옮김" : `몸 전체가 보이는 자리로 못 감 (${blocked(reason)})`)];
  }
  if (kind === "posture_check_completed") {
    const [who, what] = POSTURE[reason] ?? ["클라우드 AI", "몸 전체를 봄"];
    return [line(who, `${what}${who === "클라우드 AI" ? aiWhy(event) : ""}`)];
  }
  if (kind === "approach_returned") return [line("말벗", reason === "returned" ? "원래 자리로 돌아옴" : "원래 자리로 돌아가지 못함")];
  if (kind === "incident_resolved" && reason === "not_a_person") return [line("말벗", "사건 종료 · 사람 아님")];
  if (kind === "incident_resolved" && reason === "off_floor_verified") return [line("말벗", "사건 종료 · 바닥에 있지 않음 (묻지 않음)")];
  if (kind === "incident_resolved" && reason === "floor_activity_verified") {
    return [line("말벗", "사건 종료 · 바닥에 앉아 활동 중 (묻지 않음)")];
  }
  return null;
}

const cloudOpening = (event: FallHistoryEvent) =>
  (event.eventKind === "incident_opened" || event.eventKind === "incident_updated") &&
  (event.reason === "cloud_crosscheck" || event.reason === "target_unidentified");

function genericLine(event: FallHistoryEvent): string {
  // A request is not proof that audio played. No hard-coded speech transcript.
  if (event.eventKind === "question_requested") return line("말벗", "물어봄");
  const opening = event.eventKind === "incident_opened" || event.eventKind === "incident_updated";
  const poseReason = event.reason && Object.hasOwn(POSE_REASON, event.reason) ? POSE_REASON[event.reason] : undefined;
  if (opening && poseReason) {
    // Pose creates a suspicion, not a VLM classification. A carried-over old
    // assessment/answer must not label a newly observed Pose candidate.
    return line("말벗", `낙상 의심 발견 (자세 분석: ${poseReason})`);
  }
  if (cloudOpening(event)) {
    const what = ASSESSMENT[event.assessment ?? ""] ?? "낙상 의심";
    return line("클라우드 AI", event.eventKind === "incident_opened" ? `${what} 발견` : `${what} · 근거 갱신`);
  }
  // The incident can retain a stronger past judgment. Show THIS request's
  // classification beside its explanation, not that retained judgment.
  if (event.eventKind === "analysis_completed") {
    const assessment = event.analysis?.assessment ?? event.assessment ?? "";
    const why = event.analysis?.explanation ? aiText(event.analysis.explanation) : "AI 이유 없음";
    return line("클라우드 AI", `${ASSESSMENT[assessment] ?? (assessment || "판정")} (${why})`);
  }
  if (Object.hasOwn(CLOUD_EVENT, event.eventKind)) return line("클라우드 AI", CLOUD_EVENT[event.eventKind]);
  if (event.eventKind === "notification_requested") return line("알림", "보냄");
  let what = EVENT[event.eventKind] ?? event.eventKind;
  if (event.assessment) what += ` · ${ASSESSMENT[event.assessment] ?? event.assessment}`;
  if (!opening && event.answer) what += ` (${ANSWER[event.answer] ?? event.answer})`;
  return line("말벗", what);
}

function eventLines(event: FallHistoryEvent, round: RoundContext | null, revisit: boolean): string[] {
  return (round ? flowLines(event, round) : null) ?? (revisit ? revisitLines(event) : null) ??
    approachLines(event) ?? [genericLine(event)];
}

/** 한 기록의 줄(여러 줄이면 " / "로 이음). 표시 전용. */
export function fallEventLabel(event: FallHistoryEvent): string {
  return eventLines(event, event.checkRound != null ? newRound() : null, false).join(" / ");
}

/**
 * History lines in order (목업 28번): after revisit_started the close look reads
 * "다시", and the alert right after "1분째 확인하지 못함" or a failed question
 * joins that line instead of repeating it. Each line reads "누가: 무엇"; an AI
 * judgment and the robot's decision after it are separate lines. Display only.
 */
export function fallHistoryLines<T extends FallHistoryEvent>(events: T[]):
  Array<{ event: T; label: string; divider?: string }> {
  const lines: Array<{ event: T; label: string; divider?: string }> = [];
  const flow = isFlowIncident(events);
  // 목업 31번: a recheck starts the next round; rounds are marked only when there is more than one.
  const rounds = flow && events.some((e) => e.eventKind === "recheck_started");
  let revisit = false, round = newRound();
  for (const event of events) {
    const previous = lines.at(-1);
    // 클라우드 AI가 찾은 사건: 발견 줄과 바로 뒤 같은 판정의 이유를 한 줄로.
    if (event.eventKind === "analysis_completed" && previous && cloudOpening(previous.event) &&
        previous.event.eventKind === "incident_opened" && event.analysis?.explanation &&
        (event.analysis.assessment ?? event.assessment) === (previous.event.assessment ?? event.analysis.assessment)) {
      previous.label += ` (${aiText(event.analysis.explanation)})`;
      continue;
    }
    if (event.eventKind === "notification_requested" && event.reason === "robot_could_not_confirm" && previous) {
      const kind = previous.event.eventKind, reason = previous.event.reason ?? "";
      if (kind === "check_not_confirmed") { previous.label += " → 긴급 알림"; continue; }
      if (kind === "agent_check_failed" && Object.hasOwn(CHECK_FAILED, reason)) {
        previous.label += reason === "time_limit" ? " · 긴급 알림" : " → 바로 긴급 알림";
        continue;
      }
    }
    let divider: string | undefined;
    if (flow) {
      divider = rounds && (lines.length === 0 || event.eventKind === "recheck_started") ? `${roundOf(event)}회차` : undefined;
      if (event.eventKind === "recheck_started") round = newRound();
      updateRound(round, event);
    } else if (event.eventKind === "revisit_started") revisit = true;
    eventLines(event, flow ? round : null, revisit).forEach((label, index) => {
      lines.push({ event, label, ...(divider && index === 0 ? { divider } : {}) });
    });
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
  waited_manual_drive: "수동 조작 중이라 1분 안에 묻지 못함",
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
      : `${explanation ? `AI가 처음 장면과 옷차림을 비교했어요(참고): ${aiText(explanation)}.`
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

/** 말벗이 확인한 결과(목업 29~31번): 하나로 합친 확인의 마지막 회차. Display only. */
export type FallCheckResult = {
  round: number;
  /** 사건 화면의 결과 줄(회차는 앞에 붙임). */
  title: string;
  note: string | null;
  /** 목록 부제목의 짧은 결과. */
  summary: string;
  /** 아직 확인하는 중이면 지금 하는 일, 아니면 null. */
  looking: string | null;
};

const LOOKING_NEXT: Record<string, string> = {
  recheck_started: "다시 확인하러 가는 중", approach_started: "가까이 가는 중",
  approach_completed: "처음 장면과 비교하는 중", reverify_completed: "처음 장면과 비교하는 중",
  view_completed: "몸 전체를 보는 중", question_requested: "묻는 중",
};
const NOTE = {
  floorOkay: "바닥에서 확인을 마친 분은 30분 뒤에 다시 가서 봐요. 그 전에 일어서는 모습을 보면 그때 다시 봐요.",
  fallAsked: "넘어지는 장면을 봤으면 지금 서 있거나 의자에 앉아 있어도 물어봐요. AI가 그 장면을 스스로 앉거나 누운 동작으로 볼 때만 묻지 않아요.",
  offFloor: "바닥이 아닌 곳에 있는 걸 두 번 확인해서 묻지 않았어요. 나중에 바닥에 누우면 다시 확인해요.",
  floorActive: "바닥에서 활동하는 걸 두 번 확인해서 묻지 않았어요. 바닥에 있는 동안은 30분마다 다시 봐요.",
  object: "처음 본 것과 같은 물건이 그대로 있어서 사람 아님으로 닫았어요. 그 물건은 계속 보이는 동안 다시 묻지 않고, 그 자리에 새로 무언가 보이면 처음부터 다시 봐요.",
  animal: "처음 본 것이 사람이 아니라 동물이었어요. 사람 아님으로 닫았어요.",
  walked: "스스로 일어나 걸어간 모습이 보여서 말벗이 닫았어요. 그분이 다른 곳에서 다시 누우면 새로 확인해요.",
  crawled: "기어서 옮겼다면 아직 일어나지 못한 거예요. 말벗은 사건을 열어 두고, 근처에서 누워 있는 분이 보이면 다시 확인해요.",
  unseen: "일어나서 갔을 수도 있지만, 기어서 옮겼거나 다른 곳에서 다시 쓰러졌을 수도 있어요. 그래서 \"사람 아님\"으로 닫지 않아요.",
  dark: "1 m 앞과 처음 본 자리에서 모두 그 자리를 볼 수 없었어요. 말벗은 사건을 열어 두고, 그 자리에서 사람이 다시 보이면 처음부터 다시 확인해요.",
  differ: "처음 본 분과 옷차림이 달라 보여요. 처음 본 분을 확인하지 못했으니 말벗은 사건을 닫지 않아요. 그 근처에서 다시 누워 있는 분이 보이면 처음부터 다시 확인해요.",
  open: "말벗은 이 사건을 스스로 닫지 않아요.",
  guardian: "보호자가 처리 완료하면 말벗은 이 분의 30분 재확인과 일어섬 확인을 멈춰요. 그 뒤에 다시 바닥에 누우면 처음 거르기부터 새 사건으로 확인해요.",
};

/** Events in sequence order. Null for the older check (no check round on its events). */
export function fallCheckResult(events: FallHistoryEvent[]): FallCheckResult | null {
  if (!isFlowIncident(events)) return null;
  const start = events.map((e) => e.eventKind).lastIndexOf("recheck_started");
  const mine = start < 0 ? events : events.slice(start);
  const round = Math.max(...events.map(roundOf));
  const ctx = newRound();
  let outcome: { title: string; note: string | null; summary?: string } | null = null;
  let moment: string | null = null, answer: string | null = null, last: FallHistoryEvent | null = null;
  const posture = () => {
    const p = ctx.postures.at(-1);
    return p ? FLOW_POSTURE[p] ?? "자세를 확인하지 못함" : null;
  };
  for (const e of mine) {
    updateRound(ctx, e);
    last = e;
    const reason = e.reason ?? "";
    const p = ctx.postures.at(-1) ?? null;
    if (e.eventKind === "fall_moment_reviewed") moment = reason;
    if (e.eventKind === "confirmation_completed" || e.eventKind === "voice_result") answer = e.answer ?? answer;
    if (e.eventKind === "incident_resolved") {
      if (reason === "guardian_closed") outcome = { title: "처리 완료 뒤에는 이 사건을 다시 열지 않음", note: NOTE.guardian };
      else if (reason === "not_a_person") {
        outcome = ctx.lastArrival === "same_object"
          ? { title: "처음 본 물건이 그대로 있음 · 사람 아님 → 묻지 않음", note: NOTE.object, summary: "사람 아님" }
          : { title: "동물이 떠남 · 사람 아님 → 묻지 않음", note: NOTE.animal, summary: "동물이 떠남" };
      } else if (reason === "left_the_spot") {
        outcome = { title: "그 자리에 없음 · 걸어서 떠나는 모습이 보임 → 묻지 않음", note: NOTE.walked,
          summary: "그 자리를 걸어서 떠남" };
      } else if (reason === "off_floor_verified") {
        outcome = { title: `${posture() ?? "바닥이 아닌 곳에 있음"}${moment === "self" ? " · 스스로 앉은 동작으로 보임" : ""} → 묻지 않음`,
          note: NOTE.offFloor };
      } else if (reason === "floor_activity_verified") {
        outcome = { title: "바닥에 앉아 활동 중 → 묻지 않음", note: NOTE.floorActive };
      } else if (reason === "response_completed" || reason === "risk_cleared") {
        const off = p !== null && OFF_FLOOR.has(p);
        outcome = { title: `${posture() ?? "자세를 확인하지 못함"}${off ? " · 넘어진 것으로 보여 물어봄" : ""} · 괜찮다고 답함`,
          note: off ? NOTE.fallAsked : NOTE.floorOkay, summary: `${posture() ?? "물어봄"} · 괜찮다고 답함` };
      }
    } else if (e.eventKind === "confirmation_completed" && e.answer === "help_request") {
      outcome = { title: `${posture() ?? "물어봄"} · 도움 필요`, note: NOTE.open };
    } else if (e.eventKind === "voice_result" && e.answer === "no_response") {
      outcome = { title: `${posture() ?? "물어봄"} · 대답 없음`, note: NOTE.open };
    } else if (e.eventKind === "agent_check_failed" && Object.hasOwn(FAILED_REASON, reason)) {
      outcome = { title: FAILED_REASON[reason][0], note: NOTE.open, summary: FAILED_REASON[reason][1] };
    } else if (e.eventKind === "check_stopped") {
      outcome = { title: "'그만'이라는 말을 듣고 확인을 멈춤", note: NOTE.open };
    } else if (e.eventKind === "check_not_confirmed") {
      outcome = { title: WAITED_REASON[reason] ?? WAITED_REASON.waited_busy, note: NOTE.open, summary: "1분째 확인 못 함" };
    } else if (e.eventKind === "notification_requested") {
      if (reason === "person_crawled_away") {
        outcome = { title: "그 자리에 없음 · 바닥을 기어서 옮긴 것으로 보임", note: NOTE.crawled };
      } else if (reason === "left_unseen") {
        outcome = { title: "그 자리에 없음 · 떠나는 모습은 보지 못함", note: NOTE.unseen };
      } else if (reason === "could_not_compare") {
        outcome = { title: "그 자리를 볼 수 없음 (어둡거나 가려짐)", note: NOTE.dark };
      } else if (reason === "scene_answer_unconfirmed") {
        outcome = { title: "괜찮다는 답 · 쓰러진 분인지 모름", note: NOTE.open };
      }
    } else if (e.eventKind === "posture_check_completed" && ctx.differ && ctx.postures.length >= 2 &&
        OFF_FLOOR.has(ctx.postures.at(-2)!) === OFF_FLOOR.has(reason) && !ASKS.has(reason)) {
      outcome = { title: `다른 분이 ${posture()} · 처음 본 분은 아직 확인하지 못함`, note: NOTE.differ,
        summary: `가 보니 다른 분이 ${posture()} · 처음 본 분은 확인 못 함` };
    }
  }
  // A different person who answered they were fine: the first person is still unknown.
  if (ctx.differ && answer === "okay" && !outcome) {
    outcome = { title: "다른 분이 괜찮다고 답함 · 처음 본 분은 아직 확인하지 못함", note: NOTE.differ };
  }
  const prefix = round > 1 ? `${round}회차: ` : "";
  if (outcome) {
    return { round, title: `${prefix}${outcome.title}`, note: outcome.note,
      summary: outcome.summary ?? outcome.title.replace(/ → .*$/, ""), looking: null };
  }
  let looking = last ? LOOKING_NEXT[last.eventKind] ?? null : null;
  if (last?.eventKind === "arrival_compared" || last?.eventKind === "posture_check_completed" ||
      last?.eventKind === "fall_moment_reviewed" || last?.eventKind === "departure_reviewed") looking = "확인하는 중";
  if (last?.eventKind === "check_waiting") looking = null;
  return { round, title: `${prefix}확인하는 중`, note: null, summary: looking ?? "확인하는 중", looking };
}
