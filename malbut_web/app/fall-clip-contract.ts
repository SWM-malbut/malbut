const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
export const CLIP_ANCHOR_KINDS = ["pose_motion", "pose_found_down", "cloud_window"] as const;
export const CLIP_MAX_SEGMENT_MS = 125_000;

export type FallClipInput = {
  schemaVersion: 1;
  incidentId: string;
  bootId: string;
  segmentIndex: number;
  revision: number;
  startAt: string;
  endAt: string;
  anchorKinds: Array<(typeof CLIP_ANCHOR_KINDS)[number]>;
  foundDown: boolean;
  clockSource: "wall";
  clockStepped: boolean;
};

const keys = ["schemaVersion", "incidentId", "bootId", "segmentIndex", "revision", "startAt",
  "endAt", "anchorKinds", "foundDown", "clockSource", "clockStepped"] as const;

function isoMs(value: unknown): value is string {
  return typeof value === "string" && Number.isFinite(Date.parse(value)) &&
    new Date(value).toISOString() === value;
}

/** Robot clip range (wall clock); strict keys, no media, recipients or session IDs. */
export function parseFallClip(value: unknown): FallClipInput | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const v = value as Record<string, unknown>;
  if (Object.keys(v).length !== keys.length || !keys.every((k) => Object.hasOwn(v, k))) return null;
  if (v.schemaVersion !== 1 || typeof v.incidentId !== "string" || !uuid.test(v.incidentId) ||
      typeof v.bootId !== "string" || !/^[A-Za-z0-9._:-]{1,128}$/.test(v.bootId) ||
      !Number.isSafeInteger(v.segmentIndex) || (v.segmentIndex as number) < 0 ||
      (v.segmentIndex as number) > 31 ||
      !Number.isSafeInteger(v.revision) || (v.revision as number) < 1 ||
      (v.revision as number) > 2147483647 ||
      !isoMs(v.startAt) || !isoMs(v.endAt) ||
      typeof v.foundDown !== "boolean" || v.clockSource !== "wall" ||
      typeof v.clockStepped !== "boolean") return null;
  const duration = Date.parse(v.endAt as string) - Date.parse(v.startAt as string);
  if (duration <= 0 || duration > CLIP_MAX_SEGMENT_MS) return null;
  const kinds = v.anchorKinds;
  if (!Array.isArray(kinds) || kinds.length < 1 || kinds.length > CLIP_ANCHOR_KINDS.length ||
      new Set(kinds).size !== kinds.length ||
      !kinds.every((k) => (CLIP_ANCHOR_KINDS as readonly unknown[]).includes(k))) return null;
  return Object.fromEntries(keys.map((k) => [k, k === "anchorKinds" ? [...kinds] : v[k]])) as FallClipInput;
}
