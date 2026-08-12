import {
  getWebSessionUser,
  readCookie,
  WEB_SESSION_COOKIE,
} from "../db/web-auth";
import { getRuntimeEnvironment } from "./runtime-env";

const DEV_USER_HEADER = "x-malbut-dev-user-email";
const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

type AuthRuntimeEnvironment = {
  AUTH_MODE?: string;
  AUTH_SESSION_SECRET?: string;
  AUTH_DEV_USER_EMAIL?: string;
  PETCAM_BROADCASTER_EMAILS?: string;
  NODE_ENV?: string;
};

export type AuthenticatedUser = {
  email: string;
  fullName: string | null;
  subject: string;
};

export async function getRequestUserEmail(request: Request): Promise<string | null> {
  return (await getAuthenticatedUser(request.headers, request.url))?.email ?? null;
}

export async function getAuthenticatedUser(
  headers: Headers,
  requestUrl?: string,
): Promise<AuthenticatedUser | null> {
  const runtime = getRuntimeEnvironment() as AuthRuntimeEnvironment;
  if (runtime.AUTH_MODE === "dev_header") {
    return developmentHeaderUser(headers, requestUrl, runtime);
  }
  if (runtime.AUTH_MODE !== "cognito_session") return null;

  const secret = runtime.AUTH_SESSION_SECRET?.trim();
  const token = readCookie(headers.get("cookie"), WEB_SESSION_COOKIE);
  if (!secret || !token) return null;
  try {
    const user = await getWebSessionUser(token, secret);
    if (!user) return null;
    const email = normalizeEmail(user.email);
    if (!email || !validSubject(user.subject)) return null;
    return {
      email,
      fullName: normalizeDisplayName(user.fullName),
      subject: user.subject,
    };
  } catch {
    // Database/auth configuration errors fail closed. Never log session cookies.
    return null;
  }
}

export function canBroadcastForConfiguredAccount(userEmail: string) {
  const runtime = getRuntimeEnvironment() as AuthRuntimeEnvironment;
  const configured = runtime.PETCAM_BROADCASTER_EMAILS ?? "";
  const normalizedUser = normalizeEmail(userEmail);
  if (!normalizedUser) return false;
  return configured
    .split(",")
    .map(normalizeEmail)
    .filter((email): email is string => Boolean(email))
    .includes(normalizedUser);
}

function developmentHeaderUser(
  headers: Headers,
  requestUrl: string | undefined,
  runtime: AuthRuntimeEnvironment,
): AuthenticatedUser | null {
  if (runtime.NODE_ENV === "production" || !isLoopbackRequest(headers, requestUrl)) {
    return null;
  }
  const expectedEmail = normalizeEmail(runtime.AUTH_DEV_USER_EMAIL);
  const presentedEmail = normalizeEmail(headers.get(DEV_USER_HEADER));
  if (!expectedEmail || presentedEmail !== expectedEmail) return null;
  return { email: expectedEmail, fullName: null, subject: `dev:${expectedEmail}` };
}

function isLoopbackRequest(headers: Headers, requestUrl?: string): boolean {
  let hostname = "";
  if (requestUrl) {
    try {
      hostname = new URL(requestUrl).hostname;
    } catch {
      return false;
    }
  } else {
    const host = headers.get("host")?.trim() ?? "";
    try {
      hostname = new URL(`http://${host}`).hostname;
    } catch {
      return false;
    }
  }
  return hostname === "localhost" || hostname === "127.0.0.1" || hostname === "[::1]";
}

function normalizeEmail(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const normalized = value.trim().toLowerCase();
  if (
    normalized.length < 3 ||
    normalized.length > 254 ||
    !EMAIL_PATTERN.test(normalized)
  ) {
    return null;
  }
  return normalized;
}

function normalizeDisplayName(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const normalized = value.trim();
  if (!normalized || normalized.length > 200 || /[\u0000-\u001f\u007f]/.test(normalized)) {
    return null;
  }
  return normalized;
}

function validSubject(value: string) {
  return Boolean(value && value.length <= 256 && !/[\u0000-\u001f\u007f]/.test(value));
}
