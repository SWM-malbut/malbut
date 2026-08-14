import { NextResponse } from "next/server";
import {
  readCookie,
  revokeWebSession,
  webAuthCookieOptions,
  WEB_CHALLENGE_COOKIE,
  WEB_SESSION_COOKIE,
} from "../../../../db/web-auth";
import { getRuntimeEnvironment } from "../../../runtime-env";
import {
  encodeLogoutReturnPath,
  LOGOUT_RETURN_COOKIE,
  LOGOUT_RETURN_TTL_SECONDS,
  logoutStartPath,
  safeLogoutReturnPath,
  usesHostedUiLogout,
  type LogoutResponseMode,
} from "../../../auth/logout/logout-flow";

export const dynamic = "force-dynamic";

const ALB_AUTH_COOKIE_PREFIX = "AWSELBAuthSessionCookie";
const ALB_AUTH_COOKIE_SHARDS = 4;

export async function POST(request: Request) {
  if (!sameOriginJsonRequest(request)) {
    return NextResponse.json({ ok: false, message: "요청을 확인해 주세요." }, {
      status: 403, headers: { "cache-control": "no-store" },
    });
  }
  const secret = getRuntimeEnvironment().AUTH_SESSION_SECRET?.trim();
  if (secret) {
    await revokeWebSession(
      readCookie(request.headers.get("cookie"), WEB_SESSION_COOKIE),
      secret,
    ).catch(() => undefined);
  }
  const returnTo = safeLogoutReturnPath(
    new URL(request.url).searchParams.get("return_to"),
  );
  const hostedUi = usesHostedUiLogout(getRuntimeEnvironment().AUTH_MODE);
  const mode: LogoutResponseMode = hostedUi ? "hosted_ui" : "local";
  const response = NextResponse.json({
    ok: true,
    mode,
    redirectTo: hostedUi ? logoutStartPath(returnTo) : returnTo,
  }, {
    headers: { "cache-control": "no-store" },
  });
  expireAllAuthenticationCookies(response);
  if (hostedUi) setLogoutReturnCookie(response, returnTo);
  return response;
}

// This GET must be reached by top-level browser navigation. Fetching the
// Cognito logout endpoint would not clear its first-party Hosted UI cookie.
export async function GET(request: Request) {
  const requestUrl = new URL(request.url);
  const returnTo = safeLogoutReturnPath(requestUrl.searchParams.get("return_to"));
  const runtime = getRuntimeEnvironment();

  if (
    requestUrl.searchParams.get("continue") !== "hosted" ||
    !usesHostedUiLogout(runtime.AUTH_MODE)
  ) {
    const response = NextResponse.redirect(
      new URL(returnTo, publicOrigin(runtime.AUTH_PUBLIC_ORIGIN, requestUrl.origin)),
      303,
    );
    expireAllAuthenticationCookies(response);
    return response;
  }

  const cognitoDomain = safeHttpsOrigin(runtime.AUTH_COGNITO_DOMAIN);
  const clientId = runtime.AUTH_COGNITO_CLIENT_ID?.trim();
  const origin = safeHttpsOrigin(runtime.AUTH_PUBLIC_ORIGIN);
  if (!cognitoDomain || !origin || !clientId || !/^[A-Za-z0-9]{1,128}$/.test(clientId)) {
    const response = new NextResponse("로그아웃 서비스를 사용할 수 없습니다.", {
      status: 503,
      headers: { "cache-control": "no-store", "content-type": "text/plain; charset=utf-8" },
    });
    expireAllAuthenticationCookies(response);
    return response;
  }

  const completionUrl = new URL("/auth/logout/complete", origin);
  const logoutUrl = new URL("/logout", cognitoDomain);
  logoutUrl.searchParams.set("client_id", clientId);
  logoutUrl.searchParams.set("logout_uri", completionUrl.toString());
  const response = NextResponse.redirect(logoutUrl, 303);
  expireAllAuthenticationCookies(response);
  setLogoutReturnCookie(response, returnTo);
  return response;
}

function sameOriginJsonRequest(request: Request) {
  if (request.headers.get("sec-fetch-site")?.toLowerCase() === "cross-site") return false;
  if (!request.headers.get("content-type")?.toLowerCase().startsWith("application/json")) return false;
  const configured = getRuntimeEnvironment().AUTH_PUBLIC_ORIGIN?.trim();
  let expected = new URL(request.url).origin;
  if (configured) {
    try {
      const url = new URL(configured);
      if (url.protocol !== "https:" || url.pathname !== "/" || url.search || url.hash) return false;
      expected = url.origin;
    } catch { return false; }
  }
  return request.headers.get("origin") === expected;
}

export function expireAllAuthenticationCookies(response: NextResponse) {
  expireApplicationCookie(response, WEB_SESSION_COOKIE);
  expireApplicationCookie(response, WEB_CHALLENGE_COOKIE);
  expireApplicationCookie(response, LOGOUT_RETURN_COOKIE);
  for (const name of [
    ALB_AUTH_COOKIE_PREFIX,
    ...Array.from(
      { length: ALB_AUTH_COOKIE_SHARDS },
      (_, shard) => `${ALB_AUTH_COOKIE_PREFIX}-${shard}`,
    ),
  ]) {
    response.cookies.set(name, "", {
      httpOnly: true,
      secure: true,
      sameSite: "none",
      path: "/",
      maxAge: 0,
      expires: new Date(0),
    });
  }
}

function expireApplicationCookie(response: NextResponse, name: string) {
  response.cookies.set(name, "", {
    ...webAuthCookieOptions(0),
    expires: new Date(0),
  });
}

function setLogoutReturnCookie(response: NextResponse, returnTo: string) {
  response.cookies.set(
    LOGOUT_RETURN_COOKIE,
    encodeLogoutReturnPath(returnTo),
    webAuthCookieOptions(LOGOUT_RETURN_TTL_SECONDS),
  );
}

function safeHttpsOrigin(value: string | undefined) {
  if (!value) return null;
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || url.pathname !== "/" || url.search || url.hash) {
      return null;
    }
    return url.origin;
  } catch {
    return null;
  }
}

function publicOrigin(configured: string | undefined, requestOrigin: string) {
  return safeHttpsOrigin(configured) ?? requestOrigin;
}
