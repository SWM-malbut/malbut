import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";
import test from "node:test";
import { fallDatabase, moduleLoader } from "./helpers/fall-db-harness.mjs";

const require = createRequire(import.meta.url);
const jose = require("jose");
const ISSUER = "https://idp.test";
const SECRET = "A".repeat(43);
const ENV = {
  AUTH_MODE: "cognito_session",
  AUTH_SESSION_SECRET: SECRET,
  AUTH_PUBLIC_ORIGIN: "https://homecam.example.com",
  AUTH_KAKAO_CLIENT_ID: "kakao-client",
  AUTH_KAKAO_CLIENT_SECRET: "kakao-secret",
  AUTH_KAKAO_ISSUER: ISSUER,
};

/** A tiny OpenID provider answered in-process: discovery, then a token with an ID token. */
async function fakeProvider() {
  const { privateKey } = await jose.generateKeyPair("RS256");
  const provider = { subject: "kakao-123", nonce: null, tokenRequests: [], audience: "kakao-client" };
  provider.fetch = async (url, options) => {
    const target = new URL(url);
    if (target.pathname === "/.well-known/openid-configuration") {
      return Response.json({
        issuer: ISSUER, authorization_endpoint: `${ISSUER}/authorize`, token_endpoint: `${ISSUER}/token`,
        jwks_uri: `${ISSUER}/jwks`, response_types_supported: ["code"], subject_types_supported: ["public"],
        id_token_signing_alg_values_supported: ["RS256"], code_challenge_methods_supported: ["S256"],
        token_endpoint_auth_methods_supported: ["client_secret_post"],
      });
    }
    if (target.pathname === "/token") {
      provider.tokenRequests.push(Object.fromEntries(new URLSearchParams(String(options.body))));
      const idToken = await new jose.SignJWT({ nonce: provider.nonce })
        .setProtectedHeader({ alg: "RS256" }).setIssuer(ISSUER).setAudience(provider.audience)
        .setSubject(provider.subject).setIssuedAt().setExpirationTime("5m").sign(privateKey);
      return Response.json({ access_token: "at", token_type: "Bearer", expires_in: 60, id_token: idToken });
    }
    return new Response("not found", { status: 404 });
  };
  return provider;
}

async function withLogin(work, env = ENV) {
  const h = await fallDatabase();
  const load = moduleLoader({
    [path.join(h.root, "app/runtime-env.ts")]: {
      getRuntimeEnvironment: () => env,
      getRuntimeValue: (name) => env[name],
    },
  });
  const pg = load("db/postgres.ts");
  const provider = await fakeProvider();
  const social = load("app/social-auth.ts");
  social.setSocialProviderFetchForTest(provider.fetch);
  try {
    await pg.withPostgresPoolForTest(h.pool, () => work({
      h, provider, social,
      start: load("app/auth/oidc/[provider]/route.ts"),
      callback: load("app/auth/callback/[provider]/route.ts"),
      account: load("app/api/account/route.ts"),
      auth: load("app/server-auth.ts"),
      webAuth: load("db/web-auth.ts"),
    }));
  } finally {
    social.setSocialProviderFetchForTest(undefined);
    await h.db.close();
  }
}

const params = (provider) => ({ params: Promise.resolve({ provider }) });
const cookieOf = (response, name) => {
  const header = response.headers.getSetCookie().find((value) => value.startsWith(`${name}=`));
  return header ? header.slice(name.length + 1).split(";")[0] : null;
};

