import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fallDatabase, moduleLoader } from "./helpers/fall-db-harness.mjs";

const PROVISIONING_SECRET = "P".repeat(43);
const ENV = {
  AUTH_SESSION_SECRET: "A".repeat(43),
  AUTH_PUBLIC_ORIGIN: "https://homecam.example.com",
  DEVICE_PROVISIONING_SECRET: PROVISIONING_SECRET,
};
const CODE = /^[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}$/;

async function withRegistration(work) {
  const h = await fallDatabase();
  const load = moduleLoader({
    [path.join(h.root, "app/runtime-env.ts")]: {
      getRuntimeEnvironment: () => ENV,
      getRuntimeValue: (name) => ENV[name],
    },
    [path.join(h.root, "app/server-auth.ts")]: {
      async getRequestUserId(request) { return request.headers.get("x-test-user"); },
    },
  });
  const pg = load("db/postgres.ts");
  await h.db.exec("INSERT INTO users(id) VALUES ('u-new'),('u-other'),('u-third')");
  const internal = load("app/api/internal/registration-codes/route.ts");
  const registrations = load("app/api/registrations/route.ts");
  const issue = async (body, secret = PROVISIONING_SECRET) => internal.POST(new Request(
    "https://homecam.example.com/api/internal/registration-codes", {
      method: "POST",
      headers: { "content-type": "application/json", ...(secret ? { authorization: `Bearer ${secret}` } : {}) },
      body: JSON.stringify(body),
    }));
  const register = async (userId, body, origin = "https://homecam.example.com") => registrations.POST(new Request(
    "https://homecam.example.com/api/registrations", {
      method: "POST",
      headers: { "content-type": "application/json", origin, ...(userId ? { "x-test-user": userId } : {}) },
      body: JSON.stringify(body),
    }));
  const codeFor = async (deviceId) => (await (await issue({ deviceId })).json()).code;
  const members = async (deviceId) => (await h.db.query(
    "SELECT user_id, role FROM device_memberships WHERE device_id=$1 ORDER BY user_id", [deviceId],
  )).rows;
  try {
    await pg.withPostgresPoolForTest(h.pool, () => work({
      h, load, issue, register, codeFor, members,
      registration: load("db/registration.ts"),
      homecam: load("db/homecam.ts"),
    }));
  } finally { await h.db.close(); }
}

async function seedIncident(h, deviceId, userId) {
  const incidentId = randomUUID();
  await h.db.query(
    `INSERT INTO fall_incidents(device_id,incident_id,boot_id,evidence_revision,state,occurred_at)
     VALUES($1,$2,'boot-1',1,'verifying',now())`, [deviceId, incidentId]);
  await h.db.query(
    "INSERT INTO fall_incident_opinions(device_id,incident_id,user_id,label) VALUES($1,$2,$3,'fall')",
    [deviceId, incidentId, userId]);
  return incidentId;
}

test("the team makes 7-day codes that are stored only as a digest, one unused code per 말벗", async () => {
  await withRegistration(async ({ h, issue, register }) => {
    assert.equal((await issue({ deviceId: "robot-b" }, null)).status, 401);
    assert.equal((await issue({ deviceId: "robot-b" }, "W".repeat(43))).status, 401);
    assert.equal((await issue({ deviceId: "robot-b", owner: "x" })).status, 400);
    assert.equal((await issue({ deviceId: "no-such-robot" })).status, 404);

    const before = Date.now();
    const first = await issue({ deviceId: "robot-b" });
    assert.equal(first.status, 201);
    const created = await first.json();
    assert.match(created.code, CODE);
    const lifetime = Date.parse(created.expiresAt) - before;
    assert.ok(lifetime > 7 * 86_400_000 - 60_000 && lifetime <= 7 * 86_400_000 + 60_000, String(lifetime));
    const rows = (await h.db.query("SELECT code_digest FROM device_registration_codes")).rows;
    assert.equal(rows.length, 1);
    assert.match(rows[0].code_digest, /^[a-f0-9]{64}$/);
    assert.ok(!rows[0].code_digest.includes(created.code.replace("-", "").toLowerCase()));

    // A new code replaces the one not used yet.
    const second = await (await issue({ deviceId: "robot-b" })).json();
    assert.notEqual(second.code, created.code);
    assert.equal((await h.db.query("SELECT count(*)::int AS n FROM device_registration_codes")).rows[0].n, 1);
    assert.equal((await register("u-new", { code: created.code })).status, 404);
    assert.equal((await register("u-new", { code: second.code })).status, 200);
  });
});

