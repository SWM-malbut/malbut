import { buildFallNotification } from "../infra/aws/push-broker/fall-notification.mjs";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const states = ["verifying", "recheck_required", "help_required", "resolved"];
const assessments = ["observed_fall", "suspected_fall", "normal_activity", "unobservable"];
const answers = ["help_request", "okay", "unclear", "no_response", "failed"];
const kinds = [
  "incident_opened", "question_requested", "voice_result", "decision_required",
  "notification_requested", "agent_check_failed", "analysis_completed",
  "analysis_unavailable", "stale_analysis_result", "recheck_unavailable", "incident_resolved",
];

export type FallEventInput = {
  schemaVersion: 1;
  eventId: string;
  incidentId: string;
  bootId: string;
  sequence: number;
  evidenceRevision: number;
  occurredAt: string;
  eventKind: string;
  state: string;
  fallSeen: boolean;
  assessment: string | null;
  answer: string | null;
  reason: string | null;
  notificationLevel: "info" | "check" | "urgent" | null;
};

export function parseFallEvent(value: unknown): FallEventInput | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const v = value as Record<string, unknown>;
  const keys = ["schemaVersion", "eventId", "incidentId", "bootId", "sequence",
    "evidenceRevision", "occurredAt", "eventKind", "state", "fallSeen", "assessment",
    "answer", "reason", "notificationLevel"];
  if (Object.keys(v).length !== keys.length || !keys.every((k) => Object.hasOwn(v, k))) return null;
  if (v.schemaVersion !== 1 || typeof v.eventId !== "string" || !uuid.test(v.eventId) ||
      typeof v.incidentId !== "string" || !uuid.test(v.incidentId) ||
      typeof v.bootId !== "string" || !/^[A-Za-z0-9._:-]{1,128}$/.test(v.bootId) ||
      !Number.isSafeInteger(v.sequence) || (v.sequence as number) < 1 ||
      !Number.isSafeInteger(v.evidenceRevision) || (v.evidenceRevision as number) < 1 ||
      (v.evidenceRevision as number) > 2147483647 ||
      typeof v.occurredAt !== "string" || !Number.isFinite(Date.parse(v.occurredAt)) ||
      new Date(v.occurredAt).toISOString() !== v.occurredAt ||
      typeof v.eventKind !== "string" || !kinds.includes(v.eventKind) ||
      typeof v.state !== "string" || !states.includes(v.state) ||
      typeof v.fallSeen !== "boolean" ||
      !(v.assessment === null || (typeof v.assessment === "string" && assessments.includes(v.assessment))) ||
      !(v.answer === null || (typeof v.answer === "string" && answers.includes(v.answer))) ||
      !(v.reason === null || (typeof v.reason === "string" && /^[a-z_]{1,80}$/.test(v.reason)))) return null;
  if (v.eventKind === "notification_requested") {
    if (!buildFallNotification({
      deviceId: "validated-by-auth", notificationId: v.eventId, incidentId: v.incidentId,
      level: v.notificationLevel, reason: v.reason, occurredAt: v.occurredAt,
    })) return null;
    if (v.reason === "fall_observed_person_okay" && (!v.fallSeen || v.answer !== "okay")) return null;
    if (v.reason === "person_no_response" && v.answer !== "no_response") return null;
    if (v.reason === "help_requested" && (v.answer !== "help_request" || v.state !== "help_required")) return null;
  } else if (v.notificationLevel !== null) return null;
  if (v.state === "resolved") {
    if (v.eventKind !== "incident_resolved" ||
        !["normal_verified", "risk_cleared", "response_completed"].includes(v.reason as string)) return null;
    if (v.reason === "normal_verified" && (v.fallSeen || v.answer !== "okay" || v.assessment !== "normal_activity")) return null;
  }
  // Construct in a canonical order for idempotency comparisons; no raw media,
  // transcript, model text, caller-supplied recipient or device ID is accepted.
  return Object.fromEntries(keys.map((k) => [k, v[k]])) as FallEventInput;
}

export async function readFallEvent(request: Request): Promise<FallEventInput | null> {
  if (request.headers.get("content-type")?.split(";")[0].trim().toLowerCase() !== "application/json") return null;
  const reader = request.body?.getReader();
  if (!reader) return null;
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const part = await reader.read();
      if (part.done) break;
      size += part.value.byteLength;
      if (size > 8192) { await reader.cancel(); return null; }
      chunks.push(part.value);
    }
    const buffer = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) { buffer.set(chunk, offset); offset += chunk.byteLength; }
    return parseFallEvent(JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(buffer)));
  } catch { return null; }
  finally { reader.releaseLock(); }
}
