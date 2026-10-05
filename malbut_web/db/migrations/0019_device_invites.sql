-- Guardian invite links: one live link per 말벗, for 24 hours, used by any number of people.
-- Looked up by HMAC; the link itself is kept sealed so the owner can copy it again.
CREATE TABLE device_invites (
  id TEXT PRIMARY KEY,
  device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  token_digest TEXT NOT NULL UNIQUE CHECK (token_digest ~ '^[a-f0-9]{64}$'),
  token_ciphertext TEXT NOT NULL,
  created_by TEXT REFERENCES users(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at TIMESTAMPTZ NOT NULL,
  revoked_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX device_invites_one_live_idx ON device_invites (device_id) WHERE revoked_at IS NULL;

-- Which link a guardian came in by ("이 링크로 2명 들어옴", "링크로 들어옴").
ALTER TABLE device_memberships ADD COLUMN invite_id TEXT REFERENCES device_invites(id) ON DELETE SET NULL;
