import { getD1 } from ".";
import { ensureDatabaseSchema } from "./migration-state";

/** Per-minute request counter shared by playback, live and fall review routes. */
export async function consumeRequestRateLimit(input: {
  userId: string;
  roomCode: string;
  scope: string;
  limit: number;
}) {
  await ensureDatabaseSchema();
  const windowStartedAt = Math.floor(Date.now() / 60_000) * 60_000;
  const result = await getD1()
    .prepare(`INSERT INTO request_rate_limits (rate_key, window_started_at, request_count)
      VALUES (?, ?, 1)
      ON CONFLICT(rate_key) DO UPDATE SET
        window_started_at = CASE
          WHEN request_rate_limits.window_started_at < excluded.window_started_at
          THEN excluded.window_started_at
          ELSE request_rate_limits.window_started_at
        END,
        request_count = CASE
          WHEN request_rate_limits.window_started_at < excluded.window_started_at
          THEN 1
          ELSE request_rate_limits.request_count + 1
        END
      RETURNING request_count`)
    .bind(rateLimitKey(input), windowStartedAt)
    .first<{ request_count: number }>();

  return Boolean(result && result.request_count <= input.limit);
}

function rateLimitKey(input: { userId: string; roomCode: string; scope: string }) {
  return `${input.scope}:${input.userId}:${input.roomCode}`;
}
