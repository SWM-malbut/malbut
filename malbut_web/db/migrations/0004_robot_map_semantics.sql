ALTER TABLE robot_maps
  ADD COLUMN IF NOT EXISTS semantic_zones_json TEXT;

CREATE TABLE IF NOT EXISTS robot_map_drafts (
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
  semantic_zones_json TEXT,
  source_created_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE robot_commands
  DROP CONSTRAINT IF EXISTS robot_commands_operation_check;

ALTER TABLE robot_commands
  ADD CONSTRAINT robot_commands_operation_check CHECK (operation IN (
    'start', 'finish', 'cancel',
    'navigation_preview', 'navigation_start', 'navigation_cancel',
    'room_split', 'room_merge', 'rooms_save', 'zones_apply'
  ));
