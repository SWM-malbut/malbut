-- Per-robot fall Cloud key (encrypted) and user-requested AI reviews.
-- One row per device; the version keeps increasing across replace/delete so
-- the robot can tell that its copy is stale.
CREATE TABLE fall_cloud_keys (
  device_id TEXT PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
  -- 0: the robot reported its model but the owner never set a key.
  key_version INTEGER NOT NULL CHECK (key_version >= 0),
  ciphertext TEXT,
  last4 TEXT CHECK (last4 IS NULL OR char_length(last4) <= 4),
  updated_by TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  robot_model TEXT CHECK (robot_model IS NULL OR robot_model ~ '^[A-Za-z0-9_.:-]{1,100}$'),
  robot_model_reported_at TIMESTAMPTZ,
  robot_key_version INTEGER,
  robot_key_fetched_at TIMESTAMPTZ,
  CHECK ((ciphertext IS NULL) = (last4 IS NULL))
);

-- Photo-only verdicts. At most one queued/running review per incident.
CREATE TABLE fall_ai_reviews (
  device_id TEXT NOT NULL,
  review_id TEXT NOT NULL,
  incident_id TEXT NOT NULL,
  requested_by TEXT NOT NULL,
  moment_at TIMESTAMPTZ NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'completed', 'failed')),
  attempt_count INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  lease_id TEXT,
  lease_until TIMESTAMPTZ,
  model TEXT,
  frame_count INTEGER CHECK (frame_count IS NULL OR frame_count BETWEEN 0 AND 12),
  history_incomplete BOOLEAN,
  assessment TEXT CHECK (assessment IS NULL OR
    assessment IN ('observed_fall', 'suspected_fall', 'normal_activity', 'unobservable')),
  explanation TEXT CHECK (explanation IS NULL OR char_length(explanation) <= 1000),
  error_code TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_at TIMESTAMPTZ,
  PRIMARY KEY (device_id, review_id),
  FOREIGN KEY (device_id, incident_id) REFERENCES fall_incidents(device_id, incident_id) ON DELETE CASCADE,
  CHECK (status <> 'completed' OR assessment IS NOT NULL)
);
CREATE UNIQUE INDEX fall_ai_reviews_one_active_idx ON fall_ai_reviews(device_id, incident_id)
  WHERE status IN ('queued', 'running');
CREATE INDEX fall_ai_reviews_due_idx ON fall_ai_reviews(status, next_attempt_at);

-- Follow-up questions: reference answers only, never a verdict.
CREATE TABLE fall_ai_questions (
  device_id TEXT NOT NULL,
  question_id TEXT NOT NULL,
  review_id TEXT NOT NULL,
  asked_by TEXT NOT NULL,
  question TEXT NOT NULL CHECK (char_length(question) BETWEEN 1 AND 500),
  -- "내 메모와 이전 판정 기록 함께 보내기" switch.
  include_context BOOLEAN NOT NULL DEFAULT TRUE,
  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'completed', 'failed')),
  attempt_count INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  lease_id TEXT,
  lease_until TIMESTAMPTZ,
  answer TEXT CHECK (answer IS NULL OR char_length(answer) <= 2000),
  error_code TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_at TIMESTAMPTZ,
  PRIMARY KEY (device_id, question_id),
  FOREIGN KEY (device_id, review_id) REFERENCES fall_ai_reviews(device_id, review_id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX fall_ai_questions_one_active_idx ON fall_ai_questions(device_id, review_id)
  WHERE status IN ('queued', 'running');
