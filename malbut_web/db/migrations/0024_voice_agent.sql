CREATE TABLE device_voice_delegations (
  device_id TEXT PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
  enabled BOOLEAN NOT NULL DEFAULT FALSE,
  granted_by TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE device_voice_requests (
  device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  request_id TEXT NOT NULL,
  credential_id TEXT NOT NULL,
  operation TEXT NOT NULL CHECK (operation IN ('homecam_status','homecam_events','homecam_recordings',
    'homecam_falls','homecam_settings','result_publish')),
  arguments_json TEXT NOT NULL,
  requires_delegation BOOLEAN NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','completed','failed')),
  reply_json TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_at TIMESTAMPTZ,
  PRIMARY KEY(device_id,request_id)
);
CREATE INDEX device_voice_requests_recent_idx ON device_voice_requests(device_id,created_at DESC);

-- Media settings have their own revision: microphone/monitoring changes do not
-- change the existing fall settings revision. Heartbeats never increment it.
ALTER TABLE device_state ADD COLUMN media_settings_revision NUMERIC(20,0) NOT NULL DEFAULT 1
  CHECK (media_settings_revision BETWEEN 1 AND 18446744073709551615);
ALTER TABLE device_state ADD COLUMN media_settings_saved_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP;
CREATE FUNCTION advance_media_settings_revision() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.camera_enabled IS DISTINCT FROM OLD.camera_enabled
    OR NEW.microphone_enabled IS DISTINCT FROM OLD.microphone_enabled
    OR NEW.monitoring_enabled IS DISTINCT FROM OLD.monitoring_enabled THEN
    NEW.media_settings_revision := OLD.media_settings_revision + 1;
    NEW.media_settings_saved_at := clock_timestamp();
  ELSE
    NEW.media_settings_revision := OLD.media_settings_revision;
    NEW.media_settings_saved_at := OLD.media_settings_saved_at;
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER device_media_settings_revision BEFORE UPDATE ON device_state
  FOR EACH ROW EXECUTE FUNCTION advance_media_settings_revision();
CREATE TABLE device_media_settings_reports (
  device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  runtime_id TEXT NOT NULL,
  sequence NUMERIC(20,0) NOT NULL,
  requested_revision NUMERIC(20,0) NOT NULL,
  payload_json TEXT NOT NULL,
  report_age_s DOUBLE PRECISION NOT NULL,
  received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(device_id,runtime_id)
);
