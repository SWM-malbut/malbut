import {
  getWebSessionUser,
  readCookie,
  WEB_SESSION_COOKIE,
} from "../db/web-auth";
import { getRuntimeEnvironment } from "./runtime-env";

const ALB_CLAIMS_HEADER = "x-amzn-oidc-data";
const ALB_IDENTITY_HEADER = "x-amzn-oidc-identity";
const DEV_USER_HEADER = "x-malbut-dev-user-email";
const JWT_COMPONENT_PATTERN = /^[A-Za-z0-9_-]+={0,2}$/;
const KEY_ID_PATTERN = /^[A-Za-z0-9_-]{1,128}$/;
const REGION_PATTERN = /^[a-z]{2}-[a-z]+-\d$/;
const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const ALB_ARN_PATTERN = /^arn:aws:elasticloadbalancing:[a-z0-9-]+:\d{12}:loadbalancer\/app\/[A-Za-z0-9._/-]+$/;
const MAX_ALB_TOKEN_LENGTH = 16_384;
const PUBLIC_KEY_CACHE_MS = 6 * 60 * 60 * 1000;

type AuthRuntimeEnvironment = {
  AUTH_MODE?: string;
  AUTH_SESSION_SECRET?: string;
  AUTH_AWS_REGION?: string;
  AUTH_ALB_ARN?: string;
  AUTH_OIDC_CLIENT_ID?: string;
  AUTH_OIDC_ISSUER?: string;
  AUTH_EMAIL_CLAIM?: string;
  AUTH_DEV_USER_EMAIL?: string;
  PETCAM_BROADCASTER_EMAILS?: string;
  NODE_ENV?: string;
};

type AlbJwtHeader = {
  alg?: unknown;
  kid?: unknown;
  signer?: unknown;
  client?: unknown;
  iss?: unknown;
  exp?: unknown;
};

type AlbClaims = Record<string, unknown> & {
  sub?: unknown;
  name?: unknown;
};

export type AuthenticatedUser = {
  email: string;
  fullName: string | null;
  subject: string;
};

type CachedPublicKey = {
  key: CryptoKey;
  expiresAt: number;
};

const publicKeyCache = new Map<string, CachedPublicKey>();

export async function getRequestUserEmail(request: Request): Promise<string | null> {
  return (await getAuthenticatedUser(request.headers, request.url))?.email ?? null;
}

export async function getAuthenticatedUser(
  headers: Headers,
  requestUrl?: string,
): Promise<AuthenticatedUser | null> {
  const runtime = getRuntimeEnvironment() as AuthRuntimeEnvironment;
  if (runtime.AUTH_MODE === "dev_header") {
    return developmentHeaderUser(headers, requestUrl, runtime);
  }

  if (
    runtime.AUTH_MODE === "cognito_session" ||
    runtime.AUTH_MODE === "alb_oidc_or_cognito_session"
  ) {
    const sessionUser = await opaqueSessionUser(headers, runtime);
    if (sessionUser) return sessionUser;
    if (runtime.AUTH_MODE === "cognito_session") return null;
  }

  if (
    runtime.AUTH_MODE === "alb_oidc" ||
    runtime.AUTH_MODE === "alb_oidc_or_cognito_session"
  ) {
    try {
      return await verifiedAlbUser(headers, runtime);
    } catch {
      // Authentication is fail-closed. Do not log tokens or claims.
      return null;
    }
  }

  return null;
}

export function canBroadcastForConfiguredAccount(userEmail: string) {
  const runtime = getRuntimeEnvironment() as AuthRuntimeEnvironment;
  const configured = runtime.PETCAM_BROADCASTER_EMAILS ?? "";
  const normalizedUser = normalizeEmail(userEmail);
  if (!normalizedUser) return false;
  return configured
    .split(",")
    .map(normalizeEmail)
    .filter((email): email is string => Boolean(email))
    .includes(normalizedUser);
}

