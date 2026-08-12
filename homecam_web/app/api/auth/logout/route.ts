import { NextResponse } from "next/server";
import {
  readCookie,
  revokeWebSession,
  webAuthCookieOptions,
  WEB_CHALLENGE_COOKIE,
  WEB_SESSION_COOKIE,
} from "../../../../db/web-auth";
import { getRuntimeEnvironment } from "../../../runtime-env";

export const dynamic = "force-dynamic";

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
  const response = NextResponse.json({ ok: true, redirectTo: "/" }, {
    headers: { "cache-control": "no-store" },
  });
  expire(response, WEB_SESSION_COOKIE);
  expire(response, WEB_CHALLENGE_COOKIE);
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

function expire(response: NextResponse, name: string) {
  response.cookies.set(name, "", {
    ...webAuthCookieOptions(0),
    expires: new Date(0),
  });
}
