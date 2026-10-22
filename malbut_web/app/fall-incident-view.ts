// 사건 목록·상세·홈 카드가 같이 쓰는 사건 표시(웹 용어 3번, 목업 34~36번). Display only:
// 무엇도 로봇의 자동 판정이나 사건 묶기를 바꾸지 않는다.
import type { FallCheckResult, FallRobotCheck } from "./fall-event-label";

export type OpinionLabel = "fall" | "suspected_fall" | "normal";
export type SceneState = "preparing" | "available" | "partial" | "unavailable" | "expired";

export type IncidentSummary = {
  incidentId: string; origin: "robot" | "user_report";
  category: "check" | "normal" | "report" | "merged" | "not_person" | "off_floor";
  mergedIntoIncidentIds?: string[];
  state: string | null; fallSeen: boolean; assessment: string | null; answer: string | null;
  notificationRank: number; occurredAt: string; updatedAt: string; reviewState: "open" | "closed";
  closedAt: string | null; closedBy: string | null; closedByName: string | null; reopenedAt: string | null;
  needsCheck: boolean; aiFailed: boolean; unacknowledged: boolean; reviewPending: boolean;
  foundDown: boolean; reportedBy: string | null; reportedByName: string | null; reportedMomentAt: string | null;
  opinionCounts: Partial<Record<OpinionLabel, number>>;
  sceneState?: SceneState | null; linkedCount?: number;
  notification?: { level: string; sent: number; total: number } | null;
  /** 말벗이 다가가 확인한 마지막 단계(목업 22·25번). */
  approach?: { kind: string; reason: string | null } | null;
  /** 사람에게 물었거나 알렸음: 말벗이 보는 중이 아님. */
  asked?: boolean;
  /** 말벗이 직접 확인하지 못함(목업 28번): 이유, 다시 가서 본 결과. */
  robotCheck?: FallRobotCheck | null;
  /** 하나로 합친 확인의 결과(목업 29~31번). 예전 확인의 사건은 null. */
  checkResult?: FallCheckResult | null;
  /** 말벗이 닫은 마지막 이유. */
  closeReason?: string | null;
};

export const clock = (value: string, seconds = false) => new Date(value).toLocaleTimeString("ko-KR",
  { hour: "2-digit", minute: "2-digit", ...(seconds ? { second: "2-digit" } : {}), hour12: false });
export function dayLabel(value: string) {
  const date = new Date(value), today = new Date();
  const yesterday = new Date(today.getTime() - 86_400_000);
  if (date.toDateString() === today.toDateString()) return "오늘";
  if (date.toDateString() === yesterday.toDateString()) return "어제";
  return date.toLocaleDateString("ko-KR", { month: "long", day: "numeric" });
}
export const when = (value: string) => `${dayLabel(value)} ${clock(value)}`;

/** 날짜 필터(SWM25-60). 오늘·어제와 같은 휴대폰 날짜 기준이다. */
export type IncidentDay = "today" | "yesterday" | "week";
/** 그 날짜의 [from, to). 최근 7일은 오늘을 포함한 7일. */
export function dayRange(day: IncidentDay, now = new Date()) {
  const midnight = (days: number) =>
    new Date(now.getFullYear(), now.getMonth(), now.getDate() + days).toISOString();
  if (day === "today") return { from: midnight(0), to: midnight(1) };
  if (day === "yesterday") return { from: midnight(-1), to: midnight(0) };
  return { from: midnight(-6), to: midnight(1) };
}

export const ANSWER_LABEL: Record<string, string> = {
  help_request: "본인이 도움을 요청함", okay: "괜찮다고 답함", unclear: "답이 불분명", no_response: "무응답", failed: "질문 실패",
};

