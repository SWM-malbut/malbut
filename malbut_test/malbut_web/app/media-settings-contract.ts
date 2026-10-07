import { isFallRevision } from "./fall-settings-contract";

export type MediaSettingsReport = {
  runtimeId: string; sequence: string; requestedRevision: string; applied: boolean;
  cameraEnabled: boolean; microphoneEnabled: boolean; monitoringEnabled: boolean;
  reasonCode: "applied" | "local_media_unavailable"; reportAgeS: number;
};

export function parseMediaSettingsReport(value: unknown): MediaSettingsReport | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const row = value as Record<string, unknown>;
  const keys = ["runtimeId", "sequence", "requestedRevision", "applied", "cameraEnabled",
    "microphoneEnabled", "monitoringEnabled", "reasonCode", "reportAgeS"];
  if (Object.keys(row).length !== keys.length || Object.keys(row).some((key) => !keys.includes(key)) ||
    typeof row.runtimeId !== "string" || !/^[A-Za-z0-9_.:-]{1,128}$/.test(row.runtimeId) ||
    !isFallRevision(row.sequence) || !isFallRevision(row.requestedRevision) ||
    ["applied", "cameraEnabled", "microphoneEnabled", "monitoringEnabled"].some((key) => typeof row[key] !== "boolean") ||
    !["applied", "local_media_unavailable"].includes(row.reasonCode as string) ||
    row.applied !== (row.reasonCode === "applied") || typeof row.reportAgeS !== "number" ||
    !Number.isFinite(row.reportAgeS) || row.reportAgeS < 0) return null;
  return Object.fromEntries(keys.map((key) => [key, row[key]])) as MediaSettingsReport;
}
