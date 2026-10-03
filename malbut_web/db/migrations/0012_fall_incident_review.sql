-- Fall incident clips, human review and web-side reminders.
-- Clips are wall-clock ranges of the continuous recording, never media.
ALTER TABLE fall_incidents
  ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'robot',
  ADD COLUMN IF NOT EXISTS review_state TEXT NOT NULL DEFAULT 'open',
  ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS closed_by TEXT,
  ADD COLUMN IF NOT EXISTS closed_labels JSONB,
  ADD COLUMN IF NOT EXISTS reopened_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS unacknowledged_since TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS reported_by TEXT,
  ADD COLUMN IF NOT EXISTS reported_moment_at TIMESTAMPTZ;

-- User reports have no robot boot, state machine or evidence revision.
ALTER TABLE fall_incidents
  ALTER COLUMN boot_id DROP NOT NULL,
  ALTER COLUMN state DROP NOT NULL,
  ALTER COLUMN evidence_revision DROP NOT NULL;

ALTER TABLE fall_incidents
  ADD CONSTRAINT fall_incidents_origin_check CHECK (
    (origin = 'robot' AND boot_id IS NOT NULL AND state IS NOT NULL AND evidence_revision IS NOT NULL)
    OR (origin = 'user_report' AND reported_by IS NOT NULL AND reported_moment_at IS NOT NULL
        AND boot_id IS NULL AND state IS NULL)
  ),
  ADD CONSTRAINT fall_incidents_review_state_check CHECK (
    (review_state = 'open' AND closed_at IS NULL)
    OR (review_state = 'closed' AND closed_at IS NOT NULL AND closed_by IS NOT NULL)
  );

CREATE TABLE fall_incident_clips (
  device_id TEXT NOT NULL,
  incident_id TEXT NOT NULL,
  segment_index INTEGER NOT NULL CHECK (segment_index BETWEEN 0 AND 31),
  revision INTEGER NOT NULL CHECK (revision >= 1),
  boot_id TEXT,
  start_at TIMESTAMPTZ NOT NULL,
  end_at TIMESTAMPTZ NOT NULL,
  anchor_kinds JSONB NOT NULL,
  found_down BOOLEAN NOT NULL DEFAULT FALSE,
  clock_stepped BOOLEAN NOT NULL DEFAULT FALSE,
  payload_json TEXT,
  received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (device_id, incident_id, segment_index),
  CHECK (end_at > start_at AND end_at - start_at <= INTERVAL '125 seconds'),
  FOREIGN KEY (device_id, incident_id) REFERENCES fall_incidents(device_id, incident_id) ON DELETE CASCADE
);
CREATE INDEX fall_incident_clips_time_idx ON fall_incident_clips(device_id, start_at);

-- One current opinion per user; history lives in the activity log.
CREATE TABLE fall_incident_opinions (
  device_id TEXT NOT NULL,
  incident_id TEXT NOT NULL,
  user_email TEXT NOT NULL,
  label TEXT NOT NULL CHECK (label IN ('fall', 'suspected_fall', 'normal')),
  memo TEXT CHECK (memo IS NULL OR char_length(memo) <= 500),
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (device_id, incident_id, user_email),
  FOREIGN KEY (device_id, incident_id) REFERENCES fall_incidents(device_id, incident_id) ON DELETE CASCADE
);

CREATE TABLE fall_incident_activity (
  id BIGSERIAL PRIMARY KEY,
  device_id TEXT NOT NULL,
  incident_id TEXT NOT NULL,
  actor_email TEXT NOT NULL,
  action TEXT NOT NULL CHECK (action IN ('reported', 'opinion_set', 'opinion_cleared', 'closed', 'reopened')),
  label TEXT CHECK (label IS NULL OR label IN ('fall', 'suspected_fall', 'normal')),
  memo TEXT CHECK (memo IS NULL OR char_length(memo) <= 500),
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (device_id, incident_id) REFERENCES fall_incidents(device_id, incident_id) ON DELETE CASCADE
);
CREATE INDEX fall_incident_activity_incident_idx ON fall_incident_activity(device_id, incident_id, created_at);

-- Web-originated pushes: [재발신] reminders and reopen notices. Robot
-- notifications stay in fall_push_outbox (one per incident and level).
CREATE TABLE fall_web_notices (
  device_id TEXT NOT NULL,
  notice_id TEXT NOT NULL,
  incident_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('resend', 'reopen')),
  cycle_key TEXT NOT NULL,
  round INTEGER NOT NULL CHECK (round BETWEEN 1 AND 3),
  level TEXT NOT NULL CHECK (level IN ('check', 'urgent')),
  reason TEXT NOT NULL,
  occurred_at TIMESTAMPTZ NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'canceled')),
  attempt_count INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  lease_id TEXT,
  lease_until TIMESTAMPTZ,
  subscription_results JSONB NOT NULL DEFAULT '{}',
  last_error TEXT,
  accepted_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (device_id, notice_id),
  UNIQUE (device_id, incident_id, cycle_key, round),
  FOREIGN KEY (device_id, incident_id) REFERENCES fall_incidents(device_id, incident_id) ON DELETE CASCADE
);
CREATE INDEX fall_web_notices_due_idx ON fall_web_notices(status, next_attempt_at);
