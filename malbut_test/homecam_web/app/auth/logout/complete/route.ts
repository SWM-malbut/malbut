import { NextResponse } from "next/server";
import {
  WEB_CHALLENGE_COOKIE,
  WEB_SESSION_COOKIE,
  webAuthCookieOptions,
} from "../../../../db/web-auth";
import { getRuntimeEnvironment } from "../../../runtime-env";
import {
  decodeLogoutReturnPath,
  LOGOUT_RETURN_COOKIE,
} from "../logout-flow";

export const dynamic = "force-dynamic";

const ALB_AUTH_COOKIE_PREFIX = "AWSELBAuthSessionCookie";

export async function GET(request: Request) {
  const returnTo = decodeLogoutReturnPath(
    rawCookie(request.headers.get("cookie"), LOGOUT_RETURN_COOKIE),
  );
  const requestOrigin = new URL(request.url).origin;
  const configuredOrigin = safeHttpsOrigin(
    getRuntimeEnvironment().AUTH_PUBLIC_ORIGIN,
  );
  const response = NextResponse.redirect(
    new URL(returnTo, configuredOrigin ?? requestOrigin),
    303,
  );
  for (const name of [WEB_SESSION_COOKIE, WEB_CHALLENGE_COOKIE, LOGOUT_RETURN_COOKIE]) {
    response.cookies.set(name, "", {
      ...webAuthCookieOptions(0),
      expires: new Date(0),
    });
  }
  for (const name of [
    ALB_AUTH_COOKIE_PREFIX,
    ...Array.from({ length: 4 }, (_, shard) => `${ALB_AUTH_COOKIE_PREFIX}-${shard}`),
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
  return response;
}

function rawCookie(cookieHeader: string | null, name: string) {
  if (!cookieHeader) return null;
  for (const component of cookieHeader.split(";")) {
    const separator = component.indexOf("=");
    if (separator < 0 || component.slice(0, separator).trim() !== name) continue;
    return component.slice(separator + 1).trim();
  }
  return null;
}

function safeHttpsOrigin(value: string | undefined) {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === "https:" && url.pathname === "/" && !url.search && !url.hash
      ? url.origin
      : null;
  } catch {
    return null;
  }
}
