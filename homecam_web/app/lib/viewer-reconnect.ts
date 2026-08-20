export const AUTHORIZED_P2P_RECONNECT_MAX_ATTEMPTS = 6;
export const AUTHORIZED_P2P_RECONNECT_BASE_DELAY_MS = 1_000;
export const AUTHORIZED_P2P_RECONNECT_MAX_DELAY_MS = 16_000;
export const AUTHORIZED_P2P_RECONNECT_JITTER_RATIO = 0.25;
export const AUTHORIZED_P2P_DISCONNECT_GRACE_MS = 3_000;
export const AUTHORIZED_P2P_VIEWER_REUSE_GRACE_MS = 45_000;
export const AUTHORIZED_VIEWER_SETUP_TIMEOUT_MS = 12_000;
export const AUTHORIZED_P2P_CONNECT_TIMEOUT_MS = 25_000;
export const AUTHORIZED_P2P_MEDIA_TIMEOUT_MS = 10_000;
export const AUTHORIZED_P2P_STABLE_LIVE_MS = 10_000;

export function canAutomaticallyReconnectAuthorizedP2p(completedAttempts: number) {
  return (
    Number.isInteger(completedAttempts) &&
    completedAttempts >= 0 &&
    completedAttempts < AUTHORIZED_P2P_RECONNECT_MAX_ATTEMPTS
  );
}

export function authorizedP2pReconnectDelayMs(
  completedAttempts: number,
  randomValue = Math.random(),
) {
  const attempt = Number.isFinite(completedAttempts)
    ? Math.max(0, Math.floor(completedAttempts))
    : 0;
  const randomUnit = Number.isFinite(randomValue)
    ? Math.min(1, Math.max(0, randomValue))
    : 0.5;
  const exponentialDelay = Math.min(
    AUTHORIZED_P2P_RECONNECT_MAX_DELAY_MS,
    AUTHORIZED_P2P_RECONNECT_BASE_DELAY_MS * 2 ** attempt,
  );
  const jitterWindow = exponentialDelay * AUTHORIZED_P2P_RECONNECT_JITTER_RATIO;
  return Math.min(
    AUTHORIZED_P2P_RECONNECT_MAX_DELAY_MS,
    Math.round(
      exponentialDelay - jitterWindow + randomUnit * jitterWindow * 2,
    ),
  );
}
