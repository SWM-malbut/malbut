import { randomUUID } from "node:crypto";
import { getPostgresPool } from "./postgres";
import {
  fallBadge,
  type DemoLine,
  type DemoReport,
  type DemoView,
} from "../app/demo-feed-contract";

const owner =
  "EXISTS (SELECT 1 FROM device_memberships m WHERE m.device_id=s.device_id AND m.user_id=s.owner_id AND m.role='owner')";
const live = "s.expires_at > now() AND " + owner;

export async function demoLease(device: string) {
  const result = await getPostgresPool().query(
    `WITH expired AS (DELETE FROM demo_lines WHERE spoken_at<now()-interval '1 hour') SELECT session_id AS session,
    ceil(extract(epoch FROM (expires_at-now())) * 1000)::integer AS "ttlMs"
    FROM demo_sessions s WHERE device_id=$1 AND ${live}`,
    [device],
  );
  return result.rows[0] ?? null;
}

export async function exchangeDemo(device: string, report: DemoReport) {
  const client = await getPostgresPool().connect();
  try {
    await client.query("BEGIN");
    const { rows } = await client.query(
      `UPDATE demo_sessions s SET pose_count=$3,suspect=$4,pose_at=now(),
       last_clear_at=CASE WHEN $3::integer IS NOT NULL AND NOT $4 THEN now() ELSE last_clear_at END
      WHERE device_id=$1 AND session_id=$2 AND ${live} RETURNING owner_id,started_at`,
      [device, report.session, report.count, report.suspect],
    );
    if (rows.length && report.lines.length) {
      await client.query(
        `INSERT INTO demo_lines (device_id,owner_id,event_id,speaker,text,confirmation,spoken_at)
        SELECT $1,$2,e.id,e.speaker,e.text,e.confirmation,to_timestamp(e.at/1000.0)
        FROM jsonb_to_recordset($3::jsonb) AS e(id text,speaker text,text text,confirmation boolean,at bigint)
        WHERE e.at BETWEEN extract(epoch FROM greatest($4::timestamptz-interval '10 seconds',now()-interval '1 hour'))*1000
          AND extract(epoch FROM now()+interval '10 seconds')*1000 ON CONFLICT DO NOTHING`,
        [
          device,
          rows[0].owner_id,
          JSON.stringify(report.lines),
          rows[0].started_at,
        ],
      );
    }
    await client.query("COMMIT");
    return rows.length > 0;
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  } finally {
    client.release();
  }
}

export async function viewerDemo(
  device: string,
  user: string,
  prior: string | null,
  leave: boolean,
  renew = true,
): Promise<DemoView | null> {
  const client = await getPostgresPool().connect();
  try {
    await client.query("BEGIN");
    const membership = await client.query(
      "SELECT role FROM device_memberships WHERE device_id=$1 AND user_id=$2 FOR SHARE",
      [device, user],
    );
    if (membership.rows[0]?.role !== "owner") throw new Error("FORBIDDEN");
    // Serialize first-open, renewal, and exit, including when no session row exists.
    await client.query("SELECT pg_advisory_xact_lock(hashtext($1))", [device]);
    if (!prior)
      await client.query(
        "DELETE FROM demo_lines WHERE spoken_at < now()-interval '1 hour'",
      );
    const existing = (
      await client.query(
        `SELECT owner_id,session_id,last_clear_at,expires_at>now() AS active,
      CASE WHEN pose_at>now()-interval '4 seconds' THEN pose_count END AS count,
      (pose_at>now()-interval '4 seconds' AND suspect) AS suspect
      FROM demo_sessions WHERE device_id=$1 FOR UPDATE`,
        [device],
      )
    ).rows[0];
    if (
      prior &&
      (existing?.session_id !== prior || existing?.owner_id !== user)
    )
      throw new Error("ENDED");
    if (!prior && leave) throw new Error("ENDED");
    if (leave) {
      await client.query("DELETE FROM demo_sessions WHERE device_id=$1", [
        device,
      ]);
      await client.query("DELETE FROM demo_lines WHERE device_id=$1", [device]);
      await client.query("COMMIT");
      return null;
    }
    if (existing && existing.owner_id !== user)
      await client.query("DELETE FROM demo_lines WHERE device_id=$1", [device]);
    const session =
      existing?.active && existing.owner_id === user
        ? existing.session_id
        : randomUUID();
    if (!renew && (!prior || !existing?.active)) throw new Error("EXPIRED");
    if (renew)
      await client.query(
        `INSERT INTO demo_sessions(device_id,owner_id,session_id,expires_at)
      VALUES ($1,$2,$3,now()+interval '20 seconds') ON CONFLICT(device_id) DO UPDATE SET
      owner_id=$2,session_id=$3,expires_at=excluded.expires_at,
      pose_at=CASE WHEN demo_sessions.session_id=$3 THEN demo_sessions.pose_at ELSE NULL END,
      last_clear_at=CASE WHEN demo_sessions.session_id=$3 THEN demo_sessions.last_clear_at ELSE NULL END,
      started_at=CASE WHEN demo_sessions.session_id=$3 THEN demo_sessions.started_at ELSE now() END`,
        [device, user, session],
      );
    const pose = existing?.session_id === session ? existing : null;
    const result = await client.query(
      `SELECT event_id AS id,speaker,text,confirmation,(extract(epoch FROM spoken_at)*1000)::bigint AS at
      FROM demo_lines WHERE device_id=$1 AND owner_id=$2 AND spoken_at>=now()-interval '1 hour' ORDER BY spoken_at DESC,event_id DESC LIMIT 250`,
      [device, user],
    );
    const lines: DemoLine[] = result.rows
      .reverse()
      .map((r) => ({ ...r, at: Number(r.at) }) as DemoLine);
    const incidents = await client.query(
      `SELECT i.state,i.answer, e.received_at < $2::timestamptz AS "clearedAfterResult",
      (extract(epoch FROM (e.payload_json::jsonb->>'occurredAt')::timestamptz)*1000)::bigint AS updated,
      (extract(epoch FROM i.occurred_at)*1000)::bigint AS opened
      FROM fall_incidents i JOIN fall_incident_events e ON (e.device_id,e.incident_id,e.sequence)=(i.device_id,i.incident_id,i.latest_sequence)
      WHERE i.device_id=$1 AND i.updated_at>=now()-interval '10 minutes'
      ORDER BY opened DESC LIMIT 1`,
      [device, pose?.last_clear_at ?? null],
    );
    await client.query("COMMIT");
    return {
      session,
      lines,
      count: pose?.count ?? null,
      badge: fallBadge(incidents.rows[0], lines, !!pose?.suspect, Date.now()),
    };
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  } finally {
    client.release();
  }
}
