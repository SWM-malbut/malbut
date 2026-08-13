import { NextResponse } from "next/server";
import { getRuntimeValue } from "../../../runtime-env";

const ALB_AUTH_COOKIE_PREFIX = "AWSELBAuthSessionCookie";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const requestUrl = new URL(request.url);
  const returnTo = safeReturnPath(requestUrl.searchParams.get("return_to"));
  const publicOrigin = safeHttpsOrigin(getRuntimeValue("AUTH_PUBLIC_ORIGIN"));
  const cognitoDomain = safeHttpsOrigin(getRuntimeValue("AUTH_COGNITO_DOMAIN"));
  const clientId = getRuntimeValue("AUTH_COGNITO_CLIENT_ID");

  const completionUrl = new URL("/auth/logout/complete", publicOrigin ?? requestUrl.origin);
  const usesCognito = Boolean(cognitoDomain && clientId);
  if (!usesCognito) completionUrl.searchParams.set("return_to", returnTo);
  const redirectUrl = usesCognito
    ? cognitoLogoutUrl(cognitoDomain!, clientId!, completionUrl)
    : completionUrl;
  const response = NextResponse.redirect(redirectUrl, 303);
  expireAlbCookies(response);
  return response;
}

function cognitoLogoutUrl(
  cognitoDomain: string,
  clientId: string,
  completionUrl: URL,
) {
  const logoutUrl = new URL("/logout", cognitoDomain);
  logoutUrl.searchParams.set("client_id", clientId);
  logoutUrl.searchParams.set("logout_uri", completionUrl.toString());
  return logoutUrl;
}

function expireAlbCookies(response: NextResponse) {
  for (const name of [
    ALB_AUTH_COOKIE_PREFIX,
    ...Array.from(
      { length: 4 },
      (_, shard) => `${ALB_AUTH_COOKIE_PREFIX}-${shard}`,
    ),
  ]) {
    response.cookies.set({
      name,
      value: "",
      httpOnly: true,
      secure: true,
      sameSite: "none",
      path: "/",
      maxAge: -1,
      expires: new Date(0),
    });
  }
}

function safeReturnPath(value: string | null) {
  if (!value || !value.startsWith("/") || value.startsWith("//")) return "/";
  try {
    const url = new URL(value, "https://app.local");
    if (url.origin !== "https://app.local" || url.pathname.startsWith("/auth/")) {
      return "/";
    }
    return `${url.pathname}${url.search}${url.hash}`;
  } catch {
    return "/";
  }
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
