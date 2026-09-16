import { createCipheriv, createDecipheriv, createHmac, hkdfSync, randomBytes } from "node:crypto";
import { getD1 } from "./index";

export const WEB_SESSION_COOKIE = "__Host-malbut_session";
export const WEB_CHALLENGE_COOKIE = "__Host-malbut_challenge";
export const WEB_SESSION_TTL_SECONDS = 12 * 60 * 60;
export const WEB_CHALLENGE_TTL_SECONDS = 5 * 60;
const MAX_CHALLENGE_FAILURES = 5;
const TOKEN_PATTERN = /^[A-Za-z0-9_-]{43}$/;
const DIGEST_PATTERN = /^[a-f0-9]{64}$/;

export type WebSessionUser = {
  email: string;
  fullName: string | null;
  subject: string;
};

export type WebAuthChallengeName =
  | "NEW_PASSWORD_REQUIRED"
  | "SOFTWARE_TOKEN_MFA";

export type WebAuthChallenge = {
  tokenDigest: string;
  username: string;
  challengeName: WebAuthChallengeName;
  cognitoSession: string;
  failureCount: number;
};

type SessionRow = {
  cognito_sub: string;
  user_email: string;
  full_name: string | null;
};

type ChallengeRow = {
  token_digest: string;
  cognito_username: string;
  challenge_name: WebAuthChallengeName;
  cognito_session_ciphertext: string;
  failure_count: number;
};

