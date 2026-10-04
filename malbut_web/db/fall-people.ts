import { getPostgresPool } from "./postgres";
import { RECORDING_RETENTION_MS } from "./fall-review";
import type { FallPeopleInput, PeopleSample } from "../app/fall-people-contract";

// 사람 표시: person boxes of a clip segment, uploaded by the robot after the
// clip range. Positions only; the web numbers people, the robot never names them.

const MIGRATION = "0015_fall_incident_people";

async function peopleReady() {
  const result = await getPostgresPool().query(
    "SELECT 1 FROM homecam_schema_migrations WHERE version=$1", [MIGRATION],
  );
  return result.rows.length === 1;
}

export async function ensureFallPeopleSchema() {
  if (!(await peopleReady())) throw new Error("FALL_PEOPLE_MIGRATION_REQUIRED");
}

/** Newest revision wins; an older or repeated revision is acknowledged unchanged. */
export async function storeFallPeople(deviceId: string, people: FallPeopleInput) {
  await ensureFallPeopleSchema();
  const json = JSON.stringify(people);
  const client = await getPostgresPool().connect();
  try {
    await client.query("BEGIN");
    // Same lock as robot event and clip ingestion: one writer per device.
    await client.query("SELECT pg_advisory_xact_lock(hashtext($1))", [`fall:${deviceId}`]);
    const clip = (await client.query(
      `SELECT c.boot_id FROM fall_incident_clips c WHERE c.device_id=$1 AND c.incident_id=$2
       AND c.segment_index=$3 FOR UPDATE`,
      [deviceId, people.incidentId, people.segmentIndex],
    )).rows[0];
    // The clip range may still be in the robot's queue; retry, do not block.
    if (!clip) throw new Error("FALL_PEOPLE_CLIP_MISSING");
    if (clip.boot_id !== people.bootId) throw new Error("FALL_PEOPLE_CONFLICT");
    const existing = (await client.query(
      `SELECT revision,payload_json FROM fall_incident_people
       WHERE device_id=$1 AND incident_id=$2 AND segment_index=$3`,
      [deviceId, people.incidentId, people.segmentIndex],
    )).rows[0];
    const ack = { stored: true, incidentId: people.incidentId, segmentIndex: people.segmentIndex,
      revision: people.revision };
    let created = false;
    if (existing && existing.revision === people.revision && existing.payload_json !== json) {
      throw new Error("FALL_PEOPLE_CONFLICT");
    }
    if (!existing || existing.revision < people.revision) {
      await client.query(
        `INSERT INTO fall_incident_people(device_id,incident_id,segment_index,revision,payload_json)
         VALUES($1,$2,$3,$4,$5)
         ON CONFLICT(device_id,incident_id,segment_index) DO UPDATE SET revision=excluded.revision,
           payload_json=excluded.payload_json,updated_at=CURRENT_TIMESTAMP`,
        [deviceId, people.incidentId, people.segmentIndex, people.revision, json],
      );
      created = !existing;
    }
    await client.query("COMMIT");
    return { ...ack, created };
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  } finally { client.release(); }
}

export type ScenePeople = {
  segmentIndex: number;
  revision: number;
  truncated: boolean;
  people: Array<{ label: string; target: boolean; samples: PeopleSample[] }>;
  cloud: PeopleSample[];
};

/**
 * Boxes of one clip segment. "사람 N" is shared with linked incidents (clip
 * ranges overlapping this one): people are numbered by first appearance.
 */
export async function getFallClipPeople(deviceId: string, incidentId: string,
  segmentIndex: number): Promise<ScenePeople | null> {
  await ensureFallPeopleSchema();
  const rows = (await getPostgresPool().query(
    `SELECT o.incident_id,o.segment_index,o.start_at,p.revision,p.payload_json
     FROM fall_incident_clips mine
     JOIN fall_incident_clips o ON o.device_id=mine.device_id
       AND o.start_at<mine.end_at AND o.end_at>mine.start_at
     JOIN fall_incident_people p ON p.device_id=o.device_id AND p.incident_id=o.incident_id
       AND p.segment_index=o.segment_index
     WHERE mine.device_id=$1 AND mine.incident_id=$2 AND mine.segment_index=$3`,
    [deviceId, incidentId, segmentIndex],
  )).rows;
  const own = rows.find((r) => r.incident_id === incidentId && r.segment_index === segmentIndex);
  if (!own) return null;
  const first = new Map<string, number>();
  for (const row of rows) {
    const start = new Date(row.start_at).getTime();
    for (const track of (JSON.parse(row.payload_json) as FallPeopleInput).tracks) {
      const at = start + track.samples[0][0];
      if (!first.has(track.key) || at < first.get(track.key)!) first.set(track.key, at);
    }
  }
  const order = [...first.keys()].sort((a, b) => first.get(a)! - first.get(b)! || a.localeCompare(b));
  const payload = JSON.parse(own.payload_json) as FallPeopleInput;
  return {
    segmentIndex, revision: own.revision, truncated: payload.truncated,
    people: payload.tracks.map((t) => ({ label: `사람 ${order.indexOf(t.key) + 1}`, target: t.target,
      samples: t.samples })),
    cloud: payload.cloud,
  };
}

/** Boxes never outlive the video: deleted once the clip is past retention. */
export async function purgeExpiredFallPeople(now = Date.now()) {
  if (!(await peopleReady())) return 0;
  const result = await getPostgresPool().query(
    `DELETE FROM fall_incident_people p USING fall_incident_clips c
     WHERE c.device_id=p.device_id AND c.incident_id=p.incident_id AND c.segment_index=p.segment_index
       AND c.end_at < $1`,
    [new Date(now - RECORDING_RETENTION_MS).toISOString()],
  );
  return result.rowCount ?? 0;
}
