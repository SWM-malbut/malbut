import { NextResponse } from "next/server";
import { ensureUserForIdentity } from "../../../../db/users";
import {
  consumeOidcLoginTransaction,
  createUserWebSession,
  OIDC_LOGIN_COOKIE,
  readCookie,
  WEB_SESSION_COOKIE,
  WEB_SESSION_TTL_SECONDS,
  webAuthCookieOptions,
} from "../../../../db/web-auth";
import { getRuntimeEnvironment } from "../../../runtime-env";
import { finishSocialLogin, isSocialProvider, publicRequestUrl, publicUrl } from "../../../social-auth";
import { loginRedirect, noStoreRedirect } from "../../social-redirect";

export const dynamic = "force-dynamic";

/** The provider sends the browser back here with a one-time code. */
export async function GET(
  request: Request,
  context: { params: Promise<{ provider: string }> },
) {
  const { provider } = await context.params;
  const sessionSecret = getRuntimeEnvironment().AUTH_SESSION_SECRET?.trim();
  if (!isSocialProvider(provider) || !sessionSecret) return clearing(loginRedirect(request, "/", "unavailable"));
  const token = readCookie(request.headers.get("cookie"), OIDC_LOGIN_COOKIE);
  const transaction = token
    ? await consumeOidcLoginTransaction(token, provider, sessionSecret).catch(() => null)
    : null;
  if (!transaction) return clearing(loginRedirect(request, "/", "expired"));
  // The person pressed "취소" or refused consent at the provider.
  if (new URL(request.url).searchParams.has("error")) {
    return clearing(loginRedirect(request, transaction.returnTo, "cancelled"));
  }
  let subject: string;
  try {
    ({ subject } = await finishSocialLogin(provider, publicRequestUrl(request.url), transaction));
  } catch {
    return clearing(loginRedirect(request, transaction.returnTo, "failed"));
  }
  try {
    const userId = await ensureUserForIdentity(provider, subject);
    const session = await createUserWebSession({ userId, sessionSecret });
    // The home page asks for a name first when this person has none yet.
    const response = clearing(noStoreRedirect(publicUrl(transaction.returnTo, request.url)));
    response.cookies.set(WEB_SESSION_COOKIE, session.token, webAuthCookieOptions(WEB_SESSION_TTL_SECONDS));
    return response;
  } catch {
    return clearing(loginRedirect(request, transaction.returnTo, "failed"));
  }
}

function clearing(response: NextResponse) {
  response.cookies.set(OIDC_LOGIN_COOKIE, "", webAuthCookieOptions(0));
  response.headers.set("cache-control", "no-store");
  return response;
}
