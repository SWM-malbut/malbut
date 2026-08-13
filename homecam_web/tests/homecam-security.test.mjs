import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { runInNewContext } from "node:vm";
import ts from "typescript";

async function loadHelpers() {
  const source = await readFile(
    new URL("../db/homecam-security.ts", import.meta.url),
    "utf8",
  );
  const javascript = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
  const exports = {};
  runInNewContext(javascript, {
    crypto: globalThis.crypto,
    Date,
    exports,
    module: { exports },
    TextEncoder,
    Uint8Array,
  });
  return exports;
}

test("device token is high entropy and only its SHA-256 digest is persisted", async () => {
  const { createDeviceToken, hashDeviceToken, parseDeviceToken } =
    await loadHelpers();
  const first = createDeviceToken();
  const second = createDeviceToken();

  assert.notEqual(first.token, second.token);
  assert.match(
    first.token,
    /^hc1\.[0-9a-f-]{36}\.[a-f0-9]{64}$/,
  );
  assert.equal(parseDeviceToken(first.token).credentialId, first.credentialId);
  const digest = await hashDeviceToken(first.token);
  assert.match(digest, /^[a-f0-9]{64}$/);
  assert.equal(digest.includes(first.token.split(".").at(-1)), false);
});

test("bearer parsing is strict and revoked or expired credentials are inactive", async () => {
  const { getBearerToken, isCredentialActive } = await loadHelpers();
  assert.equal(getBearerToken("Bearer hc1.example"), "hc1.example");
  assert.equal(getBearerToken("Basic hc1.example"), null);
  assert.equal(getBearerToken("Bearer one two"), null);

  const now = new Date("2026-07-26T00:00:00.000Z");
  assert.equal(
    isCredentialActive({ revokedAt: null, expiresAt: null }, now),
    true,
  );
  assert.equal(
    isCredentialActive(
      { revokedAt: "2026-07-25T00:00:00.000Z", expiresAt: null },
      now,
    ),
    false,
  );
  assert.equal(
    isCredentialActive(
      { revokedAt: null, expiresAt: "2026-07-25T00:00:00.000Z" },
      now,
    ),
    false,
  );
});
