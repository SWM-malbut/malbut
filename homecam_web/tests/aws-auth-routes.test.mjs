import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("web logout revokes the server session and expires auth cookies", async () => {
  const [source, wrapper, completion] = await Promise.all([
    readFile(new URL("../app/api/auth/logout/route.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/auth/logout/route.ts", import.meta.url), "utf8"),
    readFile(
      new URL("../app/auth/logout/complete/route.ts", import.meta.url),
      "utf8",
    ),
  ]);
  assert.match(source, /export async function POST/);
  assert.match(source, /export async function GET/);
  assert.match(source, /sameOriginJsonRequest/);
  assert.match(source, /revokeWebSession/);
  assert.match(source, /WEB_SESSION_COOKIE/);
  assert.match(source, /WEB_CHALLENGE_COOKIE/);
  assert.match(source, /mode,/);
  assert.match(source, /"hosted_ui"/);
  assert.match(source, /"local"/);
  assert.match(source, /AWSELBAuthSessionCookie/);
  assert.match(source, /AUTH_COGNITO_DOMAIN/);
  assert.match(source, /AUTH_COGNITO_CLIENT_ID/);
  assert.match(source, /logout_uri/);
  assert.match(source, /expires:\s*new Date\(0\)/);
  assert.match(wrapper, /export \{ GET, POST \}/);
  assert.match(completion, /LOGOUT_RETURN_COOKIE/);
  assert.match(completion, /NextResponse\.redirect/);
  assert.match(completion, /AWSELBAuthSessionCookie/);
});

test("the integrated login page keeps return paths same-origin", async () => {
  const [page, flow] = await Promise.all([
    readFile(new URL("../app/auth/login/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/auth/login/login-flow.ts", import.meta.url), "utf8"),
  ]);
  assert.match(page, /safeRelativeReturnPath/);
  assert.match(flow, /value\.startsWith\("\/\/"\)/);
  assert.match(flow, /url\.pathname\.startsWith\("\/auth\/"\)/);
  assert.match(flow, /\/api\/auth\/login\?/);
});
