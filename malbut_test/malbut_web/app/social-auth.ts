import * as oidc from "openid-client";
import type { SocialProvider } from "../db/web-auth";
import { getRuntimeEnvironment } from "./runtime-env";

/**
 * Kakao, Naver and Google sign-in through OpenID Connect. Only the provider's
 * subject (`sub`) is used; names and emails are never requested.
 */
export const SOCIAL_PROVIDERS: readonly SocialProvider[] = ["kakao", "naver", "google"];

const DEFAULT_ISSUERS: Record<SocialProvider, string> = {
  kakao: "https://kauth.kakao.com",
  naver: "https://nid.naver.com",
  google: "https://accounts.google.com",
};

type SocialEnvironment = Record<string, string | undefined> & { AUTH_PUBLIC_ORIGIN?: string };

type ProviderSettings = { clientId: string; clientSecret: string; issuer: URL };

export type SocialLoginStart = {
  authorizationUrl: URL;
  state: string;
  nonce: string;
  codeVerifier: string;
};

export function isSocialProvider(value: string): value is SocialProvider {
  return (SOCIAL_PROVIDERS as readonly string[]).includes(value);
}

/** A provider is offered only when its client ID and secret are configured. */
export function socialProviderSettings(provider: SocialProvider): ProviderSettings | null {
  const runtime = getRuntimeEnvironment() as SocialEnvironment;
  const prefix = `AUTH_${provider.toUpperCase()}`;
  const clientId = runtime[`${prefix}_CLIENT_ID`]?.trim();
  const clientSecret = runtime[`${prefix}_CLIENT_SECRET`]?.trim();
  if (!clientId || !clientSecret) return null;
  try {
    const issuer = new URL(runtime[`${prefix}_ISSUER`]?.trim() || DEFAULT_ISSUERS[provider]);
    if (issuer.protocol !== "https:") return null;
    return { clientId, clientSecret, issuer };
  } catch {
    return null;
  }
}

export function enabledSocialProviders() {
  return SOCIAL_PROVIDERS.filter((provider) => socialProviderSettings(provider) !== null);
}

/** Where the provider sends the browser back; registered in each developer console. */
export function socialCallbackUrl(provider: SocialProvider, requestUrl: string) {
  return new URL(`/auth/callback/${provider}`, publicOrigin(requestUrl));
}

/** The browser-facing URL of this request, also behind an internal container listener. */
export function publicRequestUrl(requestUrl: string) {
  const url = new URL(requestUrl);
  return new URL(`${url.pathname}${url.search}`, publicOrigin(requestUrl));
}

export function publicUrl(path: string, requestUrl: string) {
  return new URL(path, publicOrigin(requestUrl));
}

function publicOrigin(requestUrl: string) {
  const configured = (getRuntimeEnvironment() as SocialEnvironment).AUTH_PUBLIC_ORIGIN?.trim();
  if (configured) {
    const url = new URL(configured);
    if (url.protocol !== "https:") throw new Error("AUTH_PUBLIC_ORIGIN_INVALID");
    return url.origin;
  }
  return new URL(requestUrl).origin;
}

const configurations = new Map<string, Promise<oidc.Configuration>>();
let providerFetch: oidc.CustomFetch | undefined;

/** Tests answer the provider's HTTP calls in-process instead of reaching Kakao, Naver or Google. */
export function setSocialProviderFetchForTest(fetcher: oidc.CustomFetch | undefined) {
  providerFetch = fetcher;
  configurations.clear();
}

function configuration(provider: SocialProvider) {
  const settings = socialProviderSettings(provider);
  if (!settings) throw new Error("SOCIAL_PROVIDER_UNAVAILABLE");
  const key = `${provider}\0${settings.issuer.href}\0${settings.clientId}`;
  let pending = configurations.get(key);
  if (!pending) {
    pending = oidc.discovery(
      settings.issuer,
      settings.clientId,
      settings.clientSecret,
      undefined,
      providerFetch ? { [oidc.customFetch]: providerFetch } : undefined,
    );
    pending.catch(() => configurations.delete(key));
    configurations.set(key, pending);
  }
  return pending;
}

export async function beginSocialLogin(
  provider: SocialProvider,
  redirectUri: URL,
): Promise<SocialLoginStart> {
  const config = await configuration(provider);
  const state = oidc.randomState();
  const nonce = oidc.randomNonce();
  const codeVerifier = oidc.randomPKCECodeVerifier();
  const authorizationUrl = oidc.buildAuthorizationUrl(config, {
    redirect_uri: redirectUri.href,
    scope: "openid",
    state,
    nonce,
    code_challenge: await oidc.calculatePKCECodeChallenge(codeVerifier),
    code_challenge_method: "S256",
  });
  return { authorizationUrl, state, nonce, codeVerifier };
}

/** Exchanges the code and verifies the ID token (issuer, audience, nonce, PKCE, state). */
export async function finishSocialLogin(
  provider: SocialProvider,
  currentUrl: URL,
  checks: { state: string; nonce: string; codeVerifier: string },
) {
  const config = await configuration(provider);
  const tokens = await oidc.authorizationCodeGrant(config, currentUrl, {
    expectedState: checks.state,
    expectedNonce: checks.nonce,
    pkceCodeVerifier: checks.codeVerifier,
    idTokenExpected: true,
  });
  const subject = tokens.claims()?.sub;
  if (typeof subject !== "string" || !subject || subject.length > 255) {
    throw new Error("SOCIAL_SUBJECT_MISSING");
  }
  return { subject };
}
