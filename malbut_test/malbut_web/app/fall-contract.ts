import { buildFallNotification, WEB_ONLY_FALL_REASONS } from "../infra/aws/push-broker/fall-notification.mjs";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const states = ["verifying", "recheck_required", "help_required", "resolved"];
const assessments = ["observed_fall", "suspected_fall", "normal_activity", "unobservable"];
const answers = ["help_request", "okay", "unclear", "no_response", "failed"];
const kinds = [
  "incident_opened", "incident_updated", "question_requested", "voice_result", "decision_required",
  "notification_requested", "agent_check_failed", "analysis_completed",
  "analysis_unavailable", "stale_analysis_result", "recheck_unavailable", "incident_resolved",
  "confirmation_completed", "incident_merged",
  // The robot drives near an uncertain suspicion and checks for a person first.
  "approach_started", "approach_completed", "person_check_completed", "approach_returned",
];
// Allowed reasons per approach event; null is never a reason for these except a start
// that interrupted nothing.
const approachReasons: Record<string, Array<string | null>> = {
  approach_started: ["patrol_stopped", "follow_stopped", null],
  approach_completed: ["arrived", "no_map", "no_path", "timeout", "failed", "rejected"],
  person_check_completed: ["person", "not_a_person"],
  approach_returned: ["returned", "return_failed"],
};
// A robot-side closure after its own check. Never a normal-activity judgment.
const resolvedReasons = ["normal_verified", "risk_cleared", "response_completed", "not_a_person"];

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
  mergedIntoIncidentIds?: string[];
  analysis?: {
    requestId: string;
    purpose: "incident" | "crosscheck" | "person_check";
    assessment: string;
    explanation: string;
  };
};

export function parseFallEvent(value: unknown): FallEventInput | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const v = value as Record<string, unknown>;
  const keys = ["schemaVersion", "eventId", "incidentId", "bootId", "sequence",
    "evidenceRevision", "occurredAt", "eventKind", "state", "fallSeen", "assessment",
    "answer", "reason", "notificationLevel"];
  if (v.eventKind === "incident_merged") keys.push("mergedIntoIncidentIds");
  if (Object.hasOwn(v, "analysis")) {
    if (v.eventKind !== "analysis_completed" && v.eventKind !== "person_check_completed") return null;
    const a = v.analysis;
    if (!a || typeof a !== "object" || Array.isArray(a)) return null;
    const record = a as Record<string, unknown>;
    if (Object.keys(record).length !== 4 ||
        typeof record.requestId !== "string" || !/^[A-Za-z0-9._:-]{1,128}$/.test(record.requestId) ||
        !(v.eventKind === "person_check_completed" ? ["person_check"] : ["incident", "crosscheck"])
          .includes(record.purpose as string) ||
        !assessments.includes(record.assessment as string) ||
        typeof record.explanation !== "string" || !record.explanation.trim() ||
        [...record.explanation].length > 1000) return null;
    keys.push("analysis");
  }
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
  if (WEB_ONLY_FALL_REASONS.includes(v.reason as string)) return null;
  if (Object.hasOwn(approachReasons, v.eventKind as string) &&
      !approachReasons[v.eventKind as string].includes(v.reason as string | null)) return null;
  if (v.eventKind === "incident_merged") {
    const targets = v.mergedIntoIncidentIds;
    if (v.state !== "resolved" || v.reason !== "findings_associated" || v.answer === "help_request" ||
        !Array.isArray(targets) || targets.length < 1 || targets.length > 128 ||
        !targets.every((id) => typeof id === "string" && uuid.test(id) && id !== v.incidentId) ||
        new Set(targets).size !== targets.length) return null;
  }
  if (v.eventKind === "notification_requested") {
    if (!buildFallNotification({
      deviceId: "validated-by-auth", notificationId: v.eventId, incidentId: v.incidentId,
      level: v.notificationLevel, reason: v.reason, occurredAt: v.occurredAt,
    })) return null;
    if (v.reason === "fall_observed_person_okay" && (!v.fallSeen || v.answer !== "okay")) return null;
    if (v.reason === "person_no_response" && v.answer !== "no_response") return null;
    if (v.reason === "help_requested" && (v.answer !== "help_request" || v.state !== "help_required")) return null;
    if (v.reason === "confirmation_help_required" &&
        (v.answer !== "help_request" || v.state !== "help_required")) return null;
  } else if (v.notificationLevel !== null) return null;
  if (v.eventKind === "confirmation_completed") {
    // The actual situation and help decision are independent. For example,
    // normal lying down may still require help getting up, while a user may
    // decline help without explaining what happened.
    if (!["confirmed_incident", "resolved", "unknown"].includes(v.reason as string) ||
        !((v.state === "help_required" && v.answer === "help_request") ||
          (v.state === "resolved" && v.answer === "okay"))) return null;
  }
  if (v.state === "resolved") {
    // A return trip ends after the robot's own not-a-person closure.
    if (v.eventKind !== "incident_merged" && v.eventKind !== "confirmation_completed" &&
        v.eventKind !== "approach_returned" &&
        (v.eventKind !== "incident_resolved" || !resolvedReasons.includes(v.reason as string))) return null;
    if (v.reason === "normal_verified" && (v.fallSeen || v.answer !== "okay" || v.assessment !== "normal_activity")) return null;
  }
  // Construct in a canonical order for idempotency comparisons; no raw media,
  // transcript, caller-supplied recipient or device ID is accepted. Only the
  // bounded analysis explanation may carry model text, for human display.
  const result = Object.fromEntries(keys.map((k) => [k, v[k]])) as FallEventInput;
  if (result.analysis) {
    const { requestId, purpose, assessment, explanation } = result.analysis;
    result.analysis = { requestId, purpose, assessment, explanation };
  }
  return result;
}

export async function readFallEvent(request: Request): Promise<FallEventInput | null> {
  const value = await readBoundedJson(request, 8192);
  return value === undefined ? null : parseFallEvent(value);
}

/** JSON body of at most `limit` bytes; undefined when absent, oversized or invalid. */
export async function readBoundedJson(request: Request, limit: number): Promise<unknown> {
  if (request.headers.get("content-type")?.split(";")[0].trim().toLowerCase() !== "application/json") return undefined;
  const reader = request.body?.getReader();
  if (!reader) return undefined;
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const part = await reader.read();
      if (part.done) break;
      size += part.value.byteLength;
      if (size > limit) { await reader.cancel(); return undefined; }
      chunks.push(part.value);
    }
    const buffer = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) { buffer.set(chunk, offset); offset += chunk.byteLength; }
    return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(buffer));
  } catch { return undefined; }
  finally { reader.releaseLock(); }
}
