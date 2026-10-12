-- One first notification per reason, not per level. "직접 확인하지 못함" (urgent)
-- and the revisit results must not swallow a later 도움 요청 of the same level;
-- db/fall-incidents.ts keeps one per level among the older robot reasons.
-- Each reason has one level, so existing rows already satisfy the new key.
ALTER TABLE fall_push_outbox DROP CONSTRAINT fall_push_outbox_device_id_incident_id_level_key;
ALTER TABLE fall_push_outbox ADD CONSTRAINT fall_push_outbox_incident_reason_key
  UNIQUE (device_id, incident_id, reason);
