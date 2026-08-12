import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { runInNewContext } from "node:vm";
import ts from "typescript";

async function loginFlow() {
  const source = await readFile(
    new URL("../app/auth/login/login-flow.ts", import.meta.url),
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
  });
  return commonJsModule.exports;
}

test("integrated login accepts only safe same-origin return paths", async () => {
  const flow = await loginFlow();

  assert.equal(flow.safeRelativeReturnPath("/?view=events#latest"), "/?view=events#latest");
  assert.equal(flow.safeRelativeReturnPath("//evil.example/steal"), "/");
  assert.equal(flow.safeRelativeReturnPath("https://evil.example/steal"), "/");
  assert.equal(flow.safeRelativeReturnPath("/auth/logout"), "/");
  assert.equal(
    flow.successfulLoginRedirect(
      { ok: true, redirectTo: "//evil.example/steal" },
      "/?view=events",
    ),
    "/?view=events",
  );
  assert.equal(
    flow.loginApiPath("/?view=events"),
    "/api/auth/login?return_to=%2F%3Fview%3Devents",
  );
});

test("integrated login maps initial-password and MFA challenges", async () => {
  const flow = await loginFlow();

  assert.equal(flow.responseLoginStep({ challenge: "new_password" }), "new_password");
  assert.equal(flow.responseLoginStep({ challenge: "mfa" }), "mfa");
  assert.equal(
    flow.responseLoginStep({ next: "NEW_PASSWORD_REQUIRED" }),
    "new_password",
  );
  assert.equal(flow.responseLoginStep({ next: "SOFTWARE_TOKEN_MFA" }), "mfa");
  assert.equal(flow.responseLoginStep({ ok: false }), null);
});

test("the login form exposes accessible credential and challenge controls", async () => {
  const source = await readFile(
    new URL("../app/auth/login/login-panel.tsx", import.meta.url),
    "utf8",
  );

  assert.match(source, /autoComplete="username"/);
  assert.match(source, /autoComplete="current-password"/);
  assert.match(source, /autoComplete="new-password"/);
  assert.match(source, /autoComplete="one-time-code"/);
  assert.match(source, /role=\{message && messageTone === "error" \? "alert" : "status"\}/);
  assert.match(source, /aria-live=\{messageTone === "error" \? "assertive" : "polite"\}/);
  assert.match(source, /aria-pressed=\{showPassword\}/);
  assert.match(source, /credentials: "same-origin"/);
});

test("the application page authenticates before mounting polling clients", async () => {
  const [page, app] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(
      new URL("../app/components/homecam-app.tsx", import.meta.url),
      "utf8",
    ),
  ]);

  assert.match(page, /await requireChatGPTUser\(returnTo\)/);
  assert.match(page, /return serialized \? `\/\?\$\{serialized\}` : "\/"/);
  assert.match(page, /<HomecamApp \/>/);
  assert.doesNotMatch(page, /fetch\("\/api\/devices"/);
  assert.match(app, /HomecamDashboard/);
});

test("both account headers POST logout while keeping sign-in as a link", async () => {
  const [header, app] = await Promise.all([
    readFile(
      new URL("../app/components/homecam-header.tsx", import.meta.url),
      "utf8",
    ),
    readFile(
      new URL("../app/components/homecam-app.tsx", import.meta.url),
      "utf8",
    ),
  ]);

  for (const source of [header, app]) {
    assert.match(source, /fetch\(authStatus\.signOutPath/);
    assert.match(source, /method:\s*"POST"/);
    assert.match(source, /"content-type":\s*"application\/json"/);
    assert.match(source, /body:\s*"\{\}"/);
    assert.match(source, /credentials:\s*"same-origin"/);
    assert.match(source, /window\.location\.replace\(redirectTo\)/);
    assert.match(source, /href=\{authStatus.*signInPath/s);
  }
});
