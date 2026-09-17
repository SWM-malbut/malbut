export const LOGOUT_RETURN_COOKIE = "__Host-malbut_logout_return";
export const LOGOUT_RETURN_TTL_SECONDS = 5 * 60;

export type LogoutResponseMode = "hosted_ui" | "local";

export type LogoutResponse = {
  ok?: unknown;
  mode?: unknown;
  redirectTo?: unknown;
};

export function usesHostedUiLogout(authMode: string | undefined): boolean {
  return authMode === "alb_oidc" || authMode === "alb_oidc_or_cognito_session";
}

export function logoutStartPath(returnTo: string): string {
  const query = new URLSearchParams({
    continue: "hosted",
    return_to: safeLogoutReturnPath(returnTo),
  });
  return `/auth/logout?${query.toString()}`;
}

export function logoutNavigationPath(
  payload: LogoutResponse,
  fallback = "/",
): string {
  if (
    payload.ok !== true ||
    (payload.mode !== "hosted_ui" && payload.mode !== "local")
  ) {
    return safeLogoutReturnPath(fallback);
  }
  if (payload.mode === "hosted_ui") {
    return safeHostedLogoutStartPath(payload.redirectTo, fallback);
  }
  return safeLogoutReturnPath(payload.redirectTo, safeLogoutReturnPath(fallback));
}

export function safeLogoutReturnPath(value: unknown, fallback = "/"): string {
  if (
    typeof value !== "string" ||
    value.length > 512 ||
    !value.startsWith("/") ||
    value.startsWith("//")
  ) {
    return fallback;
  }
  try {
    const url = new URL(value, "https://app.local");
    if (url.origin !== "https://app.local" || url.pathname.startsWith("/auth/")) {
      return fallback;
    }
    return `${url.pathname}${url.search}${url.hash}`;
  } catch {
    return fallback;
  }
}

export function encodeLogoutReturnPath(value: string): string {
  const bytes = new TextEncoder().encode(safeLogoutReturnPath(value));
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

export function decodeLogoutReturnPath(value: string | null): string {
  if (!value || !/^[A-Za-z0-9_-]{1,2731}$/.test(value)) return "/";
  try {
    const base64 = value.replaceAll("-", "+").replaceAll("_", "/");
    const padded = base64.padEnd(Math.ceil(base64.length / 4) * 4, "=");
    const binary = atob(padded);
    const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
    return safeLogoutReturnPath(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  } catch {
    return "/";
  }
}

function safeHostedLogoutStartPath(value: unknown, fallback: string) {
  if (typeof value !== "string" || !value.startsWith("/") || value.startsWith("//")) {
    return safeLogoutReturnPath(fallback);
  }
  try {
    const url = new URL(value, "https://app.local");
    const allowedParameters = [...url.searchParams.keys()].every(
      (name) => name === "continue" || name === "return_to",
    );
    if (
      url.origin !== "https://app.local" ||
      url.pathname !== "/auth/logout" ||
      url.searchParams.get("continue") !== "hosted" ||
      url.hash ||
      !allowedParameters
    ) {
      return safeLogoutReturnPath(fallback);
    }
    return logoutStartPath(
      safeLogoutReturnPath(url.searchParams.get("return_to")),
    );
  } catch {
    return safeLogoutReturnPath(fallback);
  }
}
