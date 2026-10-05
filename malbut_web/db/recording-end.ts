// KVS storage sessions have a one-hour service boundary. A one-hour backend
// lease lets the device perform its own 50-minute soft refresh and 55-minute
// hard cutover instead of replacing both signaling credentials every five
// minutes. Heartbeats still extend a healthy session as a sliding lease.
export const MEDIA_SESSION_TTL_MS = 60 * 60 * 1000;

// Same window as "online": the robot reports about once a second.
export const RECORDING_REPORT_GRACE_MS = 30_000;

// A healthy storage heartbeat slides expires_at to now + TTL, so expires_at - TTL
// is the robot's last report that it was still recording.
const LAST_REPORT_SQL = `stream_sessions.expires_at - ${MEDIA_SESSION_TTL_MS} * INTERVAL '1 millisecond'`;

/**
 * When a recording really ended, as SQL with `stream_sessions` in scope; binds the
 * closing time twice. A robot that went quiet (power cut, crash) stopped recording
 * at its last report, not when the server noticed up to an hour later. One that
 * reported within the grace window recorded until now, so a session refresh
 * leaves no gap.
 */
export const RECORDING_END_SQL = `CASE
  WHEN ${LAST_REPORT_SQL} >= ?::timestamptz - ${RECORDING_REPORT_GRACE_MS} * INTERVAL '1 millisecond'
  THEN ?::timestamptz ELSE ${LAST_REPORT_SQL} END`;

/** The same rule for a recording still open: null while the robot is still reporting. */
export function quietRecordingEnd(expiresAt: string, nowMs: number) {
  const lastReport = Date.parse(expiresAt) - MEDIA_SESSION_TTL_MS;
  return lastReport >= nowMs - RECORDING_REPORT_GRACE_MS ? null : lastReport;
}
