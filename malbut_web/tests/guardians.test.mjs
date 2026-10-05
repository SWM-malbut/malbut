import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fallDatabase, moduleLoader } from "./helpers/fall-db-harness.mjs";

const SECRET = "A".repeat(43);
const ORIGIN = "https://homecam.example.com";
const ENV = { AUTH_SESSION_SECRET: SECRET, AUTH_PUBLIC_ORIGIN: ORIGIN };
const DAY = 86_400_000;

async function withGuardians(work) {
  const h = await fallDatabase();
  const load = moduleLoader({
    [path.join(h.root, "app/runtime-env.ts")]: { getRuntimeEnvironment: () => ENV, getRuntimeValue: (name) => ENV[name] },
    [path.join(h.root, "app/server-auth.ts")]: { async getRequestUserId(request) { return request.headers.get("x-test-user"); } },
  });
  const pg = load("db/postgres.ts");
  await h.db.exec(`INSERT INTO users(id, display_name) VALUES ('u-new', '이보호'), ('u-other', '박돌봄');
    INSERT INTO user_identities(provider, subject, user_id) VALUES ('google', 'g-1', 'u-new'), ('naver', 'n-1', 'u-other');`);
  const invite = load("app/api/devices/[deviceId]/invite/route.ts");
  const accept = load("app/api/invites/accept/route.ts");
  const owner = load("app/api/devices/[deviceId]/owner/route.ts");
  const request = (url, method, userId, body, origin = ORIGIN) => new Request(`${ORIGIN}${url}`, {
    method,
    headers: { ...(userId ? { "x-test-user": userId } : {}), ...(method === "GET" ? {} : { "content-type": "application/json", origin }) },
    ...(method === "GET" ? {} : { body: JSON.stringify(body ?? {}) }),
  });
  const params = { params: Promise.resolve({ deviceId: "robot-a" }) };
  const link = (method, userId, origin) => invite[method](request("/api/devices/robot-a/invite", method, userId, {}, origin), params);
  const join = (userId, token) => accept.POST(request("/api/invites/accept", "POST", userId, { token }));
  const handOver = (userId, newOwner) => owner.POST(request("/api/devices/robot-a/owner", "POST", userId, { userId: newOwner }), params);
  const roles = async () => Object.fromEntries((await h.db.query(
    "SELECT user_id, role FROM device_memberships WHERE device_id='robot-a'")).rows.map((row) => [row.user_id, row.role]));
  try {
    await pg.withPostgresPoolForTest(h.pool, () => work({
      h, link, join, handOver, roles,
      guardians: load("db/guardians.ts"), homecam: load("db/homecam.ts"), registration: load("db/registration.ts"),
    }));
  } finally { await h.db.close(); }
}

const tokenOf = (invite) => invite.path.replace("/invite/", "");

test("owners make one live link at a time, for 24 hours, and can copy it again", async () => {
  await withGuardians(async ({ h, link, join }) => {
    assert.equal((await link("GET", null)).status, 401);
    assert.equal((await link("POST", "u-family")).status, 403, "guardians cannot make links");
    assert.equal((await link("POST", "u-owner", "https://evil.example")).status, 403);

    const before = Date.now();
    const made = await link("POST", "u-owner");
    assert.equal(made.status, 201);
    const first = (await made.json()).invite;
    assert.match(first.path, /^\/invite\/[A-Za-z0-9_-]{43}$/);
    assert.equal(first.joined, 0);
    const lifetime = Date.parse(first.expiresAt) - before;
    assert.ok(lifetime > DAY - 60_000 && lifetime <= DAY + 60_000, String(lifetime));
    // Shown again to the owner, but the link itself is not stored in the clear.
    assert.deepEqual((await (await link("GET", "u-owner")).json()).invite, first);
    assert.equal((await link("GET", "u-family")).status, 403);
    const row = (await h.db.query("SELECT token_digest, token_ciphertext FROM device_invites")).rows[0];
    assert.match(row.token_digest, /^[a-f0-9]{64}$/);
    assert.ok(!JSON.stringify(row).includes(tokenOf(first)));

    // A new link cancels the old one.
    const second = (await (await link("POST", "u-owner")).json()).invite;
    assert.notEqual(second.path, first.path);
    assert.equal((await join("u-new", tokenOf(first))).status, 404);
    assert.deepEqual((await h.db.query(
      "SELECT count(*)::int AS n FROM device_invites WHERE revoked_at IS NULL")).rows[0], { n: 1 });

    assert.deepEqual(await (await link("DELETE", "u-owner")).json(), { revoked: true });
    assert.equal((await (await link("GET", "u-owner")).json()).invite, null);
    assert.equal((await join("u-new", tokenOf(second))).status, 404);
  });
});

