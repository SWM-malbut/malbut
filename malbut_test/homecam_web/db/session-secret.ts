const PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
const PASSWORD_LENGTH = 16;
export const SESSION_AUTH_VERSION = "hmac-sha256-v1";

export function createViewerPassword() {
  const bytes = crypto.getRandomValues(new Uint8Array(PASSWORD_LENGTH));
  const raw = Array.from(
    bytes,
    (value) => PASSWORD_ALPHABET[value % PASSWORD_ALPHABET.length],
  ).join("");
  return raw.match(/.{1,4}/g)?.join("-") ?? raw;
}

export function normalizeViewerPassword(value: string) {
  return value.replace(/[^A-HJ-NP-Z2-9]/gi, "").slice(0, PASSWORD_LENGTH).toUpperCase();
}

export function isValidViewerPassword(value: string) {
  return /^[A-HJ-NP-Z2-9]{16}$/.test(normalizeViewerPassword(value));
}

export async function createViewerPasswordVerifier(
  sessionId: string,
  password: string,
  secret: string,
) {
  const key = await importHmacKey(secret, ["sign"]);
  const signature = await crypto.subtle.sign(
    "HMAC",
    key,
    verifierMessage(sessionId, password),
  );
  return bytesToHex(new Uint8Array(signature));
}

export async function verifyViewerPassword(
  sessionId: string,
  password: string,
  expectedDigest: string,
  secret: string,
) {
  const signature = hexToBytes(expectedDigest);
  if (!signature) return false;
  const key = await importHmacKey(secret, ["verify"]);
  return crypto.subtle.verify(
    "HMAC",
    key,
    signature,
    verifierMessage(sessionId, password),
  );
}

function verifierMessage(sessionId: string, password: string) {
  return new TextEncoder().encode(
    `${SESSION_AUTH_VERSION}:${sessionId}:${normalizeViewerPassword(password)}`,
  );
}

function importHmacKey(secret: string, keyUsages: KeyUsage[]) {
  return crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    keyUsages,
  );
}

function bytesToHex(bytes: Uint8Array) {
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function hexToBytes(value: string) {
  if (!/^[a-f0-9]{64}$/i.test(value)) return null;
  return new Uint8Array(value.match(/.{2}/g)!.map((byte) => Number.parseInt(byte, 16)));
}
