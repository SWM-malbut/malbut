import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { runInNewContext } from "node:vm";
import ts from "typescript";

const REGION = "ap-northeast-2";
const KEY_ID = "11111111-2222-4333-8444-555555555555";
const SIGNER =
  "arn:aws:elasticloadbalancing:ap-northeast-2:000000000000:loadbalancer/app/homecam/1234567890abcdef";
const CLIENT_ID = "homecam-client";
const ISSUER = "https://cognito-idp.ap-northeast-2.amazonaws.com/ap-northeast-2_example";

async function serverAuthHarness(runtime, sessionUser = null) {
  const source = await readFile(new URL("../app/server-auth.ts", import.meta.url), "utf8");
  const javascript = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const commonJsModule = { exports: {} };
  let lookedUpToken = null;
  let publicKeyPem = "";
  let fetchCount = 0;
  runInNewContext(javascript, {
    module: commonJsModule,
    exports: commonJsModule.exports,
    AbortSignal,
    Buffer,
    Date,
    Headers,
    Map,
    Request,
    TextDecoder,
    TextEncoder,
    URL,
    crypto: globalThis.crypto,
    fetch: async (url) => {
      fetchCount += 1;
      assert.equal(
        String(url),
        `https://public-keys.auth.elb.${REGION}.amazonaws.com/${KEY_ID}`,
      );
      return new Response(publicKeyPem, {
        status: publicKeyPem ? 200 : 503,
        headers: { "content-type": "text/plain" },
      });
    },
    require(specifier) {
      if (specifier === "./runtime-env") return { getRuntimeEnvironment: () => runtime };
      if (specifier === "../db/web-auth") {
        return {
          WEB_SESSION_COOKIE: "__Host-malbut_session",
          readCookie(header, name) {
            const match = new RegExp(`(?:^|;\\s*)${name}=([^;]+)`).exec(header ?? "");
            return match?.[1] ?? null;
          },
          async getWebSessionUser(token) {
            lookedUpToken = token;
            if (sessionUser instanceof Error) throw sessionUser;
            return sessionUser;
          },
        };
      }
      throw new Error(`Unexpected import: ${specifier}`);
    },
  });
  return {
    auth: commonJsModule.exports,
    lookedUpToken: () => lookedUpToken,
    setPublicKeyPem(value) {
      publicKeyPem = value;
    },
    fetchCount: () => fetchCount,
  };
}

async function signingKey() {
  const pair = await crypto.subtle.generateKey(
    { name: "ECDSA", namedCurve: "P-256" },
    true,
    ["sign", "verify"],
  );
  const spki = Buffer.from(await crypto.subtle.exportKey("spki", pair.publicKey));
  const body = spki.toString("base64").match(/.{1,64}/g)?.join("\n") ?? "";
  return {
    privateKey: pair.privateKey,
    publicKeyPem: `-----BEGIN PUBLIC KEY-----\n${body}\n-----END PUBLIC KEY-----\n`,
  };
}

async function albToken(privateKey, overrides = {}) {
  const header = {
    alg: "ES256",
    kid: KEY_ID,
    signer: SIGNER,
    client: CLIENT_ID,
    iss: ISSUER,
    exp: Math.floor(Date.now() / 1000) + 300,
    ...(overrides.header ?? {}),
  };
  const claims = {
    sub: "alb-user-123",
    email: "Legacy@Example.com",
    name: "Legacy ALB User",
    ...(overrides.claims ?? {}),
  };
  const encodedHeader = encodeJson(header);
  const encodedClaims = encodeJson(claims);
  const input = `${encodedHeader}.${encodedClaims}`;
  const signature = Buffer.from(
    await crypto.subtle.sign(
      { name: "ECDSA", hash: "SHA-256" },
      privateKey,
      new TextEncoder().encode(input),
    ),
  ).toString("base64url");
  return `${input}.${signature}`;
}

