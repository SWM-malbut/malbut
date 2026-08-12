const KVS_ARCHIVED_MEDIA_HOST =
  /^[a-z0-9-]+\.kinesisvideo\.ap-northeast-2\.amazonaws\.com$/;
const PLAYBACK_COOKIE_PREFIX = "petcam_hls_";
const PLAYBACK_GRANT_VERSION = 1;
const PLAYBACK_KEY_CONTEXT = "petcam-hls-proxy-v1";
const KVS_HLS_BASE_PATH = "/hls/v1";
const HLS_RESOURCES = new Set([
  "getHLSMasterPlaylist.m3u8",
  "getHLSMediaPlaylist.m3u8",
  "getMP4InitFragment.mp4",
  "getMP4MediaFragment.mp4",
]);

type PlaybackGrant = {
  version: 1;
  recordingId: string;
  playbackId: string;
  userHash: string;
  hostname: string;
  sessionToken: string;
  expiresAt: number;
};

type CreatePlaybackProxyInput = {
  requestUrl: string;
  playbackUrl: string;
  recordingId: string;
  userEmail: string;
  expiresAt: string;
};

type ResolvePlaybackProxyInput = {
  requestUrl: string;
  recordingId: string;
  playbackId: string;
  resource: string;
  userEmail: string;
  cookieHeader: string | null;
};

export type ResolvedPlaybackProxy = {
  upstreamUrl: URL;
  hostname: string;
  sessionToken: string;
  rewritePlaylist: boolean;
};

export async function createRecordingPlaybackProxy(
  input: CreatePlaybackProxyInput,
  secret: string,
) {
  if (!secret) throw new Error("PLAYBACK_PROXY_NOT_CONFIGURED");

  const upstream = new URL(input.playbackUrl);
  const sessionTokens = upstream.searchParams.getAll("SessionToken");
  const expiresAt = Math.floor(Date.parse(input.expiresAt) / 1000);
  if (
    upstream.protocol !== "https:" ||
    upstream.username ||
    upstream.password ||
    upstream.port ||
    upstream.hash ||
    !KVS_ARCHIVED_MEDIA_HOST.test(upstream.hostname) ||
    upstream.pathname !== `${KVS_HLS_BASE_PATH}/getHLSMasterPlaylist.m3u8` ||
    Array.from(upstream.searchParams.keys()).some((key) => key !== "SessionToken") ||
    sessionTokens.length !== 1 ||
    !sessionTokens[0] ||
    !Number.isSafeInteger(expiresAt) ||
    expiresAt <= Math.floor(Date.now() / 1000)
  ) {
    throw new Error("PLAYBACK_PROXY_URL_INVALID");
  }

  const playbackId = crypto.randomUUID().replace(/-/g, "");
  const grant: PlaybackGrant = {
    version: PLAYBACK_GRANT_VERSION,
    recordingId: input.recordingId,
    playbackId,
    userHash: await sha256(input.userEmail.trim().toLowerCase()),
    hostname: upstream.hostname,
    sessionToken: sessionTokens[0],
    expiresAt,
  };
  const cookieValue = await encryptGrant(grant, secret);
  const request = new URL(input.requestUrl);
  const path = `/api/recordings/${encodeURIComponent(input.recordingId)}/hls/${playbackId}/`;
  const maxAge = Math.max(1, expiresAt - Math.floor(Date.now() / 1000));
  const cookie = [
    `${PLAYBACK_COOKIE_PREFIX}${playbackId}=${cookieValue}`,
    `Path=${path}`,
    `Max-Age=${maxAge}`,
    "HttpOnly",
    "SameSite=Strict",
    ...(request.protocol === "https:" ? ["Secure"] : []),
  ].join("; ");

  return {
    playbackUrl: new URL(`${path}getHLSMasterPlaylist.m3u8`, request).toString(),
    setCookie: cookie,
  };
}

export async function resolveRecordingPlaybackProxy(
  input: ResolvePlaybackProxyInput,
  secret: string,
): Promise<ResolvedPlaybackProxy | null> {
  if (
    !secret ||
    !/^[0-9a-f]{32}$/.test(input.playbackId) ||
    !HLS_RESOURCES.has(input.resource)
  ) {
    return null;
  }

  const cookie = readCookie(
    input.cookieHeader,
    `${PLAYBACK_COOKIE_PREFIX}${input.playbackId}`,
  );
  if (!cookie) return null;

  const grant = await decryptGrant(
    cookie,
    secret,
    input.recordingId,
    input.playbackId,
  );
  if (
    !grant ||
    grant.version !== PLAYBACK_GRANT_VERSION ||
    grant.recordingId !== input.recordingId ||
    grant.playbackId !== input.playbackId ||
    grant.expiresAt <= Math.floor(Date.now() / 1000) ||
    !KVS_ARCHIVED_MEDIA_HOST.test(grant.hostname) ||
    !(await constantTimeEqual(
      grant.userHash,
      await sha256(input.userEmail.trim().toLowerCase()),
    ))
  ) {
    return null;
  }

  const incoming = new URL(input.requestUrl);
  if (!validClientQuery(input.resource, incoming.searchParams)) return null;

  const upstreamUrl = new URL(
    `https://${grant.hostname}${KVS_HLS_BASE_PATH}/${input.resource}`,
  );
  upstreamUrl.searchParams.set("SessionToken", grant.sessionToken);
  incoming.searchParams.forEach((value, key) => {
    upstreamUrl.searchParams.append(key, value);
  });
  return {
    upstreamUrl,
    hostname: grant.hostname,
    sessionToken: grant.sessionToken,
    rewritePlaylist: input.resource.endsWith(".m3u8"),
  };
}

