ALTER TABLE users ADD COLUMN voice_profile JSONB;
ALTER TABLE robot_runtime_state
  ADD COLUMN applied_voice_profile JSONB,
  ADD COLUMN voice_profile_managed BOOLEAN NOT NULL DEFAULT FALSE;