test("a person without a 말벗 becomes its owner with a code, and the code works once", async () => {
  await withRegistration(async ({ h, codeFor, register, members, homecam }) => {
    assert.equal(await homecam.userHasHomecam("u-new"), false);
    const code = await codeFor("robot-b");
    // Typed by hand: lower case, spaces, no dash.
    const typed = ` ${code.replace("-", " ").toLowerCase()} `;
    const done = await register("u-new", { code: typed });
    assert.equal(done.status, 200);
    assert.deepEqual(await done.json(), { status: "registered" });
    assert.deepEqual(await members("robot-b"), [{ user_id: "u-new", role: "owner" }]);
    assert.equal(await homecam.userHasHomecam("u-new"), true);
    const used = (await h.db.query("SELECT used_by, used_at FROM device_registration_codes")).rows[0];
    assert.equal(used.used_by, "u-new");
    assert.ok(used.used_at);

    // Pressing again is not an error for the new owner; for anyone else the code is spent.
    assert.equal((await register("u-new", { code })).status, 200);
    const spent = await register("u-other", { code });
    assert.equal(spent.status, 404);
    assert.equal((await spent.json()).status, "used");
    assert.deepEqual(await members("robot-b"), [{ user_id: "u-new", role: "owner" }]);
    const audit = (await h.db.query(
      "SELECT action, actor_id FROM access_audit_log WHERE device_id='robot-b' ORDER BY created_at")).rows;
    assert.deepEqual(audit.map((row) => row.action), ["registration_code.created", "device.registered"]);
  });
});

test("re-registering asks first, then removes everyone else and keeps the earlier incidents", async () => {
  await withRegistration(async ({ h, codeFor, register, members }) => {
    const incident = await seedIncident(h, "robot-a", "u-owner");
    await h.db.exec(`INSERT INTO push_subscriptions(id,device_id,user_id,endpoint,p256dh,auth) VALUES
      ('p-owner','robot-a','u-owner','https://push.test/1','k','a'),
      ('p-family','robot-a','u-family','https://push.test/2','k','a');
      INSERT INTO talk_leases(device_id,lease_id,user_id,client_id,expires_at)
      VALUES ('robot-a','lease-1','u-owner','client-1',now() + interval '1 minute');`);
    await h.db.exec(`INSERT INTO fall_cloud_keys(device_id,key_version,ciphertext,last4,updated_by)
      VALUES ('robot-a',3,'v1.sealed','abcd','u-owner')`);
    await h.db.exec(`INSERT INTO robot_semantic_drafts
      (device_id,kind,map_id,map_revision,payload_json,status,saved_by,saved_at)
      VALUES ('robot-a','rooms','map-1','rev-1','[]','pending','u-owner',now())`);
    const code = await codeFor("robot-a");

    const asked = await register("u-new", { code });
    assert.equal(asked.status, 409);
    assert.deepEqual(await asked.json(), { status: "needs_confirmation" });
    // Nothing changes before the choice.
    assert.equal((await members("robot-a")).length, 2);
    assert.equal((await h.db.query("SELECT used_at FROM device_registration_codes")).rows[0].used_at, null);
    assert.equal((await register("u-new", { code, history: "maybe" })).status, 400);

    assert.equal((await register("u-new", { code, history: "keep" })).status, 200);
    assert.deepEqual(await members("robot-a"), [{ user_id: "u-new", role: "owner" }]);
    const pushes = (await h.db.query("SELECT id, revoked_at FROM push_subscriptions ORDER BY id")).rows;
    assert.ok(pushes.every((row) => row.revoked_at), "earlier members stop getting fall alerts");
    assert.equal((await h.db.query("SELECT count(*)::int AS n FROM talk_leases")).rows[0].n, 0);
    assert.equal((await h.db.query("SELECT count(*)::int AS n FROM robot_semantic_drafts")).rows[0].n, 0,
      "the previous owner's unsent room edits are not sent");
    const kept = (await h.db.query(
      "SELECT count(*)::int AS n FROM fall_incident_opinions WHERE incident_id=$1", [incident])).rows[0].n;
    assert.equal(kept, 1, "남기기 keeps incidents and opinions");
    const audit = (await h.db.query(
      "SELECT metadata_json FROM access_audit_log WHERE action='device.registered'")).rows[0];
    assert.deepEqual(JSON.parse(audit.metadata_json),
      { removedMembers: 2, history: "keep", deletedIncidents: 0, deletedCloudKey: false, deletedServiceKeys: [] });
    assert.deepEqual((await h.db.query("SELECT key_version, last4 FROM fall_cloud_keys")).rows,
      [{ key_version: 3, last4: "abcd" }], "남기기 keeps the Cloud AI key");
  });
});

