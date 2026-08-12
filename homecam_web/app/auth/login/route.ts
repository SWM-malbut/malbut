import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

// This path is protected by the ALB authenticate-cognito default rule. Reaching
// the target therefore means authentication succeeded; redirect to the safe
// application destination without exposing Cognito tokens to the browser app.
export async function GET(request: Request) {
  const url = new URL(request.url);
  return NextResponse.redirect(
    new URL(safeReturnPath(url.searchParams.get("return_to")), url.origin),
    303,
  );
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
