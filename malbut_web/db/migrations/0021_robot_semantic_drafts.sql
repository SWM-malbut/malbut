-- 방·구역 편집의 반영 대기 (SWM25-237). The owner may edit rooms and Zones while the 말벗 is off;
-- the server sends the latest saved rooms or Zones when the robot is back on the same map
-- (map_id + map_revision). A robot on another map makes the edit stale and the owner is told.
CREATE TABLE robot_semantic_drafts (
  device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('rooms', 'zones')),
  map_id TEXT NOT NULL,
  map_revision TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'applied', 'stale', 'failed')),
  error TEXT CHECK (error IS NULL OR char_length(error) <= 300),
  command_id TEXT,
  saved_by TEXT NOT NULL,
  saved_at TIMESTAMPTZ NOT NULL,
  resolved_at TIMESTAMPTZ,
  PRIMARY KEY (device_id, kind)
);
