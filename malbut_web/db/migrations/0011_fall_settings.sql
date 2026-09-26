-- Independent of monitoring_enabled (continuous recording). Default consent OFF.
ALTER TABLE device_state
  ADD COLUMN fall_enabled BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN fall_cloud_consent BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN fall_settings_revision NUMERIC(20,0) NOT NULL DEFAULT 1
    CHECK (fall_settings_revision BETWEEN 1 AND 18446744073709551615),
  ADD COLUMN fall_settings_saved_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP;

CREATE TABLE fall_settings_versions (
  device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  revision NUMERIC(20,0) NOT NULL CHECK (revision BETWEEN 1 AND 18446744073709551615),
  enabled BOOLEAN NOT NULL, camera_enabled BOOLEAN NOT NULL, cloud_consent BOOLEAN NOT NULL,
  saved_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (device_id, revision)
);

CREATE FUNCTION advance_fall_settings_revision() RETURNS TRIGGER AS $$
BEGIN
  IF ROW(NEW.camera_enabled, NEW.fall_enabled, NEW.fall_cloud_consent)
      IS DISTINCT FROM ROW(OLD.camera_enabled, OLD.fall_enabled, OLD.fall_cloud_consent) THEN
    NEW.fall_settings_revision := OLD.fall_settings_revision + 1;
    NEW.fall_settings_saved_at := clock_timestamp();
  ELSE
    NEW.fall_settings_revision := OLD.fall_settings_revision;
    NEW.fall_settings_saved_at := OLD.fall_settings_saved_at;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER device_fall_settings_revision BEFORE UPDATE ON device_state
  FOR EACH ROW EXECUTE FUNCTION advance_fall_settings_revision();

CREATE FUNCTION record_fall_settings_version() RETURNS TRIGGER AS $$
BEGIN
  IF TG_OP = 'UPDATE' AND NEW.fall_settings_revision = OLD.fall_settings_revision THEN
    RETURN NEW;
  END IF;
  INSERT INTO fall_settings_versions(device_id, revision, enabled, camera_enabled, cloud_consent, saved_at)
    VALUES(NEW.device_id, NEW.fall_settings_revision, NEW.fall_enabled,
           NEW.camera_enabled = 1, NEW.fall_cloud_consent, NEW.fall_settings_saved_at)
    ON CONFLICT DO NOTHING;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER device_fall_settings_history AFTER INSERT OR UPDATE ON device_state
  FOR EACH ROW EXECUTE FUNCTION record_fall_settings_version();

INSERT INTO fall_settings_versions(device_id, revision, enabled, camera_enabled, cloud_consent, saved_at)
  SELECT device_id, fall_settings_revision, fall_enabled, camera_enabled = 1,
         fall_cloud_consent, fall_settings_saved_at FROM device_state;

CREATE TABLE fall_settings_reports (
  device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  bridge_runtime_id TEXT NOT NULL, manager_runtime_id TEXT NOT NULL,
  sequence NUMERIC(20,0) NOT NULL CHECK (sequence BETWEEN 1 AND 18446744073709551615),
  requested_revision NUMERIC(20,0) NOT NULL,
  payload_json TEXT NOT NULL,
  first_report_age_s DOUBLE PRECISION NOT NULL CHECK (first_report_age_s >= 0 AND first_report_age_s < 'Infinity'::float8),
  received_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (device_id, bridge_runtime_id, manager_runtime_id, sequence),
  FOREIGN KEY (device_id, requested_revision) REFERENCES fall_settings_versions(device_id, revision)
);
CREATE INDEX fall_settings_reports_recent_idx ON fall_settings_reports(device_id, received_at DESC);
