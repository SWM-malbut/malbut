CREATE TABLE demo_sessions (
  device_id TEXT PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
  owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  session_id UUID NOT NULL,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ NOT NULL,
  pose_count INTEGER,
  suspect BOOLEAN NOT NULL DEFAULT false,
  pose_at TIMESTAMPTZ
);

CREATE TABLE demo_lines (
  device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  event_id TEXT NOT NULL,
  speaker TEXT NOT NULL CHECK (speaker IN ('robot','user')),
  text TEXT NOT NULL,
  confirmation BOOLEAN NOT NULL,
  spoken_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (device_id, owner_id, event_id)
);
CREATE INDEX demo_lines_time ON demo_lines(spoken_at);
