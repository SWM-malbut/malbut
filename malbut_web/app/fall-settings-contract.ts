// HTTP uint64 values stay decimal strings: JavaScript numbers lose precision.
const UINT64_MAX = BigInt("18446744073709551615");
const reasons = ["applied", "already_applied", "runtime_mismatch", "stale_revision",
  "revision_conflict", "invalid_request", "internal_error"] as const;

export function isFallRevision(value: unknown, allowZero = false): value is string {
  return typeof value === "string" && /^(0|[1-9][0-9]{0,19})$/.test(value)
    && (allowZero || value !== "0") && BigInt(value) <= UINT64_MAX;
}

function record(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

export type FallSettingsPatch = {
  expectedRevision: string;
  enabled?: boolean;
  cloudConsent?: boolean;
};

export function parseFallSettingsPatch(value: unknown): FallSettingsPatch | null {
  if (!record(value) || Object.keys(value).some((k) =>
    !["expectedRevision", "enabled", "cloudConsent"].includes(k))) return null;
  if (!isFallRevision(value.expectedRevision)) return null;
  if (value.enabled === undefined && value.cloudConsent === undefined) return null;
  for (const key of ["enabled", "cloudConsent"]) {
    if (key in value && typeof value[key] !== "boolean") return null;
  }
  return { expectedRevision: value.expectedRevision,
    ...(value.enabled !== undefined ? { enabled: value.enabled as boolean } : {}),
    ...(value.cloudConsent !== undefined ? { cloudConsent: value.cloudConsent as boolean } : {}) };
}

export type FallSettingsReport = {
  bridgeRuntimeId: string; managerRuntimeId: string; runtimeId: string;
  sequence: string; snapshotSequence: string; requestedRevision: string; appliedRevision: string;
  applied: boolean; enabled: boolean; cameraEnabled: boolean; cloudConsent: boolean;
  reasonCode: typeof reasons[number]; reportAgeS: number;
};

export function parseFallSettingsReport(value: unknown): FallSettingsReport | null {
  if (!record(value)) return null;
  const keys = ["bridgeRuntimeId", "managerRuntimeId", "runtimeId", "sequence", "snapshotSequence",
    "requestedRevision", "appliedRevision", "applied", "enabled", "cameraEnabled", "cloudConsent",
    "reasonCode", "reportAgeS"];
  if (Object.keys(value).length !== keys.length || Object.keys(value).some((k) => !keys.includes(k))) return null;
  for (const key of ["bridgeRuntimeId", "managerRuntimeId", "runtimeId"]) {
    if (typeof value[key] !== "string" || !/^[A-Za-z0-9_.:-]{1,128}$/.test(value[key])) return null;
  }
  for (const key of ["sequence", "snapshotSequence", "requestedRevision", "appliedRevision"]) {
    if (!isFallRevision(value[key], key === "appliedRevision")) return null;
  }
  for (const key of ["applied", "enabled", "cameraEnabled", "cloudConsent"]) {
    if (typeof value[key] !== "boolean") return null;
  }
  if (!reasons.includes(value.reasonCode as FallSettingsReport["reasonCode"])) return null;
  if (typeof value.reportAgeS !== "number" || !Number.isFinite(value.reportAgeS) || value.reportAgeS < 0) return null;
  const success = value.reasonCode === "applied" || value.reasonCode === "already_applied";
  if (value.applied !== success || (success && value.appliedRevision !== value.requestedRevision)) return null;
  if (value.appliedRevision === "0" && (value.enabled || value.cameraEnabled || value.cloudConsent)) return null;
  // Fixed property order for idempotency, independent of the sender's JSON order.
  return Object.fromEntries(keys.map((k) => [k, value[k]])) as FallSettingsReport;
}

export type FallSettings = {
  settingsRevision: string; enabled: boolean; cameraEnabled: boolean; cloudConsent: boolean;
};
export type FallSettingsView = {
  settings: FallSettings; savedAt: string; savedAgeS: number;
  receiptState: "waiting" | "no_response" | "history_only";
  runtimeVerified: false;
  reports: Array<Omit<FallSettingsReport, "reportAgeS"> & { receivedAt: string; reportAgeS: number }>;
};
