import { getRuntimeEnvironment } from "./runtime-env";

/** Browser mutation from this site only (JSON body, matching Origin). */
export function sameOriginJsonRequest(request: Request) {
  if (request.headers.get("sec-fetch-site")?.toLowerCase() === "cross-site") return false;
  if (request.headers.get("content-type")?.split(";", 1)[0].trim().toLowerCase() !== "application/json") return false;
  const configured = getRuntimeEnvironment().AUTH_PUBLIC_ORIGIN?.trim();
  let expected = new URL(request.url).origin;
  if (configured) {
    try {
      const url = new URL(configured);
      if (url.protocol !== "https:" || url.pathname !== "/" || url.search || url.hash || url.username || url.password) return false;
      expected = url.origin;
    } catch { return false; }
  }
  return request.headers.get("origin") === expected;
}
