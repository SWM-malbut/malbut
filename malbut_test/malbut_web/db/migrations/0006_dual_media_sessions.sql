ALTER TABLE stream_sessions
  ADD COLUMN IF NOT EXISTS mode TEXT;

UPDATE stream_sessions
SET mode = CASE
  WHEN EXISTS (
    SELECT 1 FROM recording_sessions
    WHERE recording_sessions.session_id = stream_sessions.id
  ) THEN 'storage'
  ELSE 'p2p'
END
WHERE mode IS NULL;

ALTER TABLE stream_sessions
  ALTER COLUMN mode SET DEFAULT 'p2p',
  ALTER COLUMN mode SET NOT NULL,
  DROP CONSTRAINT IF EXISTS stream_sessions_mode_check,
  ADD CONSTRAINT stream_sessions_mode_check CHECK (mode IN ('p2p', 'storage'));

CREATE UNIQUE INDEX IF NOT EXISTS stream_sessions_device_active_mode_idx
  ON stream_sessions (device_id, mode)
  WHERE status = 'active';

ALTER TABLE device_state
  ADD COLUMN IF NOT EXISTS p2p_session_id TEXT
    REFERENCES stream_sessions(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS storage_session_id TEXT
    REFERENCES stream_sessions(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS p2p_healthy INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS storage_healthy INTEGER NOT NULL DEFAULT 0;

ALTER TABLE device_state
  DROP CONSTRAINT IF EXISTS device_state_p2p_healthy_check,
  DROP CONSTRAINT IF EXISTS device_state_storage_healthy_check,
  ADD CONSTRAINT device_state_p2p_healthy_check CHECK (p2p_healthy IN (0, 1)),
  ADD CONSTRAINT device_state_storage_healthy_check CHECK (storage_healthy IN (0, 1));

UPDATE device_state
SET p2p_session_id = CASE
      WHEN active_stream_mode = 'p2p' THEN active_session_id
      ELSE p2p_session_id
    END,
    storage_session_id = CASE
      WHEN active_stream_mode = 'storage' THEN active_session_id
      ELSE storage_session_id
    END,
    p2p_healthy = CASE
      WHEN active_stream_mode = 'p2p' THEN media_healthy
      ELSE p2p_healthy
    END,
    storage_healthy = CASE
      WHEN active_stream_mode = 'storage' THEN media_healthy
      ELSE storage_healthy
    END;

CREATE INDEX IF NOT EXISTS device_state_p2p_session_idx
  ON device_state (p2p_session_id);
CREATE INDEX IF NOT EXISTS device_state_storage_session_idx
  ON device_state (storage_session_id);
