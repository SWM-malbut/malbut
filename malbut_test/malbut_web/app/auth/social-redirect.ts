import { NextResponse } from "next/server";
import { publicUrl } from "../social-auth";

/** Back to the login screen with a short reason the screen explains. */
export function loginRedirect(request: Request, returnTo: string, reason: string) {
  const target = publicUrl("/auth/login", request.url);
  target.searchParams.set("return_to", returnTo);
  target.searchParams.set("error", reason);
  return noStoreRedirect(target);
}

export function noStoreRedirect(target: URL) {
  const response = NextResponse.redirect(target, 303);
  response.headers.set("cache-control", "no-store");
  return response;
}
