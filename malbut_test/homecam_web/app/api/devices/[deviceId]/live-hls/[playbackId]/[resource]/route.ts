import { userCanViewDevice } from "../../../../../../../db/homecam";
import {
  resolveDeviceLivePlaybackProxy,
  rewriteRecordingPlaylist,
} from "../../../../../../recording-playback-proxy";
import { getRuntimeEnvironment } from "../../../../../../runtime-env";
import { getRequestUserEmail } from "../../../../../../server-auth";

export const dynamic = "force-dynamic";

type PlaybackProxyEnv = { KVS_BROKER_SECRET?: string };

const FORWARDED_RESPONSE_HEADERS = [
  "accept-ranges",
  "content-length",
  "content-range",
  "content-type",
  "etag",
  "last-modified",
];

export async function GET(
  request: Request,
  context: {
    params: Promise<{ deviceId: string; playbackId: string; resource: string }>;
  },
) {
  const userEmail = await getRequestUserEmail(request);
  if (!userEmail) return failure(401);
  const { deviceId, playbackId, resource } = await context.params;
  if (!(await userCanViewDevice(deviceId, userEmail))) return failure(403);

  const runtime = getRuntimeEnvironment() as PlaybackProxyEnv;
  const playback = await resolveDeviceLivePlaybackProxy(
    {
      requestUrl: request.url,
      deviceId,
      playbackId,
      resource,
      userEmail,
      cookieHeader: request.headers.get("cookie"),
    },
    runtime.KVS_BROKER_SECRET ?? "",
  );
  if (!playback) return failure(403);

  const requestHeaders = new Headers();
  const range = request.headers.get("range");
  if (range) requestHeaders.set("range", range);
  try {
    const upstream = await fetch(playback.upstreamUrl, {
      headers: requestHeaders,
      redirect: "manual",
      signal: request.signal,
    });
    if (!upstream.ok) return failure(upstream.status >= 400 ? upstream.status : 502);
    const responseHeaders = responseHeadersFor(upstream.headers);
    if (playback.rewritePlaylist) {
      responseHeaders.delete("content-length");
      return new Response(
        rewriteRecordingPlaylist(
          await upstream.text(),
          playback.hostname,
          playback.sessionToken,
        ),
        { status: upstream.status, headers: responseHeaders },
      );
    }
    return new Response(upstream.body, {
      status: upstream.status,
      headers: responseHeaders,
    });
  } catch {
    return failure(502);
  }
}

function responseHeadersFor(upstream: Headers) {
  const responseHeaders = new Headers({
    "cache-control": "private, no-store",
    "cross-origin-resource-policy": "same-origin",
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
  });
  for (const name of FORWARDED_RESPONSE_HEADERS) {
    const value = upstream.get(name);
    if (value) responseHeaders.set(name, value);
  }
  return responseHeaders;
}

function failure(status: number) {
  return new Response(null, {
    status,
    headers: { "cache-control": "private, no-store" },
  });
}
