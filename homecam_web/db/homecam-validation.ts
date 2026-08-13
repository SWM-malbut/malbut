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
