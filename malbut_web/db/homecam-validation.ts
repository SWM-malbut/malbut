export const HOME_CAM_EVENT_TYPES = ["motion", "person", "dog", "cat"] as const;
export type HomecamEventType = (typeof HOME_CAM_EVENT_TYPES)[number];

export const HOME_CAM_MEMBER_ROLES = ["owner", "family", "broadcaster"] as const;
export type HomecamMemberRole = (typeof HOME_CAM_MEMBER_ROLES)[number];

export type DeviceSettingsPatch = {
  monitoringEnabled?: boolean;
  cameraEnabled?: boolean;
  microphoneEnabled?: boolean;
};

export type HomecamEventInput = {
  eventType: HomecamEventType;
  confidence: number | null;
  occurredAt: string;
  idempotencyKey: string;
  recordingOffsetMs: number | null;
};

export type HomecamEventClipInput = {
  eventGroupId: string;
  segmentIndex: number;
  primaryType: HomecamEventType;
  labels: HomecamEventType[];
  confidence: number;
  detectedAt: string;
  startAt: string;
  endAt: string | null;
  monotonicDurationMs: number | null;
  bootId: string;
  sessionIds: string[];
  clockSteppedDuringEvent: boolean;
  notificationEligible: boolean;
  idempotencyKey: string;
};

