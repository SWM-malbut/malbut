export type LoginStep = "credentials" | "new_password" | "mfa";

export type LoginResponse = {
  ok?: boolean;
  authenticated?: boolean;
  redirectTo?: unknown;
  challenge?: unknown;
  next?: unknown;
  message?: unknown;
  error?: unknown;
};

export function safeRelativeReturnPath(value: unknown, fallback = "/"): string {
  if (typeof value !== "string" || !value.startsWith("/") || value.startsWith("//")) {
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

export function loginApiPath(returnTo: string): string {
  const query = new URLSearchParams({
    return_to: safeRelativeReturnPath(returnTo),
  });
  return `/api/auth/login?${query.toString()}`;
}

export function responseLoginStep(payload: LoginResponse): LoginStep | null {
  const challenge = payload.challenge ?? payload.next;
  if (challenge === "new_password" || challenge === "NEW_PASSWORD_REQUIRED") {
    return "new_password";
  }
  if (challenge === "mfa" || challenge === "SOFTWARE_TOKEN_MFA") {
    return "mfa";
  }
  return null;
}

export function responseMessage(payload: LoginResponse, fallback: string): string {
  for (const value of [payload.message, payload.error]) {
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return fallback;
}

export function successfulLoginRedirect(
  payload: LoginResponse,
  returnTo: string,
): string | null {
  if (payload.ok !== true && payload.authenticated !== true) return null;
  return safeRelativeReturnPath(payload.redirectTo, safeRelativeReturnPath(returnTo));
}
