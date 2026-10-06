-- Keys the owner sets for one 말벗 besides the fall Cloud key: OpenAI (대화·목소리) and KMA (날씨).
-- Delivered to the robot like the fall key: the version keeps increasing across replace/delete,
-- 0 means the owner never set one (the robot keeps its own team key).
CREATE TABLE device_service_keys (
  device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  service TEXT NOT NULL CHECK (service IN ('openai', 'kma')),
  key_version INTEGER NOT NULL CHECK (key_version >= 0),
  ciphertext TEXT,
  last4 TEXT CHECK (last4 IS NULL OR char_length(last4) <= 4),
  updated_by TEXT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  -- The model the robot uses with this key; a new key is checked against it.
  robot_model TEXT CHECK (robot_model IS NULL OR robot_model ~ '^[A-Za-z0-9_.:-]{1,100}$'),
  robot_model_reported_at TIMESTAMPTZ,
  robot_key_version INTEGER,
  robot_key_fetched_at TIMESTAMPTZ,
  PRIMARY KEY (device_id, service),
  CHECK ((ciphertext IS NULL) = (last4 IS NULL))
);

-- What the robot last said about each key it uses (shown on the home screen).
CREATE TABLE device_key_health (
  device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  service TEXT NOT NULL CHECK (service IN ('openai', 'kma', 'fall')),
  state TEXT NOT NULL CHECK (state IN ('ok', 'missing', 'invalid', 'quota')),
  code TEXT CHECK (code IS NULL OR code ~ '^[a-z0-9_]{1,64}$'),
  reported_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (device_id, service)
);
