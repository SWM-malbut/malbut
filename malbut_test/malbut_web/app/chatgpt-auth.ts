import { headers } from "next/headers";
import { redirect } from "next/navigation";
import { getRuntimeValue } from "./runtime-env";
import { getAuthenticatedUser, userIdFor } from "./server-auth";
import { getUserProfile } from "../db/users";

export type ChatGPTUser = {
  userId: string;
  /** The name other people see; null until the person chooses one. */
  chosenName: string | null;
  displayName: string;
  email: string | null;
  fullName: string | null;
};

const DEFAULT_SIGN_IN_PATH = "/auth/login";
const DEFAULT_SIGN_OUT_PATH = "/auth/logout";

// The legacy export names are retained so existing server components can be
// migrated independently from the authentication provider.
export async function getChatGPTUser(): Promise<ChatGPTUser | null> {
  const requestHeaders = await headers();
  const user = await getAuthenticatedUser(requestHeaders, requestUrl(requestHeaders));
  if (!user) return null;
  const userId = await userIdFor(user);
  if (!userId) return null;
  const profile = await getUserProfile(userId);
  return {
    userId,
    chosenName: profile.displayName,
    displayName: profile.displayName ?? user.fullName ?? user.email ?? "이름 없는 사용자",
    email: user.email,
    fullName: user.fullName,
  };
}

export async function requireChatGPTUser(returnTo: string): Promise<ChatGPTUser> {
  const user = await getChatGPTUser();
  if (user) return user;
  redirect(chatGPTSignInPath(returnTo));
}

export function chatGPTSignInPath(returnTo: string): string {
  return authActionPath(
    getRuntimeValue("AUTH_SIGN_IN_PATH"),
    DEFAULT_SIGN_IN_PATH,
    returnTo,
  );
}

export function chatGPTSignOutPath(returnTo = "/"): string {
  return authActionPath(
    getRuntimeValue("AUTH_SIGN_OUT_PATH"),
    DEFAULT_SIGN_OUT_PATH,
    returnTo,
  );
}

function authActionPath(
  configuredPath: string | undefined,
  fallbackPath: string,
  returnTo: string,
): string {
  const actionPath = safeAuthPath(configuredPath) ?? fallbackPath;
  const safeReturnTo = safeRelativeReturnPath(returnTo, [actionPath]);
  return `${actionPath}?return_to=${encodeURIComponent(safeReturnTo)}`;
}

function safeAuthPath(value: string | undefined): string | null {
  if (!value || !value.startsWith("/") || value.startsWith("//")) return null;
  try {
    const url = new URL(value, "https://app.local");
    if (url.origin !== "https://app.local" || url.search || url.hash) return null;
    return url.pathname;
  } catch {
    return null;
  }
}

function safeRelativeReturnPath(value: string, reservedPaths: string[]): string {
  if (!value.startsWith("/") || value.startsWith("//")) return "/";
  try {
    const url = new URL(value, "https://app.local");
    if (url.origin !== "https://app.local" || reservedPaths.includes(url.pathname)) {
      return "/";
    }
    return `${url.pathname}${url.search}${url.hash}`;
  } catch {
    return "/";
  }
}

function requestUrl(requestHeaders: Headers): string | undefined {
  const host = requestHeaders.get("host")?.trim();
  if (!host) return undefined;
  const forwardedProtocol = requestHeaders.get("x-forwarded-proto")?.trim();
  const protocol = forwardedProtocol === "https" ? "https" : "http";
  try {
    return new URL(`${protocol}://${host}/`).toString();
  } catch {
    return undefined;
  }
}
