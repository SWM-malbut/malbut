-- Only consent and ephemeral encounter confirmation are synced to the cloud.
-- Daily summaries and conversation contents stay in the robot's local store.
CREATE TABLE device_companion_policy (
  device_id TEXT PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
  owner_id TEXT NOT NULL,
  revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
  erase_revision INTEGER NOT NULL DEFAULT 0,
  daily BOOLEAN NOT NULL DEFAULT FALSE,
  health BOOLEAN NOT NULL DEFAULT FALSE,
  family BOOLEAN NOT NULL DEFAULT FALSE,
  external_processing BOOLEAN NOT NULL DEFAULT FALSE,
  session_id TEXT,
  session_seen_at TIMESTAMPTZ,
  confirmed_session_id TEXT,
  confirmed_until TIMESTAMPTZ,
  applied_revision INTEGER NOT NULL DEFAULT 0
);
