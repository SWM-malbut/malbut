export const companionCategories = ["daily", "health", "family"] as const;
export type CompanionCategory = typeof companionCategories[number];
export type CompanionView = {
  revision: number; eraseRevision: number; categories: CompanionCategory[]; externalProcessing: boolean;
  sessionId: string | null; confirmed: boolean; online: boolean; appliedRevision: number;
};
export type CompanionChange = { expectedRevision: number } & (
  { action: "consent"; categories: CompanionCategory[]; externalProcessing: boolean }
  | { action: "confirm"; sessionId: string; self: true; alone: true }
  | { action: "revoke" | "erase" }
);
const record = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
export function companionSession(v: unknown): v is string {
  return typeof v === "string" && /^patrol-chat-[a-f0-9]{32}$/.test(v);
}
export function parseCompanionChange(v: unknown): CompanionChange | null {
  if (!record(v) || !Number.isSafeInteger(v.expectedRevision) || (v.expectedRevision as number) < 1) return null;
  let keys: string[];
  if (v.action === "consent") {
    keys = ["action", "expectedRevision", "categories", "externalProcessing"];
    if (!Array.isArray(v.categories) || v.categories.length > 3 ||
        new Set(v.categories).size !== v.categories.length ||
        v.categories.some((c) => !companionCategories.includes(c)) || typeof v.externalProcessing !== "boolean" ||
        (v.categories.length && (!v.categories.includes("daily") || !v.externalProcessing))) return null;
  } else if (v.action === "confirm") {
    keys = ["action", "expectedRevision", "sessionId", "self", "alone"];
    if (!companionSession(v.sessionId) || v.self !== true || v.alone !== true) return null;
  } else if (v.action === "erase" || v.action === "revoke") keys = ["action", "expectedRevision"];
  else return null;
  return Object.keys(v).length === keys.length && Object.keys(v).every((k) => keys.includes(k)) ? v as CompanionChange : null;
}
export function parseCompanionReport(v: unknown): { sessionId: string | null; appliedRevision: number } | null {
  if (!record(v) || Object.keys(v).length !== 2 || !(v.sessionId === null || companionSession(v.sessionId)) ||
      !Number.isSafeInteger(v.appliedRevision) || (v.appliedRevision as number) < 0) return null;
  return v as { sessionId: string | null; appliedRevision: number };
}