test("anyone signed in through a live link becomes a guardian, once", async () => {
  await withGuardians(async ({ h, link, join, guardians, homecam, roles }) => {
    const invite = (await (await link("POST", "u-owner")).json()).invite;
    const token = tokenOf(invite);
    assert.deepEqual(await guardians.describeInvite(token, SECRET), { deviceName: "말벗 A", ownerName: "owner@example.com" });

    const joined = await join("u-new", token);
    assert.deepEqual(await joined.json(), { status: "joined", deviceId: "robot-a" });
    assert.equal((await roles())["u-new"], "family");
    // One link, many people (a family chat room).
    assert.equal((await (await join("u-other", token)).json()).status, "joined");
    assert.equal((await (await link("GET", "u-owner")).json()).invite.joined, 2);

    assert.equal((await (await join("u-new", token)).json()).status, "already_family");
    assert.equal((await (await join("u-owner", token)).json()).status, "owner");
    assert.equal((await join(null, token)).status, 401);

    const people = await homecam.listFamilyMembers("robot-a");
    assert.deepEqual(people.filter((p) => p.viaInvite).map((p) => [p.name, p.provider]), [["이보호", "google"], ["박돌봄", "naver"]]);
    const audit = (await h.db.query("SELECT action FROM access_audit_log WHERE action='family.joined'")).rows;
    assert.equal(audit.length, 2);
  });
});

test("expired, cancelled and made-up links never let anyone in", async () => {
  await withGuardians(async ({ join, guardians, roles }) => {
    const old = await guardians.createInviteLink({
      deviceId: "robot-a", ownerUserId: "u-owner", sessionSecret: SECRET, now: new Date(Date.now() - DAY - 60_000),
    });
    assert.equal(await guardians.describeInvite(old.token, SECRET), null);
    assert.equal((await (await join("u-new", old.token)).json()).status, "unusable");
    for (const made of ["x".repeat(43), "short", "../../etc"]) assert.equal((await join("u-new", made)).status, 404);
    assert.equal((await roles())["u-new"], undefined);
  });
});

test("re-registering cancels the previous household's link", async () => {
  await withGuardians(async ({ h, link, join, registration }) => {
    const token = tokenOf((await (await link("POST", "u-owner")).json()).invite);
    const { code } = await registration.createRegistrationCode({ deviceId: "robot-a", sessionSecret: SECRET });
    const result = await registration.redeemRegistrationCode({
      code: code.replace("-", ""), userId: "u-other", history: "keep", sessionSecret: SECRET,
    });
    assert.equal(result.status, "registered");
    assert.equal((await join("u-new", token)).status, 404);
    assert.equal((await h.db.query("SELECT count(*)::int AS n FROM device_invites WHERE revoked_at IS NULL")).rows[0].n, 0);
  });
});

test("owners hand the 말벗 to a guardian and stay on as a guardian", async () => {
  await withGuardians(async ({ h, handOver, roles }) => {
    assert.equal((await handOver("u-family", "u-family")).status, 403, "a guardian cannot take it");
    assert.equal((await handOver("u-owner", "u-new")).status, 409, "only to someone already a guardian");
    assert.equal((await handOver("u-owner", "u-owner")).status, 409);
    assert.deepEqual(await roles(), { "u-owner": "owner", "u-family": "family" });

    assert.deepEqual(await (await handOver("u-owner", "u-family")).json(), { transferred: true });
    assert.deepEqual(await roles(), { "u-owner": "family", "u-family": "owner" });
    assert.equal((await handOver("u-owner", "u-family")).status, 403, "the old owner no longer can");
    const audit = (await h.db.query("SELECT actor_id, metadata_json FROM access_audit_log WHERE action='owner.transferred'")).rows;
    assert.deepEqual(audit.map((row) => [row.actor_id, JSON.parse(row.metadata_json).userId]), [["u-owner", "u-family"]]);
  });
});

test("guardians join by link only: no email invites, the invite page asks for a name first", async () => {
  const [page, screen, family, dashboard, guardiansUi] = await Promise.all([
    readFile(new URL("../app/invite/[token]/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/invite/[token]/invite-screen.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/api/devices/[deviceId]/family/route.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/components/homecam-dashboard.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/components/guardians-settings.tsx", import.meta.url), "utf8"),
  ]);
  assert.match(page, /if \(user && invite && !user\.chosenName\) redirect\(`\/auth\/name\?/);
  assert.match(screen, /fetch\("\/api\/invites\/accept", \{\s*method: "POST"/);
  assert.doesNotMatch(family, /export async function POST/);
  assert.doesNotMatch(dashboard, /초대할 보호자 이메일/);
  for (const label of ["보호자 초대 링크", "초대 링크 만들기", "링크 복사", "공유하기", "링크 취소", "소유자 넘기기"]) {
    assert.ok(guardiansUi.includes(label), label);
  }
  for (const label of ["보호자로 등록됐어요", "이미 이 말벗의 보호자예요", "초대 링크를 쓸 수 없어요", "이 휴대폰에서 낙상 알림 받기"]) {
    assert.ok(screen.includes(label), label);
  }
});
