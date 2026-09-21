ALTER TABLE homecam_events
  ADD COLUMN IF NOT EXISTS event_group_id TEXT,
  ADD COLUMN IF NOT EXISTS segment_index INTEGER,
  ADD COLUMN IF NOT EXISTS labels_json TEXT,
  ADD COLUMN IF NOT EXISTS clip_start_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS clip_end_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS clip_state TEXT NOT NULL DEFAULT 'detected',
  ADD COLUMN IF NOT EXISTS monotonic_duration_ms INTEGER,
  ADD COLUMN IF NOT EXISTS boot_id TEXT,
  ADD COLUMN IF NOT EXISTS session_ids_json TEXT,
  ADD COLUMN IF NOT EXISTS clock_stepped INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS notification_suppressed INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS start_idempotency_key TEXT,
  ADD COLUMN IF NOT EXISTS end_idempotency_key TEXT,
  ADD COLUMN IF NOT EXISTS start_request_fingerprint TEXT,
  ADD COLUMN IF NOT EXISTS end_request_fingerprint TEXT,
  ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS ai_status TEXT NOT NULL DEFAULT 'not_requested',
  ADD COLUMN IF NOT EXISTS ai_summary TEXT,
  ADD COLUMN IF NOT EXISTS ai_labels_json TEXT,
  ADD COLUMN IF NOT EXISTS ai_severity TEXT,
  ADD COLUMN IF NOT EXISTS ai_confidence REAL,
  ADD COLUMN IF NOT EXISTS ai_model_id TEXT,
  ADD COLUMN IF NOT EXISTS ai_model_version TEXT,
  ADD COLUMN IF NOT EXISTS ai_prompt_version TEXT,
  ADD COLUMN IF NOT EXISTS ai_input_spec_json TEXT,
  ADD COLUMN IF NOT EXISTS ai_error TEXT,
  ADD COLUMN IF NOT EXISTS ai_analyzed_at TIMESTAMPTZ;

ALTER TABLE homecam_events
  DROP CONSTRAINT IF EXISTS homecam_events_segment_index_check,
  DROP CONSTRAINT IF EXISTS homecam_events_clip_state_check,
  DROP CONSTRAINT IF EXISTS homecam_events_monotonic_duration_check,
  DROP CONSTRAINT IF EXISTS homecam_events_clock_stepped_check,
  DROP CONSTRAINT IF EXISTS homecam_events_notification_suppressed_check,
  DROP CONSTRAINT IF EXISTS homecam_events_ai_status_check,
  DROP CONSTRAINT IF EXISTS homecam_events_ai_confidence_check;

ALTER TABLE homecam_events
  ADD CONSTRAINT homecam_events_segment_index_check CHECK (
    segment_index IS NULL OR segment_index >= 0
  ),
  ADD CONSTRAINT homecam_events_clip_state_check CHECK (
    clip_state IN ('detected', 'recording', 'ready', 'incomplete', 'unavailable', 'expired')
  ),
  ADD CONSTRAINT homecam_events_monotonic_duration_check CHECK (
    monotonic_duration_ms IS NULL OR
    (monotonic_duration_ms > 0 AND monotonic_duration_ms <= 125000)
  ),
  ADD CONSTRAINT homecam_events_clock_stepped_check CHECK (clock_stepped IN (0, 1)),
  ADD CONSTRAINT homecam_events_notification_suppressed_check CHECK (
    notification_suppressed IN (0, 1)
  ),
  ADD CONSTRAINT homecam_events_ai_status_check CHECK (
    ai_status IN ('not_requested', 'queued', 'analyzing', 'ready', 'failed')
  ),
  ADD CONSTRAINT homecam_events_ai_confidence_check CHECK (
    ai_confidence IS NULL OR (ai_confidence >= 0 AND ai_confidence <= 1)
  );

CREATE UNIQUE INDEX IF NOT EXISTS homecam_events_device_group_segment_idx
  ON homecam_events (device_id, event_group_id, segment_index)
  WHERE event_group_id IS NOT NULL AND segment_index IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS homecam_events_device_start_idempotency_idx
  ON homecam_events (device_id, start_idempotency_key)
  WHERE start_idempotency_key IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS homecam_events_device_end_idempotency_idx
  ON homecam_events (device_id, end_idempotency_key)
  WHERE end_idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS homecam_events_clip_state_received_idx
  ON homecam_events (clip_state, received_at)
  WHERE deleted_at IS NULL;

CREATE OR REPLACE FUNCTION enqueue_homecam_push()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  IF NEW.notification_suppressed = 0 THEN
    INSERT INTO homecam_push_outbox (event_id, device_id)
    VALUES (NEW.id, NEW.device_id)
    ON CONFLICT (event_id) DO NOTHING;
  END IF;
  RETURN NEW;
END;
$$;
