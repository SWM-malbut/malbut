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

async function serverAuthHarness(runtime) {
  const source = await readFile(new URL("../app/server-auth.ts", import.meta.url), "utf8");
  const javascript = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
  const commonJsModule = { exports: {} };
  let publicKeyPem = "";
  let fetchCount = 0;
  runInNewContext(javascript, {
    module: commonJsModule,
    exports: commonJsModule.exports,
    require(specifier) {
      if (specifier === "./runtime-env") {
        return { getRuntimeEnvironment: () => runtime };
      }
      throw new Error(`Unexpected import: ${specifier}`);
    },
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
  });
  return {
    auth: commonJsModule.exports,
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
    sub: "user-123",
    email: "Owner@Example.com",
    name: "Malbut Owner",
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

function albRuntime() {
  return {
    AUTH_MODE: "alb_oidc",
    AUTH_AWS_REGION: REGION,
    AUTH_ALB_ARN: SIGNER,
    AUTH_OIDC_CLIENT_ID: CLIENT_ID,
    AUTH_OIDC_ISSUER: ISSUER,
    NODE_ENV: "production",
  };
}

test("verified ALB OIDC claims authenticate and cache the regional public key", async () => {
  const runtime = albRuntime();
  const harness = await serverAuthHarness(runtime);
  const key = await signingKey();
  harness.setPublicKeyPem(key.publicKeyPem);
  const token = await albToken(key.privateKey);
  const headers = new Headers({
    "x-amzn-oidc-data": token,
    "x-amzn-oidc-identity": "user-123",
  });

  assert.deepEqual(
    JSON.parse(JSON.stringify(await harness.auth.getAuthenticatedUser(headers))),
    {
      email: "owner@example.com",
      fullName: "Malbut Owner",
      subject: "user-123",
    },
  );
  assert.equal((await harness.auth.getAuthenticatedUser(headers)).email, "owner@example.com");
  assert.equal(harness.fetchCount(), 1);
});

test("unverified, expired, or mismatched ALB identity claims fail closed", async () => {
  const runtime = albRuntime();
  const harness = await serverAuthHarness(runtime);
  const key = await signingKey();
  harness.setPublicKeyPem(key.publicKeyPem);

  const spoofedLegacyHeader = new Headers({
    "oai-authenticated-user-email": "attacker@example.com",
  });
  assert.equal(await harness.auth.getAuthenticatedUser(spoofedLegacyHeader), null);

  const expired = await albToken(key.privateKey, {
    header: { exp: Math.floor(Date.now() / 1000) - 1 },
  });
  assert.equal(
    await harness.auth.getAuthenticatedUser(
      new Headers({
        "x-amzn-oidc-data": expired,
        "x-amzn-oidc-identity": "user-123",
      }),
    ),
    null,
  );

  const valid = await albToken(key.privateKey);
  assert.equal(
    await harness.auth.getAuthenticatedUser(
      new Headers({
        "x-amzn-oidc-data": valid,
        "x-amzn-oidc-identity": "different-user",
      }),
    ),
    null,
  );
});

test("development header authentication is explicit, loopback-only, and disabled in production", async () => {
  const runtime = {
    AUTH_MODE: "dev_header",
    AUTH_DEV_USER_EMAIL: "developer@example.com",
    NODE_ENV: "development",
  };
  const harness = await serverAuthHarness(runtime);
  const headers = new Headers({ "x-malbut-dev-user-email": "Developer@Example.com" });

  assert.equal(
    (await harness.auth.getAuthenticatedUser(headers, "http://127.0.0.1:3000/"))?.email,
    "developer@example.com",
  );
  assert.equal(
    await harness.auth.getAuthenticatedUser(headers, "https://homecam.example.com/"),
    null,
  );
  runtime.NODE_ENV = "production";
  assert.equal(
    await harness.auth.getAuthenticatedUser(headers, "http://127.0.0.1:3000/"),
    null,
  );
});

test("auth action paths stay same-origin and reject open redirect return paths", async () => {
  const runtime = {
    AUTH_SIGN_IN_PATH: "/account/login",
    AUTH_SIGN_OUT_PATH: "/account/logout",
  };
  const source = await readFile(new URL("../app/chatgpt-auth.ts", import.meta.url), "utf8");
  const javascript = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
  const commonJsModule = { exports: {} };
  runInNewContext(javascript, {
    module: commonJsModule,
    exports: commonJsModule.exports,
    URL,
    require(specifier) {
      if (specifier === "next/headers") return { headers: async () => new Headers() };
      if (specifier === "next/navigation") return { redirect: () => undefined };
      if (specifier === "./runtime-env") {
        return { getRuntimeValue: (name) => runtime[name] };
      }
      if (specifier === "./server-auth") {
        return { getAuthenticatedUser: async () => null };
      }
      throw new Error(`Unexpected import: ${specifier}`);
    },
  });

  assert.equal(
    commonJsModule.exports.chatGPTSignInPath("/events?type=person"),
    "/account/login?return_to=%2Fevents%3Ftype%3Dperson",
  );
  assert.equal(
    commonJsModule.exports.chatGPTSignOutPath("//attacker.example/"),
    "/account/logout?return_to=%2F",
  );
  runtime.AUTH_SIGN_IN_PATH = "//attacker.example/login";
  assert.equal(
    commonJsModule.exports.chatGPTSignInPath("/"),
    "/auth/login?return_to=%2F",
  );
});

test("the Node runtime migration removes direct Cloudflare imports from app code", async () => {
  const files = [
    "../app/server-auth.ts",
    "../app/kvs-broker.ts",
    "../app/push-broker.ts",
    "../app/api/device/v1/session/route.ts",
    "../app/api/devices/[deviceId]/live-session/route.ts",
    "../app/api/internal/device-provisioning/route.ts",
    "../app/api/internal/maintenance/route.ts",
    "../app/api/kvs/join/route.ts",
    "../app/api/kvs/session/route.ts",
    "../app/api/live-sessions/route.ts",
    "../app/api/push-subscriptions/vapid-public-key/route.ts",
    "../app/api/recordings/route.ts",
    "../app/api/recordings/[recordingId]/playback/route.ts",
    "../app/api/recordings/[recordingId]/hls/[playbackId]/[resource]/route.ts",
  ];
  const sources = await Promise.all(
    files.map((file) => readFile(new URL(file, import.meta.url), "utf8")),
  );
  assert.doesNotMatch(sources.join("\n"), /cloudflare:workers/);
  assert.match(sources.join("\n"), /getRuntimeEnvironment/);
});
