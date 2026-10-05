-- Social login (Kakao, Naver, Google via OpenID Connect) signs people in as users directly.
-- Email sessions keep their Cognito columns until email login is removed with registration codes.
ALTER TABLE web_auth_sessions ADD COLUMN user_id TEXT REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE web_auth_sessions ALTER COLUMN cognito_sub DROP NOT NULL;
ALTER TABLE web_auth_sessions ALTER COLUMN cognito_username DROP NOT NULL;
ALTER TABLE web_auth_sessions ALTER COLUMN user_email DROP NOT NULL;
ALTER TABLE web_auth_sessions ADD CONSTRAINT web_auth_sessions_identity_check
  CHECK (user_id IS NOT NULL OR (cognito_sub IS NOT NULL AND user_email IS NOT NULL));
CREATE INDEX web_auth_sessions_user_id_idx ON web_auth_sessions (user_id);

-- One pending sign-in per browser tab: state, nonce and PKCE verifier, used once.
CREATE TABLE oidc_login_transactions (
  token_digest TEXT PRIMARY KEY CHECK (token_digest ~ '^[a-f0-9]{64}$'),
  provider TEXT NOT NULL CHECK (provider IN ('kakao', 'naver', 'google')),
  state TEXT NOT NULL,
  nonce TEXT NOT NULL,
  code_verifier_ciphertext TEXT NOT NULL,
  return_to TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at TIMESTAMPTZ NOT NULL,
  consumed_at TIMESTAMPTZ
);
CREATE INDEX oidc_login_transactions_expires_at_idx ON oidc_login_transactions (expires_at);
