import { getPostgresPool } from "./postgres";
import { parseMediaSettingsReport, type MediaSettingsReport } from "../app/media-settings-contract";

export async function hasMediaSettingsSchema() {
  return !!(await getPostgresPool().query(
    "SELECT 1 FROM homecam_schema_migrations WHERE version='0024_voice_agent'",
  )).rowCount;
}

export async function storeMediaSettingsReport(deviceId: string, input: MediaSettingsReport) {
  const report = parseMediaSettingsReport(input);
  if (!report) throw new Error("MEDIA_SETTINGS_REPORT_INVALID");
  const { reportAgeS, ...content } = report;
  const client = await getPostgresPool().connect();
  try {
    await client.query("BEGIN");
    const current = (await client.query(`SELECT media_settings_revision::text AS revision,
      camera_enabled=1 AS camera,microphone_enabled=1 AS microphone,monitoring_enabled=1 AS monitoring
      FROM device_state WHERE device_id=$1 FOR SHARE`, [deviceId])).rows[0];
    // A save can race the preceding heartbeat response. Ignore its older receipt
    // and return the new desired revision so the robot can catch up normally.
    if (current?.revision === report.requestedRevision) {
      if (report.applied && (report.cameraEnabled !== current.camera ||
        report.microphoneEnabled !== current.microphone || report.monitoringEnabled !== current.monitoring)) {
        throw new Error("MEDIA_SETTINGS_REPORT_VALUES_MISMATCH");
      }
      await client.query(`INSERT INTO device_media_settings_reports
        (device_id,runtime_id,sequence,requested_revision,payload_json,report_age_s)
        VALUES($1,$2,$3::numeric,$4::numeric,$5,$6)
        ON CONFLICT(device_id,runtime_id) DO UPDATE SET sequence=EXCLUDED.sequence,
          requested_revision=EXCLUDED.requested_revision,payload_json=EXCLUDED.payload_json,
          report_age_s=EXCLUDED.report_age_s,received_at=clock_timestamp()
        WHERE device_media_settings_reports.sequence<EXCLUDED.sequence`,
      [deviceId, report.runtimeId, report.sequence, report.requestedRevision, JSON.stringify(content), reportAgeS]);
    }
    await client.query("COMMIT");
  } catch (error) { await client.query("ROLLBACK"); throw error; }
  finally { client.release(); }
}

// These are observed robot receipts, not a guarantee that the reporting process
// is still alive. Expose age and identity instead of equating receipt with health.
export function applyReceipt(report: Record<string, unknown> | undefined, savedAgeS: number) {
  if (!report) return { state: savedAgeS >= 6 ? "no_response" : "waiting", runtimeVerified: false };
  return { ...report, state: report.applied ? "reported_applied" : "reported_failed",
    fresh: Number(report.reportAgeS) <= 15, runtimeVerified: false };
}
