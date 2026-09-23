import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { runInNewContext } from "node:vm";
import ts from "typescript";

async function loadSecretHelpers() {
  const source = await readFile(new URL("../db/session-secret.ts", import.meta.url), "utf8");
  const javascript = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
  const exports = {};
  runInNewContext(javascript, {
    crypto: globalThis.crypto,
    exports,
    module: { exports },
    TextEncoder,
    Uint8Array,
  });
  return exports;
}

test("creates a high-entropy formatted viewer password", async () => {
  const { createViewerPassword, normalizeViewerPassword, isValidViewerPassword } =
    await loadSecretHelpers();
  const passwords = new Set(Array.from({ length: 100 }, () => createViewerPassword()));

  assert.equal(passwords.size, 100);
  for (const password of passwords) {
    assert.match(password, /^(?:[A-HJ-NP-Z2-9]{4}-){3}[A-HJ-NP-Z2-9]{4}$/);
    assert.equal(normalizeViewerPassword(password).length, 16);
    assert.equal(isValidViewerPassword(password), true);
  }
});

test("stores only an HMAC verifier and rejects a wrong password", async () => {
  const { createViewerPasswordVerifier, verifyViewerPassword } = await loadSecretHelpers();
  const sessionId = "session-123";
  const password = "ABCD-EFGH-JKLM-NPQR";
  const secret = "test-only-share-secret";
  const digest = await createViewerPasswordVerifier(sessionId, password, secret);

  assert.match(digest, /^[a-f0-9]{64}$/);
  assert.doesNotMatch(digest, /ABCD|EFGH|JKLM|NPQR/);
  assert.equal(await verifyViewerPassword(sessionId, password, digest, secret), true);
  assert.equal(
    await verifyViewerPassword(sessionId, "WXYZ-2345-6789-ABCD", digest, secret),
    false,
  );
});
