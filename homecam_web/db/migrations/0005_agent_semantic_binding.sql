CREATE SEQUENCE device_membership_generation_seq AS BIGINT START WITH 1;

ALTER TABLE device_memberships
  ADD COLUMN binding_generation BIGINT;

UPDATE device_memberships
SET binding_generation = nextval('device_membership_generation_seq');

ALTER TABLE device_memberships
  ALTER COLUMN binding_generation SET NOT NULL,
  ALTER COLUMN binding_generation
    SET DEFAULT nextval('device_membership_generation_seq');

CREATE SEQUENCE robot_map_generation_seq AS BIGINT START WITH 1;

ALTER TABLE robot_maps
  ADD COLUMN server_generation BIGINT;

UPDATE robot_maps
SET server_generation = nextval('robot_map_generation_seq');

ALTER TABLE robot_maps
  ALTER COLUMN server_generation SET NOT NULL,
  ALTER COLUMN server_generation
    SET DEFAULT nextval('robot_map_generation_seq');
