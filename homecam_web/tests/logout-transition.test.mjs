import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { runInNewContext } from "node:vm";
import ts from "typescript";

async function logoutFlow() {
  const source = await readFile(
    new URL("../app/auth/logout/logout-flow.ts", import.meta.url),
    "utf8",
  );
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
    URLSearchParams,
    TextDecoder,
    TextEncoder,
    Uint8Array,
    atob,
    btoa,
  });
  return commonJsModule.exports;
}

async function logoutRouteHarness(runtime) {
  const [source, flow] = await Promise.all([
    readFile(new URL("../app/api/auth/logout/route.ts", import.meta.url), "utf8"),
    logoutFlow(),
  ]);
  const javascript = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
  const commonJsModule = { exports: {} };
  let revokedToken = null;

  class TestResponse {
    constructor(body = null, init = {}) {
      this.body = body;
      this.status = init.status ?? 200;
      this.headers = new Headers(init.headers);
      this.cookieWrites = [];
      this.cookies = {
        set: (...values) => this.cookieWrites.push(values),
      };
    }

    static json(body, init = {}) {
      return new TestResponse(body, init);
    }

    static redirect(url, status = 307) {
      const response = new TestResponse(null, { status });
      response.headers.set("location", String(url));
      return response;
    }
  }

  runInNewContext(javascript, {
    module: commonJsModule,
    exports: commonJsModule.exports,
    Array,
    Date,
    Headers,
    URL,
    require(specifier) {
      if (specifier === "next/server") return { NextResponse: TestResponse };
      if (specifier === "../../../runtime-env") {
        return { getRuntimeEnvironment: () => runtime };
      }
      if (specifier === "../../../auth/logout/logout-flow") return flow;
      if (specifier === "../../../../db/web-auth") {
        return {
          WEB_SESSION_COOKIE: "__Host-malbut_session",
          WEB_CHALLENGE_COOKIE: "__Host-malbut_challenge",
          readCookie(header, name) {
            const match = new RegExp(`(?:^|;\\s*)${name}=([^;]+)`).exec(header ?? "");
            return match?.[1] ?? null;
          },
          async revokeWebSession(token) {
            revokedToken = token;
          },
          webAuthCookieOptions(maxAge) {
            return { httpOnly: true, secure: true, sameSite: "lax", path: "/", maxAge };
          },
        };
      }
      throw new Error(`Unexpected import: ${specifier}`);
    },
  });

  return {
    route: commonJsModule.exports,
    revokedToken: () => revokedToken,
  };
}

test("logout mode keeps Hosted UI only during ALB migration phases", async () => {
  const flow = await logoutFlow();
  assert.equal(flow.usesHostedUiLogout("alb_oidc"), true);
  assert.equal(flow.usesHostedUiLogout("alb_oidc_or_cognito_session"), true);
  assert.equal(flow.usesHostedUiLogout("cognito_session"), false);
  assert.equal(flow.usesHostedUiLogout("dev_header"), false);
  assert.equal(flow.usesHostedUiLogout(undefined), false);
});

test("legacy logout starts with same-origin top-level navigation", async () => {
  const flow = await logoutFlow();
  assert.equal(
    flow.logoutStartPath("/?view=events#latest"),
    "/auth/logout?continue=hosted&return_to=%2F%3Fview%3Devents%23latest",
  );
  assert.equal(
    flow.logoutNavigationPath({
      ok: true,
      mode: "hosted_ui",
      redirectTo: flow.logoutStartPath("/"),
    }),
    flow.logoutStartPath("/"),
  );
  assert.equal(
    flow.logoutNavigationPath({
      ok: true,
      mode: "hosted_ui",
      redirectTo: "https://attacker.example/logout",
    }),
    "/",
  );
  assert.equal(
    flow.logoutNavigationPath({ ok: true, mode: "unexpected", redirectTo: "/safe" }),
    "/",
  );
});

