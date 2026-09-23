CREATE TABLE IF NOT EXISTS devices (
  id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  kvs_channel_arn TEXT NOT NULL UNIQUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS device_memberships (
  device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  user_email TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'owner'
    CHECK (role IN ('owner', 'family', 'broadcaster')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (device_id, user_email)
);
CREATE INDEX IF NOT EXISTS device_memberships_user_email_idx
  ON device_memberships (user_email);

CREATE TABLE IF NOT EXISTS stream_sessions (
  id TEXT PRIMARY KEY,
  room_code TEXT NOT NULL UNIQUE,
  device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  started_by TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'ended', 'expired')),
  started_at TIMESTAMPTZ NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  ended_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS stream_sessions_device_status_idx
  ON stream_sessions (device_id, status);

CREATE TABLE IF NOT EXISTS stream_session_access (
  session_id TEXT PRIMARY KEY REFERENCES stream_sessions(id) ON DELETE CASCADE,
  secret_digest TEXT NOT NULL,
  auth_version TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS recording_sessions (
  session_id TEXT PRIMARY KEY REFERENCES stream_sessions(id) ON DELETE CASCADE,
  kvs_stream_arn TEXT NOT NULL,
  kvs_channel_arn TEXT NOT NULL,
  started_at TIMESTAMPTZ,
  ended_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS recording_sessions_started_at_idx
  ON recording_sessions (started_at);

CREATE TABLE IF NOT EXISTS request_rate_limits (
  rate_key TEXT PRIMARY KEY,
  window_started_at BIGINT NOT NULL,
  request_count INTEGER NOT NULL CHECK (request_count >= 0)
);

CREATE TABLE IF NOT EXISTS device_credentials (
  id TEXT PRIMARY KEY,
  device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  label TEXT NOT NULL,
  token_digest TEXT NOT NULL UNIQUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_used_at TIMESTAMPTZ,
  expires_at TIMESTAMPTZ,
  revoked_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS device_credentials_device_id_idx
  ON device_credentials (device_id);

CREATE TABLE IF NOT EXISTS device_state (
  device_id TEXT PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
  monitoring_enabled INTEGER NOT NULL DEFAULT 0
    CHECK (monitoring_enabled IN (0, 1)),
  camera_enabled INTEGER NOT NULL DEFAULT 1
    CHECK (camera_enabled IN (0, 1)),
  microphone_enabled INTEGER NOT NULL DEFAULT 1
    CHECK (microphone_enabled IN (0, 1)),
  source_profile TEXT NOT NULL DEFAULT 'unknown',
  image_topic TEXT,
  active_stream_mode TEXT NOT NULL DEFAULT 'idle'
    CHECK (active_stream_mode IN ('idle', 'p2p', 'storage')),
  active_session_id TEXT REFERENCES stream_sessions(id) ON DELETE SET NULL,
  media_healthy INTEGER NOT NULL DEFAULT 0
    CHECK (media_healthy IN (0, 1)),
  detector_healthy INTEGER NOT NULL DEFAULT 0
    CHECK (detector_healthy IN (0, 1)),
  last_seen_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS homecam_events (
  id TEXT PRIMARY KEY,
  device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  event_type TEXT NOT NULL CHECK (event_type IN ('motion', 'person', 'dog', 'cat')),
  confidence REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
  occurred_at TIMESTAMPTZ NOT NULL,
  received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  idempotency_key TEXT NOT NULL,
  request_fingerprint TEXT NOT NULL,
  recording_session_id TEXT REFERENCES stream_sessions(id) ON DELETE SET NULL,
  recording_offset_ms INTEGER CHECK (
    recording_offset_ms IS NULL OR recording_offset_ms >= 0
  ),
  UNIQUE (device_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS homecam_events_device_occurred_idx
  ON homecam_events (device_id, occurred_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS homecam_push_outbox (
  event_id TEXT PRIMARY KEY REFERENCES homecam_events(id) ON DELETE CASCADE,
  device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  delivered_at TIMESTAMPTZ,
  last_error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS homecam_push_outbox_due_idx
  ON homecam_push_outbox (device_id, delivered_at, next_attempt_at);

CREATE OR REPLACE FUNCTION enqueue_homecam_push()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  INSERT INTO homecam_push_outbox (event_id, device_id)
  VALUES (NEW.id, NEW.device_id)
  ON CONFLICT (event_id) DO NOTHING;
  RETURN NEW;
END;
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger WHERE tgname = 'homecam_events_push_outbox'
  ) THEN
    CREATE TRIGGER homecam_events_push_outbox
      AFTER INSERT ON homecam_events
      FOR EACH ROW EXECUTE FUNCTION enqueue_homecam_push();
  END IF;
END;
$$;

CREATE TABLE IF NOT EXISTS push_subscriptions (
  id TEXT PRIMARY KEY,
  device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  user_email TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  p256dh TEXT NOT NULL,
  auth TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  revoked_at TIMESTAMPTZ,
  UNIQUE (user_email, device_id, endpoint)
);
CREATE INDEX IF NOT EXISTS push_subscriptions_device_id_idx
  ON push_subscriptions (device_id);

CREATE TABLE IF NOT EXISTS access_audit_log (
  id TEXT PRIMARY KEY,
  device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  actor_type TEXT NOT NULL CHECK (actor_type IN ('user', 'device', 'system')),
  actor_id TEXT NOT NULL,
  action TEXT NOT NULL,
  metadata_json TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS access_audit_log_device_created_idx
  ON access_audit_log (device_id, created_at DESC);

CREATE TABLE IF NOT EXISTS talk_leases (
  device_id TEXT PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
  lease_id TEXT NOT NULL,
  user_email TEXT NOT NULL,
  client_id TEXT NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
