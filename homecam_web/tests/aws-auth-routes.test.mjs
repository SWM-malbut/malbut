import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("AWS logout expires the ALB auth cookie and all shards through Cognito", async () => {
  const source = await readFile(
    new URL("../app/api/auth/logout/route.ts", import.meta.url),
    "utf8",
  );
  assert.match(source, /AWSELBAuthSessionCookie/);
  assert.match(source, /length:\s*4/);
  assert.match(source, /name,/);
  assert.match(source, /maxAge:\s*-1/);
  assert.match(source, /AUTH_COGNITO_DOMAIN/);
  assert.match(source, /AUTH_COGNITO_CLIENT_ID/);
  assert.match(source, /logout_uri/);
  assert.match(source, /safeReturnPath/);
  assert.match(source, /if \(!usesCognito\)/);
});

test("logout completion refuses protocol-relative and recursive auth redirects", async () => {
  const source = await readFile(
    new URL("../app/api/auth/logout/complete/route.ts", import.meta.url),
    "utf8",
  );
  assert.match(source, /value\.startsWith\("\/\/"\)/);
  assert.match(source, /url\.pathname\.startsWith\("\/auth\/"\)/);
  assert.match(source, /NextResponse\.redirect/);
});

test("the login route only redirects to a safe same-origin application path", async () => {
  const source = await readFile(
    new URL("../app/auth/login/route.ts", import.meta.url),
    "utf8",
  );
  assert.match(source, /value\.startsWith\("\/\/"\)/);
  assert.match(source, /url\.pathname\.startsWith\("\/auth\/"\)/);
  assert.match(source, /NextResponse\.redirect/);
});