/** 말벗이 보고 있는 단계(목업 25번). 묻기 전에는 "낙상 의심"이라는 말을 쓰지 않는다. */
const LOOKING: Record<string, string> = {
  approach_started: "가까이 가는 중", "approach_completed:arrived": "가까이에서 보는 중",
  "person_check_completed:partial_body": "몸 전체가 보이는 자리로 옮기는 중", "view_completed:arrived": "몸 전체를 보는 중",
  "person_check_completed:whole_body": "몸 전체를 보는 중",
};

export function looking(i: IncidentSummary) {
  if (i.checkResult) return i.reviewState === "open" && i.category === "check" ? i.checkResult.looking : null;
  const a = i.approach;
  if (i.reviewState !== "open" || i.category !== "check" || !a || i.asked) return null;
  return LOOKING[a.kind] ?? LOOKING[`${a.kind}:${a.reason}`] ?? null;
}

/** 처리 완료: 보호자가 닫았거나 말벗이 스스로 닫음(사람 아님·바닥에 있지 않음·다른 사건에 병합). */
export const incidentClosed = (i: IncidentSummary) =>
  i.reviewState === "closed" || i.category === "not_person" || i.category === "off_floor" || i.category === "merged";

const OFF_FLOOR_TITLE: Record<string, string> = { standing: "서 있음", active_on_floor: "바닥에 앉아 활동 중" };

/** 제목 = 지금 알고 있는 결론(목업 36번). 결론 전이면 낙상·낙상 의심, 말벗이 보는 중이면 지금 단계. */
export function incidentTitle(i: IncidentSummary) {
  if (i.origin === "user_report") return "놓친 낙상 신고";
  if (i.category === "merged") return "다른 사건에 병합됨";
  if (i.category === "not_person") return i.checkResult?.summary ?? "사람 아님";
  if (i.category === "off_floor") {
    if (i.checkResult?.summary) return i.checkResult.summary;
    if (i.closeReason === "left_the_spot") return "그 자리를 떠남";
    return OFF_FLOOR_TITLE[i.approach?.reason ?? ""] ?? "침대나 의자에 앉아 있음";
  }
  const step = looking(i);
  if (step) return step;
  if (i.category === "normal") return i.answer ? ANSWER_LABEL[i.answer] ?? "정상 활동" : "정상 활동";
  return i.fallSeen || i.assessment === "observed_fall" ? "낙상" : "낙상 의심";
}

const POSTURE_TEXT: Record<string, string> = {
  on_floor: "몸 일부만 보여 가서 확인 · 바닥에 있음", body_unseen: "몸 일부만 보여 가서 확인 · 몸 전체를 못 봄",
  judgements_differ: "몸 일부만 보여 가서 확인 · 판단이 엇갈림",
  posture_unconfirmed: "몸 일부만 보여 가서 확인 · 자세를 확인하지 못함",
};

/** 말벗이 한 일(목업 22·25번). 결론과 지금 단계는 제목이 말한다. */
function robotAction(i: IncidentSummary) {
  if (i.category === "not_person") return "말벗이 가서 확인 · 묻지 않음";
  if (i.category === "off_floor") return i.checkResult ? "말벗이 가서 확인 · 묻지 않음" : "몸 일부만 보여 가서 확인 · 묻지 않음";
  if (looking(i)) return null;
  const a = i.approach;
  if (a?.kind === "person_check_completed") return a.reason === "not_a_person" ? "말벗이 가서 확인 · 사람 아님" : "말벗이 가서 확인 · 사람";
  if (a?.kind === "approach_completed" && a.reason !== "arrived") return "가까이 가지 못함";
  if (a?.kind === "view_completed" && a.reason !== "arrived") return "몸 일부만 보여 가서 확인 · 몸 전체를 못 봄";
  if (a?.kind === "posture_check_completed") return POSTURE_TEXT[a.reason ?? ""] ?? null;
  return null;
}

