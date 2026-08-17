const AGENT_SEMANTIC_SCHEMA_VERSION = 1;
const AGENT_SEMANTIC_ISSUER = "malbut-homecam-web";
const AGENT_SEMANTIC_AUDIENCE = "malbut-agent-semantic-v1";
const AGENT_SEMANTIC_TTL_MS = 5_000;
const MAX_CANONICAL_JSON_BYTES = 2 * 1024 * 1024;
const MAX_CANONICAL_JSON_NODES = 100_000;
const MAX_CANONICAL_JSON_DEPTH = 32;
const UTF8_ENCODER = new TextEncoder();
const ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const POSITIVE_DECIMAL = /^[1-9][0-9]{0,18}$/;

export type AgentSemanticBinding = Readonly<{
  serviceSecret: string;
  signingSecret: string;
  agentUserId: string;
  userEmail: string;
  principalSubject: string;
  principalSubjectDigest: string;
  deviceId: string;
}>;

export type AgentSemanticRepositorySnapshot = Readonly<{
  membershipGeneration: string;
  mapGeneration: string;
  deviceRevision: string;
  semantics: Record<string, unknown>;
}>;

export async function parseAgentSemanticBinding(
  runtime: Readonly<Record<string, string | undefined>>,
): Promise<AgentSemanticBinding | null> {
  const serviceSecret = runtime.AGENT_SEMANTIC_SECRET?.trim() ?? "";
  const signingSecret = runtime.AGENT_SEMANTIC_SIGNING_SECRET?.trim() ?? "";
  const agentUserId = runtime.AGENT_SEMANTIC_AGENT_USER_ID?.trim() ?? "";
  const userEmail = normalizeEmail(runtime.AGENT_SEMANTIC_USER_EMAIL);
  const principalSubject = runtime.AGENT_SEMANTIC_PRINCIPAL_SUBJECT?.trim() ?? "";
  const deviceId = runtime.AGENT_SEMANTIC_DEVICE_ID?.trim() ?? "";
  if (
    !secret(serviceSecret) ||
    !secret(signingSecret) ||
    serviceSecret === signingSecret ||
    !ID_PATTERN.test(agentUserId) ||
    !userEmail ||
    !principalSubject ||
    principalSubject.length > 256 ||
    !visibleAscii(principalSubject) ||
    !ID_PATTERN.test(deviceId)
  ) return null;
  return {
    serviceSecret,
    signingSecret,
    agentUserId,
    userEmail,
    principalSubject,
    principalSubjectDigest: await sha256(principalSubject),
    deviceId,
  };
}

export async function authorizedAgentSemanticRequest(
  authorization: string | null,
  expectedSecret: string,
) {
  if (!authorization?.startsWith("Bearer ")) return false;
  const received = authorization.slice("Bearer ".length);
  if (!secret(received) || !secret(expectedSecret)) return false;
  return constantTimeEqual(received, expectedSecret);
}

export function validAgentSemanticRequest(
  value: unknown,
  binding: AgentSemanticBinding,
) {
  if (!record(value)) return false;
  if (
    Object.keys(value).length !== 4 ||
    value.schemaVersion !== AGENT_SEMANTIC_SCHEMA_VERSION ||
    value.agentUserId !== binding.agentUserId ||
    value.principalSubjectDigest !== binding.principalSubjectDigest ||
    value.deviceId !== binding.deviceId
  ) return false;
  return true;
}

export async function buildAgentSemanticEnvelope(
  binding: AgentSemanticBinding,
  snapshot: AgentSemanticRepositorySnapshot,
  nowMs = Date.now(),
) {
  if (
    !Number.isSafeInteger(nowMs) ||
    nowMs < 0 ||
    !POSITIVE_DECIMAL.test(snapshot.membershipGeneration) ||
    !POSITIVE_DECIMAL.test(snapshot.mapGeneration) ||
    !ID_PATTERN.test(snapshot.deviceRevision) ||
    !record(snapshot.semantics) ||
    !validBoundedJson(snapshot.semantics)
  ) throw new Error("AGENT_SEMANTIC_SNAPSHOT_INVALID");
  const deviceRevisionDigest = await sha256(snapshot.deviceRevision);
  const sourceRevision =
    `srv-${snapshot.mapGeneration}-${deviceRevisionDigest.slice(0, 16)}`;
  const semantics = {
    ...snapshot.semantics,
    revision: sourceRevision,
  };
  const semanticsJson = canonicalJson(semantics);
  const contentSha256 = await sha256(semanticsJson);
  const deviceBindingRevision = await sha256(canonicalJson({
    schemaVersion: AGENT_SEMANTIC_SCHEMA_VERSION,
    principalSubjectDigest: binding.principalSubjectDigest,
    deviceId: binding.deviceId,
    membershipGeneration: snapshot.membershipGeneration,
  }));
  const signedFields = {
    schemaVersion: AGENT_SEMANTIC_SCHEMA_VERSION,
    issuer: AGENT_SEMANTIC_ISSUER,
    audience: AGENT_SEMANTIC_AUDIENCE,
    agentUserId: binding.agentUserId,
    principalSubjectDigest: binding.principalSubjectDigest,
    deviceId: binding.deviceId,
    deviceBindingRevision,
    authorizationRevision: `auth-${snapshot.membershipGeneration}`,
    mapGeneration: snapshot.mapGeneration,
    sourceIsFinalized: true,
    issuedAtMs: nowMs,
    expiresAtMs: nowMs + AGENT_SEMANTIC_TTL_MS,
    contentSha256,
  };
  return {
    ...signedFields,
    semanticsJson,
    signature: await hmacSha256(
      binding.signingSecret,
      canonicalJson(signedFields),
    ),
  };
}

