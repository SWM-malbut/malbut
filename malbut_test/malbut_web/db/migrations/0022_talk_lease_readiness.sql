-- A talk lease may transmit only after the robot confirms STT is suspended.
ALTER TABLE talk_leases ADD COLUMN IF NOT EXISTS ready_until TIMESTAMPTZ;
