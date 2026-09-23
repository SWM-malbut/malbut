import { randomUUID } from "node:crypto";
import { getPostgresPool } from "./postgres";
import { parseFallSettingsPatch, parseFallSettingsReport,
  type FallSettings, type FallSettingsPatch, type FallSettingsReport, type FallSettingsView,
} from "../app/fall-settings-contract";

export async function hasFallSettingsSchema(): Promise<boolean> {
  const result = await getPostgresPool().query(
    "SELECT 1 FROM homecam_schema_migrations WHERE version='0010_fall_settings'",
  );
  return !!result.rowCount;
}

async function requireSchema() {
  if (!(await hasFallSettingsSchema())) throw new Error("FALL_SETTINGS_MIGRATION_REQUIRED");
}

// Read camera permission and fall settings in ONE statement; a concurrent camera
// toggle must never yield different camera values in the two heartbeat objects.
export async function readFallSettingsSnapshot(deviceId: string) {
  await requireSchema();
  await getPostgresPool().query(
    "INSERT INTO device_state(device_id) VALUES($1) ON CONFLICT DO NOTHING", [deviceId],
  );
  const result = await getPostgresPool().query(
    `SELECT fall_settings_revision::text AS "settingsRevision", fall_enabled AS enabled,
       camera_enabled=1 AS "cameraEnabled", fall_cloud_consent AS "cloudConsent",
       monitoring_enabled=1 AS "monitoringEnabled", microphone_enabled=1 AS "microphoneEnabled",
       fall_settings_saved_at AS "savedAt", clock_timestamp() AS "checkedAt"
     FROM device_state WHERE device_id=$1`, [deviceId],
  );
  const row = result.rows[0];
  const settings: FallSettings = { settingsRevision: row.settingsRevision,
    enabled: row.enabled, cameraEnabled: row.cameraEnabled, cloudConsent: row.cloudConsent };
  return { settings, savedAt: row.savedAt as string, checkedAt: row.checkedAt as string,
    desiredState: { cameraEnabled: row.cameraEnabled as boolean,
      monitoringEnabled: row.monitoringEnabled as boolean, microphoneEnabled: row.microphoneEnabled as boolean } };
}

export async function saveFallSettings(deviceId: string, userEmail: string, input: FallSettingsPatch) {
  const patch = parseFallSettingsPatch(input);
  if (!patch) throw new Error("FALL_SETTINGS_INVALID");
  await requireSchema();
  const client = await getPostgresPool().connect();
  try {
    await client.query("BEGIN");
    // Lock membership until COMMIT: removing ownership cannot race this write.
    const owner = await client.query(
      "SELECT role FROM device_memberships WHERE device_id=$1 AND user_email=$2 FOR SHARE",
      [deviceId, userEmail],
    );
    if (owner.rows[0]?.role !== "owner") throw new Error("FALL_SETTINGS_FORBIDDEN");
    await client.query("INSERT INTO device_state(device_id) VALUES($1) ON CONFLICT DO NOTHING", [deviceId]);
    const result = await client.query(
      `UPDATE device_state SET fall_enabled=COALESCE($3,fall_enabled),
         fall_cloud_consent=COALESCE($4,fall_cloud_consent)
       WHERE device_id=$1 AND fall_settings_revision=$2::numeric
       RETURNING fall_settings_revision::text AS revision`,
      [deviceId, patch.expectedRevision, patch.enabled ?? null, patch.cloudConsent ?? null],
    );
    if (!result.rowCount) throw new Error("FALL_SETTINGS_REVISION_CONFLICT");
    await client.query(
      `INSERT INTO access_audit_log(id,device_id,actor_type,actor_id,action,metadata_json)
       VALUES($1,$2,'user',$3,'fall_settings_updated',$4)`,
      [randomUUID(), deviceId, userEmail, JSON.stringify({ ...patch, savedRevision: result.rows[0].revision })],
    );
    await client.query("COMMIT");
    return { settingsRevision: result.rows[0].revision as string };
  } catch (error) { await client.query("ROLLBACK"); throw error; }
  finally { client.release(); }
}

