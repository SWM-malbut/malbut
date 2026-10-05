-- Registration codes: the team hands one to the person who will own a 말벗.
-- Only an HMAC of the code is stored; the code itself is shown once, when it is made.
CREATE TABLE device_registration_codes (
  code_digest TEXT PRIMARY KEY CHECK (code_digest ~ '^[a-f0-9]{64}$'),
  device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at TIMESTAMPTZ NOT NULL,
  used_at TIMESTAMPTZ,
  used_by TEXT REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX device_registration_codes_device_id_idx ON device_registration_codes (device_id);
