CREATE TABLE IF NOT EXISTS robot_maps (
  device_id TEXT PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
  revision TEXT NOT NULL,
  map_id TEXT NOT NULL,
  map_revision TEXT NOT NULL,
  width INTEGER NOT NULL CHECK (width > 0 AND width <= 8192),
  height INTEGER NOT NULL CHECK (height > 0 AND height <= 8192),
  resolution REAL NOT NULL CHECK (resolution >= 0.001 AND resolution <= 1),
  origin_x REAL NOT NULL,
  origin_y REAL NOT NULL,
  origin_yaw REAL NOT NULL,
  preview_base64 TEXT NOT NULL,
  user_map_json TEXT,
  source_created_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS robot_runtime_state (
  device_id TEXT PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
  state TEXT NOT NULL,
  message TEXT NOT NULL,
  pose_x REAL,
  pose_y REAL,
  pose_yaw REAL,
  localization_state TEXT NOT NULL,
  tf_age_s REAL,
  nav2_json TEXT NOT NULL,
  target_json TEXT,
  map_revision_counter INTEGER NOT NULL CHECK (map_revision_counter >= 0),
  observed_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS robot_commands (
  id TEXT PRIMARY KEY,
  device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  operation TEXT NOT NULL CHECK (operation IN (
    'start', 'finish', 'cancel',
    'navigation_preview', 'navigation_start', 'navigation_cancel'
  )),
  payload_json TEXT NOT NULL DEFAULT '{}',
  requested_by TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued', 'claimed', 'completed', 'failed')),
  requested_at TIMESTAMPTZ NOT NULL,
  claimed_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  result_json TEXT
);
CREATE INDEX IF NOT EXISTS robot_commands_device_status_idx
  ON robot_commands (device_id, status, requested_at ASC);
CREATE UNIQUE INDEX IF NOT EXISTS robot_commands_one_active_idx
  ON robot_commands (device_id)
  WHERE status IN ('queued', 'claimed');
