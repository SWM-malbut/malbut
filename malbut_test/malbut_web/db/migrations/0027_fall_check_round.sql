-- The robot checks a closed or left-open incident again in a new round (SWM25-261,
-- 목업 31번): standing up, 30 minutes after a close on the floor, someone at a spot
-- left open. A round may alert again for a reason already sent in an earlier
-- round, so the once-per-reason key counts per round. Earlier rows are round 1.
ALTER TABLE fall_push_outbox ADD COLUMN check_round INTEGER NOT NULL DEFAULT 1
  CHECK (check_round >= 1);
ALTER TABLE fall_push_outbox DROP CONSTRAINT fall_push_outbox_incident_reason_key;
ALTER TABLE fall_push_outbox ADD CONSTRAINT fall_push_outbox_incident_reason_round_key
  UNIQUE (device_id, incident_id, reason, check_round);
