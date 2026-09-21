import { NextResponse } from "next/server";
import { normalizeEmail } from "../../../../db/homecam-validation";
import {
  claimWebAuthChallenge,
  consumeWebAuthRateLimit,
  createWebAuthChallenge,
  createWebSession,
  finishWebAuthChallenge,
  readCookie,
  webAuthCookieOptions,
  WEB_CHALLENGE_COOKIE,
  WEB_CHALLENGE_TTL_SECONDS,
  WEB_SESSION_COOKIE,
  WEB_SESSION_TTL_SECONDS,
} from "../../../../db/web-auth";
import {
  beginCognitoAuthentication,
  respondToCognitoChallenge,
  type CognitoAuthenticationOutcome,
} from "../../../cognito-auth";
import { getRuntimeEnvironment } from "../../../runtime-env";

export const dynamic = "force-dynamic";

const GENERIC_ERROR = "이메일 또는 인증 정보를 확인해 주세요.";
const RATE_LIMIT_WINDOW_MS = 15 * 60 * 1000;

type AuthRuntime = {
  AUTH_PUBLIC_ORIGIN?: string;
  AUTH_SESSION_SECRET?: string;
};

export async function POST(request: Request) {
  if (!sameOriginJsonRequest(request)) return responseError("요청을 확인해 주세요.", 403);
  const runtime = getRuntimeEnvironment() as AuthRuntime;
  const sessionSecret = runtime.AUTH_SESSION_SECRET?.trim();
  if (!sessionSecret) return responseError("로그인 서비스를 사용할 수 없습니다.", 503);
  const returnTo = safeReturnPath(new URL(request.url).searchParams.get("return_to"));
  const payload = (await request.json().catch(() => null)) as Record<string, unknown> | null;
  if (!payload || Array.isArray(payload)) return responseError(GENERIC_ERROR, 400);
  const challengeToken = readCookie(request.headers.get("cookie"), WEB_CHALLENGE_COOKIE);

  try {
    if (challengeToken && ("newPassword" in payload || "mfaCode" in payload)) {
      return await completeChallenge(payload, challengeToken, sessionSecret, returnTo);
    }
    return await beginLogin(request, payload, sessionSecret, returnTo);
  } catch {
    return responseError(GENERIC_ERROR, 401);
  }
}

async function beginLogin(
  request: Request,
  payload: Record<string, unknown>,
  sessionSecret: string,
  returnTo: string,
) {
  if (
    Object.keys(payload).some((key) => !["email", "password"].includes(key)) ||
    typeof payload.email !== "string" ||
    typeof payload.password !== "string"
  ) return responseError(GENERIC_ERROR, 400);
  const email = normalizeEmail(payload.email);
  if (!email || payload.password.length < 1 || payload.password.length > 1024) {
    return responseError(GENERIC_ERROR, 400);
  }
  const clientAddress = requestClientAddress(request);
  const [accountAllowed, addressAllowed] = await Promise.all([
    consumeWebAuthRateLimit({
      scope: "login-account", identifier: email, limit: 5,
      windowMs: RATE_LIMIT_WINDOW_MS, sessionSecret,
    }),
    consumeWebAuthRateLimit({
      scope: "login-address", identifier: clientAddress, limit: 20,
      windowMs: RATE_LIMIT_WINDOW_MS, sessionSecret,
    }),
  ]);
  if (!accountAllowed || !addressAllowed) return rateLimited();

  const outcome = await beginCognitoAuthentication({
    username: email,
    password: payload.password,
  });
  return outcomeResponse(outcome, sessionSecret, returnTo);
}

async function completeChallenge(
  payload: Record<string, unknown>,
  challengeToken: string,
  sessionSecret: string,
  returnTo: string,
) {
  const challenge = await claimWebAuthChallenge(challengeToken, sessionSecret);
  if (!challenge) return responseError("인증 시간이 만료되었습니다. 다시 로그인해 주세요.", 401, true);
  let responseValue: string | null = null;
  if (
    challenge.challengeName === "NEW_PASSWORD_REQUIRED" &&
    Object.keys(payload).every((key) => key === "newPassword") &&
    typeof payload.newPassword === "string" &&
    payload.newPassword.length >= 12 &&
    payload.newPassword.length <= 256
  ) responseValue = payload.newPassword;
  if (
    challenge.challengeName === "SOFTWARE_TOKEN_MFA" &&
    Object.keys(payload).every((key) => key === "mfaCode") &&
    typeof payload.mfaCode === "string" &&
    /^\d{6}$/.test(payload.mfaCode)
  ) responseValue = payload.mfaCode;
  if (!responseValue) {
    await finishWebAuthChallenge({ tokenDigest: challenge.tokenDigest, succeeded: false });
    return responseError(GENERIC_ERROR, 400);
  }

  try {
    const outcome = await respondToCognitoChallenge({
      username: challenge.username,
      challengeName: challenge.challengeName,
      cognitoSession: challenge.cognitoSession,
      response: responseValue,
    });
    await finishWebAuthChallenge({ tokenDigest: challenge.tokenDigest, succeeded: true });
    return outcomeResponse(outcome, sessionSecret, returnTo);
  } catch {
    await finishWebAuthChallenge({ tokenDigest: challenge.tokenDigest, succeeded: false });
    return responseError(GENERIC_ERROR, 401);
  }
}

