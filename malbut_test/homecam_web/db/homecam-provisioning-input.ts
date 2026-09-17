const DEVICE_ID_PATTERN = /^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$/;
const UUID_V4_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const TOKEN_DIGEST_PATTERN = /^[0-9a-f]{64}$/;
const MAX_CREDENTIAL_LIFETIME_MS = 366 * 24 * 60 * 60 * 1000;

export type HomecamProvisioningRequest = {
  deviceId: string;
  displayName: string;
  ownerEmail: string;
  sourceProfile: "sim" | "aurora" | "unknown";
  credential: {
    id: string;
    label: string;
    tokenDigest: string;
    expiresAt: string;
  };
};

export function parseHomecamProvisioningInput(
  value: unknown,
  now = new Date(),
): HomecamProvisioningRequest | null {
  if (!isRecord(value)) return null;
  const allowed = [
    "deviceId",
    "displayName",
    "ownerEmail",
    "sourceProfile",
    "credential",
  ];
  if (
    Object.keys(value).length !== allowed.length ||
    Object.keys(value).some((key) => !allowed.includes(key))
  ) {
    return null;
  }

  if (
    typeof value.deviceId !== "string" ||
    !DEVICE_ID_PATTERN.test(value.deviceId) ||
    typeof value.displayName !== "string" ||
    value.displayName !== value.displayName.trim() ||
    value.displayName.length < 1 ||
    value.displayName.length > 80 ||
    /[\u0000-\u001f\u007f]/.test(value.displayName) ||
    typeof value.ownerEmail !== "string" ||
    normalizeEmail(value.ownerEmail) !== value.ownerEmail ||
    typeof value.sourceProfile !== "string" ||
    !["sim", "aurora", "unknown"].includes(String(value.sourceProfile)) ||
    !isRecord(value.credential)
  ) {
    return null;
  }

  const credentialAllowed = ["id", "label", "tokenDigest", "expiresAt"];
  if (
    Object.keys(value.credential).length !== credentialAllowed.length ||
    Object.keys(value.credential).some(
      (key) => !credentialAllowed.includes(key),
    )
  ) {
    return null;
  }
  const credential = value.credential;
  if (
    typeof credential.id !== "string" ||
    !UUID_V4_PATTERN.test(credential.id) ||
    typeof credential.label !== "string" ||
    credential.label !== credential.label.trim() ||
    credential.label.length < 1 ||
    credential.label.length > 100 ||
    /[\u0000-\u001f\u007f]/.test(credential.label) ||
    typeof credential.tokenDigest !== "string" ||
    !TOKEN_DIGEST_PATTERN.test(credential.tokenDigest) ||
    typeof credential.expiresAt !== "string"
  ) {
    return null;
  }

  const expiresAtMs = Date.parse(credential.expiresAt);
  if (
    !Number.isFinite(expiresAtMs) ||
    new Date(expiresAtMs).toISOString() !== credential.expiresAt ||
    expiresAtMs <= now.getTime() ||
    expiresAtMs > now.getTime() + MAX_CREDENTIAL_LIFETIME_MS
  ) {
    return null;
  }

  return {
    deviceId: value.deviceId,
    displayName: value.displayName,
    ownerEmail: value.ownerEmail,
    sourceProfile: value.sourceProfile as
      | "sim"
      | "aurora"
      | "unknown",
    credential: {
      id: credential.id,
      label: credential.label,
      tokenDigest: credential.tokenDigest,
      expiresAt: credential.expiresAt,
    },
  };
}

export async function homecamProvisioningManifestSha256(
  input: HomecamProvisioningRequest,
) {
  const canonical = JSON.stringify({
    deviceId: input.deviceId,
    displayName: input.displayName,
    ownerEmail: input.ownerEmail,
    sourceProfile: input.sourceProfile,
    credential: {
      id: input.credential.id,
      label: input.credential.label,
      tokenDigest: input.credential.tokenDigest,
      expiresAt: input.credential.expiresAt,
    },
  });
  const digest = new Uint8Array(
    await crypto.subtle.digest(
      "SHA-256",
      new TextEncoder().encode(canonical),
    ),
  );
  return Array.from(digest, (value) =>
    value.toString(16).padStart(2, "0"),
  ).join("");
}

function normalizeEmail(value: string) {
  const normalized = value.trim().toLowerCase();
  if (
    normalized.length < 3 ||
    normalized.length > 254 ||
    !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(normalized)
  ) {
    return null;
  }
  return normalized;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
