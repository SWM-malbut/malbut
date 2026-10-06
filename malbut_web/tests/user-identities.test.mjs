import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import { fallDatabase, moduleLoader } from "./helpers/fall-db-harness.mjs";

async function withUsers(work, options) {
  const h = await fallDatabase(options), load = moduleLoader();
  const pg = load("db/postgres.ts");
  try {
    await pg.withPostgresPoolForTest(h.pool, () => work({
      h, users: load("db/users.ts"), homecam: load("db/homecam.ts"),
    }));
  } finally { await h.db.close(); }
}

test("0016 turns every email that names a person into one user, keeping records and access", async () => {
  // Seeded before 0016: owner@ and family@ memberships keyed by email.
  const h = await fallDatabase({ through: "0015_fall_incident_people" });
  try {
    await h.db.exec(`
      INSERT INTO push_subscriptions(id,device_id,user_email,endpoint,p256dh,auth)
        VALUES ('p1','robot-a','Owner@Example.com','https://push.test/1','k','a');
      INSERT INTO robot_commands(id,device_id,operation,payload_json,requested_by,status,requested_at)
        VALUES ('c1','robot-a','start','{}','owner@example.com','completed',CURRENT_TIMESTAMP);
      INSERT INTO access_audit_log(id,device_id,actor_type,actor_id,action)
        VALUES ('a1','robot-a','user','family@example.com','family.invite'),
               ('a2','robot-a','device','robot-a','heartbeat');
      INSERT INTO fall_cloud_keys(device_id,key_version,updated_by) VALUES ('robot-b',0,'robot');
      INSERT INTO request_rate_limits(rate_key,window_started_at,request_count)
        VALUES ('live:owner@example.com:robot-a',1,1),('fall-events:robot-a',1,1);`);
    await h.db.exec(readFileSync(path.join(h.root, "db/migrations/0016_user_identities.sql"), "utf8"));

    const identities = (await h.db.query(
      "SELECT subject,user_id FROM user_identities WHERE provider='email' ORDER BY subject")).rows;
    // "Owner@Example.com" and "owner@example.com" are the same person.
    assert.deepEqual(identities.map((row) => row.subject), ["family@example.com", "owner@example.com"]);
    assert.equal((await h.db.query("SELECT count(*)::int AS n FROM users")).rows[0].n, 2);
    const id = Object.fromEntries(identities.map((row) => [row.subject, row.user_id]));

    assert.deepEqual((await h.db.query(
      "SELECT user_id,role FROM device_memberships ORDER BY role DESC")).rows,
      [{ user_id: id["owner@example.com"], role: "owner" }, { user_id: id["family@example.com"], role: "family" }]);
    assert.equal((await h.db.query("SELECT user_id FROM push_subscriptions")).rows[0].user_id, id["owner@example.com"]);
    assert.equal((await h.db.query("SELECT requested_by FROM robot_commands")).rows[0].requested_by, id["owner@example.com"]);
    assert.deepEqual((await h.db.query("SELECT actor_id FROM access_audit_log ORDER BY id")).rows.map((row) => row.actor_id),
      [id["family@example.com"], "robot-a"]);
    assert.equal((await h.db.query("SELECT updated_by FROM fall_cloud_keys")).rows[0].updated_by, "robot");
    // Email-keyed rate windows are dropped; device-keyed ones stay.
    assert.deepEqual((await h.db.query("SELECT rate_key FROM request_rate_limits")).rows.map((row) => row.rate_key),
      ["fall-events:robot-a"]);
    const columns = (await h.db.query(
      `SELECT table_name FROM information_schema.columns
       WHERE column_name IN ('user_email','actor_email') AND table_name <> 'web_auth_sessions'`)).rows;
    assert.deepEqual(columns, []);
  } finally { await h.db.close(); }
});

test("a login email maps to one user, created once and matched case-insensitively", async () => {
  await withUsers(async ({ h, users }) => {
    assert.equal(await users.ensureUserForIdentity("email", "owner@example.com"), "u-owner");
    const created = await users.ensureUserForIdentity("email", "New@Example.com");
    assert.match(created, /^[0-9a-f-]{36}$/);
    assert.equal(await users.ensureUserForIdentity("email", "new@example.com"), created);
    assert.equal(await users.findUserIdForIdentity("email", "NEW@example.com"), created);
    assert.equal(await users.findUserIdForIdentity("email", "nobody@example.com"), null);
    assert.equal((await h.db.query("SELECT count(*)::int AS n FROM users")).rows[0].n, 3);
  });
});

test("people are shown by display name, else login email, never by user ID", async () => {
  await withUsers(async ({ h, users }) => {
    await h.db.query("UPDATE users SET display_name='민준' WHERE id='u-owner'");
    await h.db.query("INSERT INTO users(id) VALUES ('u-nameless')");
    const labels = await users.userLabels(["u-owner", "u-family", "u-nameless", null, "u-owner"]);
    assert.deepEqual([...labels.entries()].sort(), [
      ["u-family", "family@example.com"], ["u-nameless", "이름 없는 사용자"], ["u-owner", "민준"],
    ]);
    assert.equal(users.labelFor(labels, "u-missing"), "이름 없는 사용자");
    assert.equal(users.labelFor(labels, null), null);
  });
});

test("the guardian list shows how each person signs in, and owners remove guardians by user ID", async () => {
  await withUsers(async ({ h, homecam }) => {
    // Guardians now come in by invite link (guardians.test.mjs); here one is already in.
    await h.db.exec(`INSERT INTO users(id, display_name) VALUES ('u-guest', '박돌봄');
      INSERT INTO user_identities(provider, subject, user_id) VALUES ('naver', 'naver-1', 'u-guest');
      INSERT INTO device_memberships(device_id, user_id, role) VALUES ('robot-a', 'u-guest', 'family');`);
    const invited = { userId: "u-guest" };
    const members = await homecam.listFamilyMembers("robot-a");
    assert.deepEqual(members.map((m) => [m.userId, m.name, m.role, m.provider, m.viaInvite]), [
      ["u-owner", "owner@example.com", "owner", "email", false],
      ["u-family", "family@example.com", "family", "email", false],
      ["u-guest", "박돌봄", "family", "naver", false],
    ]);

    await h.db.query(`INSERT INTO push_subscriptions(id,device_id,user_id,endpoint,p256dh,auth)
      VALUES ('p-guest','robot-a',$1,'https://push.test/g','k','a')`, [invited.userId]);
    assert.equal(await homecam.revokeFamilyMember({
      deviceId: "robot-a", ownerUserId: "u-owner", familyUserId: invited.userId,
    }), true);
    assert.equal(await homecam.getMembershipRole("robot-a", invited.userId), null);
    assert.ok((await h.db.query("SELECT revoked_at FROM push_subscriptions WHERE id='p-guest'")).rows[0].revoked_at);
    // Owners are never removed this way.
    assert.equal(await homecam.revokeFamilyMember({
      deviceId: "robot-a", ownerUserId: "u-owner", familyUserId: "u-owner",
    }), false);
  });
});