export async function storeFallSettingsReport(deviceId: string, input: FallSettingsReport) {
  const report = parseFallSettingsReport(input);
  if (!report) throw new Error("FALL_SETTINGS_REPORT_INVALID");
  await requireSchema();
  const { reportAgeS, ...content } = report;
  const json = JSON.stringify(content);
  const client = await getPostgresPool().connect();
  try {
    await client.query("BEGIN");
    await client.query("SELECT pg_advisory_xact_lock(hashtext($1))", [`fall-settings:${deviceId}`]);
    const duplicate = await client.query(
      `SELECT payload_json FROM fall_settings_reports
       WHERE device_id=$1 AND bridge_runtime_id=$2 AND manager_runtime_id=$3 AND sequence=$4::numeric`,
      [deviceId, report.bridgeRuntimeId, report.managerRuntimeId, report.sequence],
    );
    if (duplicate.rowCount) {
      if (duplicate.rows[0].payload_json !== json) throw new Error("FALL_SETTINGS_REPORT_CONFLICT");
      await client.query("COMMIT");
      return { stored: true, created: false };
    }
    const requested = await client.query(
      "SELECT 1 FROM fall_settings_versions WHERE device_id=$1 AND revision=$2::numeric",
      [deviceId, report.requestedRevision],
    );
    if (!requested.rowCount) throw new Error("FALL_SETTINGS_REPORT_UNKNOWN_REVISION");
    if (report.appliedRevision !== "0") {
      const actual = await client.query(
        "SELECT * FROM fall_settings_versions WHERE device_id=$1 AND revision=$2::numeric",
        [deviceId, report.appliedRevision],
      );
      const row = actual.rows[0];
      if (!row || row.enabled !== report.enabled || row.camera_enabled !== report.cameraEnabled
        || row.cloud_consent !== report.cloudConsent) throw new Error("FALL_SETTINGS_REPORT_VALUES_MISMATCH");
    }
    await client.query(
      `INSERT INTO fall_settings_reports(device_id,bridge_runtime_id,manager_runtime_id,sequence,
         requested_revision,payload_json,first_report_age_s) VALUES($1,$2,$3,$4,$5,$6,$7)`,
      [deviceId, report.bridgeRuntimeId, report.managerRuntimeId, report.sequence,
        report.requestedRevision, json, reportAgeS],
    );
    await client.query("COMMIT");
    return { stored: true, created: true };
  } catch (error) { await client.query("ROLLBACK"); throw error; }
  finally { client.release(); }
}

export async function readFallSettingsView(deviceId: string): Promise<FallSettingsView> {
  const snapshot = await readFallSettingsSnapshot(deviceId);
  const rows = await getPostgresPool().query(
    `SELECT payload_json, received_at, first_report_age_s,
       GREATEST(0, EXTRACT(EPOCH FROM clock_timestamp()-received_at))::float8 AS elapsed
     FROM fall_settings_reports WHERE device_id=$1 AND requested_revision=$2::numeric
     ORDER BY received_at DESC LIMIT 5`, [deviceId, snapshot.settings.settingsRevision],
  );
  const reports = rows.rows.map((row) => ({ ...JSON.parse(row.payload_json),
    receivedAt: row.received_at, reportAgeS: row.first_report_age_s + row.elapsed }));
  const savedAgeS = Math.max(0, (Date.parse(snapshot.checkedAt) - Date.parse(snapshot.savedAt)) / 1000);
  return { settings: snapshot.settings, savedAt: snapshot.savedAt, savedAgeS,
    receiptState: reports.length ? "history_only" : savedAgeS >= 6 ? "no_response" : "waiting",
    runtimeVerified: false, reports };
}
