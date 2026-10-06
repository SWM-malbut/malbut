// Keys at rest: AES-256-GCM, key derived (HKDF-SHA256) from FALL_KEY_ENCRYPTION_SECRET.
// The device, service and key version are bound as associated data, so a
// ciphertext cannot be moved to another robot, service or version.
const FALL_INFO = new TextEncoder().encode("malbut-fall-cloud-key-v1");
const SERVICE_INFO = new TextEncoder().encode("malbut-service-key-v1");

async function deriveKey(secret: string, info: BufferSource) {
  if (typeof secret !== "string" || secret.length < 32) throw new Error("FALL_KEY_SECRET_MISSING");
  const material = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret), "HKDF", false, ["deriveKey"]);
  return crypto.subtle.deriveKey(
    { name: "HKDF", hash: "SHA-256", salt: new Uint8Array(32), info },
    material, { name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"],
  );
}

async function seal(value: string, info: BufferSource, aad: string, secret: string) {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const sealed = new Uint8Array(await crypto.subtle.encrypt(
    { name: "AES-GCM", iv, additionalData: new TextEncoder().encode(aad) },
    await deriveKey(secret, info), new TextEncoder().encode(value),
  ));
  return `v1.${Buffer.from(iv).toString("base64url")}.${Buffer.from(sealed).toString("base64url")}`;
}

async function open(value: string, info: BufferSource, aad: string, secret: string) {
  const [format, iv, sealed] = value.split(".");
  if (format !== "v1" || !iv || !sealed || Buffer.from(iv, "base64url").length !== 12 ||
      Buffer.from(sealed, "base64url").length <= 16) throw new Error("FALL_KEY_CIPHERTEXT_INVALID");
  const clear = await crypto.subtle.decrypt(
    { name: "AES-GCM", iv: Buffer.from(iv, "base64url"), additionalData: new TextEncoder().encode(aad) },
    await deriveKey(secret, info), Buffer.from(sealed, "base64url"),
  );
  return new TextDecoder("utf-8", { fatal: true }).decode(clear);
}

export async function encryptFallCloudKey(apiKey: string, deviceId: string, version: number, secret: string) {
  return seal(apiKey, FALL_INFO, `${deviceId}:${version}`, secret);
}

export async function decryptFallCloudKey(value: string, deviceId: string, version: number, secret: string) {
  return open(value, FALL_INFO, `${deviceId}:${version}`, secret);
}

/** OpenAI (대화) and KMA (날씨) keys: same sealing, their own derivation and service in the binding. */
export async function encryptServiceKey(apiKey: string, deviceId: string, service: string, version: number, secret: string) {
  return seal(apiKey, SERVICE_INFO, `${deviceId}:${service}:${version}`, secret);
}

export async function decryptServiceKey(value: string, deviceId: string, service: string, version: number, secret: string) {
  return open(value, SERVICE_INFO, `${deviceId}:${service}:${version}`, secret);
}

/** Same rule as the robot's OllamaCloudFallProvider: printable ASCII, no spaces. */
export function isValidFallCloudKey(value: unknown): value is string {
  return typeof value === "string" && value.length >= 8 && value.length <= 4096 &&
    [...value].every((c) => c.charCodeAt(0) >= 33 && c.charCodeAt(0) <= 126);
}
