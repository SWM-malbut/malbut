export const VOICE_OPERATIONS = ["homecam_status", "homecam_events", "homecam_recordings",
  "homecam_falls", "homecam_settings", "result_publish"] as const;
export type VoiceOperation = typeof VOICE_OPERATIONS[number];
export type VoiceRequest = { requestId: string; operation: VoiceOperation; arguments: Record<string, unknown> };
export type VoiceReply = { success: boolean; code: string; result: Record<string, unknown>; message: string };
export type VoiceHistory = { requestId: string; operation: VoiceOperation; state: string;
  createdAt: string; reply: VoiceReply | null };
const identifier = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
function record(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}
function keys(value: Record<string, unknown>, allowed: string[]) {
  return Object.keys(value).every((key) => allowed.includes(key));
}
export function parseVoiceRequest(value: unknown): VoiceRequest | null {
  if (!record(value) || !keys(value, ["requestId", "operation", "arguments"]) ||
    typeof value.requestId !== "string" || !identifier.test(value.requestId) ||
    !VOICE_OPERATIONS.includes(value.operation as VoiceOperation) || !record(value.arguments)) return null;
  const args = value.arguments, operation = value.operation as VoiceOperation;
  if (operation === "homecam_status" && Object.keys(args).length) return null;
  if (["homecam_events", "homecam_recordings", "homecam_falls"].includes(operation)) {
    if (!keys(args, operation === "homecam_events" ? ["limit", "eventType"] : ["limit"])) return null;
    if (args.limit !== undefined && (!Number.isSafeInteger(args.limit) || Number(args.limit) < 1 || Number(args.limit) > 20)) return null;
    if (args.eventType !== undefined && (typeof args.eventType !== "string" ||
      !["motion", "person", "dog", "cat"].includes(args.eventType))) return null;
  }
  if (operation === "homecam_settings" && (!Object.keys(args).length ||
    !keys(args, ["cameraEnabled", "microphoneEnabled", "monitoringEnabled", "fallEnabled"]) ||
    Object.values(args).some((item) => typeof item !== "boolean"))) return null;
  if (operation === "result_publish") {
    if (!keys(args, ["kind", "title", "summary", "referenceId", "state"]) ||
      typeof args.kind !== "string" || !["mission", "status", "map", "homecam", "event", "recording", "fall"].includes(args.kind) ||
      typeof args.title !== "string" || !args.title.trim() || args.title.length > 100 ||
      typeof args.summary !== "string" || args.summary.length > 1000 ||
      (args.referenceId !== undefined && (typeof args.referenceId !== "string" || !identifier.test(args.referenceId))) ||
      (["map", "event", "recording", "fall"].includes(String(args.kind)) && !args.referenceId) ||
      (args.kind === "homecam" && args.referenceId !== undefined) ||
      (args.state !== undefined && (typeof args.state !== "string" ||
        !["accepted", "running", "succeeded", "failed", "canceled", "unknown"].includes(args.state)))) return null;
  }
  return { requestId: value.requestId, operation,
    arguments: Object.fromEntries(Object.entries(args).sort(([a], [b]) => a.localeCompare(b))) };
}
export function requiresVoiceDelegation(request: VoiceRequest) {
  return request.operation !== "result_publish" || !["mission", "status", "map"].includes(String(request.arguments.kind));
}
