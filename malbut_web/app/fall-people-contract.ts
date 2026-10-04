const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
export const PEOPLE_MAX_TRACKS = 12;
export const PEOPLE_MAX_SAMPLES = 650;
export const PEOPLE_MAX_CLOUD = 32;
export const PEOPLE_MAX_BODY = 262_144;
const MAX_MS = 125_000;

/** [ms from the clip segment start, left, top, right, bottom] in 1/1000 of the frame. */
export type PeopleSample = [number, number, number, number, number];
export type FallPeopleInput = {
  schemaVersion: 1;
  incidentId: string;
  bootId: string;
  segmentIndex: number;
  revision: number;
  truncated: boolean;
  tracks: Array<{ key: string; target: boolean; samples: PeopleSample[] }>;
  cloud: PeopleSample[];
};

const keys = ["schemaVersion", "incidentId", "bootId", "segmentIndex", "revision", "truncated",
  "tracks", "cloud"] as const;
const int = (v: unknown, min: number, max: number) => Number.isSafeInteger(v) && (v as number) >= min && (v as number) <= max;

function samples(value: unknown, max: number, strictlyIncreasing: boolean): PeopleSample[] | null {
  if (!Array.isArray(value) || value.length > max) return null;
  let last = -1;
  for (const s of value) {
    if (!Array.isArray(s) || s.length !== 5 || !int(s[0], 0, MAX_MS) ||
        !s.slice(1).every((v) => int(v, 0, 1000)) || s[1] >= s[3] || s[2] >= s[4]) return null;
    if (strictlyIncreasing ? s[0] <= last : s[0] < last) return null;
    last = s[0];
  }
  return value.map((s) => [...s] as PeopleSample);
}

/** Robot person boxes for one clip segment; strict keys, no media or raw track IDs. */
export function parseFallPeople(value: unknown): FallPeopleInput | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const v = value as Record<string, unknown>;
  if (Object.keys(v).length !== keys.length || !keys.every((k) => Object.hasOwn(v, k))) return null;
  if (v.schemaVersion !== 1 || typeof v.incidentId !== "string" || !uuid.test(v.incidentId) ||
      typeof v.bootId !== "string" || !/^[A-Za-z0-9._:-]{1,128}$/.test(v.bootId) ||
      !int(v.segmentIndex, 0, 31) || !int(v.revision, 1, 2147483647) ||
      typeof v.truncated !== "boolean" || !Array.isArray(v.tracks) ||
      v.tracks.length > PEOPLE_MAX_TRACKS) return null;
  const tracks: FallPeopleInput["tracks"] = [];
  for (const t of v.tracks as unknown[]) {
    if (!t || typeof t !== "object" || Array.isArray(t)) return null;
    const track = t as Record<string, unknown>;
    if (Object.keys(track).length !== 3 || typeof track.key !== "string" ||
        !/^[0-9a-f]{12}$/.test(track.key) || typeof track.target !== "boolean") return null;
    const parsed = samples(track.samples, PEOPLE_MAX_SAMPLES, true);
    if (!parsed?.length) return null;
    tracks.push({ key: track.key, target: track.target, samples: parsed });
  }
  if (new Set(tracks.map((t) => t.key)).size !== tracks.length ||
      tracks.filter((t) => t.target).length > 1) return null;
  const cloud = samples(v.cloud, PEOPLE_MAX_CLOUD, false);
  if (!cloud || (!tracks.length && !cloud.length)) return null;
  return { schemaVersion: 1, incidentId: v.incidentId, bootId: v.bootId,
    segmentIndex: v.segmentIndex as number, revision: v.revision as number,
    truncated: v.truncated, tracks, cloud };
}
