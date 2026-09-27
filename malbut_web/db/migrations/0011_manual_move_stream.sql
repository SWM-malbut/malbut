-- Held joystick/keyboard velocities repeat several times a second. They may be
-- queued while the robot is still completing the previous velocity, so the
-- single-active-command rule no longer covers manual_move; every other
-- command still waits for the slot. The repository drops unclaimed velocities
-- when a newer one arrives and expires stale ones within two seconds.
DROP INDEX IF EXISTS robot_commands_one_active_idx;
CREATE UNIQUE INDEX IF NOT EXISTS robot_commands_one_active_idx
  ON robot_commands (device_id)
  WHERE status IN ('queued', 'claimed') AND operation <> 'manual_move';