test("지우기 removes only that 말벗's incidents; a guardian re-registering keeps their own alerts", async () => {
  await withRegistration(async ({ h, codeFor, register, members }) => {
    const mine = await seedIncident(h, "robot-a", "u-owner");
    await h.db.exec("INSERT INTO device_memberships(device_id,user_id,role) VALUES ('robot-b','u-other','owner')");
    const other = await seedIncident(h, "robot-b", "u-other");
    await h.db.exec(`INSERT INTO push_subscriptions(id,device_id,user_id,endpoint,p256dh,auth) VALUES
      ('p-owner','robot-a','u-owner','https://push.test/1','k','a'),
      ('p-family','robot-a','u-family','https://push.test/2','k','a');`);

    await h.db.exec(`INSERT INTO fall_cloud_keys(device_id,key_version,ciphertext,last4,updated_by) VALUES
      ('robot-a',3,'v1.sealed','abcd','u-owner'),('robot-b',1,'v1.other','wxyz','u-other')`);
    const code = await codeFor("robot-a");
    assert.equal((await register("u-family", { code })).status, 409);
    assert.equal((await register("u-family", { code, history: "delete" })).status, 200);
    assert.deepEqual(await members("robot-a"), [{ user_id: "u-family", role: "owner" }]);
    const incidents = (await h.db.query("SELECT incident_id FROM fall_incidents ORDER BY device_id")).rows;
    assert.deepEqual(incidents.map((row) => row.incident_id), [other]);
    assert.equal((await h.db.query(
      "SELECT count(*)::int AS n FROM fall_incident_opinions WHERE incident_id=$1", [mine])).rows[0].n, 0);
    const pushes = Object.fromEntries((await h.db.query("SELECT id, revoked_at FROM push_subscriptions")).rows
      .map((row) => [row.id, row.revoked_at]));
    assert.ok(pushes["p-owner"]);
    assert.equal(pushes["p-family"], null);
    assert.deepEqual(await members("robot-b"), [{ user_id: "u-other", role: "owner" }]);
    // The previous owner's Cloud AI key is deleted as a new version, so the robot removes its copy.
    const keys = Object.fromEntries((await h.db.query(
      "SELECT device_id, key_version, ciphertext, last4, updated_by FROM fall_cloud_keys")).rows
      .map((row) => [row.device_id, row]));
    assert.deepEqual(keys["robot-a"], { device_id: "robot-a", key_version: 4, ciphertext: null, last4: null, updated_by: "u-family" });
    assert.equal(keys["robot-b"].ciphertext, "v1.other");
  });
});