export function canonicalJson(value: unknown): string {
  if (!validBoundedJson(value)) {
    throw new Error("AGENT_SEMANTIC_JSON_INVALID");
  }
  const serialized = JSON.stringify(canonicalValue(value));
  if (
    typeof serialized !== "string" ||
    UTF8_ENCODER.encode(serialized).byteLength > MAX_CANONICAL_JSON_BYTES
  ) throw new Error("AGENT_SEMANTIC_JSON_INVALID");
  return serialized;
}

function canonicalValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalValue);
  if (record(value)) {
    return Object.fromEntries(
      Object.keys(value)
        .sort()
        .map((key) => [key, canonicalValue(value[key])]),
    );
  }
  return value;
}

function record(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

/** Validate arbitrary repository JSON iteratively before canonical recursion. */
function validBoundedJson(root: unknown) {
  const pending = [{ item: root, depth: 0 }];
  const visited = new WeakSet<object>();
  let bytes = 0;
  let nodes = 0;

  try {
    while (pending.length > 0) {
      const current = pending.pop();
      if (!current) return false;
      nodes += 1;
      if (
        nodes > MAX_CANONICAL_JSON_NODES ||
        current.depth > MAX_CANONICAL_JSON_DEPTH
      ) return false;

      const item = current.item;
      if (item === null) {
        bytes += 4;
      } else if (typeof item === "boolean") {
        bytes += item ? 4 : 5;
      } else if (typeof item === "number") {
        if (!Number.isFinite(item)) return false;
        bytes += JSON.stringify(item).length;
      } else if (typeof item === "string") {
        const rawBytes = UTF8_ENCODER.encode(item).byteLength;
        if (rawBytes > MAX_CANONICAL_JSON_BYTES) return false;
        bytes += UTF8_ENCODER.encode(JSON.stringify(item)).byteLength;
      } else if (typeof item === "object") {
        if (visited.has(item)) return false;
        visited.add(item);

        if (Array.isArray(item)) {
          const keys = Object.keys(item);
          if (
            keys.length !== item.length ||
            keys.some((key, index) => key !== String(index)) ||
            nodes + pending.length + item.length > MAX_CANONICAL_JSON_NODES
          ) return false;
          bytes += 2 + Math.max(0, item.length - 1);
          for (let index = item.length - 1; index >= 0; index -= 1) {
            pending.push({ item: item[index], depth: current.depth + 1 });
          }
        } else {
          const keys = Reflect.ownKeys(item);
          if (
            keys.some((key) => typeof key !== "string") ||
            nodes + pending.length + keys.length > MAX_CANONICAL_JSON_NODES
          ) return false;
          const descriptors = Object.getOwnPropertyDescriptors(item);
          if (keys.some((key) => {
            const descriptor = descriptors[key as string];
            return !descriptor || !descriptor.enumerable || !("value" in descriptor);
          })) return false;
          bytes += 2 + Math.max(0, keys.length - 1);
          for (const key of keys) {
            const stringKey = key as string;
            const rawKeyBytes = UTF8_ENCODER.encode(stringKey).byteLength;
            if (rawKeyBytes > MAX_CANONICAL_JSON_BYTES) return false;
            bytes += UTF8_ENCODER.encode(JSON.stringify(stringKey)).byteLength + 1;
            pending.push({
              item: descriptors[stringKey].value,
              depth: current.depth + 1,
            });
          }
        }
      } else {
        return false;
      }
      if (bytes > MAX_CANONICAL_JSON_BYTES) return false;
    }
  } catch {
    return false;
  }
  return true;
}

function normalizeEmail(value: unknown) {
  if (typeof value !== "string") return null;
  const email = value.trim().toLowerCase();
  return email.length <= 320 && EMAIL_PATTERN.test(email) ? email : null;
}

function visibleAscii(value: string) {
  return Array.from(value).every((character) => {
    const code = character.codePointAt(0) ?? 0;
    return code > 32 && code < 127;
  });
}

function secret(value: string) {
  return value.length >= 43 && value.length <= 512 &&
    Array.from(value).every((character) => {
      const code = character.codePointAt(0) ?? 0;
      return code > 32 && code < 127;
    });
}

async function constantTimeEqual(left: string, right: string) {
  const [leftDigest, rightDigest] = await Promise.all([
    sha256Bytes(left),
    sha256Bytes(right),
  ]);
  let difference = 0;
  for (let index = 0; index < leftDigest.length; index += 1) {
    difference |= leftDigest[index] ^ rightDigest[index];
  }
  return difference === 0;
}

async function sha256(value: string) {
  return toHex(await sha256Bytes(value));
}

async function sha256Bytes(value: string) {
  return new Uint8Array(
    await crypto.subtle.digest(
      "SHA-256",
      new TextEncoder().encode(value),
    ),
  );
}

async function hmacSha256(secretValue: string, value: string) {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secretValue),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  return toHex(new Uint8Array(await crypto.subtle.sign(
    "HMAC",
    key,
    new TextEncoder().encode(value),
  )));
}

function toHex(value: Uint8Array) {
  return Array.from(value, (byte) => byte.toString(16).padStart(2, "0")).join("");
}
