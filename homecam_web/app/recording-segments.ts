export const RECORDING_SEGMENT_MS = 60 * 60 * 1000;
export const RECORDING_RETENTION_MS = 7 * 24 * RECORDING_SEGMENT_MS;

export function resolveRecordingSegmentWindow(input: {
  recordingStartedAt: string;
  recordingEndedAt: string | null;
  segment: number;
  nowMs?: number;
}) {
  const nowMs = input.nowMs ?? Date.now();
  const recordingStart = Date.parse(input.recordingStartedAt);
  const endedAt = input.recordingEndedAt
    ? Date.parse(input.recordingEndedAt)
    : nowMs;
  const recordingEnd = Math.min(endedAt, nowMs);
  if (
    !Number.isFinite(recordingStart) ||
    !Number.isFinite(recordingEnd) ||
    recordingEnd <= recordingStart ||
    !Number.isSafeInteger(input.segment) ||
    input.segment < 0
  ) {
    return null;
  }

  const requestedStart = recordingStart + input.segment * RECORDING_SEGMENT_MS;
  if (requestedStart >= recordingEnd) return null;
  const requestedEnd = Math.min(
    recordingEnd,
    requestedStart + RECORDING_SEGMENT_MS,
  );
  const retentionCutoff = nowMs - RECORDING_RETENTION_MS;
  if (requestedEnd <= retentionCutoff) return null;
  const startMs = Math.max(requestedStart, retentionCutoff);
  return {
    startAt: new Date(startMs).toISOString(),
    endAt: new Date(requestedEnd).toISOString(),
    durationSeconds: Math.ceil((requestedEnd - startMs) / 1000),
    trimmedStartSeconds: (startMs - requestedStart) / 1000,
  };
}

export function recordingPlaybackPosition(offsetMs: number | null) {
  if (offsetMs === null || !Number.isSafeInteger(offsetMs) || offsetMs < 0) {
    return { recordingSegment: null, playbackOffsetSeconds: null };
  }
  return {
    recordingSegment: Math.floor(offsetMs / RECORDING_SEGMENT_MS),
    playbackOffsetSeconds: (offsetMs % RECORDING_SEGMENT_MS) / 1000,
  };
}