export function rewriteRecordingPlaylist(
  source: string,
  hostname: string,
  sessionToken: string,
) {
  const rewriteUri = (value: string) => {
    const resourceUrl = new URL(value, `https://${hostname}${KVS_HLS_BASE_PATH}/`);
    const resource = resourceUrl.pathname.slice(KVS_HLS_BASE_PATH.length + 1);
    const tokens = resourceUrl.searchParams.getAll("SessionToken");
    if (
      resourceUrl.protocol !== "https:" ||
      resourceUrl.hostname !== hostname ||
      resourceUrl.username ||
      resourceUrl.password ||
      resourceUrl.port ||
      resourceUrl.hash ||
      resourceUrl.pathname !== `${KVS_HLS_BASE_PATH}/${resource}` ||
      !HLS_RESOURCES.has(resource) ||
      tokens.length !== 1 ||
      tokens[0] !== sessionToken
    ) {
      throw new Error("PLAYBACK_PLAYLIST_INVALID");
    }
    resourceUrl.searchParams.delete("SessionToken");
    if (!validClientQuery(resource, resourceUrl.searchParams)) {
      throw new Error("PLAYBACK_PLAYLIST_INVALID");
    }
    return `${resource}${resourceUrl.search}`;
  };

  return source
    .split(/\r?\n/)
    .map((line) => {
      if (!line || line.startsWith("#")) {
        return line.replace(/URI="([^"]+)"/g, (_match, uri: string) => {
          return `URI="${rewriteUri(uri)}"`;
        });
      }
      return rewriteUri(line.trim());
    })
    .join("\n");
}

function validClientQuery(resource: string, params: URLSearchParams) {
  const names = Array.from(params.keys());
  const hasOnly = (expected: string[]) =>
    names.length === expected.length &&
    expected.every((name) => params.getAll(name).length === 1);

  if (resource === "getHLSMasterPlaylist.m3u8") return hasOnly([]);
  if (
    resource === "getHLSMediaPlaylist.m3u8" ||
    resource === "getMP4InitFragment.mp4"
  ) {
    return hasOnly(["TrackNumber"]) && /^(1|2)$/.test(params.get("TrackNumber") ?? "");
  }
  if (resource === "getMP4MediaFragment.mp4") {
    return (
      hasOnly(["FragmentNumber", "TrackNumber"]) &&
      /^\d{1,128}$/.test(params.get("FragmentNumber") ?? "") &&
      /^(1|2)$/.test(params.get("TrackNumber") ?? "")
    );
  }
  return false;
}

async function encryptGrant(grant: PlaybackGrant, secret: string) {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const key = await deriveGrantKey(secret);
  const additionalData = grantAdditionalData(grant.recordingId, grant.playbackId);
  const encrypted = new Uint8Array(
    await crypto.subtle.encrypt(
      { name: "AES-GCM", iv, additionalData },
      key,
      new TextEncoder().encode(JSON.stringify(grant)),
    ),
  );
  const combined = new Uint8Array(iv.length + encrypted.length);
  combined.set(iv);
  combined.set(encrypted, iv.length);
  return encodeBase64Url(combined);
}

async function decryptGrant(
  value: string,
  secret: string,
  recordingId: string,
  playbackId: string,
) {
  try {
    const combined = decodeBase64Url(value);
    if (!combined || combined.length <= 28) return null;
    const iv = combined.slice(0, 12);
    const encrypted = combined.slice(12);
    const key = await deriveGrantKey(secret);
    const additionalData = grantAdditionalData(recordingId, playbackId);
    const cleartext = await crypto.subtle.decrypt(
      { name: "AES-GCM", iv, additionalData },
      key,
      encrypted,
    );
    return JSON.parse(new TextDecoder().decode(cleartext)) as PlaybackGrant;
  } catch {
    return null;
  }
}

async function deriveGrantKey(secret: string) {
  const material = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    "HKDF",
    false,
    ["deriveKey"],
  );
  return crypto.subtle.deriveKey(
    {
      name: "HKDF",
      hash: "SHA-256",
      salt: new TextEncoder().encode(PLAYBACK_KEY_CONTEXT),
      info: new TextEncoder().encode("grant-encryption"),
    },
    material,
    { name: "AES-GCM", length: 256 },
    false,
    ["encrypt", "decrypt"],
  );
}

function grantAdditionalData(recordingId: string, playbackId: string) {
  return new TextEncoder().encode(
    `${PLAYBACK_KEY_CONTEXT}:${recordingId}:${playbackId}`,
  );
}

async function sha256(value: string) {
  const digest = new Uint8Array(
    await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)),
  );
  return Array.from(digest, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function constantTimeEqual(left: string, right: string) {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return difference === 0;
}

function readCookie(header: string | null, name: string) {
  if (!header) return null;
  for (const item of header.split(";")) {
    const [cookieName, ...value] = item.trim().split("=");
    if (cookieName === name) return value.join("=");
  }
  return null;
}

function encodeBase64Url(bytes: Uint8Array) {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary)
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/g, "");
}

function decodeBase64Url(value: string) {
  try {
    if (!/^[A-Za-z0-9_-]+$/.test(value)) return null;
    const base64 = value.replace(/-/g, "+").replace(/_/g, "/");
    const binary = atob(base64.padEnd(Math.ceil(base64.length / 4) * 4, "="));
    return Uint8Array.from(binary, (character) => character.charCodeAt(0));
  } catch {
    return null;
  }
}