export async function createWebSession(input: {
  cognitoSub: string;
  cognitoUsername: string;
  userEmail: string;
  fullName?: string | null;
  sessionSecret: string;
  now?: Date;
}): Promise<{ token: string; expiresAt: Date }> {
  const token = randomToken();
  const digest = tokenDigest(token, input.sessionSecret);
  const now = input.now ?? new Date();
  const expiresAt = new Date(now.getTime() + WEB_SESSION_TTL_SECONDS * 1000);
  await getD1()
    .prepare(
      `INSERT INTO web_auth_sessions
       (token_digest, cognito_sub, cognito_username, user_email, full_name,
        created_at, last_seen_at, expires_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
    )
    .bind(
      digest,
      input.cognitoSub,
      input.cognitoUsername,
      input.userEmail,
      input.fullName ?? null,
      now.toISOString(),
      now.toISOString(),
      expiresAt.toISOString(),
    )
    .run();
  return { token, expiresAt };
}

export async function getWebSessionUser(
  token: string,
  sessionSecret: string,
  now = new Date(),
): Promise<WebSessionUser | null> {
  if (!TOKEN_PATTERN.test(token)) return null;
  const digest = tokenDigest(token, sessionSecret);
  const row = await getD1()
    .prepare(
      `UPDATE web_auth_sessions
       SET last_seen_at = ?
       WHERE token_digest = ? AND revoked_at IS NULL AND expires_at > ?
       RETURNING cognito_sub, user_email, full_name`,
    )
    .bind(now.toISOString(), digest, now.toISOString())
    .first<SessionRow>();
  if (!row) return null;
  return {
    email: row.user_email,
    fullName: row.full_name,
    subject: row.cognito_sub,
  };
}

export async function revokeWebSession(
  token: string | null,
  sessionSecret: string,
  now = new Date(),
): Promise<boolean> {
  if (!token || !TOKEN_PATTERN.test(token)) return false;
  const result = await getD1()
    .prepare(
      `UPDATE web_auth_sessions SET revoked_at = ?
       WHERE token_digest = ? AND revoked_at IS NULL`,
    )
    .bind(now.toISOString(), tokenDigest(token, sessionSecret))
    .run();
  return result.meta.changes > 0;
}

export async function createWebAuthChallenge(input: {
  username: string;
  challengeName: WebAuthChallengeName;
  cognitoSession: string;
  sessionSecret: string;
  now?: Date;
}): Promise<{ token: string; expiresAt: Date }> {
  const token = randomToken();
  const now = input.now ?? new Date();
  const expiresAt = new Date(now.getTime() + WEB_CHALLENGE_TTL_SECONDS * 1000);
  await getD1()
    .prepare(
      `INSERT INTO web_auth_challenges
       (token_digest, cognito_username, challenge_name,
        cognito_session_ciphertext, failure_count, created_at, expires_at)
       VALUES (?, ?, ?, ?, 0, ?, ?)`,
    )
    .bind(
      tokenDigest(token, input.sessionSecret),
      input.username,
      input.challengeName,
      encryptCognitoSession(input.cognitoSession, input.sessionSecret),
      now.toISOString(),
      expiresAt.toISOString(),
    )
    .run();
  return { token, expiresAt };
}

export async function claimWebAuthChallenge(
  token: string,
  sessionSecret: string,
  now = new Date(),
): Promise<WebAuthChallenge | null> {
  if (!TOKEN_PATTERN.test(token)) return null;
  const digest = tokenDigest(token, sessionSecret);
  const row = await getD1()
    .prepare(
      `UPDATE web_auth_challenges
       SET claimed_at = ?
       WHERE token_digest = ? AND consumed_at IS NULL AND claimed_at IS NULL
         AND failure_count < ? AND expires_at > ?
       RETURNING token_digest, cognito_username, challenge_name,
                 cognito_session_ciphertext, failure_count`,
    )
    .bind(now.toISOString(), digest, MAX_CHALLENGE_FAILURES, now.toISOString())
    .first<ChallengeRow>();
  if (!row || !DIGEST_PATTERN.test(row.token_digest)) return null;
  try {
    return {
      tokenDigest: row.token_digest,
      username: row.cognito_username,
      challengeName: row.challenge_name,
      cognitoSession: decryptCognitoSession(
        row.cognito_session_ciphertext,
        sessionSecret,
      ),
      failureCount: row.failure_count,
    };
  } catch {
    await consumeWebAuthChallenge(row.token_digest, now);
    return null;
  }
}

export async function finishWebAuthChallenge(input: {
  tokenDigest: string;
  succeeded: boolean;
  now?: Date;
}): Promise<void> {
  const now = input.now ?? new Date();
  if (!DIGEST_PATTERN.test(input.tokenDigest)) return;
  if (input.succeeded) {
    await consumeWebAuthChallenge(input.tokenDigest, now);
    return;
  }
  await getD1()
    .prepare(
      `UPDATE web_auth_challenges
       SET failure_count = failure_count + 1, claimed_at = NULL,
           consumed_at = CASE WHEN failure_count + 1 >= ? THEN ? ELSE NULL END
       WHERE token_digest = ? AND consumed_at IS NULL`,
    )
    .bind(MAX_CHALLENGE_FAILURES, now.toISOString(), input.tokenDigest)
    .run();
}

export async function consumeWebAuthRateLimit(input: {
  scope: string;
  identifier: string;
  limit: number;
  windowMs: number;
  sessionSecret: string;
  now?: Date;
}): Promise<boolean> {
  if (
    !/^[a-z][a-z0-9-]{0,39}$/.test(input.scope) ||
    !input.identifier ||
    input.identifier.length > 512 ||
    !Number.isSafeInteger(input.limit) ||
    input.limit < 1 ||
    input.limit > 10_000 ||
    !Number.isSafeInteger(input.windowMs) ||
    input.windowMs < 1_000 ||
    input.windowMs > 24 * 60 * 60 * 1000
  ) {
    throw new Error("INVALID_AUTH_RATE_LIMIT");
  }
  const nowMs = (input.now ?? new Date()).getTime();
  const windowStartedAt = Math.floor(nowMs / input.windowMs) * input.windowMs;
  const identifierDigest = createHmac("sha256", secretKey(input.sessionSecret))
    .update(`${input.scope}\0${input.identifier}`)
    .digest("hex");
  const row = await getD1()
    .prepare(
      `INSERT INTO request_rate_limits (rate_key, window_started_at, request_count)
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
       RETURNING request_count`,
    )
    .bind(`web-auth:${input.scope}:${identifierDigest}`, windowStartedAt)
    .first<{ request_count: number }>();
  return Boolean(row && row.request_count <= input.limit);
}

export function readCookie(cookieHeader: string | null, name: string): string | null {
  if (!cookieHeader) return null;
  for (const component of cookieHeader.split(";")) {
    const separator = component.indexOf("=");
    if (separator < 0 || component.slice(0, separator).trim() !== name) continue;
    const value = component.slice(separator + 1).trim();
    return TOKEN_PATTERN.test(value) ? value : null;
  }
  return null;
}

export function webAuthCookieOptions(maxAge: number) {
  return {
    httpOnly: true,
    secure: true,
    sameSite: "lax" as const,
    path: "/",
    maxAge,
  };
}

function randomToken() {
  return randomBytes(32).toString("base64url");
}

function tokenDigest(token: string, sessionSecret: string) {
  return createHmac("sha256", secretKey(sessionSecret)).update(token).digest("hex");
}

function encryptCognitoSession(value: string, sessionSecret: string) {
  const iv = randomBytes(12);
  const cipher = createCipheriv("aes-256-gcm", encryptionKey(sessionSecret), iv);
  const ciphertext = Buffer.concat([cipher.update(value, "utf8"), cipher.final()]);
  return `v1.${iv.toString("base64url")}.${Buffer.concat([
    ciphertext,
    cipher.getAuthTag(),
  ]).toString("base64url")}`;
}

function decryptCognitoSession(value: string, sessionSecret: string) {
  const [version, encodedIv, encodedPayload] = value.split(".");
  if (version !== "v1" || !encodedIv || !encodedPayload) throw new Error("INVALID_CIPHER");
  const iv = Buffer.from(encodedIv, "base64url");
  const payload = Buffer.from(encodedPayload, "base64url");
  if (iv.length !== 12 || payload.length <= 16) throw new Error("INVALID_CIPHER");
  const ciphertext = payload.subarray(0, -16);
  const tag = payload.subarray(-16);
  const decipher = createDecipheriv("aes-256-gcm", encryptionKey(sessionSecret), iv);
  decipher.setAuthTag(tag);
  return Buffer.concat([decipher.update(ciphertext), decipher.final()]).toString("utf8");
}

function encryptionKey(sessionSecret: string) {
  return Buffer.from(
    hkdfSync(
      "sha256",
      secretKey(sessionSecret),
      Buffer.alloc(0),
      Buffer.from("malbut-cognito-challenge-v1"),
      32,
    ),
  );
}

function secretKey(value: string) {
  if (!/^[A-Za-z0-9_-]{43}$/.test(value)) {
    throw new Error("AUTH_SESSION_SECRET must be a base64url-encoded 32-byte value");
  }
  const decoded = Buffer.from(value, "base64url");
  if (decoded.length !== 32) throw new Error("AUTH_SESSION_SECRET is invalid");
  return decoded;
}

async function consumeWebAuthChallenge(tokenDigestValue: string, now: Date) {
  await getD1()
    .prepare(
      `UPDATE web_auth_challenges SET consumed_at = ?, claimed_at = NULL
       WHERE token_digest = ? AND consumed_at IS NULL`,
    )
    .bind(now.toISOString(), tokenDigestValue)
    .run();
}
