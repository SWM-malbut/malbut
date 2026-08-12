import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

async function loadReconnectPolicy() {
  const source = await readFile(
    new URL("../app/lib/viewer-reconnect.ts", import.meta.url),
    "utf8",
  );
  const javascript = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ESNext,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
  return import(
    `data:text/javascript;base64,${Buffer.from(javascript).toString("base64")}#${Date.now()}`
  );
}

test("authorized P2P reconnect delay is exponential, jittered, capped, and bounded", async () => {
  const policy = await loadReconnectPolicy();

  assert.deepEqual(
    Array.from({ length: 6 }, (_, attempt) =>
      policy.authorizedP2pReconnectDelayMs(attempt, 0.5),
    ),
    [1_000, 2_000, 4_000, 8_000, 16_000, 16_000],
  );
  assert.equal(policy.authorizedP2pReconnectDelayMs(0, 0), 750);
  assert.equal(policy.authorizedP2pReconnectDelayMs(0, 1), 1_250);
  assert.equal(policy.authorizedP2pReconnectDelayMs(20, 1), 16_000);
  assert.equal(policy.canAutomaticallyReconnectAuthorizedP2p(0), true);
  assert.equal(policy.canAutomaticallyReconnectAuthorizedP2p(5), true);
  assert.equal(policy.canAutomaticallyReconnectAuthorizedP2p(6), false);
  assert.equal(policy.canAutomaticallyReconnectAuthorizedP2p(-1), false);
});

test("viewer reconnect lifecycle is generation-safe and P2P-only", async () => {
  const [page, client] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/lib/kvs-client.ts", import.meta.url), "utf8"),
  ]);

  assert.match(page, /viewerGenerationRef/);
  assert.match(page, /automaticReconnectAttemptsRef/);
  assert.match(page, /authorizedP2pReconnectDelayMs/);
  assert.match(page, /knownStorageMode !== false/);
  assert.match(page, /viewerAccessRevokedRef\.current = true/);
  assert.match(page, /window\.addEventListener\("online"/);
  assert.match(page, /document\.visibilityState === "hidden"/);
  assert.match(page, /AUTHORIZED_VIEWER_SETUP_TIMEOUT_MS/);
  assert.match(page, /AUTHORIZED_P2P_CONNECT_TIMEOUT_MS/);
  assert.match(page, /AUTHORIZED_P2P_MEDIA_TIMEOUT_MS/);
  assert.doesNotMatch(page, /disabled=\{state === "connecting" \|\| microphonePending\}/);

  assert.match(client, /disconnectGraceMs:\s*AUTHORIZED_P2P_DISCONNECT_GRACE_MS/);
  assert.match(client, /peer\.connectionState === "disconnected"/);
  assert.match(client, /peer\.connectionState !== "connected"/);
  assert.match(client, /signal\?: AbortSignal/);
  assert.match(client, /onStorageMode\?\.\(Boolean\(config\.storageMode\)\)/);
});

test("PTT lease release keeps the client and generation that acquired it", async () => {
  const page = await readFile(new URL("../app/page.tsx", import.meta.url), "utf8");

  assert.match(page, /type ViewerTalkLease = \{[\s\S]*clientId: string;[\s\S]*generation: number;/);
  assert.match(page, /clientId: lease\.clientId/);
  assert.match(page, /generation: talkGeneration/);
  assert.match(page, /viewerGenerationRef\.current !== talkGeneration/);
  assert.match(page, /notifyTalkLeaseRelease\(acquiredLease\)/);
  assert.match(page, /if \(track\) track\.enabled = false/);
  assert.match(page, /talkIntentRef\.current = false/);
});
