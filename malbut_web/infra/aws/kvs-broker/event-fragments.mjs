// What KVS really holds for an event clip (2026-10-09). The web used to trust its
// recording-session records, and the playback fell back to a fragment before the
// clip: a clip recorded while 연속 녹화 was off showed "영상 재생 가능" but played
// nothing. These helpers read ListFragments results (ServerTimestamp Date,
// FragmentLengthInMilliseconds) without AWS calls, so tests can run them.

// Fragments closer than this are one continuous recording.
export const COVERAGE_GAP_MS = 1_500;

function sortedFragments(fragments) {
  return (fragments ?? [])
    .filter((fragment) => fragment.ServerTimestamp instanceof Date)
    .sort((left, right) => left.ServerTimestamp.getTime() - right.ServerTimestamp.getTime());
}

function fragmentEnd(fragment) {
  return fragment.ServerTimestamp.getTime() + Number(fragment.FragmentLengthInMilliseconds ?? 0);
}

/** The fragment to start playback at, or null when none overlaps [start, end). */
export function eventStartFragment(fragments, startMs, endMs) {
  const sorted = sortedFragments(fragments);
  const containing = [...sorted].reverse().find((fragment) =>
    fragment.ServerTimestamp.getTime() <= startMs && fragmentEnd(fragment) > startMs);
  return containing ?? sorted.find((fragment) => {
    const at = fragment.ServerTimestamp.getTime();
    return at >= startMs && at < endMs;
  }) ?? null;
}

/** Continuous recorded ranges inside [start, end), as ISO strings. */
export function coveredRanges(fragments, startMs, endMs) {
  const ranges = [];
  for (const fragment of sortedFragments(fragments)) {
    const from = Math.max(fragment.ServerTimestamp.getTime(), startMs);
    const to = Math.min(fragmentEnd(fragment), endMs);
    if (to <= from) continue;
    const last = ranges.at(-1);
    if (last && from - last[1] <= COVERAGE_GAP_MS) last[1] = Math.max(last[1], to);
    else ranges.push([from, to]);
  }
  return ranges.map(([from, to]) => ({
    startAt: new Date(from).toISOString(), endAt: new Date(to).toISOString(),
  }));
}
