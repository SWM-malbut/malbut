-- Recording follows the camera (2026-10-12): turning the camera off stops
-- recording, and turning it on again restores what the user had chosen, so no
-- one has to switch recording back on in the settings each time.
-- monitoring_enabled stays the effective state robots and sessions read
-- (always 0 while the camera is off); monitoring_preferred is the choice.
ALTER TABLE device_state ADD COLUMN monitoring_preferred INTEGER NOT NULL DEFAULT 0
  CHECK (monitoring_preferred IN (0, 1));
UPDATE device_state SET monitoring_preferred = monitoring_enabled;
