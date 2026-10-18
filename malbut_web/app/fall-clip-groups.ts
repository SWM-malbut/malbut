// 사건 화면 장면 구간 버튼 (SWM25-268, 목업 53번): 확인 · 발견 · 재감지 N회 · 재확인 · N회차.
// A robot from before SWM25-268 sends no check ranges: its incidents keep one button per range.

import { clock } from "./fall-incident-view";

export type GroupKind = "found" | "check" | "redetect" | "recheck";

export type GroupClip = { segmentIndex: number; startAt: string; endAt: string; anchorKinds: string[];
  checkRound?: number | null };

export type ClipGroup<C extends GroupClip = GroupClip> = {
  kind: GroupKind;
  /** The check round of a recheck group, else null. */
  round: number | null;
  /** In time order; a check longer than about two minutes is several ranges back to back. */
  clips: C[];
};

const start = (clip: GroupClip) => Date.parse(clip.startAt);

/**
 * The incident's ranges by what they show, or null for ranges from an older robot.
 * check: the first suspicion until the judgment. found: the first suspicion alone, when the
 * robot waited (직접 움직이기 or another job) before checking. recheck: one later round.
 * redetect: everything else, the person seen again while the incident was open.
 */
export function clipGroups<C extends GroupClip>(clips: C[]): ClipGroup<C>[] | null {
  const ordered = [...clips].sort((a, b) => start(a) - start(b) || a.segmentIndex - b.segmentIndex);
  const isCheck = (c: GroupClip) => c.anchorKinds.includes("check");
  const isRecheck = (c: GroupClip) => c.anchorKinds.includes("recheck");
  if (!ordered.some((c) => isCheck(c) || isRecheck(c))) return null;
  const firstCheck = ordered.findIndex(isCheck);
  const groups: ClipGroup<C>[] = [];
  let redetect: ClipGroup<C> | null = null;
  for (const [i, clip] of ordered.entries()) {
    const kind: GroupKind = isRecheck(clip) ? "recheck" : isCheck(clip) ? "check"
      : firstCheck > i ? "found" : "redetect";
    const round = kind === "recheck" ? clip.checkRound ?? null : null;
    if (kind === "redetect") {
      if (!redetect) groups.push(redetect = { kind, round: null, clips: [] });
      redetect.clips.push(clip);
      continue;
    }
    const last = groups[groups.length - 1];
    if (last && last.kind === kind && last.round === round) last.clips.push(clip);
    else groups.push({ kind, round, clips: [clip] });
  }
  return groups;
}

/** The button: 확인 · 21:31:25, 재감지 10회 · 21:33~21:48, 재확인 · 2회차 · 22:01:10. */
export function groupLabel(group: ClipGroup) {
  const first = group.clips[0], last = group.clips[group.clips.length - 1];
  if (group.kind === "redetect") {
    return `재감지 ${group.clips.length}회 · ${clock(first.startAt)}~${clock(last.startAt)}`;
  }
  if (group.kind === "recheck") return `재확인 · ${group.round ?? "?"}회차 · ${clock(first.startAt, true)}`;
  return `${group.kind === "found" ? "발견" : "확인"} · ${clock(first.startAt, true)}`;
}

/** Under the video: what part of the incident this group's video is. */
export function groupNote(group: ClipGroup, groups: ClipGroup[]) {
  if (group.kind === "found") return "의심 시점 10초 전 ~ 20초 후";
  if (group.kind === "redetect") return "다시 감지된 순간 10초 전 ~ 20초 후 · 사진을 누르면 그 순간을 재생해요";
  if (group.kind === "recheck") return "다시 확인을 시작한 때 ~ 판단이 끝난 뒤 5초";
  return groups.some((g) => g.kind === "found")
    ? "기다린 뒤 다시 가기 시작한 때 ~ 판단이 끝난 뒤 5초"
    : "처음 의심 10초 전 ~ 판단이 끝난 뒤 5초";
}

/** The time skipped between 발견 and 확인 (the robot waited), as 1분 20초. */
export function skippedText(found: ClipGroup, check: ClipGroup) {
  const from = Date.parse(found.clips[found.clips.length - 1].endAt);
  const to = start(check.clips[0]);
  const seconds = Math.max(0, Math.round((to - from) / 1000));
  const minutes = Math.floor(seconds / 60);
  return minutes ? `${minutes}분${seconds % 60 ? ` ${seconds % 60}초` : ""}` : `${seconds}초`;
}