/** Press "카카오로 계속하기", then come back from the provider with a code. */
async function signIn(ctx, { returnTo = "/?view=events", stateOverride, error } = {}) {
  const started = await ctx.start.GET(
    new Request(`http://internal:3000/auth/oidc/kakao?return_to=${encodeURIComponent(returnTo)}`), params("kakao"));
  assert.equal(started.status, 303);
  const authorize = new URL(started.headers.get("location"));
  ctx.provider.nonce = authorize.searchParams.get("nonce");
  const loginCookie = cookieOf(started, "__Host-malbut_oidc");
  const query = error ? "error=access_denied" : `code=abc&state=${stateOverride ?? authorize.searchParams.get("state")}`;
  const back = await ctx.callback.GET(new Request(`http://internal:3000/auth/callback/kakao?${query}`, {
    headers: loginCookie ? { cookie: `__Host-malbut_oidc=${loginCookie}` } : {},
  }), params("kakao"));
  return { started, authorize, loginCookie, back };
}

test("starting a social sign-in stores state, nonce and PKCE, and sends the browser to the provider", async () => {
  await withLogin(async (ctx) => {
    const { authorize, loginCookie } = await signIn(ctx);
    assert.equal(authorize.origin + authorize.pathname, `${ISSUER}/authorize`);
    assert.equal(authorize.searchParams.get("client_id"), "kakao-client");
    assert.equal(authorize.searchParams.get("scope"), "openid");
    assert.equal(authorize.searchParams.get("code_challenge_method"), "S256");
    // The provider returns to the public address, not the container's internal one.
    assert.equal(authorize.searchParams.get("redirect_uri"), "https://homecam.example.com/auth/callback/kakao");
    assert.match(loginCookie, /^[A-Za-z0-9_-]{43}$/);
    const row = (await ctx.h.db.query("SELECT state, nonce, code_verifier_ciphertext, return_to FROM oidc_login_transactions")).rows[0];
    assert.equal(row.state, authorize.searchParams.get("state"));
    assert.equal(row.nonce, authorize.searchParams.get("nonce"));
    assert.equal(row.return_to, "/?view=events");
    // The PKCE verifier is never stored in the clear.
    assert.match(row.code_verifier_ciphertext, /^v1\./);
  });
});

test("the callback signs the person in as the user behind (provider, sub) and returns once", async () => {
  await withLogin(async (ctx) => {
    const { back, loginCookie } = await signIn(ctx);
    assert.equal(back.status, 303);
    assert.equal(back.headers.get("location"), "https://homecam.example.com/?view=events");
    const session = cookieOf(back, "__Host-malbut_session");
    assert.match(session, /^[A-Za-z0-9_-]{43}$/);
    assert.equal(cookieOf(back, "__Host-malbut_oidc"), "");
    const token = ctx.provider.tokenRequests[0];
    assert.equal(token.client_secret, "kakao-secret");
    assert.ok(token.code_verifier);
    assert.equal(token.redirect_uri, "https://homecam.example.com/auth/callback/kakao");

    const identity = (await ctx.h.db.query(
      "SELECT user_id FROM user_identities WHERE provider='kakao' AND subject='kakao-123'")).rows[0];
    assert.ok(identity);
    const userId = await ctx.auth.getRequestUserId(new Request("https://homecam.example.com/api", {
      headers: { cookie: `__Host-malbut_session=${session}` },
    }));
    assert.equal(userId, identity.user_id);

    // The same callback cannot be replayed.
    const replay = await ctx.callback.GET(new Request("http://internal:3000/auth/callback/kakao?code=abc&state=x", {
      headers: { cookie: `__Host-malbut_oidc=${loginCookie}` },
    }), params("kakao"));
    assert.match(replay.headers.get("location"), /error=expired/);

    // Signing in again with the same account lands on the same user; another subject is another person.
    await signIn(ctx);
    ctx.provider.subject = "kakao-456";
    await signIn(ctx);
    const users = (await ctx.h.db.query(
      "SELECT subject FROM user_identities WHERE provider='kakao' ORDER BY subject")).rows.map((row) => row.subject);
    assert.deepEqual(users, ["kakao-123", "kakao-456"]);
  });
});

