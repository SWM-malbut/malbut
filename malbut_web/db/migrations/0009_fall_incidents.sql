-- Fall metadata and delivery intents are independent of KVS recording state.
CREATE TABLE fall_incidents (
  device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  incident_id TEXT NOT NULL,
  boot_id TEXT NOT NULL,
  latest_sequence BIGINT NOT NULL DEFAULT 0,
  evidence_revision INTEGER NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('verifying','recheck_required','help_required','resolved')),
  fall_seen BOOLEAN NOT NULL DEFAULT FALSE,
  assessment TEXT,
  answer TEXT,
  notification_rank INTEGER NOT NULL DEFAULT 0 CHECK (notification_rank BETWEEN 0 AND 3),
  occurred_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (device_id, incident_id)
);
CREATE INDEX fall_incidents_recent_idx ON fall_incidents(device_id, updated_at DESC);

CREATE TABLE fall_incident_events (
  device_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  incident_id TEXT NOT NULL,
  sequence BIGINT NOT NULL CHECK (sequence > 0),
  payload_json TEXT NOT NULL,
  received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (device_id, event_id),
  UNIQUE (device_id, incident_id, sequence),
  FOREIGN KEY (device_id, incident_id) REFERENCES fall_incidents(device_id, incident_id) ON DELETE CASCADE
);

CREATE TABLE fall_push_outbox (
  device_id TEXT NOT NULL,
  notification_id TEXT NOT NULL,
  incident_id TEXT NOT NULL,
  level TEXT NOT NULL CHECK (level IN ('info','check','urgent')),
  reason TEXT NOT NULL,
  occurred_at TIMESTAMPTZ NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','accepted','superseded')),
  attempt_count INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  lease_id TEXT,
  lease_until TIMESTAMPTZ,
  subscription_results JSONB NOT NULL DEFAULT '{}',
  last_error TEXT,
  accepted_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (device_id, notification_id),
  UNIQUE (device_id, incident_id, level),
  FOREIGN KEY (device_id, notification_id) REFERENCES fall_incident_events(device_id, event_id) ON DELETE CASCADE
);
CREATE INDEX fall_push_due_idx ON fall_push_outbox(status, next_attempt_at);