test("expired codes, the current owner, other sites and guessing are refused", async () => {
  await withRegistration(async ({ h, codeFor, register, members, registration }) => {
    const old = await registration.createRegistrationCode({
      deviceId: "robot-b", sessionSecret: ENV.AUTH_SESSION_SECRET, now: new Date(Date.now() - 8 * 86_400_000),
    });
    const expired = await register("u-new", { code: old.code });
    assert.equal(expired.status, 404);
    assert.equal((await expired.json()).status, "expired");
    assert.deepEqual(await members("robot-b"), []);

    const code = await codeFor("robot-a");
    const owner = await register("u-owner", { code });
    assert.equal(owner.status, 409);
    assert.equal((await owner.json()).status, "already_owner");
    assert.equal((await h.db.query("SELECT used_at FROM device_registration_codes WHERE device_id='robot-a'")).rows[0].used_at, null);

    assert.equal((await register(null, { code })).status, 401);
    assert.equal((await register("u-new", { code }, "https://evil.example")).status, 403);
    assert.equal((await register("u-new", { code, extra: 1 })).status, 400);
    assert.equal((await register("u-new", { code: "not a code" })).status, 404);

    // Ten tries a minute, then wait: a code cannot be found by guessing.
    for (let attempt = 0; attempt < 8; attempt += 1) {
      assert.equal((await register("u-third", { code: "AAAA-AAAA" })).status, 404);
    }
    assert.equal((await register("u-third", { code: "AAAA-AAAB" })).status, 404);
    assert.equal((await register("u-third", { code: "AAAA-AAAC" })).status, 404);
    assert.equal((await register("u-third", { code })).status, 429);
    assert.equal((await members("robot-a")).length, 2);
  });
});

test("the code box adds the dash itself: only the 8 letters and digits are typed", async () => {
  const { formatRegistrationCodeInput: format } = moduleLoader()("app/register/code-input.ts");
  assert.equal(format("7q2k"), "7Q2K");
  assert.equal(format("7q2k9"), "7Q2K-9");
  assert.equal(format("7q2k9xhm"), "7Q2K-9XHM");
  assert.equal(format("7Q2K-9XHM"), "7Q2K-9XHM");
  assert.equal(format(" 7q2k 9xhm "), "7Q2K-9XHM");
  assert.equal(format("7Q2K-9XHMZZ"), "7Q2K-9XHM", "no more than 8");
  assert.equal(format("7Q2K-"), "7Q2K", "deleting the 5th character removes the dash");
  assert.equal(format("한글7Q"), "7Q");
});

test("the app starts at 말벗 등록 until the person has a 말벗, after choosing a name", async () => {
  const [home, page, screen, dashboard] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/register/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/register/register-screen.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/components/homecam-dashboard.tsx", import.meta.url), "utf8"),
  ]);
  assert.match(home, /if \(!\(await userHasHomecam\(user\.userId\)\)\) redirect\("\/register"\)/);
  assert.ok(home.indexOf("chosenName") < home.indexOf("userHasHomecam(user.userId)"), "name comes first");
  assert.match(page, /if \(!user\.chosenName\) redirect\(`\/auth\/name\?/);
  for (const label of ["아직 연결된 말벗이 없어요", "등록 코드는 말벗 팀에게 받을 수 있어요.", "다시 등록할까요?",
    "지난 사건 기록과 의견은 어떻게 할까요?", "지우기", "남기기", "다시 등록하기", "우리 집 말벗의 소유자가 됐어요",
    "보호자로 초대받으셨나요? 소유자에게 받은 초대 링크를 다시 열어 주세요."]) assert.ok(screen.includes(label), label);
  assert.match(screen, /setCode\(formatRegistrationCodeInput\(event\.target\.value\)\)/);
  assert.match(dashboard, /소유자 넘기기 · 다시 등록/);
  assert.match(dashboard, /href="\/register"/);
});