async function outcomeResponse(
  outcome: CognitoAuthenticationOutcome,
  sessionSecret: string,
  returnTo: string,
) {
  if (outcome.status === "challenge") {
    const challenge = await createWebAuthChallenge({
      username: outcome.username,
      challengeName: outcome.challengeName,
      cognitoSession: outcome.cognitoSession,
      sessionSecret,
    });
    const response = NextResponse.json({
      ok: false,
      challenge: outcome.challengeName === "NEW_PASSWORD_REQUIRED" ? "new_password" : "mfa",
      message: outcome.challengeName === "NEW_PASSWORD_REQUIRED"
        ? "새 비밀번호를 설정해 주세요."
        : "인증 앱의 6자리 코드를 입력해 주세요.",
    }, { status: 200, headers: noStoreHeaders() });
    response.cookies.set(WEB_CHALLENGE_COOKIE, challenge.token, webAuthCookieOptions(WEB_CHALLENGE_TTL_SECONDS));
    return response;
  }
  const session = await createWebSession({
    cognitoSub: outcome.identity.subject,
    cognitoUsername: outcome.identity.username,
    userEmail: outcome.identity.email,
    fullName: outcome.identity.fullName,
    sessionSecret,
  });
  const response = NextResponse.json({ ok: true, redirectTo: returnTo }, {
    status: 200, headers: noStoreHeaders(),
  });
  response.cookies.set(WEB_SESSION_COOKIE, session.token, webAuthCookieOptions(WEB_SESSION_TTL_SECONDS));
  expireCookie(response, WEB_CHALLENGE_COOKIE);
  return response;
}

function sameOriginJsonRequest(request: Request) {
  const fetchSite = request.headers.get("sec-fetch-site")?.toLowerCase();
  if (fetchSite === "cross-site") return false;
  if (!request.headers.get("content-type")?.toLowerCase().startsWith("application/json")) return false;
  const runtime = getRuntimeEnvironment() as AuthRuntime;
  const configuredOrigin = safeHttpsOrigin(runtime.AUTH_PUBLIC_ORIGIN);
  if (runtime.AUTH_PUBLIC_ORIGIN && !configuredOrigin) return false;
  const requestOrigin = new URL(request.url).origin;
  const expectedOrigin = configuredOrigin ?? requestOrigin;
  return request.headers.get("origin") === expectedOrigin;
}

function requestClientAddress(request: Request) {
  const forwarded = request.headers.get("x-forwarded-for");
  const value = forwarded?.split(",").at(-1)?.trim() ?? "unknown";
  return /^[0-9a-f:.]{1,64}$/i.test(value) ? value.toLowerCase() : "unknown";
}

function safeReturnPath(value: string | null) {
  if (!value?.startsWith("/") || value.startsWith("//")) return "/";
  try {
    const url = new URL(value, "https://app.local");
    if (url.origin !== "https://app.local" || url.pathname.startsWith("/auth/")) return "/";
    return `${url.pathname}${url.search}${url.hash}`;
  } catch { return "/"; }
}

function safeHttpsOrigin(value: string | undefined) {
  try {
    const url = value ? new URL(value) : null;
    return url?.protocol === "https:" && url.pathname === "/" && !url.search && !url.hash
      ? url.origin : null;
  } catch { return null; }
}

function responseError(message: string, status: number, clearChallenge = false) {
  const response = NextResponse.json({ ok: false, message }, { status, headers: noStoreHeaders() });
  if (clearChallenge) expireCookie(response, WEB_CHALLENGE_COOKIE);
  return response;
}

function rateLimited() {
  return NextResponse.json({ ok: false, message: "요청이 너무 많습니다. 잠시 뒤 다시 시도해 주세요." }, {
    status: 429, headers: { ...noStoreHeaders(), "retry-after": "900" },
  });
}

function noStoreHeaders() { return { "cache-control": "no-store" }; }

function expireCookie(response: NextResponse, name: string) {
  response.cookies.set(name, "", { ...webAuthCookieOptions(0), expires: new Date(0) });
}
