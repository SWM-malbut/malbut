/**
 * 목업 24번 · 켤 때 마지막 지도 불러오기와 위치 확인 (2026-10-08).
 * The robot reports `target.mapCheck`: where its pose check on the saved map stands
 * (loading, locating, retrying, ok, low, none), the share of the scan that matches the
 * map, and whether it loaded the last chosen map by itself at start.
 */

export type MapCheck = {
  phase: "loading" | "locating" | "retrying" | "ok" | "low" | "none";
  map: string;
  match: number | null;
  auto: boolean;
  retried: boolean;
};

export type MapCheckCard = {
  title: string;
  note: string;
  tone: "info" | "warn";
  /** The robot cannot drive yet: shown before any destination state. */
  blocking: boolean;
  relocalize: boolean;
  manage: boolean;
};

const PHASES = new Set(["loading", "locating", "retrying", "ok", "low", "none"]);
// The robot searches once more below this share (malbut_bringup map_check.MATCH_MIN).
const MATCH_MIN = 0.75;
const SPIN = "제자리에서 한 바퀴 돌 수 있어요.";

export function readMapCheck(target: unknown): MapCheck | null {
  const value = target && typeof target === "object" ? (target as Record<string, unknown>).mapCheck : null;
  if (!value || typeof value !== "object") return null;
  const check = value as Record<string, unknown>;
  if (typeof check.phase !== "string" || !PHASES.has(check.phase)) return null;
  return {
    phase: check.phase as MapCheck["phase"],
    map: typeof check.map === "string" ? check.map : "",
    match: typeof check.match === "number" && Number.isFinite(check.match) ? check.match : null,
    auto: check.auto === true,
    retried: check.retried === true,
  };
}

function percent(match: number) {
  return `${Math.round(match * 100)}%`;
}

/** The status card's title, note and buttons; null when the pose fits (the usual card). */
export function mapCheckCard(check: MapCheck | null, mapName: string): MapCheckCard | null {
  if (!check) return null;
  const card = { blocking: true, relocalize: false, manage: false } as const;
  if (check.phase === "none") {
    return {
      ...card, tone: "warn", manage: true, title: "저장 지도를 고르지 않았어요",
      note: "지도 관리에서 쓸 지도를 골라 주세요. 고르기 전까지는 빈 지도라 목적지 보내기·순찰을 쓸 수 없어요.",
    };
  }
  if (check.phase === "loading") {
    return {
      ...card, tone: "info", title: "마지막에 쓴 지도를 불러오고 있어요",
      note: `'${mapName}' 지도에서 말벗의 위치를 찾고 있어요. 저장된 위치가 맞지 않으면 ${SPIN}`,
    };
  }
  if (check.phase === "locating") {
    return {
      ...card, tone: "info", title: "저장 지도에서 말벗의 위치를 찾고 있어요",
      note: `저장된 위치가 맞지 않으면 ${SPIN} 그동안 목적지 보내기·순찰은 잠시 쓸 수 없어요.`,
    };
  }
  if (check.phase === "retrying") {
    return {
      ...card, tone: "info", title: "위치를 한 번 더 확인하고 있어요",
      note: check.match !== null && check.match < MATCH_MIN
        ? `지도와 맞는 정도가 낮아(${percent(check.match)}) 위치를 다시 찾고 있어요. ${SPIN}`
        : `위치를 다시 찾고 있어요. ${SPIN}`,
    };
  }
  if (check.phase === "low") {
    return {
      blocking: false, relocalize: true, manage: true, tone: "warn", title: "지도와 주변이 잘 맞지 않아요",
      note: check.match !== null
        ? `${check.retried ? "두 번 찾았지만 " : ""}위치가 지도와 ${percent(check.match)}만 맞아요. ` +
          "가구를 옮겼거나 다른 곳이면 지도가 맞지 않을 수 있어요. 이대로 보내면 말벗이 엉뚱하게 움직일 수 있어요."
        : "말벗의 위치를 찾지 못했어요. 주변을 비우고 위치 다시 찾기를 누르거나 지도 관리에서 지도를 확인해 주세요.",
    };
  }
  return null;
}

/** 쓰는 지도 · 위치 확인 rows of the status card. */
export function mapCheckFacts(check: MapCheck, mapName: string) {
  if (check.phase === "none") return { map: "없음 (빈 기본 지도)", match: "—", tone: "" };
  const map = check.auto ? `${mapName} (켤 때 자동으로 불러옴)` : mapName;
  if (check.phase === "loading" || check.phase === "locating") return { map, match: "찾는 중", tone: "" };
  if (check.phase === "retrying") {
    return { map, match: check.match === null ? "다시 찾는 중" : `${percent(check.match)} · 다시 찾는 중`, tone: "" };
  }
  if (check.phase === "low") {
    return { map, match: check.match === null ? "찾지 못함" : percent(check.match), tone: "is-error is-strong" };
  }
  return { map, match: check.match === null ? "확인됨" : percent(check.match), tone: "is-ok" };
}

/** The 자율주행 card's line while the pose is not settled on a saved map. */
export function mapCheckDriveHint(check: MapCheck | null): string | null {
  if (!check || check.phase === "ok") return null;
  if (check.phase === "none") return "저장 지도를 고르면 시작할 수 있어요.";
  if (check.phase === "low") return "위치를 다시 찾거나 지도를 확인한 뒤 시작해 주세요.";
  return "위치를 찾는 동안은 잠시 쓸 수 없어요.";
}
