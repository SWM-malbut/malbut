-- Users are identified by an opaque ID instead of their login email.
-- A user can have several identities (provider, subject). Until social login
-- arrives every identity is provider 'email' with the lowercase login email.
CREATE TABLE users (
  id TEXT PRIMARY KEY,
  display_name TEXT CHECK (display_name IS NULL OR char_length(display_name) BETWEEN 1 AND 20),
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE user_identities (
  provider TEXT NOT NULL,
  subject TEXT NOT NULL,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (provider, subject)
);
CREATE INDEX user_identities_user_id_idx ON user_identities (user_id);

-- One user per email that already appears anywhere as a person.
CREATE TEMPORARY TABLE migrated_users AS
SELECT email, gen_random_uuid()::text AS id
FROM (
  SELECT lower(user_email) AS email FROM device_memberships
  UNION SELECT lower(user_email) FROM push_subscriptions
  UNION SELECT lower(user_email) FROM talk_leases
  UNION SELECT lower(requested_by) FROM robot_commands
  UNION SELECT lower(closed_by) FROM fall_incidents
  UNION SELECT lower(reported_by) FROM fall_incidents
  UNION SELECT lower(user_email) FROM fall_incident_opinions
  UNION SELECT lower(actor_email) FROM fall_incident_activity
  UNION SELECT lower(updated_by) FROM fall_cloud_keys WHERE updated_by <> 'robot'
  UNION SELECT lower(requested_by) FROM fall_ai_reviews
  UNION SELECT lower(asked_by) FROM fall_ai_questions
  UNION SELECT lower(actor_id) FROM access_audit_log WHERE actor_type = 'user'
) AS people
WHERE email IS NOT NULL AND email <> '';

INSERT INTO users (id) SELECT id FROM migrated_users;
INSERT INTO user_identities (provider, subject, user_id)
SELECT 'email', email, id FROM migrated_users;

-- Memberships: (device, user) instead of (device, email).
ALTER TABLE device_memberships ADD COLUMN user_id TEXT REFERENCES users(id) ON DELETE CASCADE;
UPDATE device_memberships m SET user_id = u.id
FROM migrated_users u WHERE u.email = lower(m.user_email);
ALTER TABLE device_memberships ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE device_memberships DROP COLUMN user_email;
ALTER TABLE device_memberships ADD PRIMARY KEY (device_id, user_id);
CREATE INDEX device_memberships_user_id_idx ON device_memberships (user_id);

ALTER TABLE push_subscriptions ADD COLUMN user_id TEXT REFERENCES users(id) ON DELETE CASCADE;
UPDATE push_subscriptions s SET user_id = u.id
FROM migrated_users u WHERE u.email = lower(s.user_email);
ALTER TABLE push_subscriptions ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE push_subscriptions DROP COLUMN user_email;
ALTER TABLE push_subscriptions
  ADD CONSTRAINT push_subscriptions_user_device_endpoint_key UNIQUE (user_id, device_id, endpoint);

ALTER TABLE talk_leases ADD COLUMN user_id TEXT REFERENCES users(id) ON DELETE CASCADE;
UPDATE talk_leases l SET user_id = u.id
FROM migrated_users u WHERE u.email = lower(l.user_email);
ALTER TABLE talk_leases ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE talk_leases DROP COLUMN user_email;

ALTER TABLE fall_incident_opinions ADD COLUMN user_id TEXT REFERENCES users(id) ON DELETE CASCADE;
UPDATE fall_incident_opinions o SET user_id = u.id
FROM migrated_users u WHERE u.email = lower(o.user_email);
ALTER TABLE fall_incident_opinions ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE fall_incident_opinions DROP COLUMN user_email;
ALTER TABLE fall_incident_opinions ADD PRIMARY KEY (device_id, incident_id, user_id);

ALTER TABLE fall_incident_activity RENAME COLUMN actor_email TO actor_user_id;
UPDATE fall_incident_activity a SET actor_user_id = u.id
FROM migrated_users u WHERE u.email = lower(a.actor_user_id);

-- Columns that keep their name and now hold a user ID.
UPDATE robot_commands c SET requested_by = u.id
FROM migrated_users u WHERE u.email = lower(c.requested_by);
UPDATE fall_incidents i SET closed_by = u.id
FROM migrated_users u WHERE u.email = lower(i.closed_by);
UPDATE fall_incidents i SET reported_by = u.id
FROM migrated_users u WHERE u.email = lower(i.reported_by);
UPDATE fall_cloud_keys k SET updated_by = u.id
FROM migrated_users u WHERE k.updated_by <> 'robot' AND u.email = lower(k.updated_by);
UPDATE fall_ai_reviews r SET requested_by = u.id
FROM migrated_users u WHERE u.email = lower(r.requested_by);
UPDATE fall_ai_questions q SET asked_by = u.id
FROM migrated_users u WHERE u.email = lower(q.asked_by);
UPDATE access_audit_log l SET actor_id = u.id
FROM migrated_users u WHERE l.actor_type = 'user' AND u.email = lower(l.actor_id);

-- Rate-limit windows were keyed by email; they are short-lived and rebuilt by user ID.
DELETE FROM request_rate_limits WHERE rate_key LIKE '%@%';

DROP TABLE migrated_users;