async function opaqueSessionUser(
  headers: Headers,
  runtime: AuthRuntimeEnvironment,
): Promise<AuthenticatedUser | null> {
  const secret = runtime.AUTH_SESSION_SECRET?.trim();
  const token = readCookie(headers.get("cookie"), WEB_SESSION_COOKIE);
  if (!secret || !token) return null;
  try {
    const user = await getWebSessionUser(token, secret);
    if (!user) return null;
    const email = normalizeEmail(user.email);
    if (!email || !validSubject(user.subject)) return null;
    return {
      email,
      fullName: normalizeDisplayName(user.fullName),
      subject: user.subject,
    };
  } catch {
    // Database/auth configuration errors fail closed. Never log session cookies.
    return null;
  }
}

async function verifiedAlbUser(
  headers: Headers,
  runtime: AuthRuntimeEnvironment,
): Promise<AuthenticatedUser | null> {
  const region = runtime.AUTH_AWS_REGION?.trim();
  const expectedSigner = runtime.AUTH_ALB_ARN?.trim();
  const expectedClient = runtime.AUTH_OIDC_CLIENT_ID?.trim();
  const expectedIssuer = runtime.AUTH_OIDC_ISSUER?.trim();
  if (
    !region ||
    !REGION_PATTERN.test(region) ||
    !expectedSigner ||
    !ALB_ARN_PATTERN.test(expectedSigner) ||
    !expectedClient ||
    expectedClient.length > 256 ||
    !expectedIssuer ||
    !isHttpsUrl(expectedIssuer)
  ) {
    return null;
  }

  const token = headers.get(ALB_CLAIMS_HEADER)?.trim();
  if (!token || token.length > MAX_ALB_TOKEN_LENGTH) return null;
  const components = token.split(".");
  if (
    components.length !== 3 ||
    components.some((component) => !JWT_COMPONENT_PATTERN.test(component))
  ) {
    return null;
  }

  const [encodedHeader, encodedClaims, encodedSignature] = components;
  const jwtHeader = parseJsonComponent<AlbJwtHeader>(encodedHeader);
  const claims = parseJsonComponent<AlbClaims>(encodedClaims);
  if (!jwtHeader || !claims) return null;

  const expiresAt = Number(jwtHeader.exp);
  if (
    jwtHeader.alg !== "ES256" ||
    typeof jwtHeader.kid !== "string" ||
    !KEY_ID_PATTERN.test(jwtHeader.kid) ||
    jwtHeader.signer !== expectedSigner ||
    jwtHeader.client !== expectedClient ||
    jwtHeader.iss !== expectedIssuer ||
    !Number.isFinite(expiresAt) ||
    expiresAt <= Date.now() / 1000
  ) {
    return null;
  }

  const signature = decodeBase64Url(encodedSignature);
  if (!signature || signature.byteLength !== 64) return null;
  const publicKey = await loadAlbPublicKey(region, jwtHeader.kid);
  const verified = await crypto.subtle.verify(
    { name: "ECDSA", hash: "SHA-256" },
    publicKey,
    signature,
    new TextEncoder().encode(`${encodedHeader}.${encodedClaims}`),
  );
  if (!verified) return null;

  const subject = typeof claims.sub === "string" ? claims.sub.trim() : "";
  const albIdentity = headers.get(ALB_IDENTITY_HEADER)?.trim();
  if (!subject || subject.length > 256 || !albIdentity || albIdentity !== subject) {
    return null;
  }

  const claimName = runtime.AUTH_EMAIL_CLAIM?.trim() || "email";
  if (!/^[A-Za-z][A-Za-z0-9_.:-]{0,63}$/.test(claimName)) return null;
  const email = normalizeEmail(claims[claimName]);
  if (!email) return null;
  const fullName = normalizeDisplayName(claims.name);
  return { email, fullName, subject };
}

