CREATE TABLE IF NOT EXISTS web_auth_sessions (
  token_digest TEXT PRIMARY KEY
    CHECK (token_digest ~ '^[a-f0-9]{64}$'),
  cognito_sub TEXT NOT NULL,
  cognito_username TEXT NOT NULL,
  user_email TEXT NOT NULL,
  full_name TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at TIMESTAMPTZ NOT NULL,
  revoked_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS web_auth_sessions_user_sub_idx
  ON web_auth_sessions (cognito_sub);
CREATE INDEX IF NOT EXISTS web_auth_sessions_expires_at_idx
  ON web_auth_sessions (expires_at);

CREATE TABLE IF NOT EXISTS web_auth_challenges (
  token_digest TEXT PRIMARY KEY
    CHECK (token_digest ~ '^[a-f0-9]{64}$'),
  cognito_username TEXT NOT NULL,
  challenge_name TEXT NOT NULL
    CHECK (challenge_name IN ('NEW_PASSWORD_REQUIRED', 'SOFTWARE_TOKEN_MFA')),
  cognito_session_ciphertext TEXT NOT NULL,
  failure_count INTEGER NOT NULL DEFAULT 0
    CHECK (failure_count >= 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at TIMESTAMPTZ NOT NULL,
  claimed_at TIMESTAMPTZ,
  consumed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS web_auth_challenges_expires_at_idx
  ON web_auth_challenges (expires_at);
