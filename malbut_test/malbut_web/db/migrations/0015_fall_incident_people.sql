-- 사람 표시: person boxes of a fall clip segment, positions only (no media,
-- no identities). Times are ms from the segment start; boxes are 1/1000 of the
-- frame. Removed with the clip, and by maintenance once the video has expired.
CREATE TABLE fall_incident_people (
  device_id TEXT NOT NULL,
  incident_id TEXT NOT NULL,
  segment_index INTEGER NOT NULL,
  revision INTEGER NOT NULL CHECK (revision >= 1),
  payload_json TEXT NOT NULL,
  received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (device_id, incident_id, segment_index),
  FOREIGN KEY (device_id, incident_id, segment_index)
    REFERENCES fall_incident_clips(device_id, incident_id, segment_index) ON DELETE CASCADE
);