/** 부제 = 언제 · 재확인 · N회차 · 말벗이 한 일 · 답이나 이유(목업 36번). AI 판정 실패는 아래 줄에. */
export function incidentSubtitle(i: IncidentSummary) {
  if (i.origin === "user_report") {
    return `${when(i.reportedMomentAt ?? i.occurredAt)} 구간 · ${i.reportedByName ?? "사용자"} 신고`;
  }
  // 정상으로 끝난 대답은 제목에 올라가서, 부제는 말벗이 물었다는 것만.
  const answerInTitle = i.category === "normal" && Boolean(i.answer);
  const check = i.robotCheck;
  const reason = check?.stopped ? "막혀서 10분 동안 확인하지 못해 멈춤"
    : check?.failed ? check.failed
      : answerInTitle ? "말벗이 물어봄"
        : i.answer ? ANSWER_LABEL[i.answer] ?? null : i.foundDown ? "이미 쓰러진 모습 발견" : null;
  if (i.checkResult) {
    // 목업 29~31번: 회차 · 이번 회차의 결과(결론·단계가 제목이면 말벗이 한 일만).
    const round = i.checkResult.round > 1 ? `재확인 · ${i.checkResult.round}회차` : null;
    const inTitle = Boolean(looking(i)) || i.category === "not_person" || i.category === "off_floor";
    const result = answerInTitle ? "말벗이 물어봄" : inTitle ? robotAction(i) : i.checkResult.summary;
    return [when(i.occurredAt), round, result].filter(Boolean).join(" · ");
  }
  // 목업 28번: 1분째 확인 못 함 · 다시 가 보니 무엇이 있었는지.
  return [when(i.occurredAt), check?.waited ? "1분째 확인 못 함" : null, check?.revisit?.subtitle ?? robotAction(i),
    reason].filter(Boolean).join(" · ");
}

/** 배지 모양: 화면마다 이 이름에 맞는 색을 쓴다. */
export type BadgeTone = "is-alert" | "is-info" | "is-urgent" | "is-check" | "is-ok" | "is-report" | "is-neutral" | "is-step";

/**
 * 위 배지(목업 35·36번): 열린 사건은 상태 1개 + 처리 1개, 닫힌 사건은 "처리 완료" 하나.
 * 처리: 의견 0개 검토 전 → 의견 있음 처리 전(의견끼리 다르면 의견 다름) → 처리 완료.
 */
export function incidentBadges(i: IncidentSummary): Array<[BadgeTone, string]> {
  if (incidentClosed(i)) return [["is-neutral", "처리 완료"]];
  const status: [BadgeTone, string] = i.unacknowledged ? ["is-alert", "응답 없음"]
    : looking(i) ? ["is-info", "확인 중"]
      // 말벗이 직접 확인하지 못하면 언제나 긴급 알림을 보낸다(목업 28번).
      : i.robotCheck ? ["is-urgent", "긴급"]
        : i.category === "normal" ? ["is-ok", "정상"]
          : i.category === "report" ? ["is-report", "직접 신고"]
            : ["is-check", "확인 필요"];
  const labels = Object.entries(i.opinionCounts).filter(([, count]) => (count ?? 0) > 0);
  const step: [BadgeTone, string] = labels.length === 0 ? ["is-step", "검토 전"]
    : labels.length > 1 ? ["is-step", "의견 다름"] : ["is-step", "처리 전"];
  return [status, step];
}

/** 아래 줄에 덧붙는 것(영상 상태 뒤): 알림 횟수 · 다시 열림 · AI 판정 실패. */
export function incidentExtras(i: IncidentSummary): string[] {
  const list: string[] = [];
  // Same count as 알림 이력: this send / all sends of the level.
  if (!incidentClosed(i) && i.notification && (i.unacknowledged || i.notification.sent > 1)) {
    list.push(`알림 ${i.notification.sent}/${i.notification.total}`);
  }
  if (i.reopenedAt && i.reviewState === "open") list.push("다시 열림");
  if (i.aiFailed) list.push("AI 판정 실패");
  return list;
}
