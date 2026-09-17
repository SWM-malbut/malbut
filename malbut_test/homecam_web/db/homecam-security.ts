const DEVICE_TOKEN_PREFIX = "hc1";
const DEVICE_TOKEN_PATTERN =
  /^hc1\.([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})\.([a-f0-9]{64})$/i;

export type DeviceTokenParts = {
  credentialId: string;
  token: string;
};

export function createDeviceToken(): DeviceTokenParts {
  const credentialId = crypto.randomUUID();
  const secretBytes = crypto.getRandomValues(new Uint8Array(32));
  const secret = Array.from(secretBytes, (value) =>
    value.toString(16).padStart(2, "0"),
  ).join("");
  return {
    credentialId,
    token: `${DEVICE_TOKEN_PREFIX}.${credentialId}.${secret}`,
  };
}

export function parseDeviceToken(value: string): DeviceTokenParts | null {
  const match = DEVICE_TOKEN_PATTERN.exec(value);
  if (!match) return null;
  return { credentialId: match[1].toLowerCase(), token: value };
}

export async function hashDeviceToken(token: string): Promise<string> {
  const digest = new Uint8Array(
    await crypto.subtle.digest("SHA-256", new TextEncoder().encode(token)),
  );
  return Array.from(digest, (value) => value.toString(16).padStart(2, "0")).join(
    "",
  );
}

export function getBearerToken(authorization: string | null): string | null {
  if (!authorization) return null;
  const match = /^Bearer ([^\s]+)$/i.exec(authorization.trim());
  return match?.[1] ?? null;
}

export function isCredentialActive(
  credential: { revokedAt: string | null; expiresAt: string | null },
  now = new Date(),
) {
  if (credential.revokedAt) return false;
  if (!credential.expiresAt) return true;
  const expiresAt = Date.parse(credential.expiresAt);
  return Number.isFinite(expiresAt) && expiresAt > now.getTime();
}
