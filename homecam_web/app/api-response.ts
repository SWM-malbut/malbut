export function noStore(body: unknown, status = 200, headers?: HeadersInit) {
  const responseHeaders = new Headers(headers);
  responseHeaders.set("cache-control", "no-store");
  return Response.json(body, { status, headers: responseHeaders });
}

export function unauthorized(message = "인증이 필요합니다.") {
  return noStore({ error: message }, 401, {
    "www-authenticate": 'Bearer realm="homecam-device"',
  });
}