function developmentHeaderUser(
  headers: Headers,
  requestUrl: string | undefined,
  runtime: AuthRuntimeEnvironment,
): AuthenticatedUser | null {
  if (runtime.NODE_ENV === "production" || !isLoopbackRequest(headers, requestUrl)) {
    return null;
  }
  const expectedEmail = normalizeEmail(runtime.AUTH_DEV_USER_EMAIL);
  const presentedEmail = normalizeEmail(headers.get(DEV_USER_HEADER));
  if (!expectedEmail || presentedEmail !== expectedEmail) return null;
  return { email: expectedEmail, fullName: null, subject: `dev:${expectedEmail}` };
}

function isLoopbackRequest(headers: Headers, requestUrl?: string): boolean {
  let hostname = "";
  if (requestUrl) {
    try {
      hostname = new URL(requestUrl).hostname;
    } catch {
      return false;
    }
  } else {
    const host = headers.get("host")?.trim() ?? "";
    try {
      hostname = new URL(`http://${host}`).hostname;
    } catch {
      return false;
    }
  }
  return hostname === "localhost" || hostname === "127.0.0.1" || hostname === "[::1]";
}

async function loadAlbPublicKey(region: string, keyId: string): Promise<CryptoKey> {
  const cacheKey = `${region}:${keyId}`;
  const cached = publicKeyCache.get(cacheKey);
  if (cached && cached.expiresAt > Date.now()) return cached.key;

  const response = await fetch(
    `https://public-keys.auth.elb.${region}.amazonaws.com/${encodeURIComponent(keyId)}`,
    {
      cache: "force-cache",
      signal: AbortSignal.timeout(5_000),
    },
  );
  if (!response.ok) throw new Error("ALB_PUBLIC_KEY_UNAVAILABLE");
  const pem = await response.text();
  const der = pemToDer(pem);
  const key = await crypto.subtle.importKey(
    "spki",
    der,
    { name: "ECDSA", namedCurve: "P-256" },
    false,
    ["verify"],
  );
  publicKeyCache.set(cacheKey, {
    key,
    expiresAt: Date.now() + PUBLIC_KEY_CACHE_MS,
  });
  return key;
}

function pemToDer(value: string): Uint8Array<ArrayBuffer> {
  const match = /^-----BEGIN PUBLIC KEY-----\s+([A-Za-z0-9+/=\s]+)\s+-----END PUBLIC KEY-----\s*$/.exec(
    value,
  );
  if (!match) throw new Error("ALB_PUBLIC_KEY_INVALID");
  return Uint8Array.from(Buffer.from(match[1].replace(/\s/g, ""), "base64"));
}

function parseJsonComponent<T>(value: string): T | null {
  const decoded = decodeBase64Url(value);
  if (!decoded) return null;
  try {
    const parsed = JSON.parse(new TextDecoder().decode(decoded));
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as T)
      : null;
  } catch {
    return null;
  }
}

function decodeBase64Url(value: string): Uint8Array<ArrayBuffer> | null {
  try {
    return Uint8Array.from(
      Buffer.from(value.replace(/=+$/, ""), "base64url"),
    );
  } catch {
    return null;
  }
}

function normalizeEmail(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const normalized = value.trim().toLowerCase();
  if (
    normalized.length < 3 ||
    normalized.length > 254 ||
    !EMAIL_PATTERN.test(normalized)
  ) {
    return null;
  }
  return normalized;
}

function normalizeDisplayName(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const normalized = value.trim();
  if (!normalized || normalized.length > 200 || /[\u0000-\u001f\u007f]/.test(normalized)) {
    return null;
  }
  return normalized;
}

function validSubject(value: string) {
  return Boolean(value && value.length <= 256 && !/[\u0000-\u001f\u007f]/.test(value));
}

function isHttpsUrl(value: string): boolean {
  try {
    return new URL(value).protocol === "https:";
  } catch {
    return false;
  }
}

export function clearAlbPublicKeyCacheForTests() {
  publicKeyCache.clear();
}
