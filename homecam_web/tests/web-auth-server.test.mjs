import assert from "node:assert/strict";
import * as nodeCrypto from "node:crypto";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { runInNewContext } from "node:vm";
import ts from "typescript";

const SESSION_SECRET = Buffer.alloc(32, 7).toString("base64url");

async function webAuthHarness() {
  const source = await readFile(new URL("../db/web-auth.ts", import.meta.url), "utf8");
  const javascript = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const sessions = new Map();
  const challenges = new Map();
  const rates = new Map();
  const database = {
    prepare(sql) {
      let bindings = [];
      return {
        bind(...values) { bindings = values; return this; },
        async run() {
          if (sql.includes("INSERT INTO web_auth_sessions")) {
            sessions.set(bindings[0], {
              cognito_sub: bindings[1], cognito_username: bindings[2],
              user_email: bindings[3], full_name: bindings[4],
              expires_at: bindings[7], revoked_at: null,
            });
            return changed(1);
          }
          if (sql.includes("SET revoked_at")) {
            const row = sessions.get(bindings[1]);
            if (!row || row.revoked_at) return changed(0);
            row.revoked_at = bindings[0];
            return changed(1);
          }
          if (sql.includes("INSERT INTO web_auth_challenges")) {
            challenges.set(bindings[0], {
              token_digest: bindings[0], cognito_username: bindings[1],
              challenge_name: bindings[2], cognito_session_ciphertext: bindings[3],
              failure_count: 0, expires_at: bindings[5], claimed_at: null,
              consumed_at: null,
            });
            return changed(1);
          }
          if (sql.includes("SET consumed_at = ?, claimed_at = NULL")) {
            const row = challenges.get(bindings[1]);
            if (row && !row.consumed_at) row.consumed_at = bindings[0];
            return changed(row ? 1 : 0);
          }
          if (sql.includes("failure_count = failure_count + 1")) {
            const row = challenges.get(bindings[2]);
            if (row && !row.consumed_at) {
              row.failure_count += 1;
              row.claimed_at = null;
              if (row.failure_count >= bindings[0]) row.consumed_at = bindings[1];
            }
            return changed(row ? 1 : 0);
          }
          throw new Error(`Unexpected run SQL: ${sql}`);
        },
        async first() {
          if (sql.includes("UPDATE web_auth_sessions")) {
            const row = sessions.get(bindings[1]);
            return row && !row.revoked_at && Date.parse(row.expires_at) > Date.parse(bindings[2])
              ? row : null;
          }
          if (sql.includes("UPDATE web_auth_challenges")) {
            const row = challenges.get(bindings[1]);
            if (
              !row || row.consumed_at || row.claimed_at ||
              row.failure_count >= bindings[2] ||
              Date.parse(row.expires_at) <= Date.parse(bindings[3])
            ) return null;
            row.claimed_at = bindings[0];
            return row;
          }
          if (sql.includes("INSERT INTO request_rate_limits")) {
            const [key, window] = bindings;
            const current = rates.get(key);
            const count = !current || current.window < window ? 1 : current.count + 1;
            rates.set(key, { window, count });
            return { request_count: count };
          }
          throw new Error(`Unexpected first SQL: ${sql}`);
        },
      };
    },
  };
  const commonJsModule = { exports: {} };
  runInNewContext(javascript, {
    module: commonJsModule, exports: commonJsModule.exports, Buffer, Date,
    require(specifier) {
      if (specifier === "node:crypto") return nodeCrypto;
      if (specifier === "./index") return { getD1: () => database };
      throw new Error(`Unexpected import: ${specifier}`);
    },
  });
  return { auth: commonJsModule.exports, sessions, challenges };
}

function changed(changes) {
  return { success: true, results: [], meta: { changes } };
}