function encodeJson(value) {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function dualAuthRuntime() {
  return {
    AUTH_MODE: "alb_oidc_or_cognito_session",
    AUTH_SESSION_SECRET: Buffer.alloc(32, 5).toString("base64url"),
    AUTH_AWS_REGION: REGION,
    AUTH_ALB_ARN: SIGNER,
    AUTH_OIDC_CLIENT_ID: CLIENT_ID,
    AUTH_OIDC_ISSUER: ISSUER,
    NODE_ENV: "production",
  };
}

test("opaque Cognito sessions authenticate from the HttpOnly cookie", async () => {
  const runtime = {
    AUTH_MODE: "cognito_session",
    AUTH_SESSION_SECRET: Buffer.alloc(32, 5).toString("base64url"),
    NODE_ENV: "production",
  };
  const harness = await serverAuthHarness(runtime, {
    email: "Owner@Example.com", fullName: "Malbut Owner", subject: "subject-1",
  });
  const user = await harness.auth.getAuthenticatedUser(new Headers({
    cookie: "other=1; __Host-malbut_session=opaque-session-token",
  }));
  assert.deepEqual(JSON.parse(JSON.stringify(user)), {
    email: "owner@example.com", fullName: "Malbut Owner", subject: "subject-1",
  });
  assert.equal(harness.lookedUpToken(), "opaque-session-token");
});

test("dual auth prefers an opaque session over a valid legacy ALB identity", async () => {
  const harness = await serverAuthHarness(dualAuthRuntime(), {
    email: "Session@Example.com", fullName: "Session User", subject: "session-subject",
  });
  const key = await signingKey();
  harness.setPublicKeyPem(key.publicKeyPem);
  const token = await albToken(key.privateKey);
  const user = await harness.auth.getAuthenticatedUser(new Headers({
    cookie: "__Host-malbut_session=preferred-session-token",
    "x-amzn-oidc-data": token,
    "x-amzn-oidc-identity": "alb-user-123",
  }));

  assert.deepEqual(JSON.parse(JSON.stringify(user)), {
    email: "session@example.com", fullName: "Session User", subject: "session-subject",
  });
  assert.equal(harness.lookedUpToken(), "preferred-session-token");
  assert.equal(harness.fetchCount(), 0);
});

test("dual auth falls back to a valid signed ALB identity after an invalid session", async () => {
  const harness = await serverAuthHarness(dualAuthRuntime(), null);
  const key = await signingKey();
  harness.setPublicKeyPem(key.publicKeyPem);
  const token = await albToken(key.privateKey);
  const user = await harness.auth.getAuthenticatedUser(new Headers({
    cookie: "__Host-malbut_session=invalid-session-token",
    "x-amzn-oidc-data": token,
    "x-amzn-oidc-identity": "alb-user-123",
  }));

  assert.deepEqual(JSON.parse(JSON.stringify(user)), {
    email: "legacy@example.com", fullName: "Legacy ALB User", subject: "alb-user-123",
  });
  assert.equal(harness.lookedUpToken(), "invalid-session-token");
  assert.equal(harness.fetchCount(), 1);
});

test("session-only auth rejects ALB identity and database failures fail closed", async () => {
  const runtime = {
    AUTH_MODE: "cognito_session",
    AUTH_SESSION_SECRET: Buffer.alloc(32, 5).toString("base64url"),
    NODE_ENV: "production",
  };
  const noSession = await serverAuthHarness(runtime, null);
  const key = await signingKey();
  noSession.setPublicKeyPem(key.publicKeyPem);
  const validAlbToken = await albToken(key.privateKey);
  assert.equal(
    await noSession.auth.getAuthenticatedUser(new Headers({
      "x-amzn-oidc-data": validAlbToken,
      "x-amzn-oidc-identity": "alb-user-123",
    })),
    null,
  );
  assert.equal(noSession.fetchCount(), 0);

  const failed = await serverAuthHarness(runtime, new Error("DATABASE_UNAVAILABLE"));
  assert.equal(await failed.auth.getAuthenticatedUser(new Headers({
    cookie: "__Host-malbut_session=opaque-session-token",
  })), null);
});

test("development header authentication is explicit, loopback-only, and disabled in production", async () => {
  const runtime = {
    AUTH_MODE: "dev_header", AUTH_DEV_USER_EMAIL: "developer@example.com", NODE_ENV: "development",
  };
  const harness = await serverAuthHarness(runtime);
  const headers = new Headers({ "x-malbut-dev-user-email": "Developer@Example.com" });
  assert.equal(
    (await harness.auth.getAuthenticatedUser(headers, "http://127.0.0.1:3000/"))?.email,
    "developer@example.com",
  );
  assert.equal(await harness.auth.getAuthenticatedUser(headers, "https://homecam.example.com/"), null);
  runtime.NODE_ENV = "production";
  assert.equal(await harness.auth.getAuthenticatedUser(headers, "http://127.0.0.1:3000/"), null);
});

test("auth action paths stay same-origin and reject open redirect return paths", async () => {
  const runtime = { AUTH_SIGN_IN_PATH: "/account/login", AUTH_SIGN_OUT_PATH: "/account/logout" };
  const source = await readFile(new URL("../app/chatgpt-auth.ts", import.meta.url), "utf8");
  const javascript = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const commonJsModule = { exports: {} };
  runInNewContext(javascript, {
    module: commonJsModule, exports: commonJsModule.exports, URL,
    require(specifier) {
      if (specifier === "next/headers") return { headers: async () => new Headers() };
      if (specifier === "next/navigation") return { redirect: () => undefined };
      if (specifier === "./runtime-env") return { getRuntimeValue: (name) => runtime[name] };
      if (specifier === "./server-auth") return { getAuthenticatedUser: async () => null };
      throw new Error(`Unexpected import: ${specifier}`);
    },
  });
  assert.equal(
    commonJsModule.exports.chatGPTSignInPath("/events?type=person"),
    "/account/login?return_to=%2Fevents%3Ftype%3Dperson",
  );
  assert.equal(commonJsModule.exports.chatGPTSignOutPath("//attacker.example/"), "/account/logout?return_to=%2F");
  runtime.AUTH_SIGN_IN_PATH = "//attacker.example/login";
  assert.equal(commonJsModule.exports.chatGPTSignInPath("/"), "/auth/login?return_to=%2F");
});

test("the Node runtime migration removes direct Cloudflare imports from app code", async () => {
  const files = [
    "../app/server-auth.ts", "../app/kvs-broker.ts", "../app/push-broker.ts",
    "../app/api/device/v1/session/route.ts", "../app/api/devices/[deviceId]/live-session/route.ts",
    "../app/api/internal/device-provisioning/route.ts", "../app/api/internal/maintenance/route.ts",
    "../app/api/kvs/join/route.ts", "../app/api/kvs/session/route.ts",
    "../app/api/live-sessions/route.ts", "../app/api/push-subscriptions/vapid-public-key/route.ts",
    "../app/api/recordings/route.ts", "../app/api/recordings/[recordingId]/playback/route.ts",
    "../app/api/recordings/[recordingId]/hls/[playbackId]/[resource]/route.ts",
  ];
  const sources = await Promise.all(files.map((file) => readFile(new URL(file, import.meta.url), "utf8")));
  assert.doesNotMatch(sources.join("\n"), /cloudflare:workers/);
  assert.match(sources.join("\n"), /getRuntimeEnvironment/);
});
