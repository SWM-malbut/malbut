/**
 * 목업 23번 · 목적지 이동 실패와 금지 구역에서 빠져나오기 (2026-10-08).
 * The robot names why a destination drive ended (`reason`); the owner reads one plain line.
 */

export const NAVIGATION_FAILED_TITLE = "목적지까지 가지 못했어요";
export const NAVIGATION_ESCAPING_TITLE = "금지 구역 밖으로 먼저 나가고 있어요";
export const NAVIGATION_ESCAPING_NOTE = "말벗이 금지 구역 안에 있다고 판단돼, 구역 밖으로 천천히 나간 뒤 목적지로 가요.";

const FAILURES: Record<string, string> = {
  blocked_start: "주변이 막혀 출발하지 못했어요. 말벗 둘레의 물건을 치우거나 말벗을 조금 옮긴 뒤 다시 보내 주세요.",
  zone_stuck: "금지 구역에서 나갈 길이 막혔어요. 직접 움직이기로 말벗을 구역 밖으로 옮긴 뒤 다시 보내 주세요.",
  manual_drive: "직접 움직이기 중이라 출발하지 못했어요.",
  fall_check: "낙상 확인 중이라 출발하지 못했어요.",
  fall_check_started: "낙상 확인이 시작돼 이동을 멈췄어요.",
  localization_lost: "말벗이 지도에서 위치를 잃었어요. 위치 다시 찾기를 한 뒤 보내 주세요.",
};

/** The reason line under 목적지까지 가지 못했어요; an unknown reason still says what to do. */
export function navigationFailureCopy(navigation: Record<string, unknown> | null | undefined): string {
  const reason = typeof navigation?.reason === "string" ? navigation.reason : "";
  if (reason === "blocked_way") {
    const left = navigation?.distance_remaining_m;
    return typeof left === "number" && Number.isFinite(left) && left > 0
      ? `가는 길이 막혀 멈췄어요. 목적지까지 ${left.toFixed(1)}m 남았어요.`
      : "가는 길이 막혀 멈췄어요.";
  }
  return FAILURES[reason] ?? "다시 보내 주세요.";
}