test("opaque web sessions store only a keyed digest and revoke immediately", async () => {
  const { auth, sessions } = await webAuthHarness();
  const created = await auth.createWebSession({
    cognitoSub: "subject-1", cognitoUsername: "owner-1",
    userEmail: "owner@example.com", fullName: "Owner",
    sessionSecret: SESSION_SECRET,
  });
  assert.match(created.token, /^[A-Za-z0-9_-]{43}$/);
  assert.equal(JSON.stringify([...sessions]), JSON.stringify([...sessions]).replaceAll(created.token, ""));
  assert.deepEqual(
    JSON.parse(JSON.stringify(await auth.getWebSessionUser(created.token, SESSION_SECRET))),
    { email: "owner@example.com", fullName: "Owner", subject: "subject-1" },
  );
  assert.equal(await auth.revokeWebSession(created.token, SESSION_SECRET), true);
  assert.equal(await auth.getWebSessionUser(created.token, SESSION_SECRET), null);
  assert.deepEqual(JSON.parse(JSON.stringify(auth.webAuthCookieOptions(43_200))), {
    httpOnly: true, secure: true, sameSite: "lax", path: "/", maxAge: 43_200,
  });
});

test("Cognito challenge sessions are encrypted, claimed once, and cannot replay", async () => {
  const { auth, challenges } = await webAuthHarness();
  const created = await auth.createWebAuthChallenge({
    username: "owner-1", challengeName: "NEW_PASSWORD_REQUIRED",
    cognitoSession: "private-cognito-session", sessionSecret: SESSION_SECRET,
  });
  assert.doesNotMatch(JSON.stringify([...challenges]), /private-cognito-session/);
  const claimed = await auth.claimWebAuthChallenge(created.token, SESSION_SECRET);
  assert.equal(claimed.cognitoSession, "private-cognito-session");
  assert.equal(await auth.claimWebAuthChallenge(created.token, SESSION_SECRET), null);
  await auth.finishWebAuthChallenge({ tokenDigest: claimed.tokenDigest, succeeded: false });
  assert.equal((await auth.claimWebAuthChallenge(created.token, SESSION_SECRET)).failureCount, 1);
});

test("web authentication rate limits use HMAC identifiers and fixed windows", async () => {
  const { auth } = await webAuthHarness();
  const input = {
    scope: "login-account", identifier: "owner@example.com", limit: 2,
    windowMs: 900_000, sessionSecret: SESSION_SECRET, now: new Date(0),
  };
  assert.equal(await auth.consumeWebAuthRateLimit(input), true);
  assert.equal(await auth.consumeWebAuthRateLimit(input), true);
  assert.equal(await auth.consumeWebAuthRateLimit(input), false);
  assert.equal(
    await auth.consumeWebAuthRateLimit({ ...input, now: new Date(900_000) }),
    true,
  );
});

test("login and logout routes enforce same-origin JSON and never expose Cognito state", async () => {
  const [login, logout, migration, serverAuth] = await Promise.all([
    readFile(new URL("../app/api/auth/login/route.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/api/auth/logout/route.ts", import.meta.url), "utf8"),
    readFile(new URL("../db/migration-state.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/server-auth.ts", import.meta.url), "utf8"),
  ]);
  assert.match(login, /sec-fetch-site/);
  assert.match(login, /application\/json/);
  assert.match(login, /GENERIC_ERROR/);
  assert.match(login, /NEW_PASSWORD_REQUIRED/);
  assert.match(login, /SOFTWARE_TOKEN_MFA/);
  assert.doesNotMatch(login, /NextResponse\.json\([^)]*cognitoSession/s);
  assert.match(logout, /export async function POST/);
  assert.match(logout, /export async function GET/);
  assert.match(logout, /usesHostedUiLogout/);
  assert.match(logout, /AWSELBAuthSessionCookie/);
  assert.match(migration, /0004_robot_map_semantics/);
  assert.match(serverAuth, /cognito_session/);
  assert.match(serverAuth, /alb_oidc_or_cognito_session/);
  assert.match(serverAuth, /x-amzn-oidc-data/);
  assert.match(serverAuth, /crypto\.subtle\.verify/);
});

test("Cognito server auth uses admin challenge APIs and authoritative AdminGetUser attributes", async () => {
  const source = await readFile(new URL("../app/cognito-auth.ts", import.meta.url), "utf8");
  assert.match(source, /AdminInitiateAuthCommand/);
  assert.match(source, /ADMIN_USER_PASSWORD_AUTH/);
  assert.match(source, /AdminRespondToAuthChallengeCommand/);
  assert.match(source, /AdminGetUserCommand/);
  assert.match(source, /attributes\.email_verified !== "true"/);
  assert.doesNotMatch(source, /JSON\.parse\(Buffer\.from\(components/);
});