test("cancelled, forged or unverifiable callbacks never create a session", async () => {
  await withLogin(async (ctx) => {
    const cancelled = await signIn(ctx, { error: true });
    assert.match(cancelled.back.headers.get("location"), /error=cancelled/);
    assert.equal(cookieOf(cancelled.back, "__Host-malbut_session"), null);

    const forged = await signIn(ctx, { stateOverride: "attacker-state" });
    assert.match(forged.back.headers.get("location"), /error=failed/);
    assert.equal(cookieOf(forged.back, "__Host-malbut_session"), null);

    ctx.provider.audience = "someone-else";
    const wrongAudience = await signIn(ctx);
    assert.match(wrongAudience.back.headers.get("location"), /error=failed/);
    assert.equal(cookieOf(wrongAudience.back, "__Host-malbut_session"), null);

    const noCookie = await ctx.callback.GET(new Request("http://internal:3000/auth/callback/kakao?code=abc&state=s"), params("kakao"));
    assert.match(noCookie.headers.get("location"), /error=expired/);
    assert.equal((await ctx.h.db.query("SELECT count(*)::int AS n FROM user_identities WHERE provider='kakao'")).rows[0].n, 0);
  });
});

test("providers without keys stay unavailable and return paths stay inside the site", async () => {
  await withLogin(async (ctx) => {
    assert.deepEqual(ctx.social.enabledSocialProviders(), ["kakao"]);
    const naver = await ctx.start.GET(new Request("http://internal:3000/auth/oidc/naver?return_to=/"), params("naver"));
    assert.match(naver.headers.get("location"), /^https:\/\/homecam\.example\.com\/auth\/login\?.*error=unavailable/);
    const unknown = await ctx.start.GET(new Request("http://internal:3000/auth/oidc/apple"), params("apple"));
    assert.match(unknown.headers.get("location"), /error=unavailable/);
    const { back } = await signIn(ctx, { returnTo: "https://evil.example/steal" });
    assert.equal(back.headers.get("location"), "https://homecam.example.com/");
  });
});

test("people choose their own name: 1–20 characters, same-origin, then shown to others", async () => {
  await withLogin(async (ctx) => {
    const { back } = await signIn(ctx);
    const session = cookieOf(back, "__Host-malbut_session");
    const call = (method, body, headers = {}) => ctx.account[method](new Request("https://homecam.example.com/api/account", {
      method, headers: { cookie: `__Host-malbut_session=${session}`, "content-type": "application/json",
        origin: "https://homecam.example.com", ...headers }, ...(body ? { body: JSON.stringify(body) } : {}),
    }));
    const before = await (await call("GET")).json();
    assert.equal(before.displayName, null);
    assert.deepEqual(before.providers, ["kakao"]);
    assert.equal((await call("PATCH", { displayName: "   " })).status, 400);
    assert.equal((await call("PATCH", { displayName: "가".repeat(21) })).status, 400);
    assert.equal((await call("PATCH", { displayName: "민준", extra: 1 })).status, 400);
    assert.equal((await call("PATCH", { displayName: "민준" }, { origin: "https://evil.example" })).status, 403);
    const saved = await call("PATCH", { displayName: "  김  민준 " });
    assert.deepEqual(await saved.json(), { displayName: "김 민준" });
    assert.equal((await (await call("GET")).json()).displayName, "김 민준");
    assert.equal((await ctx.account.GET(new Request("https://homecam.example.com/api/account"))).status, 401);
  });
});

test("the home page asks for a name first, and email login stays until registration codes", async () => {
  const [home, login, social] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/auth/login/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/auth/login/social-login-panel.tsx", import.meta.url), "utf8"),
  ]);
  assert.match(home, /if \(!user\.chosenName\) redirect\(`\/auth\/name\?/);
  assert.match(login, /first\(params\.method\) === "email"\) return <LoginPanel/);
  for (const label of ["카카오로 계속하기", "네이버로 계속하기", "Google로 계속하기", "기존 이메일로 로그인",
    "처음 가입한 방법으로 로그인하세요"]) assert.ok(social.includes(label), label);
});
