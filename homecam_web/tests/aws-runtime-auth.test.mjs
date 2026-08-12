import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { runInNewContext } from "node:vm";
import ts from "typescript";

async function serverAuthHarness(runtime, sessionUser = null) {
  const source = await readFile(new URL("../app/server-auth.ts", import.meta.url), "utf8");
  const javascript = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const commonJsModule = { exports: {} };
  let lookedUpToken = null;
  runInNewContext(javascript, {
    module: commonJsModule, exports: commonJsModule.exports, Headers, URL,
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
  return { auth: commonJsModule.exports, lookedUpToken: () => lookedUpToken };
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

test("missing, failed, and legacy ALB authentication state fails closed", async () => {
  const runtime = {
    AUTH_MODE: "cognito_session",
    AUTH_SESSION_SECRET: Buffer.alloc(32, 5).toString("base64url"),
    NODE_ENV: "production",
  };
  const noSession = await serverAuthHarness(runtime, null);
  assert.equal(await noSession.auth.getAuthenticatedUser(new Headers({
    "x-amzn-oidc-data": "attacker-token", "x-amzn-oidc-identity": "attacker",
  })), null);
  const failed = await serverAuthHarness(runtime, new Error("DATABASE_UNAVAILABLE"));
  assert.equal(await failed.auth.getAuthenticatedUser(new Headers({
    cookie: "__Host-malbut_session=opaque-session-token",
  })), null);
  runtime.AUTH_MODE = "alb_oidc";
  assert.equal(await noSession.auth.getAuthenticatedUser(new Headers({
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