test("logout completion preserves only a bounded safe same-origin landing", async () => {
  const flow = await logoutFlow();
  const landing = "/?view=events&kind=person#latest";
  assert.equal(flow.decodeLogoutReturnPath(flow.encodeLogoutReturnPath(landing)), landing);
  assert.equal(
    flow.decodeLogoutReturnPath(flow.encodeLogoutReturnPath("//attacker.example/")),
    "/",
  );
  assert.equal(flow.safeLogoutReturnPath("/auth/login"), "/");
  assert.equal(flow.safeLogoutReturnPath("https://attacker.example/"), "/");
  assert.equal(flow.decodeLogoutReturnPath("not+base64"), "/");
});

test("dual POST revokes opaque state, clears ALB shards, and returns a top-level Hosted UI step", async () => {
  const runtime = {
    AUTH_MODE: "alb_oidc_or_cognito_session",
    AUTH_SESSION_SECRET: "session-secret",
    AUTH_PUBLIC_ORIGIN: "https://malbut.example",
    AUTH_COGNITO_DOMAIN: "https://malbut.auth.ap-northeast-2.amazoncognito.com",
    AUTH_COGNITO_CLIENT_ID: "client123",
  };
  const harness = await logoutRouteHarness(runtime);
  const request = new Request(
    "https://malbut.example/api/auth/logout?return_to=%2F%3Fview%3Devents",
    {
      method: "POST",
      headers: {
        origin: "https://malbut.example",
        "content-type": "application/json",
        cookie: "__Host-malbut_session=opaque-token",
      },
      body: "{}",
    },
  );
  const response = await harness.route.POST(request);

  assert.equal(response.status, 200);
  assert.deepEqual(JSON.parse(JSON.stringify(response.body)), {
    ok: true,
    mode: "hosted_ui",
    redirectTo: "/auth/logout?continue=hosted&return_to=%2F%3Fview%3Devents",
  });
  assert.equal(harness.revokedToken(), "opaque-token");
  const cookieNames = response.cookieWrites.map((values) =>
    typeof values[0] === "string" ? values[0] : values[0].name,
  );
  assert.equal(cookieNames.includes("AWSELBAuthSessionCookie"), true);
  for (let shard = 0; shard < 4; shard += 1) {
    assert.equal(cookieNames.includes(`AWSELBAuthSessionCookie-${shard}`), true);
  }
  assert.equal(cookieNames.at(-1), "__Host-malbut_logout_return");
});

test("legacy GET builds the Cognito logout URL while cleanup stays local", async () => {
  const runtime = {
    AUTH_MODE: "alb_oidc",
    AUTH_PUBLIC_ORIGIN: "https://malbut.example",
    AUTH_COGNITO_DOMAIN: "https://malbut.auth.ap-northeast-2.amazoncognito.com",
    AUTH_COGNITO_CLIENT_ID: "client123",
  };
  const harness = await logoutRouteHarness(runtime);
  const legacy = await harness.route.GET(new Request(
    "https://malbut.example/auth/logout?continue=hosted&return_to=%2F%3Fview%3Devents",
  ));
  const hostedUrl = new URL(legacy.headers.get("location"));
  assert.equal(legacy.status, 303);
  assert.equal(hostedUrl.origin, runtime.AUTH_COGNITO_DOMAIN);
  assert.equal(hostedUrl.pathname, "/logout");
  assert.equal(hostedUrl.searchParams.get("client_id"), "client123");
  assert.equal(
    hostedUrl.searchParams.get("logout_uri"),
    "https://malbut.example/auth/logout/complete",
  );

  runtime.AUTH_MODE = "cognito_session";
  const local = await harness.route.GET(new Request(
    "https://malbut.example/auth/logout?continue=hosted&return_to=%2F%3Fview%3Devents",
  ));
  assert.equal(local.status, 303);
  assert.equal(local.headers.get("location"), "https://malbut.example/?view=events");
});
