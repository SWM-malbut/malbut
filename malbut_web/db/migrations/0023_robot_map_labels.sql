-- 지도 탭 › 지도 관리 (SWM25-237): the names people see for the robot's saved maps.
-- The robot keeps maps as files with ASCII names (map-20261007-1430.yaml); the owner names
-- them freely here ("우리 집 1층") when making a map and later. A map the robot no longer
-- lists keeps its row until it is deleted from the map tab; nothing reads such a row.
CREATE TABLE robot_map_labels (
  device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  map_file TEXT NOT NULL CHECK (map_file ~ '^[A-Za-z0-9][A-Za-z0-9_-]{0,63}\.ya?ml$'),
  name TEXT NOT NULL CHECK (char_length(name) BETWEEN 1 AND 40),
  updated_by TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (device_id, map_file)
);
