import { NextResponse } from "next/server";
import { loginRedirect } from "../../social-redirect";
import {
  createOidcLoginTransaction,
  OIDC_LOGIN_COOKIE,
  OIDC_LOGIN_TTL_SECONDS,
  webAuthCookieOptions,
} from "../../../../db/web-auth";
import { safeRelativeReturnPath } from "../../login/login-flow";
import { getRuntimeEnvironment } from "../../../runtime-env";
import {
  beginSocialLogin,
  isSocialProvider,
  socialCallbackUrl,
  socialProviderSettings,
} from "../../../social-auth";

export const dynamic = "force-dynamic";

/** "카카오로 계속하기" and friends: remember the sign-in, then go to the provider. */
export async function GET(
  request: Request,
  context: { params: Promise<{ provider: string }> },
) {
  const { provider } = await context.params;
  const returnTo = safeRelativeReturnPath(new URL(request.url).searchParams.get("return_to"));
  const back = (reason: string) => loginRedirect(request, returnTo, reason);
  if (!isSocialProvider(provider) || !socialProviderSettings(provider)) return back("unavailable");
  const sessionSecret = getRuntimeEnvironment().AUTH_SESSION_SECRET?.trim();
  if (!sessionSecret) return back("unavailable");
  try {
    const start = await beginSocialLogin(provider, socialCallbackUrl(provider, request.url));
    const transaction = await createOidcLoginTransaction({
      provider,
      state: start.state,
      nonce: start.nonce,
      codeVerifier: start.codeVerifier,
      returnTo,
      sessionSecret,
    });
    const response = NextResponse.redirect(start.authorizationUrl, 303);
    response.headers.set("cache-control", "no-store");
    response.cookies.set(OIDC_LOGIN_COOKIE, transaction.token, webAuthCookieOptions(OIDC_LOGIN_TTL_SECONDS));
    return response;
  } catch {
    // Discovery or database trouble: never leak provider details to the browser.
    return back("failed");
  }
}