export function normalizeEmail(value: string): string | null {
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

export function canViewHomecam(role: string | null | undefined): boolean {
  return role === "owner" || role === "family" || role === "broadcaster";
}

export function canManageHomecam(role: string | null | undefined): boolean {
  return role === "owner";
}

export function parseDeviceSettingsPatch(value: unknown): DeviceSettingsPatch | null {
  if (!isRecord(value)) return null;
  const allowed = ["monitoringEnabled", "cameraEnabled", "microphoneEnabled"];
  if (
    Object.keys(value).length === 0 ||
    Object.keys(value).some((key) => !allowed.includes(key))
  ) {
    return null;
  }

  const patch: DeviceSettingsPatch = {};
  for (const key of allowed) {
    if (!(key in value)) continue;
    if (typeof value[key] !== "boolean") return null;
    patch[key as keyof DeviceSettingsPatch] = value[key];
  }
  return patch;
}

export function parseHomecamEventInput(
  value: unknown,
  now = new Date(),
): HomecamEventInput | null {
  if (!isRecord(value)) return null;
  const allowed = [
    "eventType",
    "confidence",
    "occurredAt",
    "idempotencyKey",
    "recordingOffsetMs",
  ];
  if (Object.keys(value).some((key) => !allowed.includes(key))) return null;

  if (
    typeof value.eventType !== "string" ||
    !HOME_CAM_EVENT_TYPES.includes(value.eventType as HomecamEventType) ||
    typeof value.occurredAt !== "string" ||
    typeof value.idempotencyKey !== "string" ||
    !/^[A-Za-z0-9._:-]{8,128}$/.test(value.idempotencyKey)
  ) {
    return null;
  }

  const occurredAtMs = Date.parse(value.occurredAt);
  const sevenDaysMs = 7 * 24 * 60 * 60 * 1000;
  if (
    !Number.isFinite(occurredAtMs) ||
    new Date(occurredAtMs).toISOString() !== value.occurredAt ||
    occurredAtMs < now.getTime() - sevenDaysMs ||
    occurredAtMs > now.getTime() + 5 * 60 * 1000
  ) {
    return null;
  }

  let confidence: number | null = null;
  if (value.eventType !== "motion") {
    if (
      typeof value.confidence !== "number" ||
      !Number.isFinite(value.confidence) ||
      value.confidence < 0 ||
      value.confidence > 1
    ) {
      return null;
    }
    confidence = value.confidence;
  } else if (value.confidence !== undefined && value.confidence !== null) {
    if (
      typeof value.confidence !== "number" ||
      !Number.isFinite(value.confidence) ||
      value.confidence < 0 ||
      value.confidence > 1
    ) {
      return null;
    }
    confidence = value.confidence;
  }

  let recordingOffsetMs: number | null = null;
  if (value.recordingOffsetMs !== undefined && value.recordingOffsetMs !== null) {
    if (
      !Number.isSafeInteger(value.recordingOffsetMs) ||
      (value.recordingOffsetMs as number) < 0
    ) {
      return null;
    }
    recordingOffsetMs = value.recordingOffsetMs as number;
  }

  return {
    eventType: value.eventType as HomecamEventType,
    confidence,
    occurredAt: value.occurredAt,
    idempotencyKey: value.idempotencyKey,
    recordingOffsetMs,
  };
}

export function parseHomecamEventClipInput(
  value: unknown,
  phase: "started" | "ended",
  now = new Date(),
): HomecamEventClipInput | null {
  if (!isRecord(value)) return null;
  const commonKeys = [
    "eventGroupId",
    "segmentIndex",
    "primaryType",
    "labels",
    "confidence",
    "detectedAt",
    "startAt",
    "bootId",
    "sessionIds",
    "clockSource",
    "clockSteppedDuringEvent",
    "notificationEligible",
    "idempotencyKey",
  ];
  const allowed = phase === "ended"
    ? [...commonKeys, "endAt", "monotonicDurationMs"]
    : commonKeys;
  if (
    Object.keys(value).length !== allowed.length ||
    Object.keys(value).some((key) => !allowed.includes(key))
  ) {
    return null;
  }
  if (
    typeof value.eventGroupId !== "string" ||
    !isUuidVersion(value.eventGroupId, "7") ||
    !Number.isSafeInteger(value.segmentIndex) ||
    (value.segmentIndex as number) < 0 ||
    (value.segmentIndex as number) > 10_000 ||
    typeof value.primaryType !== "string" ||
    !HOME_CAM_EVENT_TYPES.includes(value.primaryType as HomecamEventType) ||
    !Array.isArray(value.labels) ||
    value.labels.length < 1 ||
    value.labels.length > HOME_CAM_EVENT_TYPES.length ||
    value.labels.some(
      (label) =>
        typeof label !== "string" ||
        !HOME_CAM_EVENT_TYPES.includes(label as HomecamEventType),
    ) ||
    new Set(value.labels).size !== value.labels.length ||
    !value.labels.includes(value.primaryType) ||
    typeof value.confidence !== "number" ||
    !Number.isFinite(value.confidence) ||
    value.confidence < 0 ||
    value.confidence > 1 ||
    typeof value.detectedAt !== "string" ||
    typeof value.startAt !== "string" ||
    typeof value.bootId !== "string" ||
    !isUuidVersion(value.bootId, "4") ||
    !Array.isArray(value.sessionIds) ||
    value.sessionIds.length < 1 ||
    value.sessionIds.length > 4 ||
    value.sessionIds.some(
      (sessionId) => typeof sessionId !== "string" || !isUuidVersion(sessionId, "4"),
    ) ||
    new Set(value.sessionIds).size !== value.sessionIds.length ||
    value.clockSource !== "wall" ||
    typeof value.clockSteppedDuringEvent !== "boolean" ||
    typeof value.notificationEligible !== "boolean" ||
    typeof value.idempotencyKey !== "string" ||
    !/^[a-f0-9]{64}$/.test(value.idempotencyKey)
  ) {
    return null;
  }

  const detectedAt = canonicalRecentTimestamp(value.detectedAt, now);
  const startAt = canonicalRecentTimestamp(value.startAt, now);
  if (!detectedAt || !startAt) return null;
  const detectedMs = Date.parse(detectedAt);
  const startMs = Date.parse(startAt);
  if (startMs > detectedMs || detectedMs - startMs > 10_000) return null;

  let endAt: string | null = null;
  let monotonicDurationMs: number | null = null;
  if (phase === "ended") {
    if (
      typeof value.endAt !== "string" ||
      !Number.isSafeInteger(value.monotonicDurationMs) ||
      (value.monotonicDurationMs as number) < 1 ||
      (value.monotonicDurationMs as number) > 125_000
    ) {
      return null;
    }
    endAt = canonicalRecentTimestamp(value.endAt, now);
    if (!endAt || Date.parse(endAt) <= startMs) return null;
    monotonicDurationMs = value.monotonicDurationMs as number;
    if (
      !value.clockSteppedDuringEvent &&
      Math.abs(Date.parse(endAt) - startMs - monotonicDurationMs) > 2_000
    ) {
      return null;
    }
  }

  return {
    eventGroupId: value.eventGroupId,
    segmentIndex: value.segmentIndex as number,
    primaryType: value.primaryType as HomecamEventType,
    labels: value.labels as HomecamEventType[],
    confidence: value.confidence,
    detectedAt,
    startAt,
    endAt,
    monotonicDurationMs,
    bootId: value.bootId,
    sessionIds: value.sessionIds as string[],
    clockSteppedDuringEvent: value.clockSteppedDuringEvent,
    notificationEligible: value.notificationEligible,
    idempotencyKey: value.idempotencyKey,
  };
}

export function isValidClientId(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value === value.trim() &&
    /^(?!AWS_)[A-Za-z0-9_-]{1,128}$/.test(value)
  );
}

export function shouldPrunePushSubscription(status: number) {
  return status === 404 || status === 410;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function isUuidVersion(value: string, version: "4" | "7") {
  return new RegExp(
    `^[0-9a-f]{8}-[0-9a-f]{4}-${version}[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`,
    "i",
  ).test(value);
}

function canonicalRecentTimestamp(value: string, now: Date) {
  const timestamp = Date.parse(value);
  const retentionMs = 7 * 24 * 60 * 60 * 1000;
  if (
    !Number.isFinite(timestamp) ||
    new Date(timestamp).toISOString() !== value ||
    timestamp < now.getTime() - retentionMs ||
    timestamp > now.getTime() + 5 * 60 * 1000
  ) {
    return null;
  }
  return value;
}
